# main.py — Full single-file Discord bot with Flask + Render restart support.
# Requirements (put in requirements.txt): discord.py>=2.2.0, aiohttp, flask
# Env vars required: DISCORD_BOT_TOKEN
# Optional env vars: OWNER_ID, CAT_API_KEY, RENDER_API_KEY, RENDER_SERVICE_ID, TZ, PORT

import os
import json
import re
import random
import asyncio
import aiohttp
import atexit
import sys
from datetime import datetime, timezone, timedelta
from threading import Thread
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, List

import discord
from discord.ext import commands, tasks
from discord import app_commands
from flask import Flask

# -------------------------
# ENV / CONFIG
# -------------------------
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN environment variable not set")

try:
    OWNER_ID = int(os.getenv("OWNER_ID", "1319292111325106296").strip())
except Exception:
    OWNER_ID = 1319292111325106296

# extra default admins you requested earlier (keeps you and them admins by default)
DEFAULT_EXTRA_ADMINS = {1380315427992768633, 909468887098216499}

CAT_API_KEY = os.getenv("CAT_API_KEY", "").strip()
RENDER_API_KEY = os.getenv("RENDER_API_KEY", "").strip()
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "").strip()

TZ_NAME = os.getenv("TZ", "Asia/Kolkata").strip() or "Asia/Kolkata"
try:
    BOT_TZ = ZoneInfo(TZ_NAME)
except Exception:
    BOT_TZ = ZoneInfo("Asia/Kolkata")

FLASK_PORT = int(os.getenv("PORT", os.getenv("FLASK_PORT", "8080")))

DATA_FILE = "bot_data.json"
SNIPES_KEEP = 50
LOGS_KEEP = 2000

# -------------------------
# FLASK KEEPALIVE THREAD
# -------------------------
app = Flask("bot_keepalive")


@app.route("/")
def _alive():
    return "OK", 200


def _run_flask():
    # run flask in a thread so bot.run() can run in main thread
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, use_reloader=False)


Thread(target=_run_flask, daemon=True).start()

# -------------------------
# PERSISTENT JSON STORAGE
# -------------------------
DEFAULT_DATA = {
    "admins": [],  # list of ints
    "pookies": [],
    "blacklist": [],
    "blocked_words": [],
    "triggers": {},  # word -> reply
    "log_channel": None,
    "cat_channel": None,  # daily 11:00 IST
    "hourly_cat_channel": None,
    "logs": [],
    "afk": {}
}


def save_data(d: Dict[str, Any]):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        d = DEFAULT_DATA.copy()
        # set default admins (owner + extras)
        d["admins"] = list({OWNER_ID} | DEFAULT_EXTRA_ADMINS)
        save_data(d)
        return d
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        d = json.load(f)
    # ensure keys exist
    for k, v in DEFAULT_DATA.items():
        if k not in d:
            d[k] = v.copy() if isinstance(v, (list, dict)) else v
    # ensure owner present
    admins = set(map(int, d.get("admins", [])))
    admins.add(OWNER_ID)
    admins |= DEFAULT_EXTRA_ADMINS
    d["admins"] = list(admins)
    return d


DATA = load_data()


def reload_data():
    global DATA
    DATA = load_data()
    return DATA


atexit.register(lambda: save_data(DATA))

# -------------------------
# HELPERS
# -------------------------
def is_owner(u_id: int) -> bool:
    return int(u_id) == int(OWNER_ID)


def is_admin_user(u_id: int) -> bool:
    d = reload_data()
    return int(u_id) in set(map(int, d.get("admins", []))) or is_owner(u_id)


def is_pookie_user(u_id: int) -> bool:
    d = reload_data()
    return int(u_id) in set(map(int, d.get("pookies", []))) or is_admin_user(u_id)


def is_blacklisted(u_id: int) -> bool:
    d = reload_data()
    return int(u_id) in set(map(int, d.get("blacklist", [])))


def sanitize_no_mentions(text: str) -> str:
    # inserts zero-width space after @ to avoid pings
    return text.replace("@", "@\u200b")


def exact_word_present(text: str, word: str) -> bool:
    pattern = r"\b" + re.escape(word) + r"\b"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def log_to_file(kind: str, message: str):
    d = reload_data()
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, "message": message}
    d.setdefault("logs", [])
    d["logs"].append(entry)
    if len(d["logs"]) > LOGS_KEEP:
        d["logs"] = d["logs"][-LOGS_KEEP:]
    save_data(d)


async def send_log_embed(guild: Optional[discord.Guild], title: str, description: str):
    d = reload_data()
    ch_id = d.get("log_channel")
    if not ch_id:
        return
    ch = bot.get_channel(ch_id)
    if not ch and guild:
        ch = guild.get_channel(ch_id) if hasattr(guild, "get_channel") else None
    if ch:
        embed = discord.Embed(title=title, description=description, color=discord.Color.orange(),
                              timestamp=datetime.now(timezone.utc))
        try:
            await ch.send(embed=embed)
        except Exception:
            pass


# -------------------------
# SNIPE STORAGE + VIEW
# -------------------------
SNIPES: Dict[int, List[Dict[str, Any]]] = {}
ESNIPES: Dict[int, List[Dict[str, Any]]] = {}


def push_snipe(store: Dict[int, List[Dict[str, Any]]], channel_id: int, entry: Dict[str, Any]):
    lst = store.setdefault(channel_id, [])
    lst.append(entry)
    if len(lst) > SNIPES_KEEP:
        del lst[0]


