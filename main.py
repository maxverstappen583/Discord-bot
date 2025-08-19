# main.py — full single-file bot
# Features: prefix ? + slash / hybrid commands, admin/pookie system, blacklist,
# blocked words, triggers, AFK, snipe/esnipe, logs (every event), daily/hourly cats,
# say/say_admin, moderation, warns, temp roles, restart via Render API, Flask keepalive.

import os
import sys
import json
import re
import random
import asyncio
import aiohttp
import traceback
from datetime import datetime, timezone, timedelta
from threading import Thread
from typing import Optional, Dict, Any, List
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from discord import app_commands
from flask import Flask

# ---------------------------
# ENV
# ---------------------------
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN environment variable is required")

# Owner and default admins
try:
    OWNER_ID = int(os.getenv("OWNER_ID", "1319292111325106296"))
except Exception:
    OWNER_ID = 1319292111325106296

# Additional default admins to keep always present
DEFAULT_EXTRA_ADMINS = {1380315427992768633, 909468887098216499}

CAT_API_KEY = os.getenv("CAT_API_KEY", "").strip()
RENDER_API_KEY = os.getenv("RENDER_API_KEY", "").strip()
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "").strip()

TZ_NAME = os.getenv("TZ", "Asia/Kolkata").strip() or "Asia/Kolkata"
try:
    BOT_TZ = ZoneInfo(TZ_NAME)
except Exception:
    BOT_TZ = ZoneInfo("Asia/Kolkata")

# Flask port (Render sets PORT)
FLASK_PORT = int(os.getenv("PORT", os.getenv("FLASK_PORT", "8080")))

DATA_FILE = "data.json"
SNIPES_KEEP = 150
LOGS_KEEP = 3000

# ---------------------------
# FLASK KEEPALIVE
# ---------------------------
flask_app = Flask("bot_keepalive")


@flask_app.route("/")
def alive():
    return "OK", 200


def _run_flask():
    flask_app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, use_reloader=False)


Thread(target=_run_flask, daemon=True).start()

# ---------------------------
# DATA PERSISTENCE
# ---------------------------
DEFAULT_DATA = {
    "admins": [],            # ints
    "pookies": [],           # ints
    "blacklist": [],         # ints
    "blocked_words": [],     # strings
    "triggers": {},          # word -> reply
    "log_channel": None,     # int
    "cat_channel": None,     # daily 11:00 local
    "hourly_cat_channel": None,
    "logs": [],              # list of dicts
    "afk": {},               # user_id -> {"reason","since"}
    "warns": {},             # user_id -> list
    "temp_roles": []         # list of dicts
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
        d["admins"] = list({OWNER_ID} | DEFAULT_EXTRA_ADMINS)
        save_data(d)
        return d
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = DEFAULT_DATA.copy()
    # ensure keys
    for k, v in DEFAULT_DATA.items():
        if k not in d:
            d[k] = v.copy() if isinstance(v, (list, dict)) else v
    # ensure owner present
    admins_set = set(map(int, d.get("admins", [])))
    admins_set.add(int(OWNER_ID))
    admins_set |= DEFAULT_EXTRA_ADMINS
    d["admins"] = list(admins_set)
    return d


DATA = load_data()


def reload_data():
    global DATA
    DATA = load_data()
    return DATA


# ---------------------------
# HELPERS
# ---------------------------
def is_owner(uid: int) -> bool:
    return int(uid) == int(OWNER_ID)


def is_admin(uid: int) -> bool:
    d = reload_data()
    return int(uid) in set(map(int, d.get("admins", []))) or is_owner(uid)


def is_pookie(uid: int) -> bool:
    d = reload_data()
    return int(uid) in set(map(int, d.get("pookies", []))) or is_admin(uid)


def is_blacklisted(uid: int) -> bool:
    d = reload_data()
    return int(uid) in set(map(int, d.get("blacklist", [])))


def sanitize_no_mentions(text: str) -> str:
    return text.replace("@", "@\u200b")


def exact_word_present(text: str, word: str) -> bool:
    pattern = r"\b" + re.escape(word) + r"\b"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def add_log(kind: str, message: str):
    d = reload_data()
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, "message": message}
    d.setdefault("logs", [])
    d["logs"].append(entry)
    if len(d["logs"]) > LOGS_KEEP:
        d["logs"] = d["logs"][-LOGS_KEEP:]
    save_data(d)


async def send_log_embed(title: str, description: str):
    d = reload_data()
    ch_id = d.get("log_channel")
    if not ch_id:
        return
    ch = bot.get_channel(int(ch_id))
    if not ch:
        return
    emb = discord.Embed(title=title, description=description, color=discord.Color.dark_gold(),
                        timestamp=datetime.now(timezone.utc))
    try:
        await ch.send(embed=emb)
    except Exception:
        pass


