# main.py - Full single-file Discord bot (one chunk)
# Requirements (examples for requirements.txt):
# discord.py>=2.3.2
# aiohttp
# flask
# python-dotenv  (optional for local .env testing)
#
# Put your secrets into env vars (never commit tokens).

import os
import sys
import json
import re
import random
import asyncio
import aiohttp
import atexit
import platform
from datetime import datetime, timezone, timedelta
from threading import Thread
from typing import Optional, Dict, Any, List
from zoneinfo import ZoneInfo
import traceback

import discord
from discord.ext import commands, tasks
from discord import app_commands
from flask import Flask

# -------------------------
# CONFIG / ENV
# -------------------------
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN environment variable is required")

try:
    OWNER_ID = int(os.getenv("OWNER_ID", "1319292111325106296").strip())
except Exception:
    OWNER_ID = 1319292111325106296

# Additional default admins that should always be admins
DEFAULT_EXTRA_ADMINS = {1380315427992768633, 909468887098216499}

CAT_API_KEY = os.getenv("CAT_API_KEY", "").strip()
RENDER_API_KEY = os.getenv("RENDER_API_KEY", "").strip()
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "").strip()

TZ_NAME = os.getenv("TZ", "Asia/Kolkata").strip() or "Asia/Kolkata"
try:
    BOT_TZ = ZoneInfo(TZ_NAME)
except Exception:
    BOT_TZ = timezone.utc

FLASK_PORT = int(os.getenv("PORT", os.getenv("FLASK_PORT", "8080")))

DATA_FILE = "bot_data.json"
SNIPES_KEEP = 100
LOGS_KEEP = 2000

# -------------------------
# FLASK KEEPALIVE
# -------------------------
flask_app = Flask("bot_keepalive")


@flask_app.route("/")
def alive():
    return "OK", 200


def _run_flask():
    flask_app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, use_reloader=False)


Thread(target=_run_flask, daemon=True).start()

# -------------------------
# PERSISTENT STORAGE
# -------------------------
DEFAULT_DATA = {
    "admins": [],  # ints
    "pookies": [],
    "blacklist": [],
    "blocked_words": [],
    "triggers": {},  # word -> reply
    "log_channel": None,
    "cat_channel": None,  # daily 11:00 IST
    "hourly_cat_channel": None,
    "logs": [],
    "afk": {},  # user_id -> {"reason","since"}
    "warns": {},  # user_id -> [warns]
    "temp_roles": []  # dicts {guild_id,user_id,role_id,expires_at_iso}
}


def save_data(d: Dict[str, Any]):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Failed to save data:", e)


def load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        d = DEFAULT_DATA.copy()
        # default admin list: owner + extras
        d["admins"] = list({OWNER_ID} | DEFAULT_EXTRA_ADMINS)
        save_data(d)
        return d
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = DEFAULT_DATA.copy()
    # ensure keys exist
    for k, v in DEFAULT_DATA.items():
        if k not in d:
            d[k] = v.copy() if isinstance(v, (list, dict)) else v
    # ensure owner + extras present
    admins = set(map(int, d.get("admins", [])))
    admins.add(int(OWNER_ID))
    admins |= DEFAULT_EXTRA_ADMINS
    d["admins"] = list(admins)
    return d


DATA: Dict[str, Any] = load_data()


def reload_data() -> Dict[str, Any]:
    global DATA
    DATA = load_data()
    return DATA


atexit.register(lambda: save_data(DATA))

# -------------------------
# HELPERS
# -------------------------
def is_owner_id(uid: int) -> bool:
    return int(uid) == int(OWNER_ID)


def is_admin_user(uid: int) -> bool:
    d = reload_data()
    return int(uid) in set(map(int, d.get("admins", []))) or is_owner_id(uid)


def is_pookie_user(uid: int) -> bool:
    d = reload_data()
    return int(uid) in set(map(int, d.get("pookies", []))) or is_admin_user(uid)


def is_blacklisted(uid: int) -> bool:
    d = reload_data()
    return int(uid) in set(map(int, d.get("blacklist", [])))


def sanitize_no_mentions(text: str) -> str:
    return text.replace("@", "@\u200b")


def exact_word_present(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text, flags=re.IGNORECASE) is not None


