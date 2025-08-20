# main.py  —  Full Bot (automod, trusted users, per-guild logs, /log timeline, interactive /automod UI)
# Save as main.py
# Requirements: discord.py 2.3.2, aiohttp, Flask, python-dotenv (optional), psutil (optional for debug)
# Env vars (Render): DISCORD_BOT_TOKEN (required), OWNER_ID (optional), CAT_API_KEY, RENDER_API_KEY, RENDER_SERVICE_ID, TZ, PORT

import os, re, json, random, asyncio, traceback, platform
from datetime import datetime, timezone, timedelta
from threading import Thread
from typing import Optional, Dict, Any, List
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import commands, tasks
from discord import app_commands
from flask import Flask

# -----------------------
# Environment & defaults
# -----------------------
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN environment variable required")

try:
    OWNER_ID = int(os.getenv("OWNER_ID", "1319292111325106296").strip())
except Exception:
    OWNER_ID = 1319292111325106296

CAT_API_KEY = os.getenv("CAT_API_KEY", "").strip()
RENDER_API_KEY = os.getenv("RENDER_API_KEY", "").strip()
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "").strip()
TZ_NAME = os.getenv("TZ", "Asia/Kolkata").strip() or "Asia/Kolkata"

try:
    BOT_TZ = ZoneInfo(TZ_NAME)
except Exception:
    BOT_TZ = ZoneInfo("Asia/Kolkata")

FLASK_PORT = int(os.getenv("PORT", os.getenv("FLASK_PORT", "8080")))

DATA_FILE = "data.json"
SNIPES_KEEP = 200
LOGS_KEEP = 5000

# -----------------------
# Flask keepalive
# -----------------------
flask_app = Flask("keepalive")


@flask_app.route("/")
def home():
    return "OK", 200


def run_flask():
    flask_app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, use_reloader=False)


Thread(target=run_flask, daemon=True).start()

# -----------------------
# Data structure
# -----------------------
DEFAULT_AUTOMOD = {
    "anti_link": True,
    "anti_invite": True,
    "anti_spam": {"enabled": True, "action": "delete", "threshold": 5, "interval": 6},
    "blocked_words_enabled": True,
    "trusted_users": []  # user ids
}

DEFAULT_DATA = {
    "admins": [OWNER_ID],
    "pookies": [],
    "blacklist": [],
    "blocked_words": [],      # list of strings
    "triggers": {},           # word -> reply
    "log_channels": {},       # guild_id -> channel_id
    "cat_channels": {},       # guild_id -> channel_id (daily)
    "hourly_cat_channels": {},# guild_id -> channel_id (hourly)
    "logs": [],               # list of structured log dicts
    "afk": {},                # user_id_str -> {reason, since_iso}
    "warns": {},              # user_id_str -> [warns]
    "temp_roles": [],         # list of {guild_id,user_id,role_id,expires_at_iso}
    "automod": {}             # guild_id -> automod_config (see DEFAULT_AUTOMOD)
}


def save_data(d: Dict[str, Any]):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("save_data error:", e)


def load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        save_data(DEFAULT_DATA.copy())
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = DEFAULT_DATA.copy()
    # ensure keys
    for k, v in DEFAULT_DATA.items():
        if k not in d:
            d[k] = v.copy() if isinstance(v, (list, dict)) else v
    # ensure owner in admins
    if OWNER_ID not in d.get("admins", []):
        d["admins"].append(OWNER_ID)
    return d


DATA: Dict[str, Any] = load_data()

def reload_data() -> Dict[str, Any]:
    global DATA
    DATA = load_data()
    return DATA

# -----------------------
# Helper utilities
# -----------------------
def sanitize_no_ping(text: str) -> str:
    return text.replace("@", "@\u200b")

def human_delta(dt: datetime) -> str:
    now = datetime.now(timezone.utc)
    diff = now - dt
    days = diff.days
    s = diff.seconds
    hours, rem = divmod(s, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days: parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    if seconds and not parts: parts.append(f"{seconds}s")
    return " ".join(parts) if parts else "0s"

def account_age(user: discord.abc.Snowflake) -> str:
    try:
        created = user.created_at
        return human_delta(created)
    except Exception:
        return "Unknown"

def member_join_age(member: discord.Member) -> str:
    try:
        return human_delta(member.joined_at) if member.joined_at else "Unknown"
    except Exception:
        return "Unknown"

def ensure_guild_automod(guild_id: int) -> Dict[str, Any]:
    d = reload_data()
    autos = d.setdefault("automod", {})
    gk = str(guild_id)
    if gk not in autos:
        autos[gk] = DEFAULT_AUTOMOD.copy()
        save_data(d)
    return autos[gk]

def is_trusted_user(guild_id: int, user_id: int) -> bool:
    auto = ensure_guild_automod(guild_id)
    trusted = set(map(int, auto.get("trusted_users", [])))
    return user_id in trusted

# -----------------------
# Structured logs
# -----------------------
def add_log(guild_id: Optional[int], action: str, actor_id: Optional[int], target_id: Optional[int], details: Dict[str, Any]):
    """
    Stores a normalized log entry in DATA['logs'] and trims history.
    details can include message_id, channel_id, content, attachments(list), reason, punishment, etc.
    """
    d = reload_data()
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "guild_id": str(guild_id) if guild_id else None,
        "action": action,            # e.g., "message_delete","member_join","role_add","automod_link"
        "actor_id": int(actor_id) if actor_id else None,
        "target_id": int(target_id) if target_id else None,
        "details": details or {}
    }
    d.setdefault("logs", [])
    d["logs"].append(entry)
    if len(d["logs"]) > LOGS_KEEP:
        d["logs"] = d["logs"][-LOGS_KEEP:]
    save_data(d)