class SnipeView(discord.ui.View):
    def __init__(self, items: List[Dict[str, Any]]):
        super().__init__(timeout=120)
        self.items = items
        self.index = len(items) - 1

    def make_embed(self) -> discord.Embed:
        data = self.items[self.index]
        emb = discord.Embed(color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
        emb.set_author(name=data.get("author_tag", "Unknown"), icon_url=data.get("avatar_url") or discord.Embed.Empty)
        emb.set_footer(text=f"{self.index + 1}/{len(self.items)} • {data.get('time','')}")
        if "content" in data:
            emb.add_field(name="Message", value=data.get("content") or "*empty*", inline=False)
        else:
            emb.add_field(name="Before", value=data.get("before") or "*empty*", inline=False)
            emb.add_field(name="After", value=data.get("after") or "*empty*", inline=False)
        return emb

    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index > 0:
            self.index -= 1
            await interaction.response.edit_message(embed=self.make_embed(), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index < len(self.items) - 1:
            self.index += 1
            await interaction.response.edit_message(embed=self.make_embed(), view=self)
        else:
            await interaction.response.defer()


# -------------------------
# CAT FETCHER
# -------------------------
async def fetch_random_cat_url(session: aiohttp.ClientSession) -> Optional[str]:
    headers = {}
    if CAT_API_KEY:
        headers["x-api-key"] = CAT_API_KEY
    try:
        async with session.get("https://api.thecatapi.com/v1/images/search", headers=headers, timeout=20) as resp:
            if resp.status == 200:
                j = await resp.json()
                if isinstance(j, list) and j:
                    return j[0].get("url")
    except Exception:
        return None
    return "https://cataas.com/cat"


# -------------------------
# BOT SETUP
# -------------------------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix=commands.when_mentioned_or("?"), intents=intents, help_command=None)
tree = bot.tree

# start time for uptime
BOT_START = datetime.now(timezone.utc)

# in-memory last-run trackers to avoid double-sending
_last_daily_date: Optional[str] = None
_last_hourly_minute: Optional[str] = None


@bot.event
async def on_ready():
    # sync slash commands
    try:
        await tree.sync()
    except Exception as e:
        print("Slash sync error:", e)
    # set presence
    await bot.change_presence(status=discord.Status.dnd,
                              activity=discord.Streaming(name="Max Verstappen", url="https://twitch.tv/max"))
    # start minute scheduler safely
    if not minute_scheduler.is_running():
        minute_scheduler.start()
    print(f"✅ Logged in as {bot.user} (id: {bot.user.id}). Guilds: {len(bot.guilds)}")
    log_to_file("system", f"Bot ready: {bot.user} in {len(bot.guilds)} guilds")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # reload data
    d = reload_data()

    # Clear AFK if the author was AFK
    if str(message.author.id) in d.get("afk", {}):
        d["afk"].pop(str(message.author.id), None)
        save_data(d)
        try:
            await message.channel.send(f"✅ Welcome back {message.author.mention}. AFK removed.")
        except Exception:
            pass

    # Notify if mentions include AFK users
    if message.mentions:
        for u in message.mentions:
            afk = d.get("afk", {}).get(str(u.id))
            if afk:
                reason = afk.get("reason", "AFK")
                since = afk.get("since")
                try:
                    ts = int(datetime.fromisoformat(since).timestamp())
                    await message.reply(f"{u.mention} is AFK — **{sanitize_no_mentions(reason)}** (since <t:{ts}:R>)",
                                        mention_author=False)
                except Exception:
                    await message.reply(f"{u.mention} is AFK — **{sanitize_no_mentions(reason)}**", mention_author=False)

    # Blocked words (compact bypass protection)
    content_compact = re.sub(r"[\s_\-\.]", "", message.content.lower())
    for w in d.get("blocked_words", []):
        wc = re.sub(r"[\s_\-\.]", "", w.lower())
        if wc and wc in content_compact:
            try:
                await message.delete()
            except Exception:
                pass
            await send_log_embed(message.guild, "Blocked Word",
                                 f"{message.author.mention} used blocked word `{w}` in {message.channel.mention}")
            log_to_file("blocked_word", f"{message.author} used blocked word {w} in {message.channel}")
            return

    # Triggers (exact-word)
    for word, reply in d.get("triggers", {}).items():
        if exact_word_present(message.content, word):
            out = reply.replace("{user}", message.author.mention)
            out = sanitize_no_mentions(out) if not is_admin_user(message.author.id) else out
            try:
                await message.channel.send(out)
            except Exception:
                pass
            break

    await bot.process_commands(message)


@bot.event
async def on_message_delete(message: discord.Message):
    if not message.author or message.author.bot:
        return
    push_snipe(SNIPES, message.channel.id, {
        "author_tag": str(message.author),
        "avatar_url": getattr(message.author.display_avatar, "url", ""),
        "content": message.content or "",
        "time": datetime.now(timezone.utc).isoformat()
    })
    await send_log_embed(message.guild, "Message Deleted",
                         f"{message.author.mention} deleted a message in {message.channel.mention}\n```{sanitize_no_mentions(message.content)[:900]}```")
    log_to_file("delete", f"{message.author} deleted message in {message.channel}")


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author and before.author.bot:
        return
    if before.content == after.content:
        return
    push_snipe(ESNIPES, before.channel.id, {
        "author_tag": str(before.author),
        "avatar_url": getattr(before.author.display_avatar, "url", ""),
        "before": before.content,
        "after": after.content,
        "time": datetime.now(timezone.utc).isoformat()
    })
    await send_log_embed(before.guild, "Message Edited",
                         f"{before.author.mention} edited a message in {before.channel.mention}\n**Before:**\n```{sanitize_no_mentions(before.content)[:800]}```\n**After:**\n```{sanitize_no_mentions(after.content)[:800]}```")
    log_to_file("edit", f"{before.author} edited message in {before.channel}")


# -------------------------
# SNIPE SLASH COMMANDS
# -------------------------
@tree.command(name="snipe", description="Show recent deleted messages in this channel")
async def slash_snipe(interaction: discord.Interaction):
    items = SNIPES.get(interaction.channel_id, [])
    if not items:
        return await interaction.response.send_message("Nothing to snipe here.", ephemeral=True)
    view = SnipeView(items)
    await interaction.response.send_message(embed=view.make_embed(), view=view, ephemeral=True)


@tree.command(name="esnipe", description="Show recent edited messages in this channel")
async def slash_esnipe(interaction: discord.Interaction):
    items = ESNIPES.get(interaction.channel_id, [])
    if not items:
        return await interaction.response.send_message("Nothing to esnipe here.", ephemeral=True)
    view = SnipeView(items)
    await interaction.response.send_message(embed=view.make_embed(), view=view, ephemeral=True)


# -------------------------
# MINUTE SCHEDULER (daily/hourly cats)
# -------------------------
@tasks.loop(minutes=1)
async def minute_scheduler():
    global _last_daily_date, _last_hourly_minute
    now_local = datetime.now(BOT_TZ)
    d = reload_data()
    # Daily at 11:00 local tz
    if d.get("cat_channel"):
        if now_local.hour == 11 and now_local.minute == 0:
            today_key = now_local.date().isoformat()
            if _last_daily_date != today_key:
                ch = bot.get_channel(d["cat_channel"])
                if ch:
                    try:
                        async with aiohttp.ClientSession() as s:
                            url = await fetch_random_cat_url(s)
                        if url:
                            await ch.send(url)
                            log_to_file("cat_daily", f"Sent daily cat to {ch.id}")
                            await send_log_embed(ch.guild, "Daily Cat", f"Sent daily cat in {ch.mention}")
                    except Exception:
                        pass
                _last_daily_date = today_key
                await asyncio.sleep(5)
    # Hourly at minute 0
    if d.get("hourly_cat_channel"):
        if now_local.minute == 0:
            minute_key = f"{now_local.hour}-{now_local.minute}-{now_local.date().isoformat()}"
            if _last_hourly_minute != minute_key:
                ch2 = bot.get_channel(d["hourly_cat_channel"])
                if ch2:
                    try:
                        async with aiohttp.ClientSession() as s:
                            url2 = await fetch_random_cat_url(s)
                        if url2:
                            await ch2.send(url2)
                            log_to_file("cat_hourly", f"Sent hourly cat to {ch2.id}")
                            await send_log_embed(ch2.guild, "Hourly Cat", f"Sent hourly cat in {ch2.mention}")
                    except Exception:
                        pass
                _last_hourly_minute = minute_key
                await asyncio.sleep(5)


# -------------------------
# RENDER RESTART (API)
# -------------------------
async def trigger_render_deploy(api_key: str, service_id: str) -> Dict[str, Any]:
    """
    Trigger a Render deploy: POST https://api.render.com/v1/services/{serviceId}/deploys
    Requires: Authorization: Bearer <api_key>
    See Render API docs for details.  [oai_citation:2‡Render API](https://api-docs.render.com/reference/create-deploy?utm_source=chatgpt.com) [oai_citation:3‡Render](https://render.com/docs/api?utm_source=chatgpt.com)
    """
    url = f"https://api.render.com/v1/services/{service_id}/deploys"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    async with aiohttp.ClientSession() as s:
        try:
            async with s.post(url, headers=headers, json={}, timeout=20) as resp:
                text = await resp.text()
                try:
                    js = await resp.json()
                except Exception:
                    js = {"status": resp.status, "text": text}
                return {"status": resp.status, "body": js}
        except Exception as e:
            return {"status": 0, "error": str(e)}


# -------------------------
# CORE SLASH & PREFIX COMMANDS
# -------------------------

# UPTIME
@tree.command(name="uptime", description="Show how long the bot has been online")
async def slash_uptime(interaction: discord.Interaction):
    delta = datetime.now(timezone.utc) - BOT_START
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    await interaction.response.send_message(f"⏱ Uptime: {days}d {hours}h {minutes}m {seconds}s", ephemeral=True)


@bot.command(name="uptime")
async def prefix_uptime(ctx: commands.Context):
    delta = datetime.now(timezone.utc) - BOT_START
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    await ctx.reply(f"⏱ Uptime: {days}d {hours}h {minutes}m {seconds}s", mention_author=False)


# SERVERS
@tree.command(name="servers", description="List servers the bot is in (admins only)")
async def slash_servers(interaction: discord.Interaction):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("❌ Admins only.", ephemeral=True)
    lines = []
    for g in bot.guilds:
        lines.append(f"• {g.name} — id:{g.id} members:{g.member_count}")
    text = "\n".join(lines) or "No servers."
    await interaction.response.send_message(f"🤖 Bot in {len(bot.guilds)} servers:\n{text}", ephemeral=True)


@bot.command(name="servers")
async def prefix_servers(ctx: commands.Context):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("❌ Admins only.", mention_author=False)
    lines = [f"• {g.name} — id:{g.id} members:{g.member_count}" for g in bot.guilds]
    await ctx.reply(f"🤖 Bot in {len(bot.guilds)} servers:\n" + ("\n".join(lines)[:1900]), mention_author=False)


# ASK (pings owner in log channel if set, also DMs owner)
@tree.command(name="ask", description="Ask the owner / request a command (pings owner in log channel + DM)")
async def slash_ask(interaction: discord.Interaction, *, request: str):
    owner = None
    try:
        owner = await bot.fetch_user(OWNER_ID)
    except Exception:
        owner = None
    # DM owner
    if owner:
        try:
            await owner.send(f"📨 **Ask request** from {interaction.user} ({interaction.user.id}) in {interaction.guild.name if interaction.guild else 'DM'}:\n```{request[:1800]}```")
        except Exception:
            pass
    # Ping in log channel (if set)
    d = reload_data()
    log_ch = d.get("log_channel")
    if log_ch:
        ch = bot.get_channel(int(log_ch))
        if ch:
            try:
                await ch.send(f"<@{OWNER_ID}> 📨 **Request** from {interaction.user.mention} in {interaction.guild.name if interaction.guild else 'DM'}:\n```{request[:1600]}```")
            except Exception:
                pass
    await interaction.response.send_message("✅ Your request was sent to the owner (and pinged in the log channel if configured).", ephemeral=True)
    log_to_file("ask", f"{interaction.user} asked: {request[:200]}")
    await send_log_embed(interaction.guild, "AskRequest", f"{interaction.user.mention} asked: {request[:900]}")


@bot.command(name="ask")
async def prefix_ask(ctx: commands.Context, *, request: str):
    owner = None
    try:
        owner = await bot.fetch_user(OWNER_ID)
    except Exception:
        owner = None
    if owner:
        try:
            await owner.send(f"📨 **Ask request** from {ctx.author} ({ctx.author.id}) in {ctx.guild.name if ctx.guild else 'DM'}:\n```{request[:1800]}```")
        except Exception:
            pass
    d = reload_data()
    log_ch = d.get("log_channel")
    if log_ch:
        ch = bot.get_channel(int(log_ch))
        if ch:
            try:
                await ch.send(f"<@{OWNER_ID}> 📨 **Request** from {ctx.author.mention} in {ctx.guild.name if ctx.guild else 'DM'}:\n```{request[:1600]}```")
            except Exception:
                pass
    await ctx.reply("✅ Your request was sent to the owner (and pinged in the log channel if configured).", mention_author=False)
    log_to_file("ask", f"{ctx.author} asked: {request[:200]}")
    await send_log_embed(ctx.guild if ctx.guild else None, "AskRequest", f"{ctx.author} asked: {request[:900]}")


# RESTART: uses Render API if available, otherwise os._exit(0)
@tree.command(name="restart", description="Restart the bot / trigger Render deploy (owner/admin)")
async def slash_restart(interaction: discord.Interaction):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("❌ Admins only.", ephemeral=True)
    if RENDER_API_KEY and RENDER_SERVICE_ID:
        await interaction.response.send_message("🔁 Triggering Render deploy...", ephemeral=True)
        result = await trigger_render_deploy(RENDER_API_KEY, RENDER_SERVICE_ID)
        if result.get("status") in (200, 201):
            await interaction.followup.send(f"✅ Render deploy triggered. Response: {result.get('body')}", ephemeral=True)
            log_to_file("admin", f"{interaction.user} triggered render deploy")
        else:
            await interaction.followup.send(f"⚠️ Deploy trigger failed: {result}", ephemeral=True)
    else:
        # fallback: exit process, Render will restart the service
        await interaction.response.send_message("🔁 Restarting process (no Render API configured)...", ephemeral=True)
        log_to_file("admin", f"{interaction.user} requested restart (process exit)")
        # graceful stop
        asyncio.create_task(_shutdown_and_exit())

@bot.command(name="restart")
async def prefix_restart(ctx: commands.Context):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("❌ Admins only.", mention_author=False)
    if RENDER_API_KEY and RENDER_SERVICE_ID:
        await ctx.reply("🔁 Triggering Render deploy...", mention_author=False)
        result = await trigger_render_deploy(RENDER_API_KEY, RENDER_SERVICE_ID)
        if result.get("status") in (200, 201):
            await ctx.reply("✅ Render deploy triggered.", mention_author=False)
            log_to_file("admin", f"{ctx.author} triggered render deploy")
        else:
            await ctx.reply(f"⚠️ Deploy trigger failed: {result}", mention_author=False)
    else:
        await ctx.reply("🔁 Restarting process (no Render API configured)...", mention_author=False)
        log_to_file("admin", f"{ctx.author} requested restart (process exit)")
        asyncio.create_task(_shutdown_and_exit())


async def _shutdown_and_exit(delay: float = 0.5):
    # give time for response to be sent
    await asyncio.sleep(delay)
    try:
        await bot.close()
    finally:
        os._exit(0)


# REFRESH / SYNC
@tree.command(name="refresh", description="Refresh slash commands (admin/owner)")
async def slash_refresh(interaction: discord.Interaction):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("❌ Admins only.", ephemeral=True)
    try:
        await tree.sync()
        await interaction.response.send_message("✅ Slash commands refreshed.", ephemeral=True)
        log_to_file("admin", f"{interaction.user} refreshed slash commands")
    except Exception as e:
        await interaction.response.send_message(f"⚠️ Failed: {e}", ephemeral=True)


# LOG CHANNEL
@tree.command(name="set_log_channel", description="Set the channel to receive logs")
async def slash_set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("❌ Admins only.", ephemeral=True)
    d = reload_data()
    d["log_channel"] = int(channel.id)
    save_data(d)
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)
    await send_log_embed(interaction.guild, "Logs", f"{interaction.user.mention} set the log channel to {channel.mention}")


@tree.command(name="disable_log_channel", description="Disable the log channel")
async def slash_disable_log_channel(interaction: discord.Interaction):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("❌ Admins only.", ephemeral=True)
    d = reload_data()
    d["log_channel"] = None
    save_data(d)
    await interaction.response.send_message("✅ Log channel disabled.", ephemeral=True)


@tree.command(name="logs", description="Show recent logs (admin)")
async def slash_logs(interaction: discord.Interaction, count: Optional[int] = 10):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("❌ Admins only.", ephemeral=True)
    d = reload_data()
    logs = d.get("logs", [])[-min(max(1, count or 10), 50):]
    text = "\n".join(f"`{l['ts']}` {l['kind']}: {l['message']}" for l in logs) or "No logs."
    await interaction.response.send_message(text[:1900], ephemeral=True)


# CAT CHANNELS & CAT COMMANDS
@tree.command(name="setcatchannel", description="Set daily 11:00 cat channel (admin)")
async def slash_set_cat_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("❌ Admins only.", ephemeral=True)
    d = reload_data()
    d["cat_channel"] = int(channel.id)
    save_data(d)
    await interaction.response.send_message(f"✅ Daily cat channel set to {channel.mention} (11:00 {BOT_TZ})", ephemeral=True)


@tree.command(name="sethourlycatchannel", description="Set hourly cat channel (admin)")
async def slash_set_hourly_cat_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("❌ Admins only.", ephemeral=True)
    d = reload_data()
    d["hourly_cat_channel"] = int(channel.id)
    save_data(d)
    await interaction.response.send_message(f"✅ Hourly cat channel set to {channel.mention}", ephemeral=True)


@tree.command(name="cat", description="Get a random cat image")
async def slash_cat(interaction: discord.Interaction):
    await interaction.response.defer()
    async with aiohttp.ClientSession() as s:
        url = await fetch_random_cat_url(s)
    if not url:
        return await interaction.followup.send("⚠️ Couldn't fetch a cat right now.")
    await interaction.followup.send(url)


@bot.command(name="cat")
async def prefix_cat(ctx: commands.Context):
    if is_blacklisted(ctx.author.id):
        return await ctx.reply("You are blacklisted.", mention_author=False)
    async with aiohttp.ClientSession() as s:
        url = await fetch_random_cat_url(s)
    await ctx.send(url)
    log_to_file("cat", f"{ctx.author} requested cat")


# SAY / SAY_ADMIN
@tree.command(name="say", description="Bot repeats text (no pings)")
async def slash_say(interaction: discord.Interaction, *, text: str):
    safe = sanitize_no_mentions(text)
    await interaction.response.send_message("✅ Sent (no pings).", ephemeral=True)
    await interaction.channel.send(safe, allowed_mentions=discord.AllowedMentions.none())
    log_to_file("command", f"{interaction.user} used /say")


@tree.command(name="say_admin", description="Admin say (pings allowed)")
async def slash_say_admin(interaction: discord.Interaction, *, text: str):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("❌ Admins only.", ephemeral=True)
    await interaction.response.send_message("✅ Sent.", ephemeral=True)
    await interaction.channel.send(text)
    log_to_file("command", f"{interaction.user} used /say_admin")


@bot.command(name="say")
async def prefix_say(ctx: commands.Context, *, text: str):
    safe = sanitize_no_mentions(text)
    await ctx.send(safe, allowed_mentions=discord.AllowedMentions.none())
    log_to_file("command", f"{ctx.author} used ?say")


@bot.command(name="say_admin")
async def prefix_say_admin(ctx: commands.Context, *, text: str):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    await ctx.send(text)
    log_to_file("command", f"{ctx.author} used ?say_admin")


# BAN / KICK (slash requires member object; prefix supports ID or mention)
@tree.command(name="ban", description="Ban a guild member (admin)")
async def slash_ban(interaction: discord.Interaction, member: discord.Member, *, reason: Optional[str] = "No reason provided"):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    try:
        await member.ban(reason=reason)
        await interaction.response.send_message(f"🔨 {member.mention} banned. Reason: {sanitize_no_mentions(reason)}")
        await send_log_embed(interaction.guild, "Ban", f"{interaction.user.mention} banned {member.mention} — {sanitize_no_mentions(reason)}")
        log_to_file("mod", f"{interaction.user} banned {member} — {reason}")
    except Exception as e:
        await interaction.response.send_message(f"Failed: {e}", ephemeral=True)


@bot.command(name="ban")
async def prefix_ban(ctx: commands.Context, target: str, *, reason: Optional[str] = "No reason provided"):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    uid = None
    m = re.match(r"<@!?(\d+)>", target)
    if m:
        uid = int(m.group(1))
    else:
        try:
            uid = int(target)
        except Exception:
            return await ctx.reply("Provide user mention or ID.", mention_author=False)
    member = ctx.guild.get_member(uid)
    try:
        if member:
            await member.ban(reason=reason)
            await ctx.reply(f"🔨 {member.mention} banned. Reason: {sanitize_no_mentions(reason)}", mention_author=False)
        else:
            await ctx.guild.ban(discord.Object(id=uid), reason=reason)
            await ctx.reply(f"🔨 Banned ID {uid}.", mention_author=False)
        await send_log_embed(ctx.guild, "Ban", f"{ctx.author.mention} banned {target} — {sanitize_no_mentions(reason)}")
        log_to_file("mod", f"{ctx.author} banned {target} — {reason}")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)