def add_log(kind: str, message: str):
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
        try:
            ch = guild.get_channel(ch_id)
        except Exception:
            ch = None
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
        super().__init__(timeout=180)
        self.items = items
        self.index = len(items) - 1

    def make_embed(self) -> discord.Embed:
        data = self.items[self.index]
        e = discord.Embed(color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
        e.set_author(name=data.get("author_tag", "Unknown"), icon_url=data.get("avatar_url") or discord.Embed.Empty)
        e.set_footer(text=f"{self.index + 1}/{len(self.items)} • {data.get('time','')}")
        if data.get("content") is not None:
            e.add_field(name="Message", value=data.get("content") or "*empty*", inline=False)
        else:
            e.add_field(name="Before", value=data.get("before") or "*empty*", inline=False)
            e.add_field(name="After", value=data.get("after") or "*empty*", inline=False)
        return e

    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index > 0:
            self.index -= 1
            await interaction.response.edit_message(embed=self.make_embed(), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="➡️", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
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
BOT_START = datetime.now(timezone.utc)


# -------------------------
# SCHEDULED TASKS (defined before on_ready)
# -------------------------
_last_daily_date: Optional[str] = None
_last_hourly_key: Optional[str] = None


@tasks.loop(minutes=1)
async def minute_task():
    global _last_daily_date, _last_hourly_key
    now_local = datetime.now(BOT_TZ)
    d = reload_data()
    # Daily at 11:00 local timezone
    try:
        if d.get("cat_channel") and now_local.hour == 11 and now_local.minute == 0:
            today_iso = now_local.date().isoformat()
            if _last_daily_date != today_iso:
                ch = bot.get_channel(int(d["cat_channel"]))
                if ch:
                    try:
                        async with aiohttp.ClientSession() as s:
                            url = await fetch_random_cat_url(s)
                        if url:
                            await ch.send(url)
                            add_log("cat_daily", f"Sent daily cat to {ch.id}")
                            await send_log_embed(ch.guild if ch.guild else None, "Daily Cat", f"Sent daily cat in {ch.mention}")
                    except Exception:
                        pass
                _last_daily_date = today_iso
    except Exception:
        pass
    # Hourly at minute == 0
    try:
        if d.get("hourly_cat_channel") and now_local.minute == 0:
            key = f"{now_local.date().isoformat()}-{now_local.hour}"
            if _last_hourly_key != key:
                ch2 = bot.get_channel(int(d["hourly_cat_channel"]))
                if ch2:
                    try:
                        async with aiohttp.ClientSession() as s:
                            url2 = await fetch_random_cat_url(s)
                        if url2:
                            await ch2.send(url2)
                            add_log("cat_hourly", f"Sent hourly cat to {ch2.id}")
                            await send_log_embed(ch2.guild if ch2.guild else None, "Hourly Cat", f"Sent hourly cat in {ch2.mention}")
                    except Exception:
                        pass
                _last_hourly_key = key
    except Exception:
        pass
    # Temp roles expiry check
    try:
        d = reload_data()
        changed = False
        remaining = []
        for e in d.get("temp_roles", []):
            expires_at = e.get("expires_at")
            if not expires_at:
                continue
            try:
                exp_dt = datetime.fromisoformat(expires_at)
            except Exception:
                continue
            if datetime.now(timezone.utc) >= exp_dt:
                try:
                    g = bot.get_guild(int(e["guild_id"]))
                    if g:
                        mem = g.get_member(int(e["user_id"]))
                        role = g.get_role(int(e["role_id"]))
                        if mem and role:
                            await mem.remove_roles(role, reason="Temporary role expired")
                            add_log("temp_role_removed", f"Removed role {role.id} from {mem.id}")
                except Exception:
                    pass
                changed = True
            else:
                remaining.append(e)
        if changed:
            d["temp_roles"] = remaining
            save_data(d)
    except Exception:
        pass


# -------------------------
# RENDER DEPLOY TRIGGER
# -------------------------
async def trigger_render_deploy(api_key: str, service_id: str) -> Dict[str, Any]:
    url = f"https://api.render.com/v1/services/{service_id}/deploys"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as s:
        try:
            async with s.post(url, headers=headers, json={}, timeout=30) as resp:
                text = await resp.text()
                try:
                    j = await resp.json()
                except Exception:
                    j = {"text": text}
                return {"status": resp.status, "body": j}
        except Exception as e:
            return {"status": 0, "error": str(e)}


# -------------------------
# ON READY
# -------------------------
@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
    except Exception as e:
        print("Sync error:", e)
    await bot.change_presence(status=discord.Status.dnd,
                              activity=discord.Streaming(name="Max Verstappen", url="https://twitch.tv/"))
    # Ensure minute task running
    if not minute_task.is_running():
        minute_task.start()
    print(f"✅ Logged in as {bot.user} — guilds: {len(bot.guilds)}")
    add_log("system", f"Bot ready: {bot.user} in {len(bot.guilds)} guilds")


# -------------------------
# EVENTS: message edit/delete etc
# -------------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    d = reload_data()

    # AFK removal if author posts
    if str(message.author.id) in d.get("afk", {}):
        d["afk"].pop(str(message.author.id), None)
        save_data(d)
        try:
            await message.channel.send(f"✅ Welcome back {message.author.mention}. AFK removed.")
        except Exception:
            pass

    # If mentions include AFK users, notify
    if message.mentions:
        for u in message.mentions:
            afk = d.get("afk", {}).get(str(u.id))
            if afk:
                reason = afk.get("reason", "AFK")
                since = afk.get("since")
                try:
                    ts = int(datetime.fromisoformat(since).timestamp())
                    await message.reply(f"{u.mention} is AFK — **{sanitize_no_mentions(reason)}** (since <t:{ts}:R>)", mention_author=False)
                except Exception:
                    await message.reply(f"{u.mention} is AFK — **{sanitize_no_mentions(reason)}**", mention_author=False)

    # Blocked words detection (compact)
    content_compact = re.sub(r"[\s\-\_\.]", "", message.content.lower())
    for w in d.get("blocked_words", []):
        wc = re.sub(r"[\s\-\_\.]", "", w.lower())
        if wc and wc in content_compact:
            try:
                await message.delete()
            except Exception:
                pass
            await send_log_embed(message.guild, "Blocked Word", f"{message.author.mention} used blocked word `{w}` in {message.channel.mention}")
            add_log("blocked_word", f"{message.author} used blocked word {w} in {message.channel}")
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
    add_log("delete", f"{message.author} deleted message in {message.channel}")


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
    add_log("edit", f"{before.author} edited message in {before.channel}")


@bot.event
async def on_member_join(member: discord.Member):
    await send_log_embed(member.guild, "Member Joined", f"{member.mention} joined")
    add_log("join", f"{member} joined {member.guild}")


@bot.event
async def on_member_remove(member: discord.Member):
    await send_log_embed(member.guild, "Member Left", f"{member.mention} left")
    add_log("leave", f"{member} left {member.guild}")


# -------------------------
# HYBRID COMMANDS: many features
# -------------------------
# UPTIME
@bot.hybrid_command(name="uptime", with_app_command=True, description="Show bot uptime")
async def uptime(ctx: commands.Context):
    delta = datetime.now(timezone.utc) - BOT_START
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    await ctx.reply(f"⏱ Uptime: {days}d {hours}h {minutes}m {seconds}s", mention_author=False)


# SERVERS (admins only)
@bot.hybrid_command(name="servers", with_app_command=True, description="List servers bot is in (admins only)")
async def servers(ctx: commands.Context):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("❌ Admins only.", mention_author=False)
    lines = [f"• {g.name} — id:{g.id} members:{g.member_count}" for g in bot.guilds]
    await ctx.reply(f"🤖 Bot is in {len(bot.guilds)} servers:\n" + ("\n".join(lines)[:1900]), mention_author=False)


# ASK / askforcommand (pings owner in log channel + DM)
@bot.hybrid_command(name="askforcommand", with_app_command=True, description="Ask the owner for a command (pings owner)")
async def askforcommand(ctx: commands.Context, *, request: str):
    owner = None
    try:
        owner = await bot.fetch_user(OWNER_ID)
    except Exception:
        owner = None
    if owner:
        try:
            await owner.send(f"📨 Request from {ctx.author} ({ctx.author.id}) in {ctx.guild.name if ctx.guild else 'DM'}:\n{request[:2000]}")
        except Exception:
            pass
    d = reload_data()
    log_ch = d.get("log_channel")
    if log_ch:
        ch = bot.get_channel(int(log_ch))
        if ch:
            try:
                await ch.send(f"<@{OWNER_ID}> 📨 Request from {ctx.author.mention} in {ctx.guild.name if ctx.guild else 'DM'}:\n```{request[:1500]}```")
            except Exception:
                pass
    await ctx.reply("✅ Your request was sent to the owner (and pinged in the log channel if configured).", mention_author=False)
    add_log("ask", f"{ctx.author} asked: {request[:200]}")
    await send_log_embed(ctx.guild if ctx.guild else None, "AskRequest", f"{ctx.author.mention} asked: {request[:900]}")


# REFRESH (sync slash commands)
@bot.hybrid_command(name="refresh", with_app_command=True, description="Refresh slash commands (admin)")
async def refresh(ctx: commands.Context):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    try:
        await bot.tree.sync()
        await ctx.reply("✅ Slash commands refreshed.", mention_author=False)
        add_log("admin", f"{ctx.author} refreshed slash commands")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)


# RESTART (Render API or process exit)
@bot.hybrid_command(name="restart", with_app_command=True, description="Restart bot (admin). Uses Render API if configured.")
async def restart(ctx: commands.Context):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    if RENDER_API_KEY and RENDER_SERVICE_ID:
        await ctx.reply("🔁 Triggering Render deploy...", mention_author=False)
        res = await trigger_render_deploy(RENDER_API_KEY, RENDER_SERVICE_ID)
        await ctx.send(f"Result: {res}", ephemeral=True) if hasattr(ctx, "send") else None
        add_log("admin", f"{ctx.author} triggered render deploy")
    else:
        await ctx.reply("🔁 Restarting process (no Render API configured)...", mention_author=False)
        add_log("admin", f"{ctx.author} requested restart")
        asyncio.create_task(_shutdown_and_exit())


async def _shutdown_and_exit(delay: float = 0.5):
    await asyncio.sleep(delay)
    try:
        await bot.close()
    finally:
        os._exit(0)


# DEBUG (admin) - includes uptime, mem (psutil optional), platform, latency
@bot.hybrid_command(name="debug", with_app_command=True, description="Show debug info (admins)")
async def debug(ctx: commands.Context):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    mem = "psutil not installed"
    try:
        import psutil
        p = psutil.Process(os.getpid())
        mem = f"{p.memory_info().rss/(1024*1024):.1f} MiB"
    except Exception:
        pass
    delta = datetime.now(timezone.utc) - BOT_START
    emb = discord.Embed(title="Debug Info", color=discord.Color.teal())
    emb.add_field(name="Uptime", value=str(delta))
    emb.add_field(name="Guilds", value=str(len(bot.guilds)))
    emb.add_field(name="Latency", value=f"{round(bot.latency*1000)} ms")
    emb.add_field(name="Memory", value=mem)
    emb.add_field(name="Python", value=platform.python_version())
    emb.add_field(name="discord.py", value=discord.__version__)
    await ctx.reply(embed=emb, mention_author=False)
    add_log("admin", f"{ctx.author} used debug")


# EVAL (owner only) - runs code, returns result or output
@bot.hybrid_command(name="eval", with_app_command=True, description="EVAL python (owner only)")
async def _eval(ctx: commands.Context, *, code: str):
    if not is_owner_id(ctx.author.id):
        return await ctx.reply("Owner only.", mention_author=False)
    env = {"bot": bot, "discord": discord, "commands": commands, "asyncio": asyncio, "os": os, "sys": sys}
    try:
        # Try eval first
        result = None
        try:
            result = eval(code, env)
            if asyncio.iscoroutine(result):
                result = await result
        except SyntaxError:
            exec(compile(code, "<eval>", "exec"), env)
            result = "Executed."
        await ctx.reply(f"Result:\n```\n{str(result)[:1900]}\n```", mention_author=False)
    except Exception as e:
        tb = traceback.format_exc()
        await ctx.reply(f"Error:\n```\n{tb[-1900:]}\n```", mention_author=False)


# -------------------------
# LOG CHANNEL / LOGS VIEW
# -------------------------
@bot.hybrid_command(name="set_log_channel", with_app_command=True, description="Set channel for logs (admin)")
async def set_log_channel(ctx: commands.Context, channel: discord.TextChannel):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    d["log_channel"] = int(channel.id)
    save_data(d)
    await ctx.reply(f"✅ Log channel set to {channel.mention}", mention_author=False)
    await send_log_embed(ctx.guild, "Logs", f"{ctx.author.mention} set the log channel to {channel.mention}")


@bot.hybrid_command(name="disable_log_channel", with_app_command=True, description="Disable log channel (admin)")
async def disable_log_channel(ctx: commands.Context):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    d["log_channel"] = None
    save_data(d)
    await ctx.reply("✅ Log channel disabled.", mention_author=False)


@bot.hybrid_command(name="logs", with_app_command=True, description="Show recent logs (admin)")
async def logs(ctx: commands.Context, count: Optional[int] = 10):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    entries = d.get("logs", [])[-min(max(1, count or 10), 50):]
    text = "\n".join(f"`{e['ts']}` {e['kind']}: {e['message']}" for e in entries) or "No logs."
    await ctx.reply(text[:1900], mention_author=False)


# -------------------------
# CAT: set channels & cat command
# -------------------------
@bot.hybrid_command(name="setcatchannel", with_app_command=True, description="Set daily 11:00 IST cat channel (admin)")
async def setcatchannel(ctx: commands.Context, channel: discord.TextChannel):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    d["cat_channel"] = int(channel.id)
    save_data(d)
    await ctx.reply(f"✅ Daily cat channel set to {channel.mention} (11:00 {TZ_NAME})", mention_author=False)


@bot.hybrid_command(name="sethourlycatchannel", with_app_command=True, description="Set hourly cat channel (admin)")
async def sethourlycatchannel(ctx: commands.Context, channel: discord.TextChannel):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    d["hourly_cat_channel"] = int(channel.id)
    save_data(d)
    await ctx.reply(f"✅ Hourly cat channel set to {channel.mention}", mention_author=False)


@bot.hybrid_command(name="cat", with_app_command=True, description="Get a random cat image")
async def cat(ctx: commands.Context):
    if is_blacklisted(ctx.author.id):
        return await ctx.reply("You are blacklisted.", mention_author=False)
    await ctx.defer() if hasattr(ctx, "defer") else None
    async with aiohttp.ClientSession() as s:
        url = await fetch_random_cat_url(s)
    if not url:
        return await ctx.reply("⚠️ Couldn't fetch a cat right now.", mention_author=False)
    await ctx.reply(url, mention_author=False)
    add_log("cat", f"{ctx.author} requested cat")


# -------------------------
# SAY / SAY_ADMIN
# -------------------------
@bot.hybrid_command(name="say", with_app_command=True, description="Bot repeats text (no pings)")
async def say(ctx: commands.Context, *, text: str):
    safe = sanitize_no_mentions(text)
    # Send as plain message (no pings)
    await ctx.reply("✅ Sent (no pings).", mention_author=False)
    try:
        await ctx.send(safe, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        # fallback
        await ctx.send(safe)
    add_log("command", f"{ctx.author} used say")


@bot.hybrid_command(name="say_admin", with_app_command=True, description="Admin say (pings allowed)")
async def say_admin(ctx: commands.Context, *, text: str):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    await ctx.reply("✅ Sent.", mention_author=False)
    await ctx.send(text)
    add_log("command", f"{ctx.author} used say_admin")


# -------------------------
# MODERATION: ban, kick, purge
# -------------------------
@bot.hybrid_command(name="ban", with_app_command=True, description="Ban a member (admin). Slash requires a member.")
async def ban(ctx: commands.Context, target: str, *, reason: Optional[str] = "No reason provided"):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    # If invoked by slash, target may actually be a Member object; try to resolve
    if isinstance(target, discord.Member):
        member = target
        try:
            await member.ban(reason=reason)
            await ctx.reply(f"🔨 {member.mention} banned. Reason: {sanitize_no_mentions(reason)}", mention_author=False)
            await send_log_embed(ctx.guild, "Ban", f"{ctx.author.mention} banned {member.mention} — {sanitize_no_mentions(reason)}")
            add_log("mod", f"{ctx.author} banned {member} — {reason}")
            return
        except Exception as e:
            return await ctx.reply(f"Failed: {e}", mention_author=False)
    # parse mention or id
    m = re.match(r"<@!?(\d+)>", target)
    uid = None
    if m:
        uid = int(m.group(1))
    else:
        try:
            uid = int(target)
        except Exception:
            return await ctx.reply("Provide mention or user id.", mention_author=False)
    member = ctx.guild.get_member(uid) if ctx.guild else None
    try:
        if member:
            await member.ban(reason=reason)
            await ctx.reply(f"🔨 {member.mention} banned. Reason: {sanitize_no_mentions(reason)}", mention_author=False)
        else:
            await ctx.guild.ban(discord.Object(id=uid), reason=reason)
            await ctx.reply(f"🔨 Banned ID {uid}.", mention_author=False)
        await send_log_embed(ctx.guild, "Ban", f"{ctx.author.mention} banned {target} — {sanitize_no_mentions(reason)}")
        add_log("mod", f"{ctx.author} banned {target} — {reason}")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)


@bot.hybrid_command(name="kick", with_app_command=True, description="Kick a member (admin). Slash requires a member.")
async def kick(ctx: commands.Context, target: str, *, reason: Optional[str] = "No reason provided"):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    if isinstance(target, discord.Member):
        member = target
        try:
            await member.kick(reason=reason)
            await ctx.reply(f"👢 {member.mention} kicked. Reason: {sanitize_no_mentions(reason)}", mention_author=False)
            await send_log_embed(ctx.guild, "Kick", f"{ctx.author.mention} kicked {member.mention} — {sanitize_no_mentions(reason)}")
            add_log("mod", f"{ctx.author} kicked {member} — {reason}")
            return
        except Exception as e:
            return await ctx.reply(f"Failed: {e}", mention_author=False)
    m = re.match(r"<@!?(\d+)>", target)
    uid = int(m.group(1)) if m else (int(target) if target.isdigit() else None)
    if not uid:
        return await ctx.reply("Provide mention or id.", mention_author=False)
    member = ctx.guild.get_member(uid)
    if not member:
        return await ctx.reply("Member not found in this guild.", mention_author=False)
    try:
        await member.kick(reason=reason)
        await ctx.reply(f"👢 {member.mention} kicked. Reason: {sanitize_no_mentions(reason)}", mention_author=False)
        await send_log_embed(ctx.guild, "Kick", f"{ctx.author.mention} kicked {member.mention} — {sanitize_no_mentions(reason)}")
        add_log("mod", f"{ctx.author} kicked {member} — {reason}")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)