# ---------------------------
# SNIPE STORAGE
# ---------------------------
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
        d = self.items[self.index]
        e = discord.Embed(color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
        e.set_author(name=d.get("author_tag", "Unknown"), icon_url=d.get("avatar_url") or discord.Embed.Empty)
        e.set_footer(text=f"{self.index+1}/{len(self.items)} • {d.get('time','')}")
        if "content" in d:
            e.add_field(name="Message", value=d.get("content") or "*empty*", inline=False)
        else:
            e.add_field(name="Before", value=d.get("before") or "*empty*", inline=False)
            e.add_field(name="After", value=d.get("after") or "*empty*", inline=False)
        return e

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


# ---------------------------
# CAT helper
# ---------------------------
async def fetch_random_cat_url(session: aiohttp.ClientSession) -> Optional[str]:
    headers = {}
    if CAT_API_KEY:
        headers["x-api-key"] = CAT_API_KEY
    try:
        async with session.get("https://api.thecatapi.com/v1/images/search", headers=headers, timeout=20) as r:
            if r.status == 200:
                j = await r.json()
                if isinstance(j, list) and j:
                    return j[0].get("url")
    except Exception:
        return None
    return "https://cataas.com/cat"

# ---------------------------
# BOT SETUP
# ---------------------------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix=commands.when_mentioned_or("?"), intents=intents, help_command=None)
BOT_START = datetime.now(timezone.utc)

# scheduled housekeeping — defined but started in on_ready
_last_daily_iso: Optional[str] = None
_last_hourly_key: Optional[str] = None


@tasks.loop(minutes=1)
async def minute_loop():
    global _last_daily_iso, _last_hourly_key
    now_local = datetime.now(BOT_TZ)
    d = reload_data()
    # daily cat at 11:00 local
    try:
        if d.get("cat_channel") and now_local.hour == 11 and now_local.minute == 0:
            today_iso = now_local.date().isoformat()
            if _last_daily_iso != today_iso:
                ch = bot.get_channel(int(d["cat_channel"]))
                if ch:
                    try:
                        async with aiohttp.ClientSession() as s:
                            url = await fetch_random_cat_url(s)
                        if url:
                            await ch.send(url)
                            add_log("cat_daily", f"Sent daily cat to {ch.id}")
                            await send_log_embed("Daily Cat", f"Sent daily cat in {ch.mention}")
                    except Exception:
                        pass
                _last_daily_iso = today_iso
    except Exception:
        pass

    # hourly cat at minute == 0
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
                            await send_log_embed("Hourly Cat", f"Sent hourly cat in {ch2.mention}")
                    except Exception:
                        pass
                _last_hourly_key = key
    except Exception:
        pass

    # temp roles expiry
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


# ---------------------------
# RENDER API deploy trigger
# ---------------------------
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


# ---------------------------
# on_ready
# ---------------------------
@bot.event
async def on_ready():
    # streaming presence (purple). Edit the name/url as you like.
    try:
        activity = discord.Streaming(name="Max Verstappen", url="https://twitch.tv/yourchannel")
        await bot.change_presence(status=discord.Status.online, activity=activity)
    except Exception:
        try:
            await bot.change_presence(status=discord.Status.online)
        except Exception:
            pass

    # sync commands
    try:
        await bot.tree.sync()
    except Exception as e:
        print("Slash sync error:", e)

    # start minute loop after ready to avoid "no running loop"
    if not minute_loop.is_running():
        minute_loop.start()

    print(f"✅ Logged in as {bot.user} (id:{bot.user.id}) — guilds: {len(bot.guilds)}")
    add_log("system", f"Bot ready: {bot.user} in {len(bot.guilds)} guilds")
    await send_log_embed("System", f"Bot started and ready. {bot.user} in {len(bot.guilds)} guilds")


# ---------------------------
# Events for server activity (all sent to log channel if set)
# ---------------------------
@bot.event
async def on_message_delete(message: discord.Message):
    if not message or not message.author or message.author.bot:
        return
    push_snipe(SNIPES, message.channel.id, {
        "author_tag": str(message.author),
        "avatar_url": getattr(message.author.display_avatar, "url", ""),
        "content": message.content or "",
        "time": datetime.now(timezone.utc).isoformat()
    })
    add_log("delete", f"{message.author} deleted message in #{message.channel} — {message.content}")
    await send_log_embed("Message Deleted", f"**{message.author}** deleted message in {message.channel.mention}\n```{sanitize_no_mentions((message.content or '') )[:900]}```")


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if not before.author or before.author.bot:
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
    add_log("edit", f"{before.author} edited message in #{before.channel}")
    await send_log_embed("Message Edited", f"**{before.author}** edited a message in {before.channel.mention}\n**Before:**\n`{sanitize_no_mentions(before.content)[:800]}`\n**After:**\n`{sanitize_no_mentions(after.content)[:800]}`")


@bot.event
async def on_member_join(member: discord.Member):
    add_log("join", f"{member} joined {member.guild}")
    await send_log_embed("Member Joined", f"**{member}** joined **{member.guild.name}**")


@bot.event
async def on_member_remove(member: discord.Member):
    add_log("leave", f"{member} left {member.guild}")
    await send_log_embed("Member Left", f"**{member}** left **{member.guild.name}**")


@bot.event
async def on_guild_role_create(role: discord.Role):
    add_log("role_create", f"Role {role.name} created in {role.guild}")
    await send_log_embed("Role Created", f"Role **{role.name}** created in **{role.guild.name}**")


@bot.event
async def on_guild_role_delete(role: discord.Role):
    add_log("role_delete", f"Role {role.name} deleted in {role.guild}")
    await send_log_embed("Role Deleted", f"Role **{role.name}** deleted in **{role.guild.name}**")


@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    add_log("channel_create", f"Channel {channel.name} created in {channel.guild}")
    await send_log_embed("Channel Created", f"Channel **{channel.name}** created in **{channel.guild.name}**")


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    add_log("channel_delete", f"Channel {channel.name} deleted in {channel.guild}")
    await send_log_embed("Channel Deleted", f"Channel **{channel.name}** deleted in **{channel.guild.name}**")


# on_message (process triggers, blocked words, afk, etc)
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

    # Blocked words detection
    content_compact = re.sub(r"[\s\-\_\.]", "", message.content.lower())
    for w in d.get("blocked_words", []):
        wc = re.sub(r"[\s\-\_\.]", "", w.lower())
        if wc and wc in content_compact:
            try:
                await message.delete()
            except Exception:
                pass
            add_log("blocked_word", f"{message.author} used blocked word {w} in {message.channel}")
            await send_log_embed("Blocked Word", f"**{message.author}** used blocked word `{w}` in {message.channel.mention}")
            return

    # Triggers (exact)
    for word, reply in d.get("triggers", {}).items():
        if exact_word_present(message.content, word):
            out = reply.replace("{user}", message.author.mention)
            out = sanitize_no_mentions(out) if not is_admin(message.author.id) else out
            try:
                await message.channel.send(out)
            except Exception:
                pass
            break

    await bot.process_commands(message)


# ---------------------------
# COMMANDS: Hybrid where useful
# ---------------------------

# Uptime
@bot.hybrid_command(name="uptime", with_app_command=True, description="Show bot uptime")
async def uptime_cmd(ctx: commands.Context):
    delta = datetime.now(timezone.utc) - BOT_START
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    await ctx.reply(f"⏱ Uptime: {days}d {hours}h {minutes}m {seconds}s", mention_author=False)
    add_log("command", f"{ctx.author} used uptime")

# showcommands (interactive)
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
            admin_only = c in ["add_admin", "remove_admin", "addpookie", "removepookie", "listpookie", "set_log_channel", "disable_log_channel", "logs", "restart", "refresh"]
            if admin_only and not is_admin(interaction.user.id):
                continue
            filtered.append(c)
        text = f"**{cat}**\n" + (", ".join(f"`/{x}`" for x in filtered) if filtered else "No commands you can use here.")
        await interaction.response.edit_message(content=text, view=self)


@bot.hybrid_command(name="showcommands", with_app_command=True, description="Show commands you can use")
async def showcommands_cmd(ctx: commands.Context):
    view = ShowCommandsView(ctx.author)
    if isinstance(ctx, discord.Interaction):
        await ctx.response.send_message("Pick a category:", view=view, ephemeral=True)
    else:
        await ctx.reply("Pick a category (sent to your DMs too):", mention_author=False)
        try:
            await ctx.author.send("Pick a category:", view=view)
        except Exception:
            pass
    add_log("command", f"{ctx.author} used showcommands")

# Ask for command
@bot.hybrid_command(name="askforcommand", with_app_command=True, description="Ask owner for a command (pings owner + DMs owner)")
async def askfor_cmd(ctx: commands.Context, *, request: str):
    try:
        owner = await bot.fetch_user(OWNER_ID)
        try:
            await owner.send(f"📩 Request from {ctx.author} ({ctx.author.id}) in {ctx.guild.name if ctx.guild else 'DM'}:\n{request[:1500]}")
        except Exception:
            pass
    except Exception:
        pass
    d = reload_data()
    if d.get("log_channel"):
        ch = bot.get_channel(int(d["log_channel"]))
        if ch:
            try:
                await ch.send(f"<@{OWNER_ID}> 📨 Request from {ctx.author.mention} in {ctx.guild.name if ctx.guild else 'DM'}:\n```{request[:1500]}```")
            except Exception:
                pass
    await ctx.reply("✅ Sent your request to the owner (and pinged log channel if configured).", mention_author=False)
    add_log("ask", f"{ctx.author} asked: {request[:200]}")

# refresh (sync)
@bot.hybrid_command(name="refresh", with_app_command=True, description="Refresh slash commands (admin)")
async def refresh_cmd(ctx: commands.Context):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    try:
        await bot.tree.sync()
        await ctx.reply("✅ Slash commands refreshed.", mention_author=False)
        add_log("admin", f"{ctx.author} refreshed slash commands")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)