async def send_log_embed_for_entry(entry: Dict[str, Any]):
    """ Format and send a rich embed to the configured log channel for the entry's guild. """
    d = reload_data()
    gid = entry.get("guild_id")
    if not gid:
        return
    ch_map = d.get("log_channels", {})
    ch_id = ch_map.get(gid)
    if not ch_id:
        return
    ch = bot.get_channel(int(ch_id))
    if not ch:
        return
    action = entry.get("action", "log")
    actor_id = entry.get("actor_id")
    target_id = entry.get("target_id")
    details = entry.get("details", {}) or {}
    ts = entry.get("ts")
    embed = discord.Embed(timestamp=datetime.fromisoformat(ts), color=discord.Color.blurple())

    # helper to format user display without ping
    def fmt_user(uid):
        try:
            u = bot.get_user(int(uid))
            if u:
                return f"{sanitize_no_ping(str(u))} (`{u.id}`)", u.display_avatar.url
        except Exception:
            pass
        return f"`{uid}`", None

    # Action-specific formatting
    if action == "member_join":
        embed.title = "🟢 Member Joined"
        info, avatar = fmt_user(target_id)
        embed.description = f"{info}"
        embed.add_field(name="User ID", value=f"`{target_id}`", inline=True)
        embed.add_field(name="Account age", value=details.get("account_age","Unknown"), inline=True)
        embed.add_field(name="Member Count", value=str(details.get("member_count","Unknown")), inline=True)
        if avatar:
            embed.set_thumbnail(url=avatar)
    elif action == "member_leave":
        embed.title = "⚪ Member Left"
        info, avatar = fmt_user(target_id)
        embed.description = f"{info}\nRoles: {details.get('roles','None')}"
        embed.add_field(name="User ID", value=f"`{target_id}`", inline=True)
        embed.add_field(name="Account age", value=details.get("account_age","Unknown"), inline=True)
        embed.add_field(name="Time in server", value=details.get("time_in_server","Unknown"), inline=True)
        if avatar:
            embed.set_thumbnail(url=avatar)
    elif action in ("ban","kick","timeout"):
        embed.title = f"⛔ {action.capitalize()}"
        info, avatar = fmt_user(target_id)
        embed.description = f"{info}\nReason: {details.get('reason','No reason')}"
        embed.add_field(name="Actor", value=f"<@{actor_id}>" if actor_id else "`Unknown`", inline=True)
        embed.add_field(name="User ID", value=f"`{target_id}`", inline=True)
        embed.add_field(name="Account age", value=details.get("account_age","Unknown"), inline=True)
        if avatar:
            embed.set_thumbnail(url=avatar)
    elif action == "role_update":
        embed.title = "🔁 Role Update"
        info, avatar = fmt_user(target_id)
        embed.description = f"{info}"
        embed.add_field(name="Added", value=details.get("added","None"), inline=True)
        embed.add_field(name="Removed", value=details.get("removed","None"), inline=True)
        embed.add_field(name="Actor", value=f"<@{actor_id}>" if actor_id else "`Unknown`", inline=True)
        if avatar:
            embed.set_thumbnail(url=avatar)
    elif action == "message_delete":
        embed.title = "🗑️ Message Deleted"
        content = details.get("content","")
        attachments = details.get("attachments", [])
        author_info = details.get("author_str", f"`{details.get('author_id','Unknown')}`")
        embed.description = f"{author_info}\nChannel: {details.get('channel','Unknown')} • Message ID: `{details.get('message_id','')}`\nDeleted by: {details.get('deleted_by','Unknown')}"
        embed.add_field(name="Message age", value=details.get("message_age","Unknown"), inline=True)
        embed.add_field(name="Account age", value=details.get("account_age","Unknown"), inline=True)
        if content:
            embed.add_field(name="Content", value=(content[:1000] + "..." if len(content)>1000 else content), inline=False)
        if attachments:
            embed.add_field(name="Attachments", value="\n".join(attachments)[:1000], inline=False)
    elif action.startswith("automod_"):
        kind = action.split("_",1)[1]
        embed.title = f"⚠️ Automod — {kind.replace('_',' ').title()}"
        embed.add_field(name="User", value=f"<@{target_id}> (`{target_id}`)", inline=True)
        embed.add_field(name="Action", value=details.get("punishment","deleted"), inline=True)
        embed.add_field(name="Channel", value=details.get("channel","Unknown"), inline=True)
        if details.get("content"):
            embed.add_field(name="Content", value=(details.get("content")[:1000] + "...") if len(details.get("content",""))>1000 else details.get("content",""), inline=False)
        if details.get("attachments"):
            embed.add_field(name="Attachments", value="\n".join(details.get("attachments",[]))[:1000], inline=False)
    else:
        embed.title = entry.get("action", "Log")
        embed.description = str(entry.get("details",""))

    try:
        await ch.send(embed=embed)
    except Exception:
        # ignore send errors
        pass

# -----------------------
# Snipe arrays (deleted/edited)
# -----------------------
SNIPES: Dict[int, List[Dict[str,Any]]] = {}
ESNIPES: Dict[int, List[Dict[str,Any]]] = {}