@bot.hybrid_command(name="purge", with_app_command=True, description="Delete up to 100 messages (admin/pookie)")
async def purge(ctx: commands.Context, amount: Optional[int] = 10):
    if not (is_admin_user(ctx.author.id) or is_pookie_user(ctx.author.id)):
        return await ctx.reply("Admins/Pookie only.", mention_author=False)
    amount = max(1, min(100, amount or 10))
    try:
        deleted = await ctx.channel.purge(limit=amount)
        m = await ctx.send(f"🧹 Deleted {len(deleted)} messages.")
        await asyncio.sleep(3)
        await m.delete()
        await send_log_embed(ctx.guild, "Purge", f"{ctx.author.mention} purged {len(deleted)} messages in {ctx.channel.mention}")
        add_log("mod", f"{ctx.author} purged {len(deleted)} messages")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)


# -------------------------
# TRIGGERS & BLOCKED WORDS
# -------------------------
@bot.hybrid_command(name="trigger_add", with_app_command=True, description="Add exact-word trigger (admin)")
async def trigger_add(ctx: commands.Context, word: str, *, reply: str):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    d.setdefault("triggers", {})
    d["triggers"][word.lower()] = reply
    save_data(d)
    await ctx.reply(f"✅ Trigger added: `{word}` → `{reply}`", mention_author=False)


