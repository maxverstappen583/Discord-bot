# main.py
# Monolithic Discord bot + Flask keep-alive for Render
# Features:
# - Prefix ? and slash commands
# - Owner/Admin/Pookie permissions
# - Blacklist users, blocked words (exact matches only)
# - Logs: commands, deletes/edits, joins/leaves, moderation
# - Warn system (permanent until removed), view/remove
# - Auto-responder triggers (exact word; {user} -> mention)
# - Say (no pings) / Say Admin (pings allowed for admins/pookies/owner)
# - Purge (<=100)
# - Cat command (TheCatAPI) + daily 11:00 IST + hourly configurable
# - Snipe / E-Snipe with ⬅️ ➡️ buttons
# - Role tools (give/remove/temp duration 10m/12h/4d)
# - Ban/Kick (admin/pookie/owner)
# - Server info, servers list (+invite attempt)
# - Showcommands (permission-filtered, categorized)
# - Restart Render (API), Refresh commands, Eval (owner), Debug
# - JSON persistence
# - Flask keep-alive server (/ and /health)

import os, json, asyncio, re, random, logging, traceback, time, threading, math, platform
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

import aiohttp
import psutil
import pytz

# ---------- ENV ----------
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")  # replace on Render
OWNER_ID = int(os.getenv("OWNER_ID", "1319292111325106296"))
CAT_API_KEY = os.getenv("CAT_API_KEY", "")
TZ = os.getenv("TZ", "Asia/Kolkata")
RENDER_API_KEY = os.getenv("RENDER_API_KEY", "")
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "")

IST = pytz.timezone(TZ)

# ---------- STORAGE ----------
DB_FILE = "data.json"

def _default_guild_data() -> Dict[str, Any]:
    return {
        "admins": [],
        "pookies": [],
        "blacklist": [],
        "blocked_words": [],            # exact words (case-insensitive word boundary)
        "log_channel_id": None,
        "logs": [],                     # list of dicts; recent first
        "warns": {},                    # user_id -> list of warn dicts
        "triggers": {},                 # word -> response
        "cat_daily_channel_id": None,   # daily 11:00 IST
        "cat_hourly": {                 # hourly auto-post
            "enabled": False,
            "channel_id": None,
            "interval_hours": 1,        # default 1 hour
            "last_sent": 0
        },
        "snipes": {},                   # channel_id -> list of deleted msgs
        "esnipes": {}                   # channel_id -> list of edits
    }

def load_db() -> Dict[str, Any]:
    if not os.path.exists(DB_FILE):
        return {"guilds": {}}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return {"guilds": {}}

def save_db():
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)

def gdata(guild_id: int) -> Dict[str, Any]:
    s = str(guild_id)
    if s not in DB["guilds"]:
        DB["guilds"][s] = _default_guild_data()
        save_db()
    return DB["guilds"][s]

DB = load_db()

# ---------- FLASK KEEP-ALIVE ----------
# Render doesn't need it strictly, but you asked to include Flask.
from flask import Flask
app = Flask(__name__)

@app.route("/")
def root():
    return "OK", 200

@app.route("/health")
def health():
    return "healthy", 200

def run_flask():
    # Bind to 0.0.0.0 for Render
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False, use_reloader=False)

flask_thread = threading.Thread(target=run_flask, daemon=True)
flask_thread.start()

# ---------- DISCORD SETUP ----------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
bot = commands.Bot(command_prefix="?", intents=intents, help_command=None)
bot.remove_command("help")  # double-safety

bot.start_time = datetime.now(timezone.utc)
last_error_trace = ""

# ---------- UTILS ----------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def is_owner(user: discord.abc.User) -> bool:
    return int(user.id) == OWNER_ID

async def is_admin_or_pookie(user: discord.abc.User, guild: Optional[discord.Guild] = None) -> bool:
    if is_owner(user):
        return True
    if guild is None:
        return False
    gd = gdata(guild.id)
    return str(user.id) in gd["admins"] or str(user.id) in gd["pookies"]

async def is_admin_pookie_or_owner(user: discord.abc.User, guild: Optional[discord.Guild]) -> bool:
    return await is_admin_or_pookie(user, guild) or is_owner(user)

def log_event(guild_id: int, kind: str, detail: str):
    gd = gdata(guild_id)
    entry = {
        "time": now_utc().isoformat(),
        "type": kind,
        "detail": detail
    }
    gd["logs"].insert(0, entry)
    gd["logs"] = gd["logs"][:1000]  # cap
    save_db()

def sanitize_no_mentions(text: str) -> str:
    # disable @mentions in normal say by inserting zero-width space
    text = text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    text = re.sub(r"<@&(\d+)>", r"<@\u200b&\1>", text)
    text = re.sub(r"<@!?(\d+)>", r"<@\u200b\1>", text)
    return text

def word_boundary_match(message_content: str, word: str) -> bool:
    # exact word match, case-insensitive
    pattern = rfr"\b{re.escape(word)}\b"
    return re.search(pattern, message_content, re.IGNORECASE) is not None

def parse_duration(text: str) -> Optional[int]:
    # returns seconds from strings like 10m, 12h, 4d
    m = re.fullmatch(r"(\d+)([smhd])", text.strip().lower())
    if not m: return None
    val = int(m.group(1)); unit = m.group(2)
    mult = {"s":1, "m":60, "h":3600, "d":86400}[unit]
    return val * mult