@tree.command(name="kick", description="Kick a guild member (admin)")
async def slash_kick(interaction: discord.Interaction, member: discord.Member, *, reason: Optional[str] = "No reason provided"):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(f"👢 {member.mention} kicked. Reason: {sanitize_no_mentions(reason)}")
        await send_log_embed(interaction.guild, "Kick", f"{interaction.user.mention} kicked {member.mention} — {sanitize_no_mentions(reason)}")
        log_to_file("mod", f"{interaction.user} kicked {member} — {reason}")
    except Exception as e:
        await interaction.response.send_message(f"Failed: {e}", ephemeral=True)


@bot.command(name="kick")
async def prefix_kick(ctx: commands.Context, target: str, *, reason: Optional[str] = "No reason provided"):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    m = re.match(r"<@!?(\d+)>", target)
    uid = int(m.group(1)) if m else (int(target) if target.isdigit() else None)
    if not uid:
        return await ctx.reply("Provide user mention or ID.", mention_author=False)
    member = ctx.guild.get_member(uid)
    if not member:
        return await ctx.reply("Member not found in this guild.", mention_author=False)
    try:
        await member.kick(reason=reason)
        await ctx.reply(f"👢 {member.mention} kicked. Reason: {sanitize_no_mentions(reason)}", mention_author=False)
        await send_log_embed(ctx.guild, "Kick", f"{ctx.author.mention} kicked {member.mention} — {sanitize_no_mentions(reason)}")
        log_to_file("mod", f"{ctx.author} kicked {member} — {reason}")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)