@bot.hybrid_command(name="trigger_remove", with_app_command=True, description="Remove trigger (admin)")
async def trigger_remove(ctx: commands.Context, word: str):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    if word.lower() in d.get("triggers", {}):
        d["triggers"].pop(word.lower(), None)
        save_data(d)
        return await ctx.reply(f"✅ Removed trigger `{word}`", mention_author=False)
    await ctx.reply("Trigger not found.", mention_author=False)


@bot.hybrid_command(name="showtrigger", with_app_command=True, description="Show triggers (admin)")
async def showtrigger(ctx: commands.Context):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    t = d.get("triggers", {})
    if not t:
        return await ctx.reply("No triggers set.", mention_author=False)
    text = "\n".join(f"`{k}` → `{v[:300]}`" for k, v in t.items())
    await ctx.reply(text[:1900], mention_author=False)


@bot.hybrid_command(name="blocked_add", with_app_command=True, description="Add blocked word (admin)")
async def blocked_add(ctx: commands.Context, word: str):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    w = word.strip().lower()
    if w and w not in d["blocked_words"]:
        d["blocked_words"].append(w)
        save_data(d)
    await ctx.reply(f"✅ Blocked word `{w}` added.", mention_author=False)


@bot.hybrid_command(name="blocked_remove", with_app_command=True, description="Remove blocked word (admin)")
async def blocked_remove(ctx: commands.Context, word: str):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    w = word.strip().lower()
    if w in d["blocked_words"]:
        d["blocked_words"].remove(w)
        save_data(d)
        return await ctx.reply(f"✅ Blocked word `{w}` removed.", mention_author=False)
    await ctx.reply("Word not found.", mention_author=False)


