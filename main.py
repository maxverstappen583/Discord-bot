# main.py
# Monolithic Discord bot with many features (one-file)
# - Prefix: "?" (hybrid commands also available as slash commands)
# - Flask keep-alive for Render / UptimeRobot
# - Owner/Admin/Pookie hierarchy
# - AFK, warns (permanent), blacklist, blocked words
# - Role management: give/remove/temprole
# - Mute/unmute & create Muted role
# - Lock/unlock channel
# - Ban/kick, purge
# - Cat command + daily 11:00 TZ + hourly posting
# - Snipe / E-snipe with ⬅️ / ➡️ navigation buttons
# - Triggers (exact-word auto-response, supports {user})
# - Logs, set_log_channel, view logs
# - Restart (Render API), refresh, eval, debug, showcommands
# - JSON persistence

import os, json, asyncio, traceback, aiohttp, re, platform, psutil, time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any

import discord
from discord.ext import commands, tasks
from discord import app_commands

from flask import Flask
from threading import Thread

# ---------------------------
# ENV
# ---------------------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN not set")

OWNER_ID = int(os.getenv("OWNER_ID", "1319292111325106296"))
RENDER_API_KEY = os.getenv("RENDER_API_KEY", "").strip()
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "").strip()
CAT_API_KEY = os.getenv("CAT_API_KEY", "").strip()
TZ_NAME = os.getenv("TZ", "Asia/Kolkata")

# ---------------------------
# GLOBALS
# ---------------------------
LOCAL_TZ = ZoneInfo(TZ_NAME)
START_TS = time.time()

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="?", intents=intents, help_command=None)
# ensure bot has message_content intent enabled in dev portal & hosting
bot.start_time = datetime.now(timezone.utc)

DATA_FILE = "data.json"
DEFAULT_DATA = {
    "admins": [],          # list of ints
    "pookies": [],         # list of ints
    "blacklist": [],       # list of ints
    "blocked_words": [],   # list of str (exact-word)
    "warns": {},           # user_id (str) -> list of warn objects
    "logs": [],            # recent logs (dict)
    "log_channel": None,   # int or None
    "triggers": {},        # exact-word -> reply (str)
    "daily_cat_channel": None,
    "hourly_cat": {"enabled": False, "channel": None, "interval_hours": 1, "last_sent": 0},
    "snipes": {},          # channel_id (str) -> list of deleted messages
    "esnipes": {},         # channel_id -> list of edits
    "afk": {}              # user_id (str) -> {"reason": str, "since": ts}
}

# ---------------------------
# STORAGE
# ---------------------------
def load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w") as f:
            json.dump(DEFAULT_DATA, f, indent=2)
        return DEFAULT_DATA.copy()
    with open(DATA_FILE, "r") as f:
        try:
            d = json.load(f)
        except Exception:
            d = DEFAULT_DATA.copy()
    # ensure keys present
    changed = False
    for k, v in DEFAULT_DATA.items():
        if k not in d:
            d[k] = v
            changed = True
    if changed:
        save_data(d)
    return d

def save_data(d: Optional[Dict[str, Any]] = None):
    global DATA
    if d is None:
        d = DATA
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, indent=2, default=str)

DATA = load_data()

# ---------------------------
# FLASK keep-alive
# ---------------------------
flask_app = Flask("bot_keepalive")

@flask_app.route("/")
def flask_index():
    return "Bot is running", 200