# restart (admin)
@bot.hybrid_command(name="restart", with_app_command=True, description="Restart bot (admin). Uses Render API if configured.")
async def restart_cmd(ctx: commands.Context):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    if RENDER_API_KEY and RENDER_SERVICE_ID:
        await ctx.reply("🔁 Triggering Render deploy...", mention_author=False)
        res = await trigger_render_deploy(RENDER_API_KEY, RENDER_SERVICE_ID)
        await ctx.reply(f"Result: {res}", mention_author=False)
        add_log("admin", f"{ctx.author} triggered render deploy")
    else:
        await ctx.reply("🔁 Restarting process (no Render API configured)...", mention_author=False)
        add_log("admin", f"{ctx.author} requested restart")
        asyncio.create_task(_shutdown_exit())

async def _shutdown_exit(delay: float = 0.5):
    await asyncio.sleep(delay)
    try:
        await bot.close()
    finally:
        os._exit(0)

# debug (admin)
@bot.hybrid_command(name="debug", with_app_command=True, description="Show debug info (admin)")
async def debug_cmd(ctx: commands.Context):
    if not is_admin(ctx.author.id):
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

# eval (owner only)
@bot.hybrid_command(name="eval", with_app_command=True, description="Eval python (owner only)")
async def eval_cmd(ctx: commands.Context, *, code: str):
    if not is_owner(ctx.author.id):
        return await ctx.reply("Owner only.", mention_author=False)
    env = {"bot": bot, "discord": discord, "commands": commands, "asyncio": asyncio, "os": os, "sys": sys}
    try:
        result = None
        try:
            result = eval(code, env)
            if asyncio.iscoroutine(result):
                result = await result
        except SyntaxError:
            exec(compile(code, "<eval>", "exec"), env)
            result = "Executed."
        await ctx.reply(f"Result:\n```\n{str(result)[:1900]}\n```", mention_author=False)
    except Exception:
        tb = traceback.format_exc()
        await ctx.reply(f"Error:\n```\n{tb[-1900:]}\n```", mention_author=False)
    add_log("owner", f"{ctx.author} used eval")