@bot.hybrid_command(name="blocked_list", with_app_command=True, description="List blocked words (admin)")
async def blocked_list(ctx: commands.Context):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    words = d.get("blocked_words", [])
    if not words:
        return await ctx.reply("No blocked words.", mention_author=False)
    await ctx.reply("\n".join(f"`{w}`" for w in words[:200]), mention_author=False)


# -------------------------
# AFK
# -------------------------
@bot.hybrid_command(name="afk", with_app_command=True, description="Set AFK with optional reason")
async def afk(ctx: commands.Context, *, reason: Optional[str] = "AFK"):
    d = reload_data()
    d.setdefault("afk", {})
    d["afk"][str(ctx.author.id)] = {"reason": reason or "AFK", "since": datetime.now(timezone.utc).isoformat()}
    save_data(d)
    await ctx.reply(f"✅ AFK set: **{sanitize_no_mentions(reason or 'AFK')}**", mention_author=False)


@bot.hybrid_command(name="afk_clear", with_app_command=True, description="Clear AFK")
async def afk_clear(ctx: commands.Context):
    d = reload_data()
    if str(ctx.author.id) in d.get("afk", {}):
        d["afk"].pop(str(ctx.author.id), None)
        save_data(d)
        return await ctx.reply("✅ AFK removed.", mention_author=False)
    await ctx.reply("ℹ️ You were not AFK.", mention_author=False)


# -------------------------
# ADMIN/POOKIE/BLACKLIST MANAGEMENT
# -------------------------
@bot.hybrid_command(name="add_admin", with_app_command=True, description="Add admin (owner only)")
async def add_admin(ctx: commands.Context, user: discord.User):
    if not is_owner_id(ctx.author.id):
        return await ctx.reply("Owner only.", mention_author=False)
    d = reload_data()
    if int(user.id) not in d["admins"]:
        d["admins"].append(int(user.id))
        save_data(d)
    await ctx.reply(f"✅ {user.mention} added as admin.", mention_author=False)


@bot.hybrid_command(name="remove_admin", with_app_command=True, description="Remove admin (owner only)")
async def remove_admin(ctx: commands.Context, user: discord.User):
    if not is_owner_id(ctx.author.id):
        return await ctx.reply("Owner only.", mention_author=False)
    d = reload_data()
    if int(user.id) == OWNER_ID:
        return await ctx.reply("Cannot remove owner.", mention_author=False)
    if int(user.id) in d["admins"]:
        d["admins"].remove(int(user.id))
        save_data(d)
        return await ctx.reply(f"✅ {user.mention} removed from admins.", mention_author=False)
    await ctx.reply("User not an admin.", mention_author=False)