# PURGE (slash and prefix)
@tree.command(name="purge", description="Delete up to 100 messages (admin/pookie)")
async def slash_purge(interaction: discord.Interaction, amount: Optional[int] = 10):
    if not (is_admin_user(interaction.user.id) or is_pookie_user(interaction.user.id)):
        return await interaction.response.send_message("Admins/Pookie only.", ephemeral=True)
    amount = max(1, min(100, amount or 10))
    try:
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.response.send_message(f"🧹 Deleted {len(deleted)} messages.", ephemeral=True)
        log_to_file("mod", f"{interaction.user} purged {len(deleted)} messages in {interaction.channel}")
        await send_log_embed(interaction.guild, "Purge", f"{interaction.user.mention} purged {len(deleted)} messages in {interaction.channel.mention}")
    except Exception as e:
        await interaction.response.send_message(f"Failed: {e}", ephemeral=True)


@bot.command(name="purge")
async def prefix_purge(ctx: commands.Context, amount: int = 10):
    if not is_pookie_user(ctx.author.id) and not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins/Pookie only.", mention_author=False)
    amount = max(1, min(100, amount))
    try:
        deleted = await ctx.channel.purge(limit=amount)
        m = await ctx.send(f"🧹 Deleted {len(deleted)} messages.")
        await asyncio.sleep(3)
        await m.delete()
        await send_log_embed(ctx.guild, "Purge", f"{ctx.author.mention} purged {len(deleted)} messages in {ctx.channel.mention}")
        log_to_file("mod", f"{ctx.author} purged {len(deleted)}")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)