# ---------------------------
# Admin & Pookie management
# ---------------------------
@bot.hybrid_command(name="add_admin", with_app_command=True, description="Add admin (owner only)")
async def add_admin_cmd(ctx: commands.Context, user: discord.User):
    if not is_owner(ctx.author.id):
        return await ctx.reply("Owner only.", mention_author=False)
    d = reload_data()
    if int(user.id) not in d["admins"]:
        d["admins"].append(int(user.id))
        save_data(d)
    await ctx.reply(f"✅ {user.mention} added as admin.", mention_author=False)
    add_log("admin", f"{ctx.author} added admin {user}")

@bot.hybrid_command(name="remove_admin", with_app_command=True, description="Remove admin (owner only)")
async def remove_admin_cmd(ctx: commands.Context, user: discord.User):
    if not is_owner(ctx.author.id):
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
async def show_admins_cmd(ctx: commands.Context):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    mentions = [f"<@{uid}>" for uid in d.get("admins", [])]
    await ctx.reply("Admins:\n" + ("\n".join(mentions) or "None"), mention_author=False)
    add_log("admin", f"{ctx.author} listed admins")

@bot.hybrid_command(name="addpookie", with_app_command=True, description="Add pookie (admin)")
async def addpookie_cmd(ctx: commands.Context, user: discord.User):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    if int(user.id) not in d["pookies"]:
        d["pookies"].append(int(user.id))
        save_data(d)
    await ctx.reply(f"✅ {user.mention} added as pookie.", mention_author=False)
    add_log("pookie", f"{ctx.author} added pookie {user}")

@bot.hybrid_command(name="removepookie", with_app_command=True, description="Remove pookie (admin)")
async def removepookie_cmd(ctx: commands.Context, user: discord.User):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    if int(user.id) in d["pookies"]:
        d["pookies"].remove(int(user.id))
        save_data(d)
        return await ctx.reply(f"✅ {user.mention} removed from pookie.", mention_author=False)
    await ctx.reply("User not a pookie.", mention_author=False)

@bot.hybrid_command(name="listpookie", with_app_command=True, description="List pookies (admin)")
async def listpookie_cmd(ctx: commands.Context):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    mentions = [f"<@{uid}>" for uid in d.get("pookies", [])]
    await ctx.reply("Pookies:\n" + ("\n".join(mentions) or "None"), mention_author=False)
    add_log("pookie", f"{ctx.author} listed pookies")

# blacklist
@bot.hybrid_command(name="blacklist_add", with_app_command=True, description="Add user to blacklist (admin)")
async def blacklist_add_cmd(ctx: commands.Context, user: discord.User):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    if int(user.id) not in d["blacklist"]:
        d["blacklist"].append(int(user.id))
        save_data(d)
    await ctx.reply(f"✅ {user.mention} blacklisted.", mention_author=False)
    add_log("admin", f"{ctx.author} blacklisted {user}")

@bot.hybrid_command(name="blacklist_remove", with_app_command=True, description="Remove user from blacklist (admin)")
async def blacklist_remove_cmd(ctx: commands.Context, user: discord.User):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    if int(user.id) in d["blacklist"]:
        d["blacklist"].remove(int(user.id))
        save_data(d)
        return await ctx.reply(f"✅ {user.mention} removed from blacklist.", mention_author=False)
    await ctx.reply("User not blacklisted.", mention_author=False)

# ---------------------------
# Blocked words & triggers
# ---------------------------
@bot.hybrid_command(name="blocked_add", with_app_command=True, description="Add blocked word (admin)")
async def blocked_add_cmd(ctx: commands.Context, *, word: str):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    w = word.strip().lower()
    if w and w not in d["blocked_words"]:
        d["blocked_words"].append(w)
        save_data(d)
    await ctx.reply(f"✅ Blocked word `{w}` added.", mention_author=False)
    add_log("admin", f"{ctx.author} added blocked word {w}")