@bot.hybrid_command(name="show_admins", with_app_command=True, description="List admins")
async def show_admins(ctx: commands.Context):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    mentions = [f"<@{uid}>" for uid in d.get("admins", [])]
    await ctx.reply("Admins:\n" + ("\n".join(mentions) or "None"), mention_author=False)


@bot.hybrid_command(name="addpookie", with_app_command=True, description="Add pookie user")
async def addpookie(ctx: commands.Context, user: discord.User):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    if int(user.id) not in d["pookies"]:
        d["pookies"].append(int(user.id))
        save_data(d)
    await ctx.reply(f"✅ {user.mention} added as pookie.", mention_author=False)


@bot.hybrid_command(name="removepookie", with_app_command=True, description="Remove pookie user")
async def removepookie(ctx: commands.Context, user: discord.User):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    if int(user.id) in d["pookies"]:
        d["pookies"].remove(int(user.id))
        save_data(d)
        return await ctx.reply(f"✅ {user.mention} removed from pookie.", mention_author=False)
    await ctx.reply("User not a pookie.", mention_author=False)


@bot.hybrid_command(name="listpookie", with_app_command=True, description="List pookie users (pings)")
async def listpookie(ctx: commands.Context):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    mentions = [f"<@{uid}>" for uid in d.get("pookies", [])]
    await ctx.reply("Pookies:\n" + ("\n".join(mentions) or "None"), mention_author=False)


@bot.hybrid_command(name="blacklist_add", with_app_command=True, description="Add user to blacklist (admin)")
async def blacklist_add(ctx: commands.Context, user: discord.User):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    if int(user.id) not in d["blacklist"]:
        d["blacklist"].append(int(user.id))
        save_data(d)
    await ctx.reply(f"✅ {user.mention} blacklisted.", mention_author=False)


@bot.hybrid_command(name="blacklist_remove", with_app_command=True, description="Remove user from blacklist (admin)")
async def blacklist_remove(ctx: commands.Context, user: discord.User):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    if int(user.id) in d["blacklist"]:
        d["blacklist"].remove(int(user.id))
        save_data(d)
        return await ctx.reply(f"✅ {user.mention} removed from blacklist.", mention_author=False)
    await ctx.reply("User not blacklisted.", mention_author=False)


# -------------------------
# SHOWCOMMANDS interactive
# -------------------------
CATEGORIES = {
    "Fun": ["cat", "8ball", "joke", "dadjoke", "coinflip", "rolldice", "rps", "avatar", "userinfo"],
    "Moderation": ["ban", "kick", "purge", "say", "say_admin", "mute", "unmute", "give_role", "remove_role"],
    "Management": ["add_admin", "remove_admin", "show_admins", "addpookie", "removepookie", "listpookie", "set_log_channel", "disable_log_channel", "logs", "restart", "refresh"],
    "Cats": ["cat", "setcatchannel", "sethourlycatchannel"],
}


class ShowCommandsView(discord.ui.View):
    def __init__(self, user: discord.User):
        super().__init__(timeout=120)
        self.user = user

    @discord.ui.select(placeholder="Choose category", min_values=1, max_values=1,
                       options=[discord.SelectOption(label=k) for k in CATEGORIES.keys()])
    async def select_cb(self, interaction: discord.Interaction, select: discord.ui.Select):
        cat = select.values[0]
        items = CATEGORIES.get(cat, [])
        filtered = []
        for c in items:
            if c in ["add_admin", "remove_admin", "addpookie", "removepookie", "listpookie", "set_log_channel", "disable_log_channel", "logs", "restart", "refresh"]:
                if not is_admin_user(interaction.user.id):
                    continue
            filtered.append(c)
        text = f"**{cat}**\n" + (", ".join(f"`/{x}`" for x in filtered) if filtered else "No commands you can use here.")
        await interaction.response.edit_message(content=text, view=self)


@bot.hybrid_command(name="showcommands", with_app_command=True, description="Interactive list of commands you can use")
async def showcommands(ctx: commands.Context):
    view = ShowCommandsView(ctx.author)
    # For prefix invocations, reply in channel; for slash it's also valid
    if isinstance(ctx, discord.Interaction):
        await ctx.response.send_message("Choose a category:", view=view, ephemeral=True)
    else:
        await ctx.reply("Choose a category (check your DMs if ephemeral):", mention_author=False)
        try:
            await ctx.author.send("Choose a category:", view=view)
        except Exception:
            pass


# -------------------------
# FUN commands (8ball, jokes, coin, dice, rps)
# -------------------------
@bot.hybrid_command(name="8ball", with_app_command=True, description="Ask the magic 8-ball")
async def eightball(ctx: commands.Context, *, question: str):
    answers = ["Yes.", "No.", "Maybe.", "Absolutely!", "Ask again later.", "Definitely not.", "Probably.", "Unlikely."]
    await ctx.reply(f"🎱 {random.choice(answers)}", mention_author=False)


@bot.hybrid_command(name="joke", with_app_command=True, description="Tell a joke")
async def joke(ctx: commands.Context):
    jokes = ["I told my computer I needed a break — it went to sleep.", "Why do programmers prefer dark mode? Because light attracts bugs."]
    await ctx.reply(random.choice(jokes), mention_author=False)


@bot.hybrid_command(name="dadjoke", with_app_command=True, description="Tell a dad joke")
async def dadjoke(ctx: commands.Context):
    jokes = ["I used to play piano by ear — now I use my hands.", "Why don't eggs tell jokes? They'd crack each other up."]
    await ctx.reply(random.choice(jokes), mention_author=False)