def push_snipe(store: Dict[int, List[Dict[str,Any]]], chan_id: int, item: Dict[str,Any]):
    lst = store.setdefault(chan_id, [])
    lst.append(item)
    if len(lst) > SNIPES_KEEP:
        del lst[0]

# -----------------------
# Helper: detect deleter via audit logs (best-effort)
# -----------------------
async def attempt_find_deleter(guild: discord.Guild, message) -> str:
    """
    Best-effort: look up recent audit logs for message_delete or member_role_update etc.
    Returns: 'bot' / 'self' / '<@id>' / 'unknown'
    """
    # if message was by bot
    try:
        if message.author and message.author.id == bot.user.id:
            return "bot"
    except Exception:
        pass
    # Attempt to read audit logs: catch any exception
    try:
        # look for recent audit logs (last 6 entries)
        async for entry in guild.audit_logs(limit=6):
            # entry.action is an enum; compare names to include message_delete / message_bulk_delete etc
            aname = getattr(entry.action, "name", str(entry.action)).lower()
            if "message_delete" in aname:
                # if entry happened very recently
                delta = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
                if delta < 15:
                    # we can't reliably match message id but assume this is probably the one
                    return f"<@{entry.user.id}>"
    except Exception:
        pass
    # fallback: unknown (we can't reliably find)
    return "unknown"

# -----------------------
# Automod regexes
# -----------------------
URL_REGEX = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
INVITE_REGEX = re.compile(r"(?:discord\.gg|discord(?:app)?\.com\/invite)\/[A-Za-z0-9\-]+", re.IGNORECASE)

# spam tracker in-memory: { (guild_id, user_id) : [timestamps] }
SPAM_TRACKER: Dict[str, List[float]] = {}

# -----------------------
# Bot Setup
# -----------------------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix=commands.when_mentioned_or("?"), intents=intents, help_command=None)
BOT_START = datetime.now(timezone.utc)

# -----------------------
# periodic housekeeping: daily/hourly cat and temp role cleanup
# -----------------------
_last_daily_sent = {}
_last_hourly_sent = {}

@tasks.loop(minutes=1)
async def housekeeping():
    d = reload_data()
    now_local = datetime.now(BOT_TZ)
    # daily cat at 11:00
    if now_local.hour == 11 and now_local.minute == 0:
        date_key = now_local.date().isoformat()
        for gid, ch_id in d.get("cat_channels", {}).items():
            try:
                if _last_daily_sent.get(gid) == date_key:
                    continue
                ch = bot.get_channel(int(ch_id))
                if ch:
                    async with aiohttp.ClientSession() as s:
                        headers = {"x-api-key": CAT_API_KEY} if CAT_API_KEY else {}
                        async with s.get("https://api.thecatapi.com/v1/images/search", headers=headers) as r:
                            if r.status == 200:
                                j = await r.json()
                                url = j[0].get("url") if j else None
                            else:
                                url = "https://cataas.com/cat"
                    if url:
                        await ch.send(url)
                        add_log(int(gid), "cat_daily", None, None, {"url":url})
                        await send_log_embed_for_entry({
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "guild_id": str(gid),
                            "action": "cat_daily",
                            "actor_id": None,
                            "target_id": None,
                            "details": {"url": url}
                        })
                _last_daily_sent[gid] = date_key
            except Exception:
                pass
    # hourly cat at minute == 0
    if now_local.minute == 0:
        key = f"{now_local.date().isoformat()}-{now_local.hour}"
        for gid, ch_id in d.get("hourly_cat_channels", {}).items():
            try:
                if _last_hourly_sent.get(gid) == key:
                    continue
                ch = bot.get_channel(int(ch_id))
                if ch:
                    async with aiohttp.ClientSession() as s:
                        headers = {"x-api-key": CAT_API_KEY} if CAT_API_KEY else {}
                        async with s.get("https://api.thecatapi.com/v1/images/search", headers=headers) as r:
                            if r.status == 200:
                                j = await r.json()
                                url = j[0].get("url") if j else None
                            else:
                                url = "https://cataas.com/cat"
                    if url:
                        await ch.send(url)
                        add_log(int(gid), "cat_hourly", None, None, {"url":url})
                        await send_log_embed_for_entry({
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "guild_id": str(gid),
                            "action": "cat_hourly",
                            "actor_id": None,
                            "target_id": None,
                            "details": {"url": url}
                        })
                _last_hourly_sent[gid] = key
            except Exception:
                pass
    # temp roles expiry
    try:
        d = reload_data()
        changed = False
        remaining = []
        for entry in d.get("temp_roles", []):
            expires = entry.get("expires_at")
            try:
                exp_dt = datetime.fromisoformat(expires)
            except Exception:
                continue
            if datetime.now(timezone.utc) >= exp_dt:
                try:
                    g = bot.get_guild(int(entry["guild_id"]))
                    if g:
                        mem = g.get_member(int(entry["user_id"]))
                        role = g.get_role(int(entry["role_id"]))
                        if mem and role:
                            await mem.remove_roles(role, reason="Temp role expired")
                            add_log(g.id, "temp_role_expired", None, mem.id, {"role_id":role.id})
                            await send_log_embed_for_entry({
                                "ts": datetime.now(timezone.utc).isoformat(),
                                "guild_id": str(g.id),
                                "action":"temp_role_expired",
                                "actor_id": None,
                                "target_id": mem.id,
                                "details":{"role_id": role.id}
                            })
                except Exception:
                    pass
            else:
                remaining.append(entry)
        if len(remaining) != len(d.get("temp_roles", [])):
            d["temp_roles"] = remaining
            save_data(d)
    except Exception:
        pass