@bot.hybrid_command(name="blocked_remove", with_app_command=True, description="Remove blocked word (admin)")
async def blocked_remove_cmd(ctx: commands.Context, *, word: str):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    w = word.strip().lower()
    if w in d["blocked_words"]:
        d["blocked_words"].remove(w)
        save_data(d)
        return await ctx.reply(f"✅ Blocked word `{w}` removed.", mention_author=False)
    await ctx.reply("Word not found.", mention_author=False)

@bot.hybrid_command(name="blocked_list", with_app_command=True, description="List blocked words (admin)")
async def blocked_list_cmd(ctx: commands.Context):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    await ctx.reply("\n".join(f"`{w}`" for w in d.get("blocked_words", [])[:200]) or "No blocked words.", mention_author=False)

# triggers
@bot.hybrid_command(name="trigger_add", with_app_command=True, description="Add exact-word trigger (admin). Use {user} to mention author.")
async def trigger_add_cmd(ctx: commands.Context, word: str, *, reply: str):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    d.setdefault("triggers", {})
    d["triggers"][word.lower()] = reply
    save_data(d)
    await ctx.reply(f"✅ Trigger added: `{word}` → `{reply}`", mention_author=False)
    add_log("admin", f"{ctx.author} added trigger {word}")

@bot.hybrid_command(name="trigger_remove", with_app_command=True, description="Remove trigger (admin)")
async def trigger_remove_cmd(ctx: commands.Context, word: str):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    if word.lower() in d.get("triggers", {}):
        d["triggers"].pop(word.lower(), None)
        save_data(d)
        return await ctx.reply(f"✅ Removed trigger `{word}`", mention_author=False)
    await ctx.reply("Trigger not found.", mention_author=False)

@bot.hybrid_command(name="showtrigger", with_app_command=True, description="Show triggers (admin)")
async def showtrigger_cmd(ctx: commands.Context):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    t = d.get("triggers", {})
    if not t:
        return await ctx.reply("No triggers set.", mention_author=False)
    text = "\n".join(f"`{k}` → `{v[:300]}`" for k, v in t.items())
    await ctx.reply(text[:1900], mention_author=False)

# ---------------------------
# AFK
# ---------------------------
@bot.hybrid_command(name="afk", with_app_command=True, description="Set AFK with optional reason")
async def afk_cmd(ctx: commands.Context, *, reason: Optional[str] = "AFK"):
    d = reload_data()
    d.setdefault("afk", {})
    d["afk"][str(ctx.author.id)] = {"reason": reason or "AFK", "since": datetime.now(timezone.utc).isoformat()}
    save_data(d)
    await ctx.reply(f"✅ AFK set: **{sanitize_no_mentions(reason or 'AFK')}**", mention_author=False)
    add_log("afk", f"{ctx.author} set AFK: {reason}")

@bot.hybrid_command(name="afk_clear", with_app_command=True, description="Clear AFK")
async def afk_clear_cmd(ctx: commands.Context):
    d = reload_data()
    if str(ctx.author.id) in d.get("afk", {}):
        d["afk"].pop(str(ctx.author.id), None)
        save_data(d)
        return await ctx.reply("✅ AFK removed.", mention_author=False)
    await ctx.reply("ℹ️ You were not AFK.", mention_author=False)

# ---------------------------
# Cat settings and command
# ---------------------------
@bot.hybrid_command(name="setcatchannel", with_app_command=True, description="Set daily cat channel at 11:00 local (admin)")
async def setcatchannel_cmd(ctx: commands.Context, channel: discord.TextChannel):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    d["cat_channel"] = int(channel.id)
    save_data(d)
    await ctx.reply(f"✅ Daily cat channel set to {channel.mention}.", mention_author=False)
    add_log("admin", f"{ctx.author} set daily cat channel {channel.id}")

@bot.hybrid_command(name="sethourlycatchannel", with_app_command=True, description="Set hourly cat channel (admin)")
async def sethourlycatchannel_cmd(ctx: commands.Context, channel: discord.TextChannel):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    d["hourly_cat_channel"] = int(channel.id)
    save_data(d)
    await ctx.reply(f"✅ Hourly cat channel set to {channel.mention}.", mention_author=False)
    add_log("admin", f"{ctx.author} set hourly cat channel {channel.id}")

@bot.hybrid_command(name="cat", with_app_command=True, description="Get a random cat image")
async def cat_cmd(ctx: commands.Context):
    if is_blacklisted(ctx.author.id):
        return await ctx.reply("You are blacklisted.", mention_author=False)
    await ctx.defer() if hasattr(ctx, "defer") else None
    async with aiohttp.ClientSession() as s:
        url = await fetch_random_cat_url(s)
    if not url:
        return await ctx.reply("⚠️ Couldn't fetch a cat.", mention_author=False)
    await ctx.reply(url, mention_author=False)
    add_log("cat", f"{ctx.author} requested cat")