# -------------------------
# TRIGGERS (auto-responder)
# -------------------------
@tree.command(name="trigger_add", description="Add exact-word trigger (admin)")
async def slash_trigger_add(interaction: discord.Interaction, word: str, *, reply: str):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    d = reload_data()
    d.setdefault("triggers", {})
    d["triggers"][word.lower()] = reply
    save_data(d)
    await interaction.response.send_message(f"✅ Trigger added: `{word}` → `{reply}`", ephemeral=True)


@tree.command(name="trigger_remove", description="Remove trigger (admin)")
async def slash_trigger_remove(interaction: discord.Interaction, word: str):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    d = reload_data()
    if word.lower() in d.get("triggers", {}):
        d["triggers"].pop(word.lower(), None)
        save_data(d)
        return await interaction.response.send_message(f"✅ Removed trigger `{word}`", ephemeral=True)
    await interaction.response.send_message("Trigger not found.", ephemeral=True)


@tree.command(name="showtrigger", description="Show triggers (admin)")
async def slash_show_trigger(interaction: discord.Interaction):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    d = reload_data()
    t = d.get("triggers", {})
    if not t:
        return await interaction.response.send_message("No triggers set.", ephemeral=True)
    text = "\n".join(f"`{k}` → `{v[:400]}`" for k, v in t.items())
    await interaction.response.send_message(text[:1900], ephemeral=True)