async def send_cat(session: aiohttp.ClientSession, channel: discord.TextChannel):
    # Try to fetch an image/video from TheCatAPI
    params = {
        "limit": 1,
        "size": "full",
        "mime_types": "jpg,png,gif,mp4"
    }
    headers = {"x-api-key": CAT_API_KEY} if CAT_API_KEY else {}
    url = "https://api.thecatapi.com/v1/images/search"
    try:
        async with session.get(url, params=params, headers=headers, timeout=20) as resp:
            data = await resp.json()
            if isinstance(data, list) and data:
                link = data[0].get("url")
                if link:
                    await channel.send(link)
                    return True
    except Exception as e:
        print("Cat API error:", e)
    # fallback fun text
    await channel.send("Meow! 🐾 (Couldn’t fetch a cat picture right now.)")
    return False

# ---------- EVENTS ----------
@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
    except Exception as e:
        print("Slash sync error:", e)
    print(f"Logged in as {bot.user} | {len(bot.guilds)} guilds")
    daily_cat_loop.start()
    hourly_cat_loop.start()

@bot.event
async def on_guild_join(guild: discord.Guild):
    _ = gdata(guild.id)  # ensure
    save_db()
    log_event(guild.id, "guild_join", f"Joined {guild.name} ({guild.id})")

@bot.event
async def on_member_join(member: discord.Member):
    log_event(member.guild.id, "member_join", f"{member} ({member.id}) joined")

@bot.event
async def on_member_remove(member: discord.Member):
    log_event(member.guild.id, "member_leave", f"{member} ({member.id}) left")

@bot.event
async def on_message_delete(message: discord.Message):
    if not message.guild or message.author.bot:
        return
    gd = gdata(message.guild.id)
    ch_id = str(message.channel.id)
    gd["snipes"].setdefault(ch_id, [])
    info = {
        "author_id": str(message.author.id),
        "author_name": str(message.author),
        "content": message.content,
        "created_at": message.created_at.isoformat() if message.created_at else "",
        "deleted_at": now_utc().isoformat(),
        "attachments": [a.url for a in message.attachments]
    }
    gd["snipes"][ch_id].insert(0, info)
    gd["snipes"][ch_id] = gd["snipes"][ch_id][:50]
    save_db()
    log_event(message.guild.id, "delete", f"Msg by {message.author} in #{message.channel}: {message.content[:180]}")

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if not before.guild or before.author.bot:
        return
    if before.content == after.content:
        return
    gd = gdata(before.guild.id)
    ch_id = str(before.channel.id)
    gd["esnipes"].setdefault(ch_id, [])
    info = {
        "author_id": str(before.author.id),
        "author_name": str(before.author),
        "before": before.content,
        "after": after.content,
        "edited_at": now_utc().isoformat()
    }
    gd["esnipes"][ch_id].insert(0, info)
    gd["esnipes"][ch_id] = gd["esnipes"][ch_id][:50]
    save_db()
    log_event(before.guild.id, "edit", f"Msg edit by {before.author} in #{before.channel}")

@bot.event
async def on_message(message: discord.Message):
    # blocked words exact-word auto-delete (skip admins/pookies/owner)
    if message.guild and not message.author.bot:
        gd = gdata(message.guild.id)
        if not await is_admin_pookie_or_owner(message.author, message.guild):
            for w in gd["blocked_words"]:
                if word_boundary_match(message.content, w):
                    try:
                        await message.delete()
                        await message.channel.send(f"{message.author.mention} that word is not allowed.", delete_after=5)
                        log_event(message.guild.id, "blocked", f"Deleted blocked word by {message.author}")
                    except:
                        pass
                    return

        # auto-responder (exact word)
        for trig, resp in gd["triggers"].items():
            if word_boundary_match(message.content, trig):
                out = resp.replace("{user}", message.author.mention)
                await message.channel.send(out)
                break

    await bot.process_commands(message)

# ---------- PERMISSION DECORATORS ----------
def owner_only():
    async def predicate(inter: discord.Interaction):
        return is_owner(inter.user)
    return app_commands.check(predicate)

def admin_or_pookie_only():
    async def predicate(inter: discord.Interaction):
        return await is_admin_or_pookie(inter.user, inter.guild)
    return app_commands.check(predicate)

# ---------- CATEGORY MAP FOR SHOWCOMMANDS ----------
CMD_CATEGORIES = {
    "Admin": [],
    "Moderation": [],
    "Utility": [],
    "Fun": [],
    "Cats": [],
    "Info": [],
    "Owner": []
}
def register_cmd(name: str, cat: str):
    CMD_CATEGORIES.setdefault(cat, [])
    if name not in CMD_CATEGORIES[cat]:
        CMD_CATEGORIES[cat].append(name)

# ---------- BASIC /showcommands ----------
@bot.tree.command(name="showcommands", description="Show commands you can use, grouped by category.")
async def showcommands(inter: discord.Interaction):
    can_admin = await is_admin_or_pookie(inter.user, inter.guild)
    is_own = is_owner(inter.user)
    lines = []
    for cat, cmds in CMD_CATEGORIES.items():
        filtered = []
        for c in cmds:
            if cat == "Owner" and not is_own:
                continue
            if cat in ("Admin","Moderation") and not (can_admin or is_own):
                continue
            filtered.append(c)
        if filtered:
            lines.append(f"**{cat}**: " + ", ".join(sorted(f"/{x}" for x in filtered)))
    if not lines:
        await inter.response.send_message("You have no available commands.", ephemeral=True)
    else:
        await inter.response.send_message("\n".join(lines), ephemeral=True)