# -----------------------
# On ready
# -----------------------
@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
    except Exception:
        pass
    # streaming presence (purple)
    try:
        act = discord.Streaming(name="Max Verstappen", url="https://twitch.tv/yourchannel")
        await bot.change_presence(status=discord.Status.do_not_disturb, activity=act)
    except Exception:
        try:
            await bot.change_presence(status=discord.Status.do_not_disturb)
        except Exception:
            pass
    if not housekeeping.is_running():
        housekeeping.start()
    print(f"Bot ready: {bot.user} — guilds: {len(bot.guilds)}")
    add_log(None, "bot_ready", None, None, {"guilds": len(bot.guilds)})
    # send system start to all configured log channels (optional)
    d = reload_data()
    for gid_str, ch_id in d.get("log_channels", {}).items():
        try:
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "guild_id": gid_str,
                "action": "system_start",
                "actor_id": None,
                "target_id": None,
                "details": {"msg": "Bot restarted"}
            }
            await send_log_embed_for_entry(entry)
        except Exception:
            pass

# -----------------------
# Event handlers & automod checks
# -----------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    d = reload_data()
    gid = message.guild.id if message.guild else None

    # AFK auto-clear
    if str(message.author.id) in d.get("afk", {}):
        d["afk"].pop(str(message.author.id), None)
        save_data(d)
        try:
            await message.channel.send(f"✅ Welcome back {message.author.mention}. AFK removed.")
        except Exception:
            pass
        add_log(gid, "afk_cleared", message.author.id, message.author.id, {"reason":"returned"})

    # If mentions someone AFK, reply
    if message.mentions:
        for u in message.mentions:
            afk = d.get("afk", {}).get(str(u.id))
            if afk:
                since = afk.get("since")
                reason = afk.get("reason","AFK")
                try:
                    ts = int(datetime.fromisoformat(since).timestamp())
                    await message.reply(f"{sanitize_no_ping(str(u))} is AFK — **{sanitize_no_ping(reason)}** (since <t:{ts}:R>)", mention_author=False)
                except Exception:
                    await message.reply(f"{sanitize_no_ping(str(u))} is AFK — **{sanitize_no_ping(reason)}**", mention_author=False)

    # AUTOMOD: blocked words, anti_link, anti_invite, anti_spam
    if message.guild:
        auto = ensure_guild_automod(message.guild.id)
        if is_trusted_user(message.guild.id, message.author.id):
            # trusted bypass for message filters
            pass
        else:
            # blocked words (normalized)
            if auto.get("blocked_words_enabled", True) and d.get("blocked_words"):
                content_compact = re.sub(r"[\s\-\_\.]", "", message.content.lower())
                for bw in d.get("blocked_words", []):
                    bwc = re.sub(r"[\s\-\_\.]", "", bw.lower())
                    if bwc and bwc in content_compact:
                        # delete and log
                        try:
                            await message.delete()
                        except Exception:
                            pass
                        details = {
                            "content": message.content,
                            "channel": f"{message.channel.name}",
                            "message_id": str(message.id),
                            "author_id": message.author.id,
                            "account_age": account_age(message.author),
                            "attachments": [att.url for att in message.attachments]
                        }
                        add_log(message.guild.id, "automod_blocked_word", None, message.author.id, details)
                        # send embed
                        await send_log_embed_for_entry({
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "guild_id": str(message.guild.id),
                            "action": "automod_blocked_word",
                            "actor_id": None,
                            "target_id": message.author.id,
                            "details": details
                        })
                        return

            # anti-invite
            if auto.get("anti_invite", True):
                if INVITE_REGEX.search(message.content):
                    try:
                        await message.delete()
                    except Exception:
                        pass
                    details = {
                        "content": message.content,
                        "channel": f"{message.channel.name}",
                        "message_id": str(message.id),
                        "author_id": message.author.id,
                        "account_age": account_age(message.author),
                        "attachments": [att.url for att in message.attachments]
                    }
                    add_log(message.guild.id, "automod_invite", None, message.author.id, details)
                    await send_log_embed_for_entry({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "guild_id": str(message.guild.id),
                        "action": "automod_invite",
                        "actor_id": None,
                        "target_id": message.author.id,
                        "details": details
                    })
                    return

            # anti-link
            if auto.get("anti_link", True):
                if URL_REGEX.search(message.content):
                    try:
                        await message.delete()
                    except Exception:
                        pass
                    details = {
                        "content": message.content,
                        "channel": f"{message.channel.name}",
                        "message_id": str(message.id),
                        "author_id": message.author.id,
                        "account_age": account_age(message.author),
                        "attachments": [att.url for att in message.attachments]
                    }
                    add_log(message.guild.id, "automod_link", None, message.author.id, details)
                    await send_log_embed_for_entry({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "guild_id": str(message.guild.id),
                        "action": "automod_link",
                        "actor_id": None,
                        "target_id": message.author.id,
                        "details": details
                    })
                    return

            # anti-spam (flood)
            spam_cfg = auto.get("anti_spam", {"enabled": True})
            if spam_cfg.get("enabled", True):
                key = f"{message.guild.id}:{message.author.id}"
                now_ts = asyncio.get_event_loop().time()
                arr = SPAM_TRACKER.get(key, [])
                arr = [t for t in arr if now_ts - t <= spam_cfg.get("interval", 6)]
                arr.append(now_ts)
                SPAM_TRACKER[key] = arr
                if len(arr) >= int(spam_cfg.get("threshold",5)):
                    # default action = delete
                    action = spam_cfg.get("action","delete")
                    # perform delete + log
                    try:
                        await message.delete()
                    except Exception:
                        pass
                    details = {
                        "content": message.content,
                        "channel": f"{message.channel.name}",
                        "message_id": str(message.id),
                        "author_id": message.author.id,
                        "account_age": account_age(message.author),
                        "attachments": [att.url for att in message.attachments],
                        "count": len(arr),
                        "action": action
                    }
                    add_log(message.guild.id, "automod_spam", None, message.author.id, details)
                    await send_log_embed_for_entry({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "guild_id": str(message.guild.id),
                        "action": "automod_spam",
                        "actor_id": None,
                        "target_id": message.author.id,
                        "details": details
                    })
                    # punishment not implemented here (delete) — you can later expand to mute/kick/ban
                    SPAM_TRACKER[key] = []
                    return

    # process triggers and commands
    # triggers: exact word detection
    d = reload_data()
    for word, reply in d.get("triggers", {}).items():
        try:
            if re.search(r"\b" + re.escape(word) + r"\b", message.content, flags=re.IGNORECASE):
                out = reply.replace("{user}", message.author.mention)
                out = sanitize_no_ping(out) if not is_admin(message.author.id) else out
                await message.channel.send(out)
                add_log(message.guild.id if message.guild else None, "trigger_fired", message.author.id, message.author.id, {"trigger":word, "reply":reply})
                break
        except Exception:
            pass

    await bot.process_commands(message)