# -------------------------
# BLOCKED WORDS
# -------------------------
@tree.command(name="blocked_add", description="Add blocked word (admin)")
async def slash_blocked_add(interaction: discord.Interaction, word: str):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    d = reload_data()
    w = word.strip().lower()
    if w and w not in d["blocked_words"]:
        d["blocked_words"].append(w)
        save_data(d)
    await interaction.response.send_message(f"✅ Blocked word `{w}` added.", ephemeral=True)


@tree.command(name="blocked_remove", description="Remove blocked word (admin)")
async def slash_blocked_remove(interaction: discord.Interaction, word: str):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    d = reload_data()
    w = word.strip().lower()
    if w in d["blocked_words"]:
        d["blocked_words"].remove(w)
        save_data(d)
        return await interaction.response.send_message(f"✅ Blocked word `{w}` removed.", ephemeral=True)
    await interaction.response.send_message("Word not found.", ephemeral=True)


@tree.command(name="blocked_list", description="List blocked words (admin)")
async def slash_blocked_list(interaction: discord.Interaction):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    d = reload_data()
    words = d.get("blocked_words", [])
    if not words:
        return await interaction.response.send_message("No blocked words.", ephemeral=True)
    await interaction.response.send_message("\n".join(f"`{w}`" for w in words[:200]), ephemeral=True)


# -------------------------
# AFK (slash + prefix)
# -------------------------
@tree.command(name="afk", description="Set AFK with optional reason")
async def slash_afk(interaction: discord.Interaction, *, reason: Optional[str] = "AFK"):
    d = reload_data()
    d.setdefault("afk", {})
    d["afk"][str(interaction.user.id)] = {"reason": reason or "AFK", "since": datetime.now(timezone.utc).isoformat()}
    save_data(d)
    await interaction.response.send_message(f"💤 Set AFK: **{sanitize_no_mentions(reason or 'AFK')}**", ephemeral=True)