register_cmd("showcommands", "Utility")

# ---------- LOG CHANNEL & VIEW ----------
@bot.tree.command(name="setlogchannel", description="Set the log channel (Admin/Pookie/Owner).")
@admin_or_pookie_only()
async def setlogchannel(inter: discord.Interaction, channel: discord.TextChannel):
    gd = gdata(inter.guild.id)
    gd["log_channel_id"] = channel.id
    save_db()
    await inter.response.send_message(f"Log channel set to {channel.mention}", ephemeral=True)
register_cmd("setlogchannel", "Admin")

@bot.tree.command(name="logs", description="Show recent logs (default 10).")
async def logs_cmd(inter: discord.Interaction, amount: Optional[int] = 10):
    gd = gdata(inter.guild.id)
    amount = max(1, min(50, amount or 10))
    items = gd["logs"][:amount]
    if not items:
        return await inter.response.send_message("No logs yet.", ephemeral=True)
    desc = []
    for e in items:
        ts = e.get("time","")
        desc.append(f"`{ts}` **{e.get('type','?')}** — {e.get('detail','')}")
    embed = discord.Embed(title=f"Last {len(items)} logs", description="\n".join(desc), color=discord.Color.blurple())
    await inter.response.send_message(embed=embed, ephemeral=True)
register_cmd("logs", "Utility")

# ---------- ADMIN/POOKIE MANAGEMENT ----------
@bot.tree.command(name="addadmin", description="Add an admin (Owner only)")
@owner_only()
async def addadmin(inter: discord.Interaction, user: discord.User):
    gd = gdata(inter.guild.id)
    if str(user.id) not in gd["admins"]:
        gd["admins"].append(str(user.id))
        save_db()
    await inter.response.send_message(f"Added **{user}** as admin.", ephemeral=True)
register_cmd("addadmin", "Owner")

@bot.tree.command(name="removeadmin", description="Remove an admin (Owner only)")
@owner_only()
async def removeadmin(inter: discord.Interaction, user: discord.User):
    gd = gdata(inter.guild.id)
    if str(user.id) in gd["admins"]:
        gd["admins"].remove(str(user.id))
        save_db()
    await inter.response.send_message(f"Removed **{user}** from admins.", ephemeral=True)
register_cmd("removeadmin", "Owner")

@bot.tree.command(name="listadmin", description="List admins")
async def listadmin(inter: discord.Interaction):
    gd = gdata(inter.guild.id)
    if not gd["admins"]:
        return await inter.response.send_message("No admins set.", ephemeral=True)
    mentions = [f"<@{uid}>" for uid in gd["admins"]]
    await inter.response.send_message("Admins: " + ", ".join(mentions))
register_cmd("listadmin", "Info")

@bot.tree.command(name="addpookie", description="Add a pookie (Owner/Admin only)")
@admin_or_pookie_only()
async def addpookie(inter: discord.Interaction, user: discord.User):
    gd = gdata(inter.guild.id)
    if str(user.id) not in gd["pookies"]:
        gd["pookies"].append(str(user.id))
        save_db()
    await inter.response.send_message(f"Added **{user}** as pookie.", ephemeral=True)
register_cmd("addpookie", "Admin")

@bot.tree.command(name="removepookie", description="Remove a pookie (Owner/Admin only)")
@admin_or_pookie_only()
async def removepookie(inter: discord.Interaction, user: discord.User):
    gd = gdata(inter.guild.id)
    if str(user.id) in gd["pookies"]:
        gd["pookies"].remove(str(user.id))
        save_db()
    await inter.response.send_message(f"Removed **{user}** from pookies.", ephemeral=True)
register_cmd("removepookie", "Admin")

@bot.tree.command(name="listpookie", description="List pookies")
async def listpookie(inter: discord.Interaction):
    gd = gdata(inter.guild.id)
    if not gd["pookies"]:
        return await inter.response.send_message("No pookies set.", ephemeral=True)
    mentions = [f"<@{uid}>" for uid in gd["pookies"]]
    await inter.response.send_message("Pookies: " + ", ".join(mentions))
register_cmd("listpookie", "Info")

# ---------- BLACKLIST & BLOCKED WORDS ----------
@bot.tree.command(name="blacklist", description="Blacklist a user (Admin/Pookie/Owner)")
@admin_or_pookie_only()
async def blacklist_cmd(inter: discord.Interaction, user: discord.User):
    gd = gdata(inter.guild.id)
    if str(user.id) not in gd["blacklist"]:
        gd["blacklist"].append(str(user.id))
        save_db()
    await inter.response.send_message(f"Blacklisted {user}.", ephemeral=True)
register_cmd("blacklist", "Moderation")

@bot.tree.command(name="unblacklist", description="Remove a user from blacklist (Admin/Pookie/Owner)")
@admin_or_pookie_only()
async def unblacklist_cmd(inter: discord.Interaction, user: discord.User):
    gd = gdata(inter.guild.id)
    if str(user.id) in gd["blacklist"]:
        gd["blacklist"].remove(str(user.id))
        save_db()
    await inter.response.send_message(f"Un-blacklisted {user}.", ephemeral=True)
register_cmd("unblacklist", "Moderation")