# ---------------------------
# Say commands
# ---------------------------
@bot.hybrid_command(name="say", with_app_command=True, description="Bot repeats text (no pings)")
async def say_cmd(ctx: commands.Context, *, text: str):
    safe = sanitize_no_mentions(text)
    try:
        await ctx.reply("✅ Sent (no pings).", mention_author=False)
    except Exception:
        pass
    try:
        await ctx.send(safe, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        await ctx.send(safe)
    add_log("command", f"{ctx.author} used say")

@bot.hybrid_command(name="say_admin", with_app_command=True, description="Admin say (pings allowed)")
async def say_admin_cmd(ctx: commands.Context, *, text: str):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    await ctx.reply("✅ Sent.", mention_author=False)
    await ctx.send(text)
    add_log("admin", f"{ctx.author} used say_admin")

# ---------------------------
# Moderation commands (ban/kick/purge)
# ---------------------------
# Slash ban
@bot.tree.command(name="ban", description="Ban a member (admin)")
@app_commands.describe(member="Member to ban", reason="Reason")
async def slash_ban(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = "No reason"):
    if not is_admin(interaction.user.id):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    try:
        await member.ban(reason=reason)
        await interaction.response.send_message(f"🔨 {member.mention} banned. Reason: {sanitize_no_mentions(reason)}")
        add_log("mod", f"{interaction.user} banned {member} — {reason}")
        await send_log_embed("Ban", f"{interaction.user.mention} banned {member.mention} — {sanitize_no_mentions(reason)}")
    except Exception as e:
        await interaction.response.send_message(f"Failed: {e}", ephemeral=True)

# Prefix ban
@bot.command(name="ban")
async def prefix_ban(ctx: commands.Context, target: str, *, reason: Optional[str] = "No reason"):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    uid = None
    m = re.match(r"<@!?(\d+)>", target)
    if m:
        uid = int(m.group(1))
    else:
        try:
            uid = int(target)
        except Exception:
            return await ctx.reply("Provide mention or id.", mention_author=False)
    member = ctx.guild.get_member(uid) if ctx.guild else None
    try:
        if member:
            await member.ban(reason=reason)
            await ctx.reply(f"🔨 {member.mention} banned. Reason: {sanitize_no_mentions(reason)}", mention_author=False)
        else:
            await ctx.guild.ban(discord.Object(id=uid), reason=reason)
            await ctx.reply(f"🔨 Banned ID {uid}.", mention_author=False)
        add_log("mod", f"{ctx.author} banned {target} — {reason}")
        await send_log_embed("Ban", f"{ctx.author.mention} banned {target} — {sanitize_no_mentions(reason)}")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)

# Slash kick
@bot.tree.command(name="kick", description="Kick a member (admin)")
@app_commands.describe(member="Member to kick", reason="Reason")
async def slash_kick(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = "No reason"):
    if not is_admin(interaction.user.id):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(f"👢 {member.mention} kicked.")
        add_log("mod", f"{interaction.user} kicked {member} — {reason}")
        await send_log_embed("Kick", f"{interaction.user.mention} kicked {member.mention} — {sanitize_no_mentions(reason)}")
    except Exception as e:
        await interaction.response.send_message(f"Failed: {e}", ephemeral=True)

# Prefix kick
@bot.command(name="kick")
async def prefix_kick(ctx: commands.Context, target: str, *, reason: Optional[str] = "No reason"):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
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
        add_log("mod", f"{ctx.author} kicked {member} — {reason}")
        await send_log_embed("Kick", f"{ctx.author.mention} kicked {member.mention} — {sanitize_no_mentions(reason)}")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)

# Purge
@bot.hybrid_command(name="purge", with_app_command=True, description="Delete up to 100 messages (admin/pookie)")
async def purge_cmd(ctx: commands.Context, amount: Optional[int] = 10):
    if not (is_admin(ctx.author.id) or is_pookie(ctx.author.id)):
        return await ctx.reply("Admins/Pookie only.", mention_author=False)
    amount = max(1, min(100, amount or 10))
    try:
        deleted = await ctx.channel.purge(limit=amount)
        m = await ctx.send(f"🧹 Deleted {len(deleted)} messages.")
        await asyncio.sleep(3)
        await m.delete()
        add_log("mod", f"{ctx.author} purged {len(deleted)} messages in {ctx.channel}")
        await send_log_embed("Purge", f"{ctx.author.mention} purged {len(deleted)} messages in {ctx.channel.mention}")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)

# ---------------------------
# Warn system
# ---------------------------
@bot.hybrid_command(name="warn", with_app_command=True, description="Warn a user (admin/pookie)")
async def warn_cmd(ctx: commands.Context, user: discord.Member, *, reason: Optional[str] = "No reason"):
    if not (is_admin(ctx.author.id) or is_pookie(ctx.author.id)):
        return await ctx.reply("Admins/Pookie only.", mention_author=False)
    d = reload_data()
    d.setdefault("warns", {})
    lst = d["warns"].setdefault(str(user.id), [])
    entry = {"mod": ctx.author.id, "reason": reason, "time": datetime.now(timezone.utc).isoformat()}
    lst.append(entry)
    save_data(d)
    await ctx.reply(f"⚠️ Warned {user.mention} — {sanitize_no_mentions(reason)}", mention_author=False)
    add_log("warn", f"{ctx.author} warned {user} — {reason}")
    await send_log_embed("Warn", f"{ctx.author.mention} warned {user.mention}: {sanitize_no_mentions(reason)}")