@bot.event
async def on_message_delete(message: discord.Message):
    if not message or not message.author:
        return
    # push snipe
    push_snipe(SNIPES, message.channel.id, {
        "author_tag": str(message.author),
        "avatar_url": getattr(message.author.display_avatar, "url", ""),
        "content": message.content or "",
        "time": datetime.now(timezone.utc).isoformat(),
        "message_id": str(message.id)
    })
    # attempt find deleter
    deleted_by = "unknown"
    try:
        if message.guild:
            deleted_by = await attempt_find_deleter(message.guild, message)
    except Exception:
        deleted_by = "unknown"
    # details
    details = {
        "author_str": sanitize_no_ping(str(message.author)),
        "author_id": message.author.id,
        "channel": str(message.channel),
        "message_id": str(message.id),
        "content": message.content or "",
        "attachments": [att.url for att in message.attachments],
        "message_age": human_delta(datetime.now(timezone.utc) - (datetime.now(timezone.utc) - timedelta(seconds=0))) if False else (human_delta(message.created_at) if message.created_at else "Unknown"),
        "account_age": account_age(message.author),
        "deleted_by": deleted_by
    }
    gid = message.guild.id if message.guild else None
    add_log(gid, "message_delete", None, message.author.id, details)
    await send_log_embed_for_entry({
        "ts": datetime.now(timezone.utc).isoformat(),
        "guild_id": str(gid) if gid else None,
        "action": "message_delete",
        "actor_id": None,
        "target_id": message.author.id,
        "details": details
    })

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if not before.author or before.author.bot:
        return
    push_snipe(ESNIPES, before.channel.id, {
        "author_tag": str(before.author),
        "avatar_url": getattr(before.author.display_avatar, "url", ""),
        "before": before.content,
        "after": after.content,
        "time": datetime.now(timezone.utc).isoformat()
    })
    details = {
        "author_id": before.author.id,
        "channel": str(before.channel),
        "before": before.content or "",
        "after": after.content or ""
    }
    gid = before.guild.id if before.guild else None
    add_log(gid, "message_edit", None, before.author.id, details)
    await send_log_embed_for_entry({
        "ts": datetime.now(timezone.utc).isoformat(),
        "guild_id": str(gid) if gid else None,
        "action": "message_edit",
        "actor_id": None,
        "target_id": before.author.id,
        "details": details
    })

@bot.event
async def on_member_join(member: discord.Member):
    details = {"account_age": account_age(member), "member_count": member.guild.member_count}
    gid = member.guild.id
    add_log(gid, "member_join", None, member.id, details)
    await send_log_embed_for_entry({
        "ts": datetime.now(timezone.utc).isoformat(),
        "guild_id": str(gid),
        "action": "member_join",
        "actor_id": None,
        "target_id": member.id,
        "details": details
    })

@bot.event
async def on_member_remove(member: discord.Member):
    roles = ", ".join([r.name for r in member.roles if r.name != "@everyone"]) or "None"
    details = {"account_age": account_age(member), "time_in_server": member_join_age(member), "roles": roles, "member_count": member.guild.member_count}
    gid = member.guild.id
    add_log(gid, "member_leave", None, member.id, details)
    await send_log_embed_for_entry({
        "ts": datetime.now(timezone.utc).isoformat(),
        "guild_id": str(gid),
        "action": "member_leave",
        "actor_id": None,
        "target_id": member.id,
        "details": details
    })

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    # roles changed?
    before_roles = set(r.id for r in before.roles)
    after_roles = set(r.id for r in after.roles)
    added = after_roles - before_roles
    removed = before_roles - after_roles
    if added or removed:
        # try fetch audit logs to know who changed roles (best-effort)
        actor = None
        try:
            async for entry in after.guild.audit_logs(limit=6, action=discord.AuditLogAction.member_role_update):
                delta = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
                if delta < 10 and entry.target.id == after.id:
                    actor = entry.user
                    break
        except Exception:
            actor = None
        added_names = ", ".join([after.guild.get_role(r).name for r in added if after.guild.get_role(r)]) or "None"
        removed_names = ", ".join([before.guild.get_role(r).name for r in removed if before.guild.get_role(r)]) or "None"
        details = {"added": added_names, "removed": removed_names}
        gid = after.guild.id
        add_log(gid, "role_update", actor.id if actor else None, after.id, details)
        await send_log_embed_for_entry({
            "ts": datetime.now(timezone.utc).isoformat(),
            "guild_id": str(gid),
            "action": "role_update",
            "actor_id": actor.id if actor else None,
            "target_id": after.id,
            "details": details
        })