@bot.tree.command(name="blockedword_add", description="Add a blocked word (exact match) (Admin/Pookie/Owner)")
@admin_or_pookie_only()
async def blockedword_add(inter: discord.Interaction, word: str):
    gd = gdata(inter.guild.id)
    w = word.strip().lower()
    if w and w not in gd["blocked_words"]:
        gd["blocked_words"].append(w)
        save_db()
    await inter.response.send_message(f"Added blocked word: `{w}`", ephemeral=True)
register_cmd("blockedword_add", "Moderation")

@bot.tree.command(name="blockedword_remove", description="Remove a blocked word (Admin/Pookie/Owner)")
@admin_or_pookie_only()
async def blockedword_remove(inter: discord.Interaction, word: str):
    gd = gdata(inter.guild.id)
    w = word.strip().lower()
    if w in gd["blocked_words"]:
        gd["blocked_words"].remove(w)
        save_db()
    await inter.response.send_message(f"Removed blocked word: `{w}`", ephemeral=True)
register_cmd("blockedword_remove", "Moderation")

@bot.tree.command(name="blockedword_list", description="List blocked words")
async def blockedword_list(inter: discord.Interaction):
    gd = gdata(inter.guild.id)
    if not gd["blocked_words"]:
        return await inter.response.send_message("No blocked words.", ephemeral=True)
    await inter.response.send_message("Blocked words: " + ", ".join(f"`{w}`" for w in gd["blocked_words"]), ephemeral=True)
register_cmd("blockedword_list", "Info")

# ---------- WARNS ----------
@bot.tree.command(name="warn", description="Warn a user (Admin/Pookie/Owner)")
@admin_or_pookie_only()
async def warn_cmd(inter: discord.Interaction, user: discord.User, reason: Optional[str] = "No reason"):
    gd = gdata(inter.guild.id)
    l = gd["warns"].setdefault(str(user.id), [])
    w = {"by": str(inter.user.id), "reason": reason or "No reason", "time": now_utc().isoformat()}
    l.append(w); save_db()
    log_event(inter.guild.id, "warn", f"{user} warned by {inter.user}: {reason}")
    await inter.response.send_message(f"Warned {user.mention}: **{reason}**")
register_cmd("warn", "Moderation")

@bot.tree.command(name="show_warns", description="Show a user's warns")
async def show_warns(inter: discord.Interaction, user: discord.User):
    gd = gdata(inter.guild.id)
    lst = gd["warns"].get(str(user.id), [])
    if not lst:
        return await inter.response.send_message(f"{user} has no warns.", ephemeral=True)
    lines = []
    for i, w in enumerate(lst, 1):
        lines.append(f"**{i}.** by <@{w['by']}> at `{w['time']}` — {w['reason']}")
    await inter.response.send_message("\n".join(lines), ephemeral=True)
register_cmd("show_warns", "Moderation")

@bot.tree.command(name="remove_warn", description="Remove a warn by index or 'all' (Admin/Pookie/Owner)")
@admin_or_pookie_only()
async def remove_warn(inter: discord.Interaction, user: discord.User, index_or_all: str):
    gd = gdata(inter.guild.id)
    k = str(user.id)
    if k not in gd["warns"] or not gd["warns"][k]:
        return await inter.response.send_message("No warns found.", ephemeral=True)
    if index_or_all.lower() == "all":
        gd["warns"][k] = []
        save_db()
        return await inter.response.send_message(f"Removed all warns for {user}.", ephemeral=True)
    if index_or_all.isdigit():
        idx = int(index_or_all) - 1
        if 0 <= idx < len(gd["warns"][k]):
            removed = gd["warns"][k].pop(idx)
            save_db()
            return await inter.response.send_message(f"Removed warn {idx+1}: {removed['reason']}", ephemeral=True)
    await inter.response.send_message("Invalid index.", ephemeral=True)
register_cmd("remove_warn", "Moderation")

# ---------- SAY & PURGE ----------
@bot.tree.command(name="say", description="Make the bot say something (no pings)")
async def say_cmd(inter: discord.Interaction, text: str):
    out = sanitize_no_mentions(text)
    await inter.response.send_message("✅ Sent.", ephemeral=True)
    await inter.channel.send(out)
register_cmd("say", "Utility")

@bot.tree.command(name="say_admin", description="Admin say (mentions allowed)")
@admin_or_pookie_only()
async def say_admin(inter: discord.Interaction, text: str):
    await inter.response.send_message("✅ Sent.", ephemeral=True)
    await inter.channel.send(text, allowed_mentions=discord.AllowedMentions(everyone=True, users=True, roles=True))
register_cmd("say_admin", "Admin")

@bot.tree.command(name="purge", description="Delete recent messages (<= 100)")
@admin_or_pookie_only()
async def purge_cmd(inter: discord.Interaction, amount: int):
    amount = max(1, min(100, amount))
    await inter.response.defer(ephemeral=True, thinking=True)
    deleted = await inter.channel.purge(limit=amount)
    await inter.followup.send(f"Deleted {len(deleted)} messages.", ephemeral=True)
register_cmd("purge", "Moderation")

# ---------- TRIGGERS ----------
@bot.tree.command(name="trigger_add", description="Admin: add trigger: when users send WORD, bot replies with RESPONSE")
@admin_or_pookie_only()
async def trigger_add(inter: discord.Interaction, word: str, response: str):
    gd = gdata(inter.guild.id)
    gd["triggers"][word.strip().lower()] = response
    save_db()
    await inter.response.send_message(f"Added trigger `{word}` -> `{response}`", ephemeral=True)