@bot.hybrid_command(name="coinflip", with_app_command=True, description="Flip a coin")
async def coinflip(ctx: commands.Context):
    await ctx.reply("Heads" if random.random() < 0.5 else "Tails", mention_author=False)


@bot.hybrid_command(name="rolldice", with_app_command=True, description="Roll a dice")
async def rolldice(ctx: commands.Context):
    await ctx.reply(f"🎲 {random.randint(1,6)}", mention_author=False)


@bot.hybrid_command(name="rps", with_app_command=True, description="Rock Paper Scissors (rock/paper/scissors)")
async def rps(ctx: commands.Context, choice: str):
    choice = choice.lower().strip()
    if choice not in ("rock", "paper", "scissors"):
        return await ctx.reply("Choose rock/paper/scissors.", mention_author=False)
    bot_choice = random.choice(["rock", "paper", "scissors"])
    result = "draw"
    if (choice, bot_choice) in [("rock", "scissors"), ("paper", "rock"), ("scissors", "paper")]:
        result = "You win!"
    elif choice != bot_choice:
        result = "You lose!"
    await ctx.reply(f"You: **{choice}** | Bot: **{bot_choice}** → {result}", mention_author=False)


# -------------------------
# AVATAR / USERINFO / GUILDINFO
# -------------------------
@bot.hybrid_command(name="avatar", with_app_command=True, description="Show a user's avatar")
async def avatar(ctx: commands.Context, user: Optional[discord.User] = None):
    u = user or ctx.author
    emb = discord.Embed(title=f"{u}", color=discord.Color.green())
    emb.set_image(url=u.display_avatar.url)
    await ctx.reply(embed=emb, mention_author=False)


@bot.hybrid_command(name="userinfo", with_app_command=True, description="Show information about a user")
async def userinfo(ctx: commands.Context, user: Optional[discord.User] = None):
    u = user or ctx.author
    emb = discord.Embed(title=f"{u}", color=discord.Color.blurple())
    emb.add_field(name="ID", value=str(u.id))
    emb.add_field(name="Bot?", value=str(u.bot))
    emb.set_thumbnail(url=u.display_avatar.url)
    await ctx.reply(embed=emb, mention_author=False)


@bot.hybrid_command(name="guildinfo", with_app_command=True, description="Show guild info by id (admin)")
async def guildinfo(ctx: commands.Context, guild_id: str):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    try:
        gid = int(guild_id)
    except Exception:
        return await ctx.reply("Invalid guild id.", mention_author=False)
    g = bot.get_guild(gid)
    if not g:
        return await ctx.reply("Bot not in that guild.", mention_author=False)
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
    await ctx.reply(embed=emb, mention_author=False)


# -------------------------
# WARN SYSTEM
# -------------------------
@bot.hybrid_command(name="warn", with_app_command=True, description="Warn a user (admin/pookie)")
async def warn(ctx: commands.Context, user: discord.Member, *, reason: Optional[str] = "No reason"):
    if not (is_admin_user(ctx.author.id) or is_pookie_user(ctx.author.id)):
        return await ctx.reply("Admins/Pookie only.", mention_author=False)
    d = reload_data()
    d.setdefault("warns", {})
    lst = d["warns"].setdefault(str(user.id), [])
    entry = {"mod": ctx.author.id, "reason": reason, "time": datetime.now(timezone.utc).isoformat()}
    lst.append(entry)
    save_data(d)
    await ctx.reply(f"⚠️ Warned {user.mention} — {sanitize_no_mentions(reason)}", mention_author=False)
    await send_log_embed(ctx.guild, "Warn", f"{ctx.author.mention} warned {user.mention}: {sanitize_no_mentions(reason)}")
    add_log("warn", f"{ctx.author} warned {user} — {reason}")


@bot.hybrid_command(name="show_warns", with_app_command=True, description="Show warns for a user (admin)")
async def show_warns(ctx: commands.Context, user: discord.User):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    warns = d.get("warns", {}).get(str(user.id), [])
    if not warns:
        return await ctx.reply("No warns.", mention_author=False)
    text = "\n".join(f"- {w['time']} by <@{w['mod']}>: {w['reason']}" for w in warns[-20:])
    await ctx.reply(text[:1900], mention_author=False)


@bot.hybrid_command(name="remove_warn", with_app_command=True, description="Remove a warn by index (admin)")
async def remove_warn(ctx: commands.Context, user: discord.User, index: int = 0):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    arr = d.get("warns", {}).get(str(user.id), [])
    if not arr or index < 0 or index >= len(arr):
        return await ctx.reply("No such warn index.", mention_author=False)
    removed = arr.pop(index)
    save_data(d)
    await ctx.reply("✅ Removed warn.", mention_author=False)
    add_log("warn_remove", f"{ctx.author} removed warn for {user}: {removed}")


# -------------------------
# ROLES / MUTE / LOCK / TEMP ROLE
# -------------------------
@bot.hybrid_command(name="give_role", with_app_command=True, description="Give a role to a member (admin)")
async def give_role(ctx: commands.Context, member: discord.Member, role: discord.Role):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    try:
        await member.add_roles(role, reason=f"Given by {ctx.author}")
        await ctx.reply(f"✅ Given role {role.name} to {member.mention}", mention_author=False)
        add_log("role", f"{ctx.author} gave role {role.id} to {member.id}")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)


@bot.hybrid_command(name="remove_role", with_app_command=True, description="Remove a role from a member (admin)")
async def remove_role(ctx: commands.Context, member: discord.Member, role: discord.Role):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    try:
        await member.remove_roles(role, reason=f"Removed by {ctx.author}")
        await ctx.reply(f"✅ Removed role {role.name} from {member.mention}", mention_author=False)
        add_log("role", f"{ctx.author} removed role {role.id} from {member.id}")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)