@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    details = {"account_age": account_age(user)}
    add_log(guild.id, "ban", None, user.id, details)
    await send_log_embed_for_entry({
        "ts": datetime.now(timezone.utc).isoformat(),
        "guild_id": str(guild.id),
        "action": "ban",
        "actor_id": None,
        "target_id": user.id,
        "details": details
    })

@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User):
    details = {"account_age": account_age(user)}
    add_log(guild.id, "unban", None, user.id, details)
    await send_log_embed_for_entry({
        "ts": datetime.now(timezone.utc).isoformat(),
        "guild_id": str(guild.id),
        "action": "unban",
        "actor_id": None,
        "target_id": user.id,
        "details": details
    })

# -----------------------
# Commands & UI
# -----------------------

# Utilities for per-guild log channel
@bot.hybrid_command(name="set_log_channel", with_app_command=True, description="Set guild's log channel (admin)")
async def set_log_channel(ctx: commands.Context, channel: discord.TextChannel):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    d.setdefault("log_channels", {})[str(ctx.guild.id)] = int(channel.id)
    save_data(d)
    await ctx.reply(f"✅ Log channel set to {channel.mention}", mention_author=False)
    add_log(ctx.guild.id, "set_log_channel", ctx.author.id, None, {"channel_id": channel.id})
    await send_log_embed_for_entry({
        "ts": datetime.now(timezone.utc).isoformat(),
        "guild_id": str(ctx.guild.id),
        "action": "set_log_channel",
        "actor_id": ctx.author.id,
        "target_id": None,
        "details": {"channel": str(channel)}
    })

@bot.hybrid_command(name="disable_log_channel", with_app_command=True, description="Disable logs for this guild (admin)")
async def disable_log_channel(ctx: commands.Context):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    d.setdefault("log_channels", {}).pop(str(ctx.guild.id), None)
    save_data(d)
    await ctx.reply("✅ Log channel disabled.", mention_author=False)
    add_log(ctx.guild.id, "disable_log_channel", ctx.author.id, None, {})

@bot.hybrid_command(name="show_log_channel", with_app_command=True, description="Show this guild's log channel")
async def show_log_channel(ctx: commands.Context):
    d = reload_data()
    ch = d.get("log_channels", {}).get(str(ctx.guild.id))
    if ch:
        c = bot.get_channel(int(ch))
        await ctx.reply(f"Log channel: {c.mention if c else str(ch)}", mention_author=False)
    else:
        await ctx.reply("No log channel set.", mention_author=False)

# /log command: timeline for user
class LogPaginationView(discord.ui.View):
    def __init__(self, pages: List[str], author_id: int):
        super().__init__(timeout=120)
        self.pages = pages
        self.idx = 0
        self.author_id = author_id

    async def update_msg(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content=self.pages[self.idx], view=self)

    @discord.ui.button(label="⬅️ Prev", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Not your session.", ephemeral=True)
        if self.idx > 0:
            self.idx -= 1
            await self.update_msg(interaction)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="➡️ Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Not your session.", ephemeral=True)
        if self.idx < len(self.pages)-1:
            self.idx += 1
            await self.update_msg(interaction)
        else:
            await interaction.response.defer()

@bot.hybrid_command(name="log", with_app_command=True, description="Show timeline logs for a user (admin)")
async def log_cmd(ctx: commands.Context, user: discord.User, category: Optional[str] = None):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    uid = int(user.id)
    entries = []
    for e in d.get("logs", []):
        if e.get("actor_id") == uid or e.get("target_id") == uid:
            if category:
                if not e.get("action","").startswith(category):
                    continue
            entries.append(e)
    if not entries:
        return await ctx.reply("No logs for that user.", mention_author=False)
    # sort desc
    entries = list(reversed(entries))
    # format pages (10 per page)
    pages = []
    chunk = 10
    for i in range(0, len(entries), chunk):
        block = entries[i:i+chunk]
        lines = []
        for e in block:
            ts = e.get("ts","")
            action = e.get("action","")
            actor = e.get("actor_id")
            target = e.get("target_id")
            details = e.get("details",{})
            s = f"`{ts}` **{action}** • actor: `{actor}` target: `{target}` • {details.get('channel','')} • {details.get('message_id','')}"
            lines.append(s)
        pages.append("\n".join(lines)[:1900])
    view = LogPaginationView(pages, ctx.author.id)
    await ctx.reply(pages[0], view=view, mention_author=False)
    add_log(ctx.guild.id if ctx.guild else None, "command_log_view", ctx.author.id, uid, {"count": len(entries)})