register_cmd("trigger_add", "Admin")

@bot.tree.command(name="trigger_remove", description="Admin: remove trigger by exact word")
@admin_or_pookie_only()
async def trigger_remove(inter: discord.Interaction, word: str):
    gd = gdata(inter.guild.id)
    if gd["triggers"].pop(word.strip().lower(), None) is None:
        return await inter.response.send_message("No such trigger.", ephemeral=True)
    save_db()
    await inter.response.send_message(f"Removed trigger `{word}`", ephemeral=True)
register_cmd("trigger_remove", "Admin")

@bot.tree.command(name="showtrigger", description="Show all triggers (exact-word)")
async def showtrigger(inter: discord.Interaction):
    gd = gdata(inter.guild.id)
    if not gd["triggers"]:
        return await inter.response.send_message("No triggers set.", ephemeral=True)
    lines = [f"`{w}` → {r}" for w, r in gd["triggers"].items()]
    await inter.response.send_message("\n".join(lines), ephemeral=True)
register_cmd("showtrigger", "Utility")

# ---------- FUN ----------
JOKES = [
    "Why did the developer go broke? Because he used up all his cache.",
    "I would tell you a UDP joke, but you might not get it.",
    "Why do Java developers wear glasses? Because they don’t C#."
]
DAD_JOKES = [
    "I used to be a baker, then I couldn't make enough dough.",
    "I’m reading a book on anti-gravity. It’s impossible to put down!",
    "What do you call fake spaghetti? An impasta."
]

@bot.tree.command(name="joke", description="Random joke")
async def joke_cmd(inter: discord.Interaction):
    await inter.response.send_message(random.choice(JOKES))
register_cmd("joke", "Fun")

@bot.tree.command(name="dadjoke", description="Random dad joke")
async def dadjoke_cmd(inter: discord.Interaction):
    await inter.response.send_message(random.choice(DAD_JOKES))
register_cmd("dadjoke", "Fun")

@bot.tree.command(name="8ball", description="Magic 8-ball")
async def eight_ball(inter: discord.Interaction, question: str):
    answers = ["Yes.", "No.", "Maybe.", "Absolutely!", "Ask again later.", "Definitely not."]
    await inter.response.send_message(f"🎱 {random.choice(answers)}")
register_cmd("8ball", "Fun")

@bot.tree.command(name="coinflip", description="Flip a coin")
async def coinflip(inter: discord.Interaction):
    await inter.response.send_message("🪙 " + random.choice(["Heads", "Tails"]))
register_cmd("coinflip", "Fun")

@bot.tree.command(name="rolldice", description="Roll a die (1-6)")
async def rolldice(inter: discord.Interaction):
    await inter.response.send_message(f"🎲 {random.randint(1,6)}")
register_cmd("rolldice", "Fun")

@bot.tree.command(name="rps", description="Rock, Paper, Scissors vs bot")
async def rps(inter: discord.Interaction, move: str):
    move = move.lower()
    if move not in ("rock","paper","scissors"):
        return await inter.response.send_message("Choose one of: rock, paper, scissors", ephemeral=True)
    botm = random.choice(["rock","paper","scissors"])
    result = "Draw!"
    win = {("rock","scissors"), ("scissors","paper"), ("paper","rock")}
    if (move, botm) in win:
        result = "You win!"
    elif (botm, move) in win:
        result = "I win!"
    await inter.response.send_message(f"You: **{move}** | Me: **{botm}** → **{result}**")
register_cmd("rps", "Fun")

# ---------- CAT ----------
@bot.tree.command(name="cat", description="Random cat picture/video")
async def cat_cmd(inter: discord.Interaction):
    await inter.response.defer(thinking=True)
    async with aiohttp.ClientSession() as session:
        ok = False
        if inter.guild:
            log_event(inter.guild.id, "cat", f"cat command by {inter.user}")
        # fetch
        if isinstance(inter.channel, discord.TextChannel):
            ok = await send_cat(session, inter.channel)
        if not ok:
            await inter.followup.send("Couldn’t fetch a cat now.", ephemeral=True)
        else:
            await inter.followup.send("Meow!", ephemeral=True)
register_cmd("cat", "Cats")

@bot.tree.command(name="setdailycatchannel", description="Set channel for daily cat at 11:00 IST")
@admin_or_pookie_only()
async def setdailycatchannel(inter: discord.Interaction, channel: discord.TextChannel):
    gd = gdata(inter.guild.id)
    gd["cat_daily_channel_id"] = channel.id
    save_db()
    await inter.response.send_message(f"Daily cat channel set to {channel.mention}", ephemeral=True)
register_cmd("setdailycatchannel", "Cats")

@bot.tree.command(name="sethourlycatchannel", description="Enable hourly cats and choose channel + interval (hours)")
@admin_or_pookie_only()
async def sethourlycatchannel(inter: discord.Interaction, channel: discord.TextChannel, interval_hours: Optional[int] = 1, enabled: Optional[bool] = True):
    gd = gdata(inter.guild.id)
    h = gd["cat_hourly"]
    h["channel_id"] = channel.id
    h["interval_hours"] = max(1, min(24, interval_hours or 1))
    h["enabled"] = bool(enabled)
    save_db()
    await inter.response.send_message(f"Hourly cats **{'enabled' if h['enabled'] else 'disabled'}** in {channel.mention} every {h['interval_hours']}h", ephemeral=True)