@bot.hybrid_command(name="show_warns", with_app_command=True, description="Show warns for a user (admin)")
async def show_warns_cmd(ctx: commands.Context, user: discord.User):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    warns = d.get("warns", {}).get(str(user.id), [])
    if not warns:
        return await ctx.reply("No warns.", mention_author=False)
    text = "\n".join(f"- {w['time']} by <@{w['mod']}>: {w['reason']}" for w in warns[-20:])
    await ctx.reply(text[:1900], mention_author=False)

@bot.hybrid_command(name="remove_warn", with_app_command=True, description="Remove a warn (admin)")
async def remove_warn_cmd(ctx: commands.Context, user: discord.User, index: int = 0):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    arr = d.get("warns", {}).get(str(user.id), [])
    if not arr or index < 0 or index >= len(arr):
        return await ctx.reply("No such warn index.", mention_author=False)
    removed = arr.pop(index)
    save_data(d)
    await ctx.reply("✅ Removed warn.", mention_author=False)
    add_log("warn_remove", f"{ctx.author} removed warn for {user}: {removed}")

# ---------------------------
# Roles (give/remove/temp), mute/unmute, lock/unlock channel
# ---------------------------
@bot.hybrid_command(name="give_role", with_app_command=True, description="Give a role to a member (admin)")
async def give_role_cmd(ctx: commands.Context, member: discord.Member, role: discord.Role):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    try:
        await member.add_roles(role, reason=f"Given by {ctx.author}")
        await ctx.reply(f"✅ Given role {role.name} to {member.mention}", mention_author=False)
        add_log("role", f"{ctx.author} gave role {role.id} to {member.id}")
        await send_log_embed("Role Given", f"{ctx.author.mention} gave role {role.name} to {member.mention}")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)

@bot.hybrid_command(name="remove_role", with_app_command=True, description="Remove a role from a member (admin)")
async def remove_role_cmd(ctx: commands.Context, member: discord.Member, role: discord.Role):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    try:
        await member.remove_roles(role, reason=f"Removed by {ctx.author}")
        await ctx.reply(f"✅ Removed role {role.name} from {member.mention}", mention_author=False)
        add_log("role", f"{ctx.author} removed role {role.id} from {member.id}")
        await send_log_embed("Role Removed", f"{ctx.author.mention} removed role {role.name} from {member.mention}")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)

@bot.hybrid_command(name="give_temp_role", with_app_command=True, description="Give a temporary role (e.g., 10m) (admin)")
async def give_temp_role_cmd(ctx: commands.Context, member: discord.Member, role: discord.Role, duration: str):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    m = re.match(r"^(\d+)([smhd])$", duration.strip().lower())
    if not m:
        return await ctx.reply("Invalid duration (10m, 2h, 4d).", mention_author=False)
    num = int(m.group(1)); unit = m.group(2)
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
        await send_log_embed("Temp Role", f"{ctx.author.mention} gave {role.name} to {member.mention} until {expire_dt}")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)

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
async def mute_cmd(ctx: commands.Context, member: discord.Member, duration: Optional[str] = None):
    if not is_admin(ctx.author.id):
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
        await send_log_embed("Mute", f"{ctx.author.mention} muted {member.mention}")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)

@bot.hybrid_command(name="unmute", with_app_command=True, description="Unmute a member (admin)")
async def unmute_cmd(ctx: commands.Context, member: discord.Member):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    try:
        role = discord.utils.get(ctx.guild.roles, name="Muted")
        if role:
            await member.remove_roles(role, reason=f"Unmuted by {ctx.author}")
            await ctx.reply(f"🔊 Unmuted {member.mention}", mention_author=False)
            add_log("unmute", f"{ctx.author} unmuted {member}")
            await send_log_embed("Unmute", f"{ctx.author.mention} unmuted {member.mention}")
        else:
            await ctx.reply("No Muted role found.", mention_author=False)
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)

@bot.hybrid_command(name="lock_channel", with_app_command=True, description="Lock current channel (admin)")
async def lock_channel_cmd(ctx: commands.Context):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    ch = ctx.channel
    try:
        overwrite = ch.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = False
        await ch.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Locked by {ctx.author}")
        await ctx.reply("🔒 Channel locked.", mention_author=False)
        add_log("channel", f"{ctx.author} locked {ch}")
        await send_log_embed("Channel Locked", f"{ctx.author.mention} locked {ch.mention}")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)

@bot.hybrid_command(name="unlock_channel", with_app_command=True, description="Unlock current channel (admin)")
async def unlock_channel_cmd(ctx: commands.Context):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    ch = ctx.channel
    try:
        overwrite = ch.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = None
        await ch.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Unlocked by {ctx.author}")
        await ctx.reply("🔓 Channel unlocked.", mention_author=False)
        add_log("channel", f"{ctx.author} unlocked {ch}")
        await send_log_embed("Channel Unlocked", f"{ctx.author.mention} unlocked {ch.mention}")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)

# ---------------------------
# Snipe / Esnipe commands
# ---------------------------
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

# ---------------------------
# Avatar / Userinfo / Guildinfo
# ---------------------------
@bot.hybrid_command(name="avatar", with_app_command=True, description="Show a user's avatar")
async def avatar_cmd(ctx: commands.Context, user: Optional[discord.User] = None):
    u = user or ctx.author
    emb = discord.Embed(title=f"{u}", color=discord.Color.green())
    emb.set_image(url=u.display_avatar.url)
    await ctx.reply(embed=emb, mention_author=False)
    add_log("command", f"{ctx.author} used avatar")