# -----------------------
# Automod interactive UI
# -----------------------
class AutomodView(discord.ui.View):
    def __init__(self, guild_id: int, author_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.author_id = author_id

    async def refresh_message(self, interaction: discord.Interaction):
        # update embed content
        cfg = ensure_guild_automod(self.guild_id)
        emb = discord.Embed(title="Automod Control Panel", color=discord.Color.yellow())
        emb.add_field(name="Anti Link", value=str(cfg.get("anti_link")), inline=True)
        emb.add_field(name="Anti Invite", value=str(cfg.get("anti_invite")), inline=True)
        emb.add_field(name="Blocked Words", value=str(cfg.get("blocked_words_enabled")), inline=True)
        spam = cfg.get("anti_spam", {})
        emb.add_field(name="Anti Spam", value=f"enabled={spam.get('enabled')} action={spam.get('action')} threshold={spam.get('threshold')}", inline=False)
        emb.add_field(name="Trusted users", value=str(len(cfg.get("trusted_users",[]))), inline=False)
        await interaction.response.edit_message(embed=emb, view=self)

    @discord.ui.button(label="Toggle Anti-Link", style=discord.ButtonStyle.primary)
    async def toggle_link(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Not your session.", ephemeral=True)
        cfg = ensure_guild_automod(self.guild_id)
        cfg["anti_link"] = not cfg.get("anti_link", True)
        d = reload_data(); d["automod"][str(self.guild_id)] = cfg; save_data(d)
        await self.refresh_message(interaction)
        add_log(self.guild_id, "automod_config_change", interaction.user.id, None, {"key":"anti_link","value":cfg["anti_link"]})

    @discord.ui.button(label="Toggle Anti-Invite", style=discord.ButtonStyle.primary)
    async def toggle_invite(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Not your session.", ephemeral=True)
        cfg = ensure_guild_automod(self.guild_id)
        cfg["anti_invite"] = not cfg.get("anti_invite", True)
        d = reload_data(); d["automod"][str(self.guild_id)] = cfg; save_data(d)
        await self.refresh_message(interaction)
        add_log(self.guild_id, "automod_config_change", interaction.user.id, None, {"key":"anti_invite","value":cfg["anti_invite"]})

    @discord.ui.button(label="Toggle Blocked Words", style=discord.ButtonStyle.secondary)
    async def toggle_blocked(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Not your session.", ephemeral=True)
        cfg = ensure_guild_automod(self.guild_id)
        cfg["blocked_words_enabled"] = not cfg.get("blocked_words_enabled", True)
        d = reload_data(); d["automod"][str(self.guild_id)] = cfg; save_data(d)
        await self.refresh_message(interaction)
        add_log(self.guild_id, "automod_config_change", interaction.user.id, None, {"key":"blocked_words_enabled","value":cfg["blocked_words_enabled"]})

    @discord.ui.button(label="Manage Trusted", style=discord.ButtonStyle.success)
    async def manage_trusted(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Not your session.", ephemeral=True)
        # send modal to add/remove trusted via message (we'll give followup commands)
        await interaction.response.send_message("Use `/add_trusted` or `/remove_trusted` to modify trusted list. Or use `/show_trusted` to view them.", ephemeral=True)

# automod command
@bot.hybrid_command(name="automod", with_app_command=True, description="Open automod control panel (admin)")
async def automod_cmd(ctx: commands.Context):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    view = AutomodView(ctx.guild.id, ctx.author.id)
    cfg = ensure_guild_automod(ctx.guild.id)
    emb = discord.Embed(title="Automod Control Panel", color=discord.Color.yellow())
    emb.add_field(name="Anti Link", value=str(cfg.get("anti_link")), inline=True)
    emb.add_field(name="Anti Invite", value=str(cfg.get("anti_invite")), inline=True)
    emb.add_field(name="Blocked Words", value=str(cfg.get("blocked_words_enabled")), inline=True)
    spam = cfg.get("anti_spam", {})
    emb.add_field(name="Anti Spam", value=f"enabled={spam.get('enabled')} action={spam.get('action')} threshold={spam.get('threshold')}", inline=False)
    emb.add_field(name="Trusted users", value=str(len(cfg.get("trusted_users",[]))), inline=False)
    await ctx.reply(embed=emb, view=view, mention_author=False)
    add_log(ctx.guild.id, "command", ctx.author.id, None, {"cmd":"automod"})

# Manage trusted users (admin)
@bot.hybrid_command(name="add_trusted", with_app_command=True, description="Add trusted user (admin) — bypasses message filters")
async def add_trusted_cmd(ctx: commands.Context, user: discord.User):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    cfg = ensure_guild_automod(ctx.guild.id)
    if int(user.id) not in cfg.get("trusted_users", []):
        cfg.setdefault("trusted_users", []).append(int(user.id))
        d = reload_data()
        d.setdefault("automod", {})[str(ctx.guild.id)] = cfg
        save_data(d)
        await ctx.reply(f"✅ {user.mention} added to trusted users.", mention_author=False)
        add_log(ctx.guild.id, "trusted_add", ctx.author.id, user.id, {})
        await send_log_embed_for_entry({
            "ts": datetime.now(timezone.utc).isoformat(),
            "guild_id": str(ctx.guild.id),
            "action": "trusted_add",
            "actor_id": ctx.author.id,
            "target_id": user.id,
            "details": {}
        })
    else:
        await ctx.reply("User already trusted.", mention_author=False)

@bot.hybrid_command(name="remove_trusted", with_app_command=True, description="Remove trusted user (admin)")
async def remove_trusted_cmd(ctx: commands.Context, user: discord.User):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    cfg = ensure_guild_automod(ctx.guild.id)
    if int(user.id) in cfg.get("trusted_users", []):
        cfg["trusted_users"].remove(int(user.id))
        d = reload_data()
        d.setdefault("automod", {})[str(ctx.guild.id)] = cfg
        save_data(d)
        await ctx.reply(f"✅ {user.mention} removed from trusted users.", mention_author=False)
        add_log(ctx.guild.id, "trusted_remove", ctx.author.id, user.id, {})
        await send_log_embed_for_entry({
            "ts": datetime.now(timezone.utc).isoformat(),
            "guild_id": str(ctx.guild.id),
            "action": "trusted_remove",
            "actor_id": ctx.author.id,
            "target_id": user.id,
            "details": {}
        })
    else:
        await ctx.reply("User not in trusted list.", mention_author=False)

@bot.hybrid_command(name="show_trusted", with_app_command=True, description="Show trusted users for this guild")
async def show_trusted_cmd(ctx: commands.Context):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    cfg = ensure_guild_automod(ctx.guild.id)
    arr = cfg.get("trusted_users", [])
    if not arr:
        return await ctx.reply("No trusted users.", mention_author=False)
    mentions = ", ".join(f"<@{x}>" for x in arr)
    await ctx.reply(f"Trusted users: {mentions}", mention_author=False)

# -----------------------
# Cat, fun and admin commands (abridged but functional)
# -----------------------
@bot.hybrid_command(name="cat", with_app_command=True, description="Send random cat image")
async def cat_cmd(ctx: commands.Context):
    await ctx.defer()
    async with aiohttp.ClientSession() as s:
        headers = {"x-api-key": CAT_API_KEY} if CAT_API_KEY else {}
        try:
            async with s.get("https://api.thecatapi.com/v1/images/search", headers=headers, timeout=15) as r:
                if r.status == 200:
                    j = await r.json()
                    url = j[0].get("url") if j else "https://cataas.com/cat"
                else:
                    url = "https://cataas.com/cat"
        except Exception:
            url = "https://cataas.com/cat"
    await ctx.reply(url, mention_author=False)
    add_log(ctx.guild.id if ctx.guild else None, "cat", ctx.author.id, ctx.author.id, {"url": url})

@bot.hybrid_command(name="say", with_app_command=True, description="Bot repeats text (no pings)")
async def say_cmd(ctx: commands.Context, *, text: str):
    safe = sanitize_no_ping(text)
    await ctx.send(safe, allowed_mentions=discord.AllowedMentions.none())
    await ctx.reply("✅ Sent (no pings).", ephemeral=True, mention_author=False)
    add_log(ctx.guild.id if ctx.guild else None, "say_public", ctx.author.id, None, {"text": safe})

@bot.hybrid_command(name="say_admin", with_app_command=True, description="Admin say (pings allowed)")
async def say_admin_cmd(ctx: commands.Context, *, text: str):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    await ctx.channel.send(text)
    await ctx.reply("✅ Sent.", mention_author=False)
    add_log(ctx.guild.id, "say_admin", ctx.author.id, None, {"text": text})

# moderation commands (ban/kick/purge) — kept simple
@bot.hybrid_command(name="purge", with_app_command=True, description="Purge up to 100 messages (admin/pookie)")
async def purge_cmd(ctx: commands.Context, amount: Optional[int] = 10):
    if not (is_admin(ctx.author.id) or is_pookie(ctx.author.id)):
        return await ctx.reply("Admins/Pookie only.", mention_author=False)
    amount = max(1, min(100, amount or 10))
    try:
        deleted = await ctx.channel.purge(limit=amount)
        m = await ctx.send(f"🧹 Deleted {len(deleted)} messages.")
        await asyncio.sleep(3)
        await m.delete()
        add_log(ctx.guild.id if ctx.guild else None, "purge", ctx.author.id, None, {"count": len(deleted), "channel": str(ctx.channel)})
        await send_log_embed_for_entry({
            "ts": datetime.now(timezone.utc).isoformat(),
            "guild_id": str(ctx.guild.id),
            "action": "purge",
            "actor_id": ctx.author.id,
            "target_id": None,
            "details": {"count": len(deleted), "channel": str(ctx.channel)}
        })
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)

# Debug (admin)
@bot.hybrid_command(name="debug", with_app_command=True, description="Debug info (admin)")
async def debug_cmd(ctx: commands.Context):
    if not is_admin(ctx.author.id):
        return await ctx.reply("Admins only.", mention_author=False)
    try:
        import psutil
        p = psutil.Process(os.getpid())
        mem = f"{p.memory_info().rss/(1024*1024):.1f} MiB"
    except Exception:
        mem = "psutil missing"
    delta = datetime.now(timezone.utc) - BOT_START
    emb = discord.Embed(title="Debug", color=discord.Color.teal())
    emb.add_field(name="Uptime", value=str(delta))
    emb.add_field(name="Guilds", value=str(len(bot.guilds)))
    emb.add_field(name="Latency", value=f"{round(bot.latency*1000)} ms")
    emb.add_field(name="Memory", value=mem)
    emb.add_field(name="Python", value=platform.python_version())
    emb.add_field(name="discord.py", value=discord.__version__)
    await ctx.reply(embed=emb, mention_author=False)
    add_log(ctx.guild.id if ctx.guild else None, "debug", ctx.author.id, None, {})

# -----------------------
# Ensure owner & run
# -----------------------
# make sure owner in admins
dtemp = reload_data()
if OWNER_ID not in dtemp.get("admins", []):
    dtemp["admins"].append(OWNER_ID)
save_data(dtemp)

BOT_START = datetime.now(timezone.utc)

def run_bot():
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    run_bot()