register_cmd("sethourlycatchannel", "Cats")

# ---------- CAT TASKS ----------
@tasks.loop(minutes=1)
async def daily_cat_loop():
    # send at 11:00 IST (local per TZ env)
    try:
        now_local = datetime.now(IST)
        if now_local.hour == 11 and now_local.minute == 0:
            async with aiohttp.ClientSession() as session:
                for guild in bot.guilds:
                    gd = gdata(guild.id)
                    ch_id = gd.get("cat_daily_channel_id")
                    if not ch_id: continue
                    ch = guild.get_channel(ch_id)
                    if isinstance(ch, discord.TextChannel):
                        try:
                            await send_cat(session, ch)
                            log_event(guild.id, "daily_cat", f"Sent daily cat to #{ch.name}")
                        except Exception as e:
                            print("daily cat error:", e)
        # don't spam: it runs every minute; only minute==0 triggers
    except Exception as e:
        print("daily loop error:", e)

@tasks.loop(minutes=1)
async def hourly_cat_loop():
    # multi-guild hourly scheduling
    try:
        now_ts = time.time()
        async with aiohttp.ClientSession() as session:
            for guild in bot.guilds:
                gd = gdata(guild.id)
                h = gd["cat_hourly"]
                if not h["enabled"] or not h["channel_id"]:
                    continue
                interval = max(1, int(h.get("interval_hours", 1))) * 3600
                last = float(h.get("last_sent", 0))
                if now_ts - last >= interval:
                    ch = guild.get_channel(h["channel_id"])
                    if isinstance(ch, discord.TextChannel):
                        try:
                            ok = await send_cat(session, ch)
                            if ok:
                                h["last_sent"] = now_ts
                                save_db()
                                log_event(guild.id, "hourly_cat", f"Sent hourly cat to #{ch.name}")
                        except Exception as e:
                            print("hourly cat error:", e)
    except Exception as e:
        print("hourly loop fail:", e)

# ---------- SNIPE / E-SNIPE ----------
class NavView(discord.ui.View):
    def __init__(self, items: List[Dict[str, Any]]):
        super().__init__(timeout=60)
        self.items_list = items
        self.idx = 0

    def format_embed(self) -> discord.Embed:
        it = self.items_list[self.idx]
        embed = discord.Embed(color=discord.Color.orange())
        # Deleted
        if "content" in it:
            embed.title = f"🗑️ Deleted message ({self.idx+1}/{len(self.items_list)})"
            embed.add_field(name="Author", value=f"<@{it['author_id']}> ({it['author_name']})", inline=False)
            embed.add_field(name="Content", value=it["content"][:1024] or "(no text)", inline=False)
            embed.add_field(name="Deleted at", value=it.get("deleted_at",""), inline=True)
            embed.add_field(name="Sent at", value=it.get("created_at",""), inline=True)
            if it.get("attachments"):
                embed.add_field(name="Attachments", value="\n".join(it["attachments"][:5]), inline=False)
        else:
            embed.title = f"✏️ Edited message ({self.idx+1}/{len(self.items_list)})"
            embed.add_field(name="Author", value=f"<@{it['author_id']}> ({it['author_name']})", inline=False)
            embed.add_field(name="Before", value=(it["before"] or "(empty)")[:1024], inline=False)
            embed.add_field(name="After", value((it["after"] or "(empty)")[:1024]) if len((it["after"] or ""))<=1024 else (it["after"] or "")[:1024], inline=False)
            embed.add_field(name="Edited at", value=it.get("edited_at",""), inline=False)
        return embed

    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.secondary)
    async def left(self, inter: discord.Interaction, button: discord.ui.Button):
        self.idx = (self.idx - 1) % len(self.items_list)
        await inter.response.edit_message(embed=self.format_embed(), view=self)

    @discord.ui.button(label="➡️", style=discord.ButtonStyle.secondary)
    async def right(self, inter: discord.Interaction, button: discord.ui.Button):
        self.idx = (self.idx + 1) % len(self.items_list)
        await inter.response.edit_message(embed=self.format_embed(), view=self)

@bot.tree.command(name="snipe", description="View recently deleted messages in this channel")
async def snipe(inter: discord.Interaction):
    gd = gdata(inter.guild.id)
    items = gd["snipes"].get(str(inter.channel.id), [])
    if not items:
        return await inter.response.send_message("Nothing to snipe.", ephemeral=True)
    view = NavView(items)
    await inter.response.send_message(embed=view.format_embed(), view=view, ephemeral=True)
register_cmd("snipe", "Utility")

@bot.tree.command(name="esnipe", description="View recent message edits in this channel")
async def esnipe(inter: discord.Interaction):
    gd = gdata(inter.guild.id)
    items = gd["esnipes"].get(str(inter.channel.id), [])
    if not items:
        return await inter.response.send_message("Nothing to e-snipe.", ephemeral=True)
    view = NavView(items)
    await inter.response.send_message(embed=view.format_embed(), view=view, ephemeral=True)
register_cmd("esnipe", "Utility")

# ---------- MODERATION ----------
@bot.tree.command(name="kick", description="Kick a member (Admin/Pookie/Owner)")
@admin_or_pookie_only()
async def kick_cmd(inter: discord.Interaction, member: discord.Member, reason: Optional[str] = "No reason"):
    try:
        await member.kick(reason=reason)
        log_event(inter.guild.id, "kick", f"{member} kicked by {inter.user}: {reason}")
        await inter.response.send_message(f"Kicked {member} — {reason}")
    except Exception as e:
        await inter.response.send_message(f"Error: {e}", ephemeral=True)