@tree.command(name="afk_clear", description="Clear your AFK")
async def slash_afk_clear(interaction: discord.Interaction):
    d = reload_data()
    if str(interaction.user.id) in d.get("afk", {}):
        d["afk"].pop(str(interaction.user.id), None)
        save_data(d)
        return await interaction.response.send_message("✅ AFK removed.", ephemeral=True)
    await interaction.response.send_message("ℹ️ You were not AFK.", ephemeral=True)


@bot.command(name="afk")
async def prefix_afk(ctx: commands.Context, *, reason: Optional[str] = "AFK"):
    d = reload_data()
    d.setdefault("afk", {})
    d["afk"][str(ctx.author.id)] = {"reason": reason or "AFK", "since": datetime.now(timezone.utc).isoformat()}
    save_data(d)
    await ctx.reply(f"💤 Set AFK: **{sanitize_no_mentions(reason or 'AFK')}**", mention_author=False)


@bot.command(name="afk_clear")
async def prefix_afk_clear(ctx: commands.Context):
    d = reload_data()
    if str(ctx.author.id) in d.get("afk", {}):
        d["afk"].pop(str(ctx.author.id), None)
        save_data(d)
        return await ctx.reply("✅ AFK removed.", mention_author=False)
    await ctx.reply("ℹ️ You were not AFK.", mention_author=False)


# -------------------------
# ADMIN / POKIE MANAGEMENT
# -------------------------
@tree.command(name="add_admin", description="Add admin (owner only)")
async def slash_add_admin(interaction: discord.Interaction, user: discord.User):
    if not is_owner(interaction.user.id):
        return await interaction.response.send_message("Only owner can add admins.", ephemeral=True)
    d = reload_data()
    if int(user.id) not in d["admins"]:
        d["admins"].append(int(user.id))
        save_data(d)
    await interaction.response.send_message(f"✅ {user.mention} added as admin.", ephemeral=True)


@tree.command(name="remove_admin", description="Remove admin (owner only)")
async def slash_remove_admin(interaction: discord.Interaction, user: discord.User):
    if not is_owner(interaction.user.id):
        return await interaction.response.send_message("Only owner can remove admins.", ephemeral=True)
    d = reload_data()
    if int(user.id) == OWNER_ID:
        return await interaction.response.send_message("Cannot remove the owner.", ephemeral=True)
    if int(user.id) in d["admins"]:
        d["admins"].remove(int(user.id))
        save_data(d)
        return await interaction.response.send_message(f"✅ {user.mention} removed from admins.", ephemeral=True)
    await interaction.response.send_message("User not an admin.", ephemeral=True)


@tree.command(name="show_admins", description="Show admins (owner/admin)")
async def slash_show_admins(interaction: discord.Interaction):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    d = reload_data()
    mentions = [f"<@{uid}>" for uid in d.get("admins", [])]
    await interaction.response.send_message("Admins:\n" + ("\n".join(mentions) or "None"), ephemeral=True)


@tree.command(name="addpookie", description="Add pookie (owner/admin)")
async def slash_add_pookie(interaction: discord.Interaction, user: discord.User):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    d = reload_data()
    if int(user.id) not in d["pookies"]:
        d["pookies"].append(int(user.id))
        save_data(d)
    await interaction.response.send_message(f"✅ {user.mention} added as pookie.", ephemeral=True)


@tree.command(name="removepookie", description="Remove pookie (owner/admin)")
async def slash_remove_pookie(interaction: discord.Interaction, user: discord.User):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    d = reload_data()
    if int(user.id) in d["pookies"]:
        d["pookies"].remove(int(user.id))
        save_data(d)
        return await interaction.response.send_message(f"✅ {user.mention} removed from pookie.", ephemeral=True)
    await interaction.response.send_message("User not a pookie.", ephemeral=True)


@tree.command(name="listpookie", description="List pookies (owner/admin)")
async def slash_list_pookie(interaction: discord.Interaction):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    d = reload_data()
    mentions = [f"<@{uid}>" for uid in d.get("pookies", [])]
    await interaction.response.send_message("Pookies:\n" + (("\n".join(mentions)) or "None"), ephemeral=True)


# -------------------------
# SHOWCOMMANDS interactive
# -------------------------
CATEGORIES = {
    "Fun": ["cat", "8ball", "joke", "dadjoke", "coinflip", "rolldice", "rps", "avatar", "userinfo"],
    "Moderation": ["ban", "kick", "purge", "say", "say_admin"],
    "Management": ["add_admin", "remove_admin", "show_admins", "addpookie", "removepookie", "listpookie", "set_log_channel", "disable_log_channel", "logs"],
    "Cats": ["cat", "setcatchannel", "sethourlycatchannel"],
}


class ShowCommandsView(discord.ui.View):
    def __init__(self, user: discord.User):
        super().__init__(timeout=120)
        self.user = user

    @discord.ui.select(placeholder="Choose category", options=[discord.SelectOption(label=k) for k in CATEGORIES.keys()])
    async def select_cb(self, interaction: discord.Interaction, select: discord.ui.Select):
        cat = select.values[0]
        items = CATEGORIES.get(cat, [])
        filtered = []
        for c in items:
            if c in ["add_admin", "remove_admin", "addpookie", "removepookie", "listpookie", "set_log_channel", "disable_log_channel", "logs"]:
                if not is_admin_user(interaction.user.id):
                    continue
            filtered.append(c)
        text = f"**{cat}**\n" + (", ".join(f"`/{x}`" for x in filtered) if filtered else "No commands you can use here.")
        await interaction.response.edit_message(content=text, view=self)