@bot.hybrid_command(name="give_temp_role", with_app_command=True, description="Give a temporary role, e.g., 10m, 2h, 4d (admin)")
async def give_temp_role(ctx: commands.Context, member: discord.Member, role: discord.Role, duration: str):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    m = re.match(r"^(\d+)([smhd])$", duration.strip().lower())
    if not m:
        return await ctx.reply("Invalid duration. Use like 10m, 2h, 4d.", mention_author=False)
    num = int(m.group(1))
    unit = m.group(2)
    seconds = num * (1 if unit == "s" else 60 if unit == "m" else 3600 if unit == "h" else 86400)
    try:
        await member.add_roles(role, reason=f"Temp role by {ctx.author}")
        expire_dt = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
        d = reload_data()
        d.setdefault("temp_roles", [])
        d["temp_roles"].append({"guild_id": ctx.guild.id, "user_id": member.id, "role_id": role.id, "expires_at": expire_dt})
        save_data(d)
        await ctx.reply(f"✅ Given {role.name} to {member.mention} for {duration}", mention_author=False)
        add_log("temp_role", f"{ctx.author} gave {role.id} to {member.id} until {expire_dt}")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)


@bot.hybrid_command(name="lock_channel", with_app_command=True, description="Lock current channel (admin)")
async def lock_channel(ctx: commands.Context):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    ch = ctx.channel
    try:
        overwrite = ch.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = False
        await ch.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Locked by {ctx.author}")
        await ctx.reply("🔒 Channel locked.", mention_author=False)
        add_log("channel", f"{ctx.author} locked {ch}")
        await send_log_embed(ctx.guild, "Channel Locked", f"{ctx.author.mention} locked {ch.mention}")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)


@bot.hybrid_command(name="unlock_channel", with_app_command=True, description="Unlock current channel (admin)")
async def unlock_channel(ctx: commands.Context):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    ch = ctx.channel
    try:
        overwrite = ch.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = None
        await ch.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Unlocked by {ctx.author}")
        await ctx.reply("🔓 Channel unlocked.", mention_author=False)
        add_log("channel", f"{ctx.author} unlocked {ch}")
        await send_log_embed(ctx.guild, "Channel Unlocked", f"{ctx.author.mention} unlocked {ch.mention}")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)


# Mute/unmute helpers
async def ensure_muted_role(guild: discord.Guild) -> Optional[discord.Role]:
    role = discord.utils.get(guild.roles, name="Muted")
    if role:
        return role
    try:
        role = await guild.create_role(name="Muted", reason="Create Muted role for moderation")
        for ch in guild.text_channels:
            try:
                await ch.set_permissions(role, send_messages=False, add_reactions=False)
            except Exception:
                pass
        return role
    except Exception:
        return None


@bot.hybrid_command(name="mute", with_app_command=True, description="Mute a member (admin). Optionally specify duration like 10m")
async def mute(ctx: commands.Context, member: discord.Member, duration: Optional[str] = None):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    try:
        role = await ensure_muted_role(ctx.guild)
        if not role:
            return await ctx.reply("Failed to create Muted role.", mention_author=False)
        await member.add_roles(role, reason=f"Muted by {ctx.author}")
        if duration:
            m = re.match(r"^(\d+)([smhd])$", duration.strip().lower())
            if m:
                num = int(m.group(1)); unit = m.group(2)
                seconds = num * (1 if unit == "s" else 60 if unit == "m" else 3600 if unit == "h" else 86400)
                expire = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
                d = reload_data()
                d.setdefault("temp_roles", [])
                d["temp_roles"].append({"guild_id": ctx.guild.id, "user_id": member.id, "role_id": role.id, "expires_at": expire})
                save_data(d)
        await ctx.reply(f"🔇 Muted {member.mention}", mention_author=False)
        add_log("mute", f"{ctx.author} muted {member}")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)


@bot.hybrid_command(name="unmute", with_app_command=True, description="Unmute a member (admin)")
async def unmute(ctx: commands.Context, member: discord.Member):
    if not is_admin_user(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    try:
        role = discord.utils.get(ctx.guild.roles, name="Muted")
        if role:
            await member.remove_roles(role, reason=f"Unmuted by {ctx.author}")
            await ctx.reply(f"🔊 Unmuted {member.mention}", mention_author=False)
            add_log("unmute", f"{ctx.author} unmuted {member}")
        else:
            await ctx.reply("No Muted role found.", mention_author=False)
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)


# -------------------------
# SNIPE / ESNIPE commands
# -------------------------
@bot.hybrid_command(name="snipe", with_app_command=True, description="Show recent deleted messages (this channel)")
async def snipe_cmd(ctx: commands.Context):
    items = SNIPES.get(ctx.channel.id, [])
    if not items:
        return await ctx.reply("Nothing to snipe here.", mention_author=False)
    view = SnipeView(items)
    await ctx.reply(embed=view.make_embed(), view=view, mention_author=False)


@bot.hybrid_command(name="esnipe", with_app_command=True, description="Show recent edited messages (this channel)")
async def esnipe_cmd(ctx: commands.Context):
    items = ESNIPES.get(ctx.channel.id, [])
    if not items:
        return await ctx.reply("Nothing to esnipe here.", mention_author=False)
    view = SnipeView(items)
    await ctx.reply(embed=view.make_embed(), view=view, mention_author=False)


# -------------------------
# Final preparations & run
# -------------------------
# Ensure owner + default extras are in admins
_d = reload_data()
if OWNER_ID not in _d.get("admins", []):
    _d["admins"].append(int(OWNER_ID))
for aid in DEFAULT_EXTRA_ADMINS:
    if aid not in _d["admins"]:
        _d["admins"].append(int(aid))
save_data(_d)

# Bot start time used earlier
BOT_START = datetime.now(timezone.utc)


def run():
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        print("Bot run error:", e)
        raise


if __name__ == "__main__":
    run()