register_cmd("kick", "Moderation")

@bot.tree.command(name="ban", description="Ban a member (Admin/Pookie/Owner)")
@admin_or_pookie_only()
async def ban_cmd(inter: discord.Interaction, member: discord.Member, reason: Optional[str] = "No reason"):
    try:
        await member.ban(reason=reason, delete_message_days=0)
        log_event(inter.guild.id, "ban", f"{member} banned by {inter.user}: {reason}")
        await inter.response.send_message(f"Banned {member} — {reason}")
    except Exception as e:
        await inter.response.send_message(f"Error: {e}", ephemeral=True)
register_cmd("ban", "Moderation")

@bot.tree.command(name="giverole", description="Give a role to a user (Admin/Pookie/Owner)")
@admin_or_pookie_only()
async def giverole(inter: discord.Interaction, member: discord.Member, role: discord.Role):
    try:
        await member.add_roles(role, reason=f"by {inter.user}")
        log_event(inter.guild.id, "giverole", f"{role.name} → {member}")
        await inter.response.send_message(f"Gave **{role.name}** to {member}.")
    except Exception as e:
        await inter.response.send_message(f"Error: {e}", ephemeral=True)
register_cmd("giverole", "Moderation")

@bot.tree.command(name="removerole", description="Remove a role from a user (Admin/Pookie/Owner)")
@admin_or_pookie_only()
async def removerole(inter: discord.Interaction, member: discord.Member, role: discord.Role):
    try:
        await member.remove_roles(role, reason=f"by {inter.user}")
        log_event(inter.guild.id, "removerole", f"{role.name} x {member}")
        await inter.response.send_message(f"Removed **{role.name}** from {member}.")
    except Exception as e:
        await inter.response.send_message(f"Error: {e}", ephemeral=True)
register_cmd("removerole", "Moderation")

@bot.tree.command(name="temprole", description="Give a temporary role (e.g., 10m/12h/4d)")
@admin_or_pookie_only()
async def temprole(inter: discord.Interaction, member: discord.Member, role: discord.Role, duration: str):
    secs = parse_duration(duration)
    if not secs:
        return await inter.response.send_message("Use duration like `10m`, `12h`, `4d`.", ephemeral=True)
    try:
        await member.add_roles(role, reason=f"temp by {inter.user} for {duration}")
        await inter.response.send_message(f"Gave **{role.name}** to {member} for {duration}.")
        async def remove_later():
            await asyncio.sleep(secs)
            try:
                await member.remove_roles(role, reason="temp expired")
            except: pass
        bot.loop.create_task(remove_later())
    except Exception as e:
        await inter.response.send_message(f"Error: {e}", ephemeral=True)
register_cmd("temprole", "Moderation")