@tree.command(name="showcommands", description="Interactive list of commands you can use")
async def slash_showcommands(interaction: discord.Interaction):
    view = ShowCommandsView(interaction.user)
    await interaction.response.send_message("Choose a category:", view=view, ephemeral=True)


# -------------------------
# FUN: 8ball, jokes, coin, dice, rps
# -------------------------
@tree.command(name="8ball", description="Ask the magic 8-ball")
async def slash_8ball(interaction: discord.Interaction, *, question: str):
    answers = ["Yes.", "No.", "Maybe.", "Absolutely!", "Ask again later.", "Definitely not.", "Probably.", "Unlikely."]
    await interaction.response.send_message(f"🎱 {random.choice(answers)}")


@bot.command(name="8ball")
async def prefix_8ball(ctx: commands.Context, *, question: str):
    answers = ["Yes.", "No.", "Maybe.", "Absolutely!", "Ask again later.", "Definitely not.", "Probably.", "Unlikely."]
    await ctx.reply(f"🎱 {random.choice(answers)}", mention_author=False)


@tree.command(name="joke", description="Tell a joke")
async def slash_joke(interaction: discord.Interaction):
    jokes = ["I told my computer I needed a break, and it said 'No problem — I'll go to sleep.'",
             "Why do programmers prefer dark mode? Because light attracts bugs."]
    await interaction.response.send_message(random.choice(jokes))


@tree.command(name="dadjoke", description="Tell a dad joke")
async def slash_dadjoke(interaction: discord.Interaction):
    jokes = ["I used to play piano by ear, now I use my hands.", "Why don't eggs tell jokes? They'd crack each other up."]
    await interaction.response.send_message(random.choice(jokes))


@tree.command(name="coinflip", description="Flip a coin")
async def slash_coin(interaction: discord.Interaction):
    await interaction.response.send_message("Heads" if random.random() < 0.5 else "Tails")


@tree.command(name="rolldice", description="Roll a dice (1-6)")
async def slash_dice(interaction: discord.Interaction):
    await interaction.response.send_message(f"🎲 {random.randint(1,6)}")


# rps using choices decorator (supported)
@app_commands.choices(choice=[
    app_commands.Choice(name="rock", value="rock"),
    app_commands.Choice(name="paper", value="paper"),
    app_commands.Choice(name="scissors", value="scissors"),
])
@tree.command(name="rps", description="Rock Paper Scissors")
async def slash_rps(interaction: discord.Interaction, choice: app_commands.Choice[str]):
    bot_choice = random.choice(["rock", "paper", "scissors"])
    user = choice.value
    result = "draw"
    if (user, bot_choice) in [("rock", "scissors"), ("paper", "rock"), ("scissors", "paper")]:
        result = "You win!"
    elif user != bot_choice:
        result = "You lose!"
    await interaction.response.send_message(f"You: **{user}** | Bot: **{bot_choice}** → {result}")


# -------------------------
# AVATAR / USERINFO / GUILDINFO
# -------------------------
@tree.command(name="avatar", description="Show a user's avatar")
async def slash_avatar(interaction: discord.Interaction, user: Optional[discord.User] = None):
    u = user or interaction.user
    emb = discord.Embed(title=f"{u} — Avatar", color=discord.Color.green())
    emb.set_image(url=u.display_avatar.url)
    await interaction.response.send_message(embed=emb)


@tree.command(name="userinfo", description="Show information about a user")
async def slash_userinfo(interaction: discord.Interaction, user: Optional[discord.User] = None):
    u = user or interaction.user
    emb = discord.Embed(title=f"{u}", color=discord.Color.blurple())
    emb.add_field(name="ID", value=str(u.id))
    emb.add_field(name="Bot?", value=str(u.bot))
    emb.set_thumbnail(url=u.display_avatar.url)
    await interaction.response.send_message(embed=emb, ephemeral=True)


@tree.command(name="guildinfo", description="Show guild info by id (admin)")
async def slash_guildinfo(interaction: discord.Interaction, guild_id: str):
    if not is_admin_user(interaction.user.id):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    try:
        gid = int(guild_id)
    except Exception:
        return await interaction.response.send_message("Invalid guild id.", ephemeral=True)
    g = bot.get_guild(gid)
    if not g:
        return await interaction.response.send_message("Bot is not in that guild.", ephemeral=True)
    inv = None
    try:
        for ch in g.text_channels:
            if ch.permissions_for(g.me).create_instant_invite:
                inv_obj = await ch.create_invite(max_age=3600, max_uses=1, unique=True)
                inv = str(inv_obj)
                break
    except Exception:
        inv = None
    emb = discord.Embed(title=f"{g.name}", color=discord.Color.gold())
    emb.add_field(name="ID", value=str(g.id))
    emb.add_field(name="Owner", value=f"{g.owner} ({g.owner_id})")
    emb.add_field(name="Members", value=str(g.member_count))
    emb.add_field(name="Channels", value=f"{len(g.text_channels)} text / {len(g.voice_channels)} voice")
    if inv:
        emb.add_field(name="Invite (1h)", value=inv)
    await interaction.response.send_message(embed=emb, ephemeral=True)


# -------------------------
# START BOT
# -------------------------
# ensure data file exists & owner present
_data = reload_data()
if OWNER_ID not in _data.get("admins", []):
    _data["admins"].append(int(OWNER_ID))
    for aid in DEFAULT_EXTRA_ADMINS:
        if aid not in _data["admins"]:
            _data["admins"].append(int(aid))
    save_data(_data)


def _run_bot():
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        print("Bot run error:", e)
        raise


if __name__ == "__main__":
    _run_bot()