@bot.hybrid_command(name="userinfo", with_app_command=True, description="Show user info")
async def userinfo_cmd(ctx: commands.Context, user: Optional[discord.User] = None):
    u = user or ctx.author
    emb = discord.Embed(title=f"{u}", color=discord.Color.blurple())
    emb.add_field(name="ID", value=str(u.id))
    emb.add_field(name="Bot?", value=str(u.bot))
    emb.set_thumbnail(url=u.display_avatar.url)
    await ctx.reply(embed=emb, mention_author=False)
    add_log("command", f"{ctx.author} used userinfo")

@bot.hybrid_command(name="guildinfo", with_app_command=True, description="Show guild info (admin). Provide guild id")
async def guildinfo_cmd(ctx: commands.Context, guild_id: str):
    if not is_admin(ctx.author.id):
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
    add_log("command", f"{ctx.author} used guildinfo {gid}")

# ---------------------------
# Log channel management (must send embeds for everything)
# ---------------------------
@bot.hybrid_command(name="set_log_channel", with_app_command=True, description="Set channel for logs (admin)")
async def set_log_channel_cmd(ctx: commands.Context, channel: discord.TextChannel):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    d["log_channel"] = int(channel.id)
    save_data(d)
    await ctx.reply(f"✅ Log channel set to {channel.mention}", mention_author=False)
    add_log("admin", f"{ctx.author} set log channel {channel.id}")
    # send test embed
    await send_log_embed("Logs", f"Log channel set to {channel.mention} by {ctx.author.mention}")

@bot.hybrid_command(name="disable_log_channel", with_app_command=True, description="Disable log channel (admin)")
async def disable_log_channel_cmd(ctx: commands.Context):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    d["log_channel"] = None
    save_data(d)
    await ctx.reply("✅ Log channel disabled.", mention_author=False)
    add_log("admin", f"{ctx.author} disabled log channel")

@bot.hybrid_command(name="logs", with_app_command=True, description="Show recent logs (admin)")
async def logs_cmd(ctx: commands.Context, count: Optional[int] = 10):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    entries = d.get("logs", [])[-min(max(1, count or 10), 100):]
    text = "\n".join(f"`{e['ts']}` {e['kind']}: {e['message']}" for e in entries) or "No logs."
    await ctx.reply(text[:1900], mention_author=False)

# ---------------------------
# Fun commands (8ball, jokes, dadjoke, coin, dice, rps)
# ---------------------------
@bot.hybrid_command(name="8ball", with_app_command=True, description="Ask the magic 8-ball")
async def eightball_cmd(ctx: commands.Context, *, question: str):
    answers = ["Yes.", "No.", "Maybe.", "Absolutely!", "Ask again later.", "Definitely not.", "Probably.", "Unlikely."]
    await ctx.reply(f"🎱 {random.choice(answers)}", mention_author=False)

@bot.hybrid_command(name="joke", with_app_command=True, description="Tell a joke")
async def joke_cmd(ctx: commands.Context):
    jokes = ["I told my computer I needed a break — it went to sleep.", "Why do programmers prefer dark mode? Because light attracts bugs."]
    await ctx.reply(random.choice(jokes), mention_author=False)

@bot.hybrid_command(name="dadjoke", with_app_command=True, description="Tell a dad joke")
async def dadjoke_cmd(ctx: commands.Context):
    jokes = ["I used to play piano by ear — now I use my hands.", "Why don't eggs tell jokes? They'd crack each other up."]
    await ctx.reply(random.choice(jokes), mention_author=False)

@bot.hybrid_command(name="coinflip", with_app_command=True, description="Flip a coin")
async def coinflip_cmd(ctx: commands.Context):
    await ctx.reply("Heads" if random.random() < 0.5 else "Tails", mention_author=False)

@bot.hybrid_command(name="rolldice", with_app_command=True, description="Roll a dice")
async def rolldice_cmd(ctx: commands.Context):
    await ctx.reply(f"🎲 {random.randint(1,6)}", mention_author=False)

@bot.hybrid_command(name="rps", with_app_command=True, description="Rock Paper Scissors")
async def rps_cmd(ctx: commands.Context, choice: str):
    choice = choice.lower().strip()
    if choice not in ("rock", "paper", "scissors"):
        return await ctx.reply("Choose rock/paper/scissors.", mention_author=False)
    bot_choice = random.choice(["rock", "paper", "scissors"])
    result = "Draw"
    if (choice, bot_choice) in [("rock", "scissors"), ("paper", "rock"), ("scissors", "paper")]:
        result = "You win!"
    elif choice != bot_choice:
        result = "You lose!"
    await ctx.reply(f"You: **{choice}** | Bot: **{bot_choice}** → {result}", mention_author=False)

# ---------------------------
# Final initialization
# ---------------------------
# ensure owner + extras in admins
_d = reload_data()
if OWNER_ID not in _d.get("admins", []):
    _d["admins"].append(int(OWNER_ID))
for aid in DEFAULT_EXTRA_ADMINS:
    if aid not in _d["admins"]:
        _d["admins"].append(int(aid))
save_data(_d)

BOT_START = datetime.now(timezone.utc)

def run():
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    run()