def run_flask():
    port = int(os.getenv("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port)

Thread(target=run_flask, daemon=True).start()

# ---------------------------
# UTILITIES
# ---------------------------
def is_owner(user: discord.abc.User) -> bool:
    return user.id == OWNER_ID

def is_admin_or_pookie(user: discord.abc.User) -> bool:
    return user.id == OWNER_ID or user.id in DATA.get("admins", []) or user.id in DATA.get("pookies", [])

def user_blacklisted(user: discord.abc.User) -> bool:
    return user.id in DATA.get("blacklist", [])

def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone(LOCAL_TZ)

def log_command(user: discord.abc.User, command_name: str, channel: Optional[discord.abc.Messageable]):
    entry = {
        "time": now_local().isoformat(timespec="seconds"),
        "user": f"{user} ({user.id})",
        "command": command_name,
        "channel": getattr(channel, "name", str(channel) if channel else "DM")
    }
    DATA["logs"].append(entry)
    DATA["logs"] = DATA["logs"][-1000:]
    save_data(DATA)
    # send to log channel if set
    ch_id = DATA.get("log_channel")
    if ch_id:
        try:
            ch = bot.get_channel(int(ch_id))
            if ch:
                embed = discord.Embed(title="Command Log", color=discord.Color.green(), timestamp=datetime.utcnow())
                embed.add_field(name="User", value=entry["user"], inline=False)
                embed.add_field(name="Command", value=entry["command"], inline=False)
                embed.add_field(name="Channel", value=entry["channel"], inline=False)
                asyncio.create_task(ch.send(embed=embed))
        except Exception:
            pass

def parse_duration(text: str) -> Optional[int]:
    """Parse '10m', '12h', '4d' to seconds"""
    if not isinstance(text, str):
        return None
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*$", text.strip(), re.IGNORECASE)
    if not m:
        return None
    n = int(m.group(1)); unit = m.group(2).lower()
    mult = {"s":1,"m":60,"h":3600,"d":86400}[unit]
    return n * mult

def sanitize_no_mentions(text: str) -> str:
    # replace @ to prevent mentions
    text = text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    # block user/role mentions visually
    text = re.sub(r"<@&?(\d+)>", r"<@\u200b\1>", text)
    return text

def exact_word_in_text(word: str, text: str) -> bool:
    # exact word match, case-insensitive
    return re.search(rf"\b{re.escape(word)}\b", text, flags=re.IGNORECASE) is not None

# ---------------------------
# EVENT HANDLERS: snipes & AFK & blocked words
# ---------------------------
@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
    except Exception as e:
        print("Slash sync failed:", e)
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    # start scheduled loops
    daily_cat_loop.start()
    hourly_cat_loop.start()

@bot.event
async def on_message_delete(message: discord.Message):
    if not message.guild:
        return
    if message.author.bot:
        return
    ch_id = str(message.channel.id)
    DATA["snipes"].setdefault(ch_id, [])
    DATA["snipes"][ch_id].insert(0, {
        "author_id": str(message.author.id),
        "author_name": str(message.author),
        "content": message.content,
        "attachments": [a.url for a in message.attachments],
        "created_at": message.created_at.isoformat() if message.created_at else "",
        "deleted_at": now_local().isoformat()
    })
    DATA["snipes"][ch_id] = DATA["snipes"][ch_id][:50]
    save_data(DATA)
    log_command(message.author, "message_delete", message.channel)

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if not before.guild or before.author.bot:
        return
    if before.content == after.content:
        return
    ch_id = str(before.channel.id)
    DATA["esnipes"].setdefault(ch_id, [])
    DATA["esnipes"][ch_id].insert(0, {
        "author_id": str(before.author.id),
        "author_name": str(before.author),
        "before": before.content,
        "after": after.content,
        "edited_at": now_local().isoformat()
    })
    DATA["esnipes"][ch_id] = DATA["esnipes"][ch_id][:50]
    save_data(DATA)
    log_command(before.author, "message_edit", before.channel)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Auto-clear AFK when AFK user speaks
    uid = str(message.author.id)
    if uid in DATA.get("afk", {}):
        afk_info = DATA["afk"].pop(uid, None)
        save_data(DATA)
        try:
            await message.channel.send(f"✅ Welcome back {message.author.mention}. I removed your AFK (was: {afk_info.get('reason')}).")
        except Exception:
            pass

    # Blocked words enforcement (exact-word). Bypass for admins/pookies/owner
    if message.guild and not is_admin_or_pookie(message.author):
        for w in DATA.get("blocked_words", []):
            if exact_word_in_text(w, message.content):
                try:
                    await message.delete()
                    await message.channel.send(f"{message.author.mention} that word is not allowed here.", delete_after=5)
                    log_command(message.author, "blocked_word_deleted", message.channel)
                except Exception:
                    pass
                return

    # AFK mentions: if a message mentions someone, notify about their AFK
    if message.mentions:
        for m in message.mentions:
            afkmap = DATA.get("afk", {})
            if str(m.id) in afkmap:
                info = afkmap[str(m.id)]
                since = datetime.fromtimestamp(info["since"], timezone.utc).astimezone(LOCAL_TZ)
                embed = discord.Embed(title="User is AFK", color=discord.Color.orange())
                embed.add_field(name="User", value=f"{m.mention}", inline=False)
                embed.add_field(name="Reason", value=info.get("reason", "AFK"), inline=False)
                embed.set_footer(text=f"Since {since.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                try:
                    await message.channel.send(embed=embed)
                except Exception:
                    pass
                # do not break — multiple mentions handled

    # reply-to AFK detection
    if message.reference and message.reference.resolved:
        ref = message.reference.resolved
        if ref.author and str(ref.author.id) in DATA.get("afk", {}):
            info = DATA["afk"][str(ref.author.id)]
            since = datetime.fromtimestamp(info["since"], timezone.utc).astimezone(LOCAL_TZ)
            embed = discord.Embed(title="User is AFK", color=discord.Color.orange())
            embed.add_field(name="User", value=f"{ref.author.mention}", inline=False)
            embed.add_field(name="Reason", value=info.get("reason", "AFK"), inline=False)
            embed.set_footer(text=f"Since {since.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            try:
                await message.channel.send(embed=embed)
            except Exception:
                pass

    # process commands
    await bot.process_commands(message)

# ---------------------------
# NAV VIEW FOR SNIPE
# ---------------------------
class NavView(discord.ui.View):
    def __init__(self, items: List[Dict[str,Any]]):
        super().__init__(timeout=120)
        self.items_list = items
        self.idx = 0

    def format_embed(self) -> discord.Embed:
        it = self.items_list[self.idx]
        embed = discord.Embed(color=discord.Color.orange())
        # Deleted message type
        if "content" in it:
            embed.title = f"🗑 Deleted Message ({self.idx+1}/{len(self.items_list)})"
            embed.add_field(name="Author", value=f"<@{it['author_id']}> ({it['author_name']})", inline=False)
            embed.add_field(name="Content", value=it.get("content") or "(no text)", inline=False)
            if it.get("attachments"):
                embed.add_field(name="Attachments", value="\n".join(it.get("attachments",[])[:5]), inline=False)
            embed.set_footer(text=f"Deleted at {it.get('deleted_at','')}")
        else:
            embed.title = f"✏️ Edited Message ({self.idx+1}/{len(self.items_list)})"
            embed.add_field(name="Author", value=f"<@{it['author_id']}> ({it['author_name']})", inline=False)
            embed.add_field(name="Before", value=it.get("before","(empty)")[:1024], inline=False)
            embed.add_field(name="After", value=it.get("after","(empty)")[:1024], inline=False)
            embed.set_footer(text=f"Edited at {it.get('edited_at','')}")
        return embed

    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.secondary)
    async def left(self, inter: discord.Interaction, btn: discord.ui.Button):
        self.idx = (self.idx - 1) % len(self.items_list)
        await inter.response.edit_message(embed=self.format_embed(), view=self)

    @discord.ui.button(label="➡️", style=discord.ButtonStyle.secondary)
    async def right(self, inter: discord.Interaction, btn: discord.ui.Button):
        self.idx = (self.idx + 1) % len(self.items_list)
        await inter.response.edit_message(embed=self.format_embed(), view=self)

# ---------------------------
# COMMANDS (hybrid => supports / and ?)
# ---------------------------

# ----- AFK -----
@bot.hybrid_command(name="afk", description="Set your AFK status with optional reason.")
async def cmd_afk(ctx: commands.Context, *, reason: str = "AFK"):
    uid = str(ctx.author.id)
    DATA.setdefault("afk", {})[uid] = {"reason": reason, "since": time.time()}
    save_data(DATA)
    log_command(ctx.author, "afk", ctx.channel)
    await ctx.reply(f"✅ {ctx.author.mention} is now AFK: **{reason}**")

@bot.hybrid_command(name="show_afk", description="Show AFK status for a user")
async def cmd_show_afk(ctx: commands.Context, user: Optional[discord.Member] = None):
    target = user or ctx.author
    info = DATA.get("afk", {}).get(str(target.id))
    if not info:
        return await ctx.reply(f"{target.mention} is not AFK.")
    since = datetime.fromtimestamp(info["since"], timezone.utc).astimezone(LOCAL_TZ)
    embed = discord.Embed(title=f"AFK: {target}", color=discord.Color.orange())
    embed.add_field(name="Reason", value=info.get("reason","AFK"))
    embed.add_field(name="Since", value=since.strftime("%Y-%m-%d %H:%M:%S %Z"))
    await ctx.reply(embed=embed)

@bot.hybrid_command(name="remove_afk", description="Remove your AFK manually")
async def cmd_remove_afk(ctx: commands.Context):
    uid = str(ctx.author.id)
    if uid in DATA.get("afk", {}):
        DATA["afk"].pop(uid, None)
        save_data(DATA)
        await ctx.reply("✅ Your AFK was removed.")
    else:
        await ctx.reply("You are not AFK.")

# ----- SAY / SAY_ADMIN -----
@bot.hybrid_command(name="say", description="Bot repeats your message (no pings allowed).")
async def cmd_say(ctx: commands.Context, *, message: str):
    if user_blacklisted(ctx.author):
        return await ctx.reply("🚫 You are blacklisted.")
    if any(x in message for x in ("@everyone","@here")):
        return await ctx.reply("Pings are not allowed in this command.")
    if any(exact_word_in_text(w, message) for w in DATA.get("blocked_words",[])):
        return await ctx.reply("Message contains blocked word.")
    out = sanitize_no_mentions(message)
    log_command(ctx.author, "say", ctx.channel)
    await ctx.reply(out)

@bot.hybrid_command(name="say_admin", description="Admin/Pookie only: bot repeats with mentions allowed")
async def cmd_say_admin(ctx: commands.Context, *, message: str):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    log_command(ctx.author, "say_admin", ctx.channel)
    await ctx.reply(message)  # mentions allowed

# ----- PING/DEBUG/REFRESH/RESTART/EVAL -----
@bot.hybrid_command(name="ping", description="Latency")
async def cmd_ping(ctx: commands.Context):
    await ctx.reply(f"🏓 {round(bot.latency*1000)}ms")

@bot.hybrid_command(name="refresh", description="Sync slash commands (Admin/Pookie/Owner)")
async def cmd_refresh(ctx: commands.Context):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    try:
        synced = await bot.tree.sync()
        log_command(ctx.author, "refresh", ctx.channel)
        await ctx.reply(f"✅ Synced {len(synced)} commands.")
    except Exception as e:
        await ctx.reply(f"Error: {e}")

@bot.hybrid_command(name="restart", description="Owner only: trigger Render deploy (if configured) or restart process")
async def cmd_restart(ctx: commands.Context):
    if not is_owner(ctx.author):
        return await ctx.reply("❌ Owner only.")
    log_command(ctx.author, "restart", ctx.channel)
    if RENDER_API_KEY and RENDER_SERVICE_ID:
        url = f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/deploys"
        headers = {"Authorization": f"Bearer {RENDER_API_KEY}", "Content-Type": "application/json"}
        async with aiohttp.ClientSession() as sess:
            try:
                async with sess.post(url, headers=headers, json={"clearCache": True}, timeout=30) as r:
                    txt = await r.text()
                    await ctx.reply(f"Render response {r.status}: {txt[:1500]}")
            except Exception as e:
                await ctx.reply(f"Render API error: {e}")
    else:
        await ctx.reply("No Render API configured. Exiting process now.")
        await asyncio.sleep(1)
        os._exit(1)

@bot.hybrid_command(name="eval", description="Owner-only: evaluate Python expression")
async def cmd_eval(ctx: commands.Context, *, code: str):
    if not is_owner(ctx.author):
        return await ctx.reply("❌ Owner only.")
    env = {"bot": bot, "discord": discord, "asyncio": asyncio, "ctx": ctx, "DATA": DATA}
    try:
        # strip code block
        if code.startswith("```") and code.endswith("```"):
            code = "\n".join(code.splitlines()[1:-1])
        result = eval(code, env)
        if asyncio.iscoroutine(result):
            result = await result
        out = str(result)
    except Exception:
        out = traceback.format_exc()
    if len(out) > 1900:
        out = out[:1900] + "…"
    await ctx.reply(f"```py\n{out}\n```")

@bot.hybrid_command(name="debug", description="Debug info (uptime, guilds, mem, cpu)")
async def cmd_debug(ctx: commands.Context):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    delta = datetime.now(timezone.utc) - bot.start_time
    uptime = str(delta).split(".")[0]
    process = psutil.Process(os.getpid())
    mem = round(process.memory_info().rss / 1024**2, 2)
    cpu = psutil.cpu_percent(interval=0.1)
    guild_count = len(bot.guilds)
    member_count = sum(g.member_count for g in bot.guilds)
    commands_loaded = len(bot.tree.get_commands())
    embed = discord.Embed(title="🛠 Debug Info", color=discord.Color.green())
    embed.add_field(name="Uptime", value=uptime, inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency*1000)} ms", inline=True)
    embed.add_field(name="Guilds", value=str(guild_count), inline=True)
    embed.add_field(name="Members", value=str(member_count), inline=True)
    embed.add_field(name="Commands Loaded", value=str(commands_loaded), inline=True)
    embed.add_field(name="CPU %", value=f"{cpu}%", inline=True)
    embed.add_field(name="Memory (MB)", value=f"{mem}", inline=True)
    embed.add_field(name="Timezone", value=TZ_NAME, inline=True)
    embed.add_field(name="Owner ID", value=str(OWNER_ID), inline=True)
    embed.add_field(name="Render API", value=("Configured" if RENDER_API_KEY else "Not configured"), inline=True)
    embed.set_footer(text=f"python {platform.python_version()} | discord.py {discord.__version__}")
    await ctx.reply(embed=embed, ephemeral=True)

# ----- SHOWCOMMANDS (permission filtered) -----
@bot.hybrid_command(name="showcommands", description="Show commands available to you")
async def cmd_showcommands(ctx: commands.Context):
    # simple hardcoded categories and commands
    public = ["ping","cat","say","showcommands","serverinfo","show_afk"]
    admin = ["say_admin","setlogchannel","disable_log_channel","add_admin","remove_admin","add_pookie","remove_pookie","warn","show_warns","remove_warn","clear_warns","giverole","removerole","temprole","lock","unlock","mute","unmute","setdailycatchannel","sethourlycatchannel","hourlycat_on","hourlycat_off","trigger_add","trigger_remove"]
    owner = ["eval","restart","refresh"]
    out = ["**Public**: " + ", ".join(public)]
    if is_admin_or_pookie(ctx.author) or is_owner(ctx.author):
        out.append("**Admin/Pookie**: " + ", ".join(admin))
    if is_owner(ctx.author):
        out.append("**Owner**: " + ", ".join(owner))
    await ctx.reply("\n".join(out), ephemeral=True)

# ----- LOG CHANNEL -----
@bot.hybrid_command(name="setlogchannel", description="Set channel to receive command logs (Admin/Pookie)")
async def cmd_setlogchannel(ctx: commands.Context, channel: discord.TextChannel):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    DATA["log_channel"] = channel.id
    save_data(DATA)
    await ctx.reply(f"✅ Log channel set to {channel.mention}")

@bot.hybrid_command(name="disable_log_channel", description="Disable log channel")
async def cmd_disable_log_channel(ctx: commands.Context):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    DATA["log_channel"] = None
    save_data(DATA)
    await ctx.reply("✅ Log channel disabled")

@bot.hybrid_command(name="logs", description="Show recent logs (default 10)")
async def cmd_logs(ctx: commands.Context, amount: int = 10):
    amount = max(1, min(100, amount))
    items = DATA.get("logs", [])[-amount:]
    if not items:
        return await ctx.reply("No logs yet.")
    lines = [f"{i+1}. [{e['time']}] {e['command']} — {e['user']} in {e['channel']}" for i,e in enumerate(items)]
    msg = "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[-1900:]
    await ctx.reply(f"```\n{msg}\n```", ephemeral=True)

# ----- TRIGGERS (auto-responder) -----
@bot.hybrid_command(name="trigger_add", description="Add an exact-word trigger (Admin/Pookie). Use {user} to mention.")
async def cmd_trigger_add(ctx: commands.Context, word: str, *, reply: str):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    DATA.setdefault("triggers", {})[word.lower()] = reply
    save_data(DATA)
    await ctx.reply(f"✅ Trigger added: `{word}` -> {reply}")

@bot.hybrid_command(name="trigger_remove", description="Remove an exact-word trigger")
async def cmd_trigger_remove(ctx: commands.Context, word: str):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    if DATA.get("triggers", {}).pop(word.lower(), None) is None:
        return await ctx.reply("Trigger not found.")
    save_data(DATA)
    await ctx.reply(f"✅ Trigger removed: `{word}`")

@bot.hybrid_command(name="showtrigger", description="Show triggers")
async def cmd_showtrigger(ctx: commands.Context):
    t = DATA.get("triggers", {})
    if not t:
        return await ctx.reply("No triggers set.", ephemeral=True)
    lines = [f"`{k}` -> {v}" for k,v in t.items()]
    await ctx.reply("\n".join(lines), ephemeral=True)

# ----- CAT -----
async def fetch_cat_url():
    url = "https://api.thecatapi.com/v1/images/search"
    headers = {"x-api-key": CAT_API_KEY} if CAT_API_KEY else {}
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url, headers=headers, timeout=20) as r:
            j = await r.json()
            if isinstance(j, list) and j:
                return j[0].get("url")
    return None

@bot.hybrid_command(name="cat", description="Random cat picture/video")
async def cmd_cat(ctx: commands.Context):
    await ctx.defer()
    url = await fetch_cat_url()
    if not url:
        await ctx.followup.send("Could not fetch cat right now.")
    else:
        await ctx.followup.send(url)
    log_command(ctx.author, "cat", ctx.channel)

@bot.hybrid_command(name="setdailycatchannel", description="Set channel for daily cat at 11:00 local TZ")
async def cmd_setdailycatchannel(ctx: commands.Context, channel: discord.TextChannel):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    DATA["daily_cat_channel"] = channel.id
    save_data(DATA)
    await ctx.reply(f"Daily cat channel set to {channel.mention}")

@bot.hybrid_command(name="sethourlycatchannel", description="Set hourly cat channel and interval (hours) (Admin/Pookie)")
async def cmd_sethourlycatchannel(ctx: commands.Context, channel: discord.TextChannel, interval_hours: int = 1):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    h = DATA.setdefault("hourly_cat", {})
    h["channel"] = channel.id
    h["interval_hours"] = max(1, min(24, interval_hours))
    h["enabled"] = True
    save_data(DATA)
    await ctx.reply(f"Hourly cats enabled in {channel.mention} every {h['interval_hours']}h")

@bot.hybrid_command(name="hourlycat_on", description="Enable hourly cat posting (Admin/Pookie)")
async def cmd_hourlycat_on(ctx: commands.Context):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    DATA.setdefault("hourly_cat", {})["enabled"] = True
    save_data(DATA)
    await ctx.reply("Hourly cat enabled.")

@bot.hybrid_command(name="hourlycat_off", description="Disable hourly cat posting (Admin/Pookie)")
async def cmd_hourlycat_off(ctx: commands.Context):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    DATA.setdefault("hourly_cat", {})["enabled"] = False
    save_data(DATA)
    await ctx.reply("Hourly cat disabled.")

# TASKS for daily/hourly cat
@tasks.loop(minutes=1)
async def daily_cat_loop():
    now = datetime.now(timezone.utc).astimezone(LOCAL_TZ)
    if now.hour == 11 and now.minute == 0:
        ch_id = DATA.get("daily_cat_channel")
        if not ch_id:
            return
        ch = bot.get_channel(int(ch_id))
        if not ch:
            return
        url = await fetch_cat_url()
        if url:
            try:
                await ch.send(f"🐱 Daily Cat ({LOCAL_TZ}) — {now.strftime('%Y-%m-%d')}\n{url}")
                log_command(bot.user, "daily_cat", ch)
            except Exception:
                pass

@tasks.loop(minutes=5)
async def hourly_cat_loop():
    # checks every 5 minutes; will post when interval passed
    now_ts = time.time()
    h = DATA.get("hourly_cat", {})
    if not h.get("enabled"):
        return
    ch_id = h.get("channel")
    if not ch_id:
        return
    interval = max(1, int(h.get("interval_hours", 1))) * 3600
    last = float(h.get("last_sent", 0))
    if now_ts - last >= interval:
        ch = bot.get_channel(int(ch_id))
        if ch:
            url = await fetch_cat_url()
            if url:
                try:
                    await ch.send(f"⏰ Hourly Cat — {datetime.now().astimezone(LOCAL_TZ).strftime('%H:%M %Z')}\n{url}")
                    h["last_sent"] = now_ts
                    DATA["hourly_cat"] = h
                    save_data(DATA)
                except Exception:
                    pass

# ----- SNIPE / E-SNIPE commands -----
@bot.hybrid_command(name="snipe", description="Show recently deleted messages in this channel")
async def cmd_snipe(ctx: commands.Context):
    ch_id = str(ctx.channel.id)
    items = DATA.get("snipes", {}).get(ch_id, [])
    if not items:
        return await ctx.reply("Nothing to snipe here.", ephemeral=True)
    view = NavView(items)
    await ctx.reply(embed=view.format_embed(), view=view, ephemeral=True)

@bot.hybrid_command(name="esnipe", description="Show recently edited messages in this channel")
async def cmd_esnipe(ctx: commands.Context):
    ch_id = str(ctx.channel.id)
    items = DATA.get("esnipes", {}).get(ch_id, [])
    if not items:
        return await ctx.reply("Nothing to e-snipe here.", ephemeral=True)
    view = NavView(items)
    await ctx.reply(embed=view.format_embed(), view=view, ephemeral=True)

# ----- MODERATION: ban/kick/purge -----
@bot.hybrid_command(name="kick", description="Kick a member (Admin/Pookie/Owner)")
async def cmd_kick(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    try:
        await member.kick(reason=reason)
        log_command(ctx.author, f"kick {member}", ctx.channel)
        await ctx.reply(f"✅ Kicked {member.mention}")
    except Exception as e:
        await ctx.reply(f"Error: {e}")

@bot.hybrid_command(name="ban", description="Ban a member (Admin/Pookie/Owner)")
async def cmd_ban(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason"):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    try:
        await member.ban(reason=reason, delete_message_days=0)
        log_command(ctx.author, f"ban {member}", ctx.channel)
        await ctx.reply(f"✅ Banned {member.mention}")
    except Exception as e:
        await ctx.reply(f"Error: {e}")

@bot.hybrid_command(name="purge", description="Purge messages (<=100) (Admin required)")
async def cmd_purge(ctx: commands.Context, amount: int = 10):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    amount = max(1, min(100, amount))
    deleted = await ctx.channel.purge(limit=amount)
    log_command(ctx.author, f"purge {len(deleted)}", ctx.channel)
    await ctx.reply(f"Deleted {len(deleted)} messages.", delete_after=6)

# ----- ROLE MANAGEMENT -----
@bot.hybrid_command(name="giverole", description="Give role to a member (Admin/Pookie)")
async def cmd_giverole(ctx: commands.Context, member: discord.Member, role: discord.Role):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    try:
        await member.add_roles(role, reason=f"by {ctx.author}")
        log_command(ctx.author, f"giverole {role.id} to {member.id}", ctx.channel)
        await ctx.reply(f"✅ Gave {role.mention} to {member.mention}")
    except Exception as e:
        await ctx.reply(f"Error: {e}")

@bot.hybrid_command(name="removerole", description="Remove role from a member (Admin/Pookie)")
async def cmd_removerole(ctx: commands.Context, member: discord.Member, role: discord.Role):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    try:
        await member.remove_roles(role, reason=f"by {ctx.author}")
        log_command(ctx.author, f"removerole {role.id} from {member.id}", ctx.channel)
        await ctx.reply(f"✅ Removed {role.mention} from {member.mention}")
    except Exception as e:
        await ctx.reply(f"Error: {e}")

@bot.hybrid_command(name="temprole", description="Give a role temporarily, e.g., 10m 12h 4d (Admin/Pookie)")
async def cmd_temprole(ctx: commands.Context, member: discord.Member, role: discord.Role, duration: str):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    seconds = parse_duration(duration)
    if not seconds:
        return await ctx.reply("Invalid duration. Use `10m`, `12h`, `4d`.")
    try:
        await member.add_roles(role, reason=f"temp by {ctx.author} for {duration}")
        log_command(ctx.author, f"temprole {role.id} to {member.id} for {duration}", ctx.channel)
        await ctx.reply(f"✅ Gave {role.mention} to {member.mention} for {duration}")
        async def _rem():
            await asyncio.sleep(seconds)
            try:
                await member.remove_roles(role, reason="temp role expired")
            except Exception:
                pass
        bot.loop.create_task(_rem())
    except Exception as e:
        await ctx.reply(f"Error: {e}")

# ----- LOCK / UNLOCK channel -----
@bot.hybrid_command(name="lock", description="Lock a channel for @everyone (Admin/Pookie)")
async def cmd_lock(ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    channel = channel or ctx.channel
    try:
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        log_command(ctx.author, f"lock {channel.id}", ctx.channel)
        await ctx.reply(f"🔒 Locked {channel.mention}")
    except Exception as e:
        await ctx.reply(f"Error: {e}")

@bot.hybrid_command(name="unlock", description="Unlock a channel for @everyone (Admin/Pookie)")
async def cmd_unlock(ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    channel = channel or ctx.channel
    try:
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = True
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        log_command(ctx.author, f"unlock {channel.id}", ctx.channel)
        await ctx.reply(f"🔓 Unlocked {channel.mention}")
    except Exception as e:
        await ctx.reply(f"Error: {e}")

# ----- MUTE / UNMUTE -----
async def ensure_muted_role(guild: discord.Guild) -> discord.Role:
    role = discord.utils.get(guild.roles, name="Muted")
    if role:
        return role
    role = await guild.create_role(name="Muted", reason="Mute role created by bot")
    for ch in guild.channels:
        try:
            if isinstance(ch, discord.TextChannel):
                await ch.set_permissions(role, send_messages=False, add_reactions=False)
            elif isinstance(ch, discord.VoiceChannel):
                await ch.set_permissions(role, speak=False, connect=False)
        except Exception:
            pass
    return role

@bot.hybrid_command(name="mute", description="Mute a user for time, e.g., 10m (Admin/Pookie)")
async def cmd_mute(ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = "Muted"):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    seconds = parse_duration(duration)
    if not seconds:
        return await ctx.reply("Invalid duration.")
    try:
        role = await ensure_muted_role(ctx.guild)
        await member.add_roles(role, reason=f"{reason} by {ctx.author}")
        log_command(ctx.author, f"mute {member.id} for {duration}", ctx.channel)
        await ctx.reply(f"🔇 Muted {member.mention} for {duration}")
        async def _unmute():
            await asyncio.sleep(seconds)
            try:
                await member.remove_roles(role, reason="Mute expired")
            except Exception:
                pass
        bot.loop.create_task(_unmute())
    except Exception as e:
        await ctx.reply(f"Error: {e}")

@bot.hybrid_command(name="unmute", description="Unmute a member (Admin/Pookie)")
async def cmd_unmute(ctx: commands.Context, member: discord.Member):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    role = discord.utils.get(ctx.guild.roles, name="Muted")
    if not role:
        return await ctx.reply("Muted role not found.")
    try:
        await member.remove_roles(role, reason=f"Unmuted by {ctx.author}")
        log_command(ctx.author, f"unmute {member.id}", ctx.channel)
        await ctx.reply(f"🔈 Unmuted {member.mention}")
    except Exception as e:
        await ctx.reply(f"Error: {e}")

# ----- WARNS (permanent) -----
@bot.hybrid_command(name="warn", description="Warn a user (Admin/Pookie)")
async def cmd_warn(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason"):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    uid = str(member.id)
    DATA.setdefault("warns", {}).setdefault(uid, []).append({
        "by": f"{ctx.author} ({ctx.author.id})",
        "reason": reason,
        "time": now_local().isoformat()
    })
    save_data(DATA)
    log_command(ctx.author, f"warn {member.id}", ctx.channel)
    await ctx.reply(f"⚠️ Warned {member.mention}: {reason}")

@bot.hybrid_command(name="show_warns", description="Show warns for a user")
async def cmd_show_warns(ctx: commands.Context, member: discord.Member):
    uid = str(member.id)
    lst = DATA.get("warns", {}).get(uid, [])
    if not lst:
        return await ctx.reply(f"{member.mention} has no warns.")
    embed = discord.Embed(title=f"Warns for {member}", color=discord.Color.orange())
    for i,w in enumerate(lst, start=1):
        embed.add_field(name=f"#{i}", value=f"By: {w['by']}\nAt: {w['time']}\nReason: {w['reason']}", inline=False)
    await ctx.reply(embed=embed)

@bot.hybrid_command(name="remove_warn", description="Remove a single warn by index (Admin/Pookie)")
async def cmd_remove_warn(ctx: commands.Context, member: discord.Member, index: int):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    uid = str(member.id)
    lst = DATA.get("warns", {}).get(uid, [])
    if 1 <= index <= len(lst):
        removed = lst.pop(index-1)
        DATA["warns"][uid] = lst
        save_data(DATA)
        await ctx.reply(f"Removed warn #{index} for {member.mention}: {removed['reason']}")
        log_command(ctx.author, f"remove_warn {member.id} #{index}", ctx.channel)
    else:
        await ctx.reply("Invalid warn index.")

@bot.hybrid_command(name="clear_warns", description="Clear all warns for a user (Admin/Pookie)")
async def cmd_clear_warns(ctx: commands.Context, member: discord.Member):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    DATA.setdefault("warns", {})[str(member.id)] = []
    save_data(DATA)
    await ctx.reply(f"Cleared all warns for {member.mention}")
    log_command(ctx.author, f"clear_warns {member.id}", ctx.channel)

# ----- BLACKLIST -----
@bot.hybrid_command(name="blacklist", description="Blacklist a user (Admin/Pookie)")
async def cmd_blacklist(ctx: commands.Context, member: discord.Member):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    if member.id not in DATA["blacklist"]:
        DATA["blacklist"].append(member.id)
        save_data(DATA)
    await ctx.reply(f"Blacklisted {member.mention}")

@bot.hybrid_command(name="unblacklist", description="Remove user from blacklist (Admin/Pookie)")
async def cmd_unblacklist(ctx: commands.Context, member: discord.Member):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    if member.id in DATA["blacklist"]:
        DATA["blacklist"].remove(member.id)
        save_data(DATA)
    await ctx.reply(f"Removed {member.mention} from blacklist")

# ----- BLOCKED WORDS -----
@bot.hybrid_command(name="add_blocked_word", description="Add an exact blocked word (Admin/Pookie)")
async def cmd_add_blocked_word(ctx: commands.Context, word: str):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    w = word.strip().lower()
    if w not in DATA["blocked_words"]:
        DATA["blocked_words"].append(w)
        save_data(DATA)
    await ctx.reply(f"Added blocked word `{w}`")

@bot.hybrid_command(name="remove_blocked_word", description="Remove blocked word (Admin/Pookie)")
async def cmd_remove_blocked_word(ctx: commands.Context, word: str):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    w = word.strip().lower()
    if w in DATA["blocked_words"]:
        DATA["blocked_words"].remove(w)
        save_data(DATA)
        await ctx.reply(f"Removed blocked word `{w}`")
    else:
        await ctx.reply("Word not found.")

# ----- SERVER INFO & SERVERS -----
@bot.hybrid_command(name="serverinfo", description="Detailed server info")
async def cmd_serverinfo(ctx: commands.Context):
    g = ctx.guild
    if not g:
        return await ctx.reply("Use this inside a guild.")
    owner = g.owner or await g.fetch_owner()
    embed = discord.Embed(title=f"Server Info: {g.name}", color=discord.Color.blue())
    embed.set_thumbnail(url=g.icon.url if g.icon else discord.Embed.Empty)
    embed.add_field(name="ID", value=str(g.id), inline=True)
    embed.add_field(name="Owner", value=f"{owner} ({owner.id})", inline=True)
    embed.add_field(name="Created", value=g.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"), inline=True)
    embed.add_field(name="Members", value=str(g.member_count), inline=True)
    embed.add_field(name="Text/Voice", value=f"{len(g.text_channels)}/{len(g.voice_channels)}", inline=True)
    embed.add_field(name="Roles", value=str(len(g.roles)), inline=True)
    embed.add_field(name="Boosts", value=f"{g.premium_subscription_count}", inline=True)
    embed.add_field(name="Verification", value=str(g.verification_level).title(), inline=True)
    await ctx.reply(embed=embed)

@bot.hybrid_command(name="servers", description="List all servers bot is in (Owner/Admin/Pookie)")
async def cmd_servers(ctx: commands.Context):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    lines = [f"**{g.name}** (`{g.id}`) — Members: {g.member_count} — Owner: {g.owner or g.owner_id}" for g in bot.guilds]
    out = "\n".join(lines)
    if len(out) > 1900:
        out = out[:1900] + "…"
    await ctx.reply(out, ephemeral=True)

# ---------------------------
# PREFIX: some convenience (mirror a few commands)
# ---------------------------
@bot.command(name="say")
async def prefix_say(ctx: commands.Context, *, message: str):
    await cmd_say.callback(ctx, message)

@bot.command(name="cat")
async def prefix_cat(ctx: commands.Context):
    await cmd_cat.callback(ctx)

# ---------------------------
# START
# ---------------------------
if __name__ == "__main__":
    print(f"Starting bot (owner={OWNER_ID}) TZ={TZ_NAME}")
    bot.run(TOKEN)