# ---------- SERVER INFO ----------
@bot.tree.command(name="serverinfo", description="Detailed server information")
async def serverinfo(inter: discord.Interaction):
    g = inter.guild
    if not g:
        return await inter.response.send_message("Not in a guild.", ephemeral=True)
    text_channels = len([c for c in g.channels if isinstance(c, discord.TextChannel)])
    voice_channels = len([c for c in g.channels if isinstance(c, discord.VoiceChannel)])
    categories = len([c for c in g.channels if isinstance(c, discord.CategoryChannel)])
    roles = len(g.roles)
    features = ", ".join(g.features) if g.features else "None"
    verification = str(g.verification_level).title()
    explicit = str(g.explicit_content_filter).title()
    boosts = g.premium_subscription_count or 0
    owner = g.owner or (await g.fetch_owner())
    embed = discord.Embed(title=f"{g.name}", color=discord.Color.blurple())
    embed.add_field(name="Owner", value=f"{owner} ({owner.id})", inline=False)
    embed.add_field(name="Created", value=g.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"), inline=True)
    embed.add_field(name="Members", value=str(g.member_count or "?"), inline=True)
    embed.add_field(name="Text/Voice/Categories", value=f"{text_channels}/{voice_channels}/{categories}", inline=True)
    embed.add_field(name="Roles", value=str(roles), inline=True)
    embed.add_field(name="Verification", value=verification, inline=True)
    embed.add_field(name="Filter", value=explicit, inline=True)
    embed.add_field(name="Boosts", value=str(boosts), inline=True)
    embed.add_field(name="Features", value=features, inline=False)
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    await inter.response.send_message(embed=embed)
register_cmd("serverinfo", "Info")

@bot.tree.command(name="servers", description="List servers the bot is in")
async def servers(inter: discord.Interaction):
    rows = []
    for g in bot.guilds:
        owner = g.owner or (await g.fetch_owner())
        rows.append(f"**{g.name}** (`{g.id}`) — Owner: {owner} — Members: {g.member_count}")
    msg = "\n".join(rows) if rows else "No servers."
    await inter.response.send_message(msg, ephemeral=True)
register_cmd("servers", "Info")

# ---------- OWNER / UTILS ----------
@bot.tree.command(name="refresh", description="Refresh (sync) slash commands (Owner)")
@owner_only()
async def refresh(inter: discord.Interaction):
    await bot.tree.sync()
    await inter.response.send_message("Commands refreshed.", ephemeral=True)
register_cmd("refresh", "Owner")

@bot.tree.command(name="restart_render", description="Trigger a deploy/restart on Render (Owner)")
@owner_only()
async def restart_render(inter: discord.Interaction):
    if not (RENDER_API_KEY and RENDER_SERVICE_ID):
        return await inter.response.send_message("RENDER_API_KEY or RENDER_SERVICE_ID not set.", ephemeral=True)
    await inter.response.defer(ephemeral=True, thinking=True)
    import base64, json as _json
    # Render API: POST /v1/services/{serviceId}/deploys
    async with aiohttp.ClientSession() as sess:
        url = f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/deploys"
        headers = {"Authorization": f"Bearer {RENDER_API_KEY}", "Content-Type": "application/json"}
        payload = {"clearCache": True}
        try:
            async with sess.post(url, headers=headers, json=payload, timeout=30) as r:
                txt = await r.text()
                await inter.followup.send(f"Render deploy requested. Status {r.status}:\n```json\n{txt[:1900]}\n```", ephemeral=True)
        except Exception as e:
            await inter.followup.send(f"Error: {e}", ephemeral=True)
register_cmd("restart_render", "Owner")

@bot.tree.command(name="eval", description="Owner-only Python eval")
@owner_only()
async def eval_cmd(inter: discord.Interaction, code: str):
    global last_error_trace
    env = {
        "bot": bot, "discord": discord, "asyncio": asyncio,
        "inter": inter, "DB": DB, "gdata": gdata, "now_utc": now_utc
    }
    try:
        result = eval(code, env, env)
        if asyncio.iscoroutine(result):
            result = await result
        await inter.response.send_message(f"```py\n{result}\n```", ephemeral=True)
    except Exception:
        last_error_trace = traceback.format_exc()
        await inter.response.send_message(f"```py\n{last_error_trace[-1800:]}\n```", ephemeral=True)
register_cmd("eval", "Owner")

@bot.tree.command(name="trace", description="Show last error trace (Owner)")
@owner_only()
async def trace_cmd(inter: discord.Interaction):
    if not last_error_trace:
        return await inter.response.send_message("No trace stored.", ephemeral=True)
    await inter.response.send_message(f"```py\n{last_error_trace[-1900:]}\n```", ephemeral=True)
register_cmd("trace", "Owner")

@bot.tree.command(name="debug", description="Show bot/system debug info")
async def debug_cmd(inter: discord.Interaction):
    if not await is_admin_pookie_or_owner(inter.user, inter.guild):
        return await inter.response.send_message("No permission.", ephemeral=True)
    # uptime
    delta = now_utc() - bot.start_time
    uptime = str(delta).split(".")[0]
    process = psutil.Process(os.getpid())
    cpu = psutil.cpu_percent()
    mem = round(process.memory_info().rss / (1024**2), 2)
    guild_count = len(bot.guilds)
    members = sum((g.member_count or 0) for g in bot.guilds)
    commands_loaded = len(bot.tree.get_commands())
    ping = round(bot.latency*1000)
    embed = discord.Embed(title="🛠 Debug", color=discord.Color.green(), timestamp=now_utc())
    embed.add_field(name="Uptime", value=uptime)
    embed.add_field(name="Ping", value=f"{ping}ms")
    embed.add_field(name="Guilds", value=str(guild_count))
    embed.add_field(name="Members", value=str(members))
    embed.add_field(name="Commands", value=str(commands_loaded))
    embed.add_field(name="CPU", value=f"{cpu}%")
    embed.add_field(name="Memory", value=f"{mem} MB")
    embed.add_field(name="Timezone", value=TZ)
    embed.add_field(name="Owner ID", value=str(OWNER_ID))
    embed.add_field(name="Render API", value=("Configured ✅" if RENDER_API_KEY else "Not set ❌"))
    embed.add_field(name="Python", value=platform.python_version())
    embed.add_field(name="discord.py", value=discord.__version__)
    await inter.response.send_message(embed=embed, ephemeral=True)
register_cmd("debug", "Owner")

# ---------- PREFIX MIRRORS (popular ones) ----------
@bot.command(name="say")
async def p_say(ctx: commands.Context, *, text: str):
    out = sanitize_no_mentions(text)
    await ctx.reply("✅ Sent.", delete_after=3, mention_author=False)
    await ctx.send(out)

@bot.command(name="purge")
@commands.has_permissions(manage_messages=True)
async def p_purge(ctx: commands.Context, amount: int):
    amount = max(1, min(100, amount))
    deleted = await ctx.channel.purge(limit=amount)
    await ctx.send(f"Deleted {len(deleted)} messages.", delete_after=5)

@bot.command(name="snipe")
async def p_snipe(ctx: commands.Context):
    gd = gdata(ctx.guild.id)
    items = gd["snipes"].get(str(ctx.channel.id), [])
    if not items:
        return await ctx.reply("Nothing to snipe.", mention_author=False)
    it = items[0]
    embed = discord.Embed(title="🗑️ Deleted", color=discord.Color.orange())
    embed.add_field(name="Author", value=f"<@{it['author_id']}> ({it['author_name']})", inline=False)
    embed.add_field(name="Content", value=it["content"][:1024] or "(no text)", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="cat")
async def p_cat(ctx: commands.Context):
    async with aiohttp.ClientSession() as session:
        await send_cat(session, ctx.channel)

# ---------- RUN ----------
if __name__ == "__main__":
    # Friendly startup print
    print("Starting bot with Flask keep-alive…")
    # Login
    bot.run(DISCORD_TOKEN)
