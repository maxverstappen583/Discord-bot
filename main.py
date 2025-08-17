# main.py - Monster Bot (combined / and ? prefix)
# Owner ID: 1319292111325106296
# Env vars used:
#   DISCORD_TOKEN (required)
#   CAT_API_KEY (optional)
#   RENDER_API_KEY (optional, for restart_render)
#   RENDER_SERVICE_ID (optional, for restart_render)
#   TZ (optional, defaults to Asia/Kolkata for daily cat scheduling)
#
# NOTE: Set these env vars in Render or your runtime.

import os
import sys
import json
import random
import asyncio
import textwrap
import traceback
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import pytz
import aiohttp
import logging
import psutil

import discord
from discord.ext import commands, tasks
from discord import app_commands, Interaction, Embed, AllowedMentions

# simple Flask health endpoint (for Render / UptimeRobot)
from flask import Flask
from threading import Thread

# -------------------------
# Logging to file (so /trace_logs can read it)
# -------------------------
LOGFILE = "bot.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(LOGFILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("bot")

# -------------------------
# Basic config
# -------------------------
OWNER_ID = 1319292111325106296
PREFIX = "?"
DATA_FILE = "botdata.json"
COMMANDS_META_FILE = "commands.json"

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CAT_API_KEY = os.getenv("CAT_API_KEY", "")
RENDER_API_KEY = os.getenv("RENDER_API_KEY", "")
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "")
TZ_NAME = os.getenv("TZ", "Asia/Kolkata")

if not DISCORD_TOKEN:
    logger.error("DISCORD_TOKEN is not set. Exiting.")
    sys.exit(1)

intents = discord.Intents.all()
intents.message_content = True

# Bot that supports both prefix and slash
bot = commands.Bot(command_prefix=commands.when_mentioned_or(PREFIX), intents=intents, help_command=None)
tree = bot.tree

ALLOW_NONE = AllowedMentions(everyone=False, roles=False, users=False)
ALLOW_ALL  = AllowedMentions(everyone=True, roles=True, users=True)

# -------------------------
# Flask health server (so Render health check / UptimeRobot works)
# -------------------------
app = Flask("uptime")

@app.route("/")
def home():
    return "OK - bot is running"

def run_flask():
    app.run(host="0.0.0.0", port=8080)

Thread(target=run_flask, daemon=True).start()

# -------------------------
# Persistence / defaults
# -------------------------
DEFAULT_DATA = {
    "admins": [],           # list of user id strings
    "pookies": [],          # list of user id strings
    "blacklist": [],        # list of user id strings
    "blocked_words": [],    # list of words
    "logs": [],             # list of logs
    "log_channel": None,    # single channel id string
    "triggers": {},         # word -> reply
    "daily_cat_channel": None,
    "hourly_cat_channel": None,
    "custom_commands": {},  # name -> reply
    "_started_at": None
}

def ensure_file(path: str, default: Any):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=4)

ensure_file(DATA_FILE, DEFAULT_DATA)
ensure_file(COMMANDS_META_FILE, {})

def load_data() -> Dict[str, Any]:
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = dict(DEFAULT_DATA)
    # ensure keys exist
    for k,v in DEFAULT_DATA.items():
        if k not in d:
            d[k] = v
    return d

def save_data(d: Dict[str, Any]):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=4)

def load_commands_meta() -> Dict[str, Any]:
    try:
        with open(COMMANDS_META_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_commands_meta(meta: Dict[str, Any]):
    with open(COMMANDS_META_FILE, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=4)

data = load_data()
commands_meta = load_commands_meta()
if not commands_meta:
    # basic metadata for showcommands
    commands_meta = {
        "cat": {"category":"Fun","access":"public","desc":"Random cat image"},
        "roll": {"category":"Fun","access":"public","desc":"Roll NdM dice"},
        "coinflip": {"category":"Fun","access":"public","desc":"Flip a coin"},
        "eightball": {"category":"Fun","access":"public","desc":"8-ball"},
        "joke": {"category":"Fun","access":"public","desc":"Tell a joke"},
        "dadjoke": {"category":"Fun","access":"public","desc":"Dad joke"},
        "rps": {"category":"Fun","access":"public","desc":"Rock paper scissors"},
        "snipe": {"category":"Utility","access":"public","desc":"See deleted messages"},
        "esnipe": {"category":"Utility","access":"public","desc":"See edited messages"},
        "say": {"category":"General","access":"public","desc":"Public say (no pings)"},
        "say_admin": {"category":"General","access":"admin","desc":"Admin say (pings allowed)"},
        "trigger_add": {"category":"Moderation","access":"admin","desc":"Add trigger"},
        "trigger_remove": {"category":"Moderation","access":"admin","desc":"Remove trigger"},
        "logs": {"category":"Moderation","access":"admin","desc":"Show logs"},
        "setdailycatchannel": {"category":"Cats","access":"admin","desc":"Set daily cat channel"},
        "sethourlycatchannel": {"category":"Cats","access":"admin","desc":"Set hourly cat channel"},
        "restart": {"category":"System","access":"admin","desc":"Restart by closing (Render restarts)"},
        "restart_render": {"category":"System","access":"admin","desc":"Trigger Render deploy (restart)"},
        "refresh_commands": {"category":"System","access":"admin","desc":"Refresh slash commands"},
        "eval": {"category":"System","access":"owner","desc":"Owner-only eval"},
        "debug": {"category":"System","access":"owner","desc":"Owner-only debug"},
        "trace_logs": {"category":"System","access":"owner","desc":"Show last lines of bot.log"}
    }
    save_commands_meta(commands_meta)

# -------------------------
# Permission helpers
# -------------------------
def is_owner(uid: int) -> bool:
    return uid == OWNER_ID

def is_pookie(uid: int) -> bool:
    return str(uid) in data.get("pookies", [])

def is_admin(uid: int) -> bool:
    return str(uid) in data.get("admins", [])

def has_staff(uid: int) -> bool:
    return is_owner(uid) or is_pookie(uid) or is_admin(uid)

def is_blacklisted(uid: int) -> bool:
    return str(uid) in data.get("blacklist", [])

# -------------------------
# Logging helper (appends to data["logs"] and to log file)
# -------------------------
def log_action(user: discord.abc.User, command_name: str, channel: Optional[discord.abc.GuildChannel]):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = {"user": f"{user} ({user.id})", "user_id": str(user.id), "command": command_name, "channel": str(channel) if channel else "DM", "time": ts}
    data.setdefault("logs", []).append(entry)
    if len(data["logs"]) > 2000:
        data["logs"] = data["logs"][-2000:]
    save_data(data)
    logger.info(f"CMD {command_name} by {user} in {entry['channel']}")

# -------------------------
# Blocked words detection with bypass normalization
# -------------------------
def normalize_alnum(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())

def contains_blocked(content: str) -> bool:
    compact = normalize_alnum(content)
    for w in data.get("blocked_words", []):
        if not w: continue
        if normalize_alnum(w) in compact:
            return True
    return False

# -------------------------
# Snipe structures
# -------------------------
class SnipeEntry:
    def __init__(self, author: discord.User, content: str, time: datetime, avatar_url: str):
        self.author = author; self.content = content; self.time = time; self.avatar_url = avatar_url

class EditEntry:
    def __init__(self, author: discord.User, before: str, after: str, time: datetime, avatar_url: str):
        self.author = author; self.before = before; self.after = after; self.time = time; self.avatar_url = avatar_url

SNIPES: Dict[int, List[SnipeEntry]] = {}
ESNIPES: Dict[int, List[EditEntry]] = {}

def push_snipe(channel_id: int, entry: SnipeEntry):
    lst = SNIPES.setdefault(channel_id, [])
    lst.insert(0, entry)
    if len(lst) > 100: lst.pop()

def push_esnipe(channel_id: int, entry: EditEntry):
    lst = ESNIPES.setdefault(channel_id, [])
    lst.insert(0, entry)
    if len(lst) > 100: lst.pop()

# pager for snipe/esnipe
class Pager(discord.ui.View):
    def __init__(self, kind: str, channel_id: int, index: int = 0, timeout: int = 120):
        super().__init__(timeout=timeout)
        self.kind = kind
        self.channel_id = channel_id
        self.index = index
        self.prev = discord.ui.Button(style=discord.ButtonStyle.secondary, emoji="⬅️")
        self.next = discord.ui.Button(style=discord.ButtonStyle.secondary, emoji="➡️")
        self.prev.callback = self.on_prev
        self.next.callback = self.on_next
        self.add_item(self.prev); self.add_item(self.next)
        self.update_buttons()

    def length(self) -> int:
        return len(SNIPES.get(self.channel_id, [])) if self.kind == "snipe" else len(ESNIPES.get(self.channel_id, []))

    def update_buttons(self):
        total = self.length()
        if total == 0:
            self.prev.disabled = True; self.next.disabled = True
        else:
            self.prev.disabled = (self.index >= total - 1)
            self.next.disabled = (self.index <= 0)

    async def on_prev(self, interaction: Interaction):
        if self.index < self.length() - 1: self.index += 1
        self.update_buttons()
        embed = build_snipe_embed(self.channel_id, self.index) if self.kind == "snipe" else build_esnipe_embed(self.channel_id, self.index)
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_next(self, interaction: Interaction):
        if self.index > 0: self.index -= 1
        self.update_buttons()
        embed = build_snipe_embed(self.channel_id, self.index) if self.kind == "snipe" else build_esnipe_embed(self.channel_id, self.index)
        await interaction.response.edit_message(embed=embed, view=self)

def build_snipe_embed(channel_id: int, index: int) -> Embed:
    e = SNIPES[channel_id][index]
    embed = Embed(title="💀 Snipe", description=e.content or "*<no content>*", color=discord.Color.red(), timestamp=e.time)
    embed.set_author(name=str(e.author), icon_url=e.avatar_url)
    embed.set_footer(text=f"{index+1}/{len(SNIPES[channel_id])}")
    return embed

def build_esnipe_embed(channel_id: int, index: int) -> Embed:
    e = ESNIPES[channel_id][index]
    desc = f"**Before:** {e.before or '*<no content>*'}\n**After:** {e.after or '*<no content>*'}"
    embed = Embed(title="✏️ E-Snipe", description=desc, color=discord.Color.orange(), timestamp=e.time)
    embed.set_author(name=str(e.author), icon_url=e.avatar_url)
    embed.set_footer(text=f"{index+1}/{len(ESNIPES[channel_id])}")
    return embed

# -------------------------
# Events
# -------------------------
@bot.event
async def on_ready():
    if not data.get("_started_at"):
        data["_started_at"] = datetime.utcnow().isoformat()
        save_data(data)
    logger.info(f"Logged in as {bot.user} ({bot.user.id})")
    try:
        await tree.sync()
        logger.info("Slash commands synced.")
    except Exception as e:
        logger.exception("Slash sync failed.")
    # ensure owner is admin for convenience
    if str(OWNER_ID) not in data.get("admins", []):
        data.setdefault("admins", []).append(str(OWNER_ID))
        save_data(data)
    # start tasks
    if not daily_cat_conditional.is_running(): daily_cat_conditional.start()
    if not hourly_cat_task.is_running(): hourly_cat_task.start()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if is_blacklisted(message.author.id):
        return
    # blocked words check
    if contains_blocked(message.content):
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        await message.channel.send("🚫 That word is not allowed here.", delete_after=5)
        return
    # triggers exact match
    if message.guild:
        content = message.content.strip().lower()
        for k,v in data.get("triggers", {}).items():
            if content == k.strip().lower():
                response = v.replace("{user}", message.author.mention)
                await message.channel.send(response, allowed_mentions=AllowedMentions(everyone=False, roles=False, users=True))
                break
    # custom commands: single-word exact match
    if message.guild:
        first = message.content.strip().split(" ",1)[0].lower()
        if first in data.get("custom_commands", {}):
            reply = data["custom_commands"][first]
            await message.channel.send(reply.replace("{user}", message.author.mention), allowed_mentions=AllowedMentions(everyone=False, roles=False, users=True))
            return
    await bot.process_commands(message)

@bot.event
async def on_message_delete(message: discord.Message):
    if not message.guild or message.author.bot: return
    push_snipe(message.channel.id, SnipeEntry(message.author, message.content or "", datetime.utcnow(), message.author.display_avatar.url))

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if not before.guild or before.author.bot: return
    push_esnipe(before.channel.id, EditEntry(before.author, before.content or "", after.content or "", datetime.utcnow(), before.author.display_avatar.url))
    if contains_blocked(after.content):
        try: await after.delete()
        except discord.Forbidden: pass
        await before.channel.send("🚫 That word is not allowed here.", delete_after=5)

@bot.event
async def on_member_join(member: discord.Member):
    log_action(member, "member_join", member.guild)

@bot.event
async def on_member_remove(member: discord.Member):
    log_action(member, "member_leave", member.guild)

# -------------------------
# Cat fetching
# -------------------------
async def fetch_cat_url() -> Optional[str]:
    url = "https://api.thecatapi.com/v1/images/search"
    headers = {"x-api-key": CAT_API_KEY} if CAT_API_KEY else {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    j = await resp.json()
                    if isinstance(j, list) and j:
                        return j[0].get("url")
    except Exception as e:
        logger.exception("fetch_cat_url failed")
        return None
    return None

# -------------------------
# Tasks: daily (11:00 IST) and hourly cat
# -------------------------
@tasks.loop(minutes=1)
async def daily_cat_conditional():
    try:
        tz = pytz.timezone(TZ_NAME)
        now = datetime.now(tz)
        if now.hour == 11 and now.minute == 0:
            chid = data.get("daily_cat_channel")
            if chid:
                ch = bot.get_channel(int(chid))
                if ch:
                    url = await fetch_cat_url()
                    if url:
                        try: await ch.send(f"📅 **Daily Cat (11:00 IST)**\n{url}")
                        except Exception: logger.exception("Failed send daily cat")
    except Exception:
        logger.exception("daily_cat_conditional error")

@tasks.loop(minutes=60)
async def hourly_cat_task():
    try:
        chid = data.get("hourly_cat_channel")
        if not chid: return
        ch = bot.get_channel(int(chid))
        if not ch: return
        url = await fetch_cat_url()
        if url:
            try: await ch.send(f"⏰ **Hourly Cat**\n{url}")
            except Exception: logger.exception("Failed send hourly cat")
    except Exception:
        logger.exception("hourly_cat_task error")

# -------------------------
# showcommands (dynamic)
# -------------------------
@tree.command(name="showcommands", description="Show commands you can use (grouped by category)")
async def slash_showcommands(interaction: Interaction):
    meta = load_commands_meta()
    uid = interaction.user.id
    if is_owner(uid): level = "owner"
    elif is_pookie(uid): level = "pookie"
    elif is_admin(uid): level = "admin"
    else: level = "public"
    categories: Dict[str, List[str]] = {}
    for cname, info in meta.items():
        cat = info.get("category","Other"); access = info.get("access","public")
        allowed = False
        if access == "public": allowed = True
        elif access == "admin" and level in ("admin","pookie","owner"): allowed = True
        elif access == "pookie" and level in ("pookie","owner"): allowed = True
        elif access == "owner" and level == "owner": allowed = True
        if allowed:
            categories.setdefault(cat, []).append(f"/{cname} — {info.get('desc','')}")
    if not categories:
        return await interaction.response.send_message("No commands available for you.", ephemeral=True)
    embed = Embed(title="Available commands", color=discord.Color.blue())
    for cat, items in categories.items():
        embed.add_field(name=cat, value="\n".join(items[:25]), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# -------------------------
# Say / Say_admin (slash + prefix)
# -------------------------
@tree.command(name="say", description="Public say (mentions disabled). Use {user} to mention author.")
@app_commands.describe(message="Message to send")
async def slash_say(interaction: Interaction, message: str):
    safe = message.replace("@everyone", "everyone").replace("@here","here").replace("{user}", interaction.user.mention)
    await interaction.response.send_message(safe, allowed_mentions=ALLOW_NONE)
    log_action(interaction.user, "/say", interaction.channel)

@tree.command(name="say_admin", description="Admin/pookie say (mentions allowed).")
@app_commands.describe(message="Message to send")
async def slash_say_admin(interaction: Interaction, message: str):
    if not has_staff(interaction.user.id):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    msg = message.replace("{user}", interaction.user.mention)
    await interaction.response.send_message(msg, allowed_mentions=ALLOW_ALL)
    log_action(interaction.user, "/say_admin", interaction.channel)

@bot.command(name="say")
async def prefix_say(ctx: commands.Context, *, message: str):
    safe = message.replace("@everyone","everyone").replace("@here","here").replace("{user}", ctx.author.mention)
    await ctx.send(safe, allowed_mentions=ALLOW_NONE)
    log_action(ctx.author, "?say", ctx.channel)

@bot.command(name="say_admin")
@commands.check(lambda ctx: has_staff(ctx.author.id))
async def prefix_say_admin(ctx: commands.Context, *, message: str):
    msg = message.replace("{user}", ctx.author.mention)
    await ctx.send(msg, allowed_mentions=ALLOW_ALL)
    log_action(ctx.author, "?say_admin", ctx.channel)

# -------------------------
# Fun: cat, roll, coinflip, 8ball, jokes, dadjoke, rps
# -------------------------
@tree.command(name="cat", description="Get a random cat image")
async def slash_cat(interaction: Interaction):
    url = await fetch_cat_url()
    if not url: return await interaction.response.send_message("Couldn't fetch a cat right now 😿")
    await interaction.response.send_message(url); log_action(interaction.user, "/cat", interaction.channel)

@bot.command(name="cat")
async def prefix_cat(ctx):
    url = await fetch_cat_url()
    if not url: return await ctx.send("Couldn't fetch a cat right now 😿")
    await ctx.send(url); log_action(ctx.author, "?cat", ctx.channel)

@tree.command(name="roll", description="Roll NdM (e.g., 2d6)")
@app_commands.describe(spec="Format: NdM (e.g., 2d6)")
async def slash_roll(interaction: Interaction, spec: str):
    try:
        n, m = spec.lower().split("d"); rolls = int(n); sides = int(m)
        if rolls < 1 or sides < 1 or rolls > 50:
            raise ValueError
    except Exception:
        return await interaction.response.send_message("Use format like `2d6` (1 ≤ rolls ≤ 50).", ephemeral=True)
    results = [random.randint(1, sides) for _ in range(rolls)]
    await interaction.response.send_message(f"🎲 Rolls: {results} → total: **{sum(results)}**"); log_action(interaction.user, f"/roll {spec}", interaction.channel)

@tree.command(name="coinflip", description="Flip a coin")
async def slash_coin(interaction: Interaction):
    await interaction.response.send_message(random.choice(["🪙 Heads", "🪙 Tails"])); log_action(interaction.user, "/coinflip", interaction.channel)

@tree.command(name="eightball", description="Ask the 8-ball")
@app_commands.describe(question="Your question")
async def slash_8ball(interaction: Interaction, question: str):
    answers = ["It is certain.", "Without a doubt.", "You may rely on it.", "Most likely.", "Outlook good.", "Yes.", "Reply hazy, try again.", "Ask again later.", "Better not tell you now.", "Don't count on it.", "My reply is no.", "Very doubtful."]
    await interaction.response.send_message(f"🎱 {random.choice(answers)}"); log_action(interaction.user, "/eightball", interaction.channel)

@tree.command(name="joke", description="Get a random joke")
async def slash_joke(interaction: Interaction):
    jokes = ["Why did the chicken cross the road? To get to the other side!", "I told my computer I needed a break — it said 'No problem, I'll go to sleep'.", "There are 10 types of people: those who understand binary and those who don't."]
    await interaction.response.send_message(random.choice(jokes)); log_action(interaction.user, "/joke", interaction.channel)

@tree.command(name="dadjoke", description="A corny dad joke")
async def slash_dadjoke(interaction: Interaction):
    jokes = ["I'm reading a book on anti-gravity. It's impossible to put down!", "I used to be a baker, then I kneaded a change."]
    await interaction.response.send_message(random.choice(jokes)); log_action(interaction.user, "/dadjoke", interaction.channel)

@tree.command(name="rps", description="Rock Paper Scissors")
@app_commands.choices(choice=[app_commands.Choice(name="rock", value="rock"), app_commands.Choice(name="paper", value="paper"), app_commands.Choice(name="scissors", value="scissors")])
async def slash_rps(interaction: Interaction, choice: app_commands.Choice[str]):
    c = choice.value; bot_choice = random.choice(["rock","paper","scissors"])
    if c == bot_choice: res = "Tie!"
    elif (c=="rock" and bot_choice=="scissors") or (c=="scissors" and bot_choice=="paper") or (c=="paper" and bot_choice=="rock"): res = "You win!"
    else: res = "I win!"
    await interaction.response.send_message(f"You: **{c}**, Me: **{bot_choice}** → **{res}**"); log_action(interaction.user, "/rps", interaction.channel)

# -------------------------
# Snipe / Esnipe (slash)
# -------------------------
@tree.command(name="snipe", description="Show deleted messages in this channel")
async def slash_snipe(interaction: Interaction):
    cid = interaction.channel.id
    if cid not in SNIPES or not SNIPES[cid]:
        return await interaction.response.send_message("Nothing to snipe here.", ephemeral=True)
    embed = build_snipe_embed(cid, 0); view = Pager("snipe", cid)
    await interaction.response.send_message(embed=embed, view=view); log_action(interaction.user, "/snipe", interaction.channel)

@tree.command(name="esnipe", description="Show edited messages in this channel")
async def slash_esnipe(interaction: Interaction):
    cid = interaction.channel.id
    if cid not in ESNIPES or not ESNIPES[cid]:
        return await interaction.response.send_message("Nothing to esnipe here.", ephemeral=True)
    embed = build_esnipe_embed(cid, 0); view = Pager("esnipe", cid)
    await interaction.response.send_message(embed=embed, view=view); log_action(interaction.user, "/esnipe", interaction.channel)

# -------------------------
# Triggers (exact match)
# -------------------------
@tree.command(name="trigger_add", description="Admin/Pookie: add/update trigger")
async def slash_trigger_add(interaction: Interaction, word: str, reply: str):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    data.setdefault("triggers", {})[word] = reply; save_data(data)
    await interaction.response.send_message(f"✅ Trigger set: `{word}` → `{reply}`"); log_action(interaction.user, "/trigger_add", interaction.channel)

@tree.command(name="trigger_remove", description="Admin/Pookie: remove trigger")
async def slash_trigger_remove(interaction: Interaction, word: str):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    if word in data.get("triggers", {}):
        data["triggers"].pop(word); save_data(data)
        await interaction.response.send_message(f"🗑️ Removed `{word}`")
    else:
        await interaction.response.send_message("No such trigger.")
    log_action(interaction.user, "/trigger_remove", interaction.channel)

@tree.command(name="showtrigger", description="Show triggers")
async def slash_showtrigger(interaction: Interaction):
    if not data.get("triggers"): return await interaction.response.send_message("No triggers set.")
    lines = [f"`{k}` → {v}" for k,v in data["triggers"].items()]
    await interaction.response.send_message("**Triggers:**\n" + "\n".join(lines))

# -------------------------
# Logs & log channel
# -------------------------
@tree.command(name="logs", description="Show recent logs (staff only)")
async def slash_logs(interaction: Interaction, amount: int = 10):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    amount = max(1, min(100, amount))
    logs = data.get("logs", [])[-amount:]
    if not logs: return await interaction.response.send_message("No logs yet.")
    embed = Embed(title=f"Last {amount} logs", color=discord.Color.blue())
    for e in logs:
        embed.add_field(name=e["command"], value=f"{e['user']} in {e['channel']} at {e['time']}", inline=False)
    await interaction.response.send_message(embed=embed); log_action(interaction.user, f"/logs {amount}", interaction.channel)

@tree.command(name="set_log_channel", description="Admin/Pookie: set a channel to receive command logs")
async def slash_set_log_channel(interaction: Interaction, channel: discord.TextChannel):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    data["log_channel"] = str(channel.id); save_data(data)
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}"); log_action(interaction.user, "/set_log_channel", interaction.channel)

@tree.command(name="remove_log_channel", description="Admin/Pookie: disable log channel")
async def slash_remove_log_channel(interaction: Interaction):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    data["log_channel"] = None; save_data(data)
    await interaction.response.send_message("🛑 Log channel mirroring disabled."); log_action(interaction.user, "/remove_log_channel", interaction.channel)

# -------------------------
# Admin & Pookie management
# -------------------------
@tree.command(name="add_admin", description="Owner: add an admin")
async def slash_add_admin(interaction: Interaction, user: discord.User):
    if not is_owner(interaction.user.id): return await interaction.response.send_message("Owner only.", ephemeral=True)
    if str(user.id) not in data.get("admins", []):
        data.setdefault("admins", []).append(str(user.id)); save_data(data)
    await interaction.response.send_message(f"✅ {user.mention} added as admin."); log_action(interaction.user, "/add_admin", interaction.channel)

@tree.command(name="remove_admin", description="Owner: remove an admin")
async def slash_remove_admin(interaction: Interaction, user: discord.User):
    if not is_owner(interaction.user.id): return await interaction.response.send_message("Owner only.", ephemeral=True)
    if str(user.id) in data.get("admins", []):
        data["admins"].remove(str(user.id)); save_data(data)
    await interaction.response.send_message(f"✅ {user.mention} removed from admin."); log_action(interaction.user, "/remove_admin", interaction.channel)

@tree.command(name="show_admins", description="Show admins (pings)")
async def slash_show_admins(interaction: Interaction):
    admins = data.get("admins", [])
    if not admins: return await interaction.response.send_message("No admins set.")
    await interaction.response.send_message("👑 Admins:\n" + "\n".join(f"<@{a}>" for a in admins))

@tree.command(name="add_pookie", description="Owner/Admin: add pookie")
async def slash_add_pookie(interaction: Interaction, user: discord.User):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    if str(user.id) not in data.get("pookies", []):
        data.setdefault("pookies", []).append(str(user.id)); save_data(data)
    await interaction.response.send_message(f"✅ {user.mention} added as Pookie."); log_action(interaction.user, "/add_pookie", interaction.channel)

@tree.command(name="remove_pookie", description="Owner/Admin: remove pookie")
async def slash_remove_pookie(interaction: Interaction, user: discord.User):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    if str(user.id) in data.get("pookies", []):
        data["pookies"].remove(str(user.id)); save_data(data)
    await interaction.response.send_message(f"✅ {user.mention} removed from Pookie."); log_action(interaction.user, "/remove_pookie", interaction.channel)

@tree.command(name="list_pookie", description="List Pookie users (pings)")
async def slash_list_pookie(interaction: Interaction):
    pks = data.get("pookies", [])
    if not pks: return await interaction.response.send_message("No Pookie users.")
    await interaction.response.send_message("🍪 Pookies:\n" + "\n".join(f"<@{p}>" for p in pks))

# -------------------------
# Moderation (ban/kick/blacklist)
# -------------------------
@tree.command(name="blacklist", description="Admin/Pookie: blacklist a user")
async def slash_blacklist(interaction: Interaction, user: discord.User):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    if str(user.id) not in data.get("blacklist", []):
        data.setdefault("blacklist", []).append(str(user.id)); save_data(data)
    await interaction.response.send_message(f"🚫 {user.mention} blacklisted."); log_action(interaction.user, "/blacklist", interaction.channel)

@tree.command(name="unblacklist", description="Admin/Pookie: remove from blacklist")
async def slash_unblacklist(interaction: Interaction, user: discord.User):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    if str(user.id) in data.get("blacklist", []):
        data["blacklist"].remove(str(user.id)); save_data(data)
    await interaction.response.send_message(f"✅ {user.mention} removed from blacklist."); log_action(interaction.user, "/unblacklist", interaction.channel)

@tree.command(name="ban", description="Admin/Pookie: ban a member")
async def slash_ban(interaction: Interaction, user: discord.User, reason: Optional[str] = "No reason provided"):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    if not interaction.guild: return await interaction.response.send_message("Guild only.", ephemeral=True)
    try:
        await interaction.guild.ban(user, reason=reason)
        await interaction.response.send_message(f"🔨 Banned {user.mention}. Reason: {reason}")
        log_action(interaction.user, f"/ban {user.id}", interaction.channel)
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to ban that user.", ephemeral=True)

@tree.command(name="kick", description="Admin/Pookie: kick a member")
async def slash_kick(interaction: Interaction, member: discord.Member, reason: Optional[str] = "No reason provided"):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(f"👢 Kicked {member.mention}. Reason: {reason}")
        log_action(interaction.user, f"/kick {member.id}", interaction.channel)
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to kick that member.", ephemeral=True)

# -------------------------
# Blocked words management
# -------------------------
@tree.command(name="add_blocked_word", description="Admin/Pookie: add blocked word")
async def slash_add_blocked(interaction: Interaction, word: str):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    if word not in data.get("blocked_words", []):
        data.setdefault("blocked_words", []).append(word); save_data(data)
    await interaction.response.send_message(f"🚫 '{word}' added to blocked words."); log_action(interaction.user, "/add_blocked_word", interaction.channel)

@tree.command(name="remove_blocked_word", description="Admin/Pookie: remove blocked word")
async def slash_remove_blocked(interaction: Interaction, word: str):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    try:
        data["blocked_words"].remove(word); save_data(data)
        await interaction.response.send_message(f"✅ '{word}' removed.")
    except ValueError:
        await interaction.response.send_message("That word is not in the blocked list.")
    log_action(interaction.user, "/remove_blocked_word", interaction.channel)

@tree.command(name="show_blocked_words", description="Show blocked words")
async def slash_show_blocked(interaction: Interaction):
    bw = data.get("blocked_words", [])
    if not bw: return await interaction.response.send_message("No blocked words configured.")
    await interaction.response.send_message("🚫 Blocked words:\n" + ", ".join(f"`{w}`" for w in bw))

# -------------------------
# Cat channel setters
# -------------------------
@tree.command(name="setdailycatchannel", description="Admin/Pookie: set daily 11:00 IST cat channel")
async def slash_setdailycat(interaction: Interaction, channel: discord.TextChannel):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    data["daily_cat_channel"] = str(channel.id); save_data(data)
    await interaction.response.send_message(f"✅ Daily cat channel set to {channel.mention}"); log_action(interaction.user, "/setdailycatchannel", interaction.channel)

@tree.command(name="sethourlycatchannel", description="Admin/Pookie: set hourly cat channel")
async def slash_sethourlycat(interaction: Interaction, channel: discord.TextChannel):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    data["hourly_cat_channel"] = str(channel.id); save_data(data)
    await interaction.response.send_message(f"✅ Hourly cat channel set to {channel.mention}"); log_action(interaction.user, "/sethourlycatchannel", interaction.channel)

@tree.command(name="removehourlycatchannel", description="Admin/Pookie: disable hourly cat channel")
async def slash_removehourlycat(interaction: Interaction):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    data["hourly_cat_channel"] = None; save_data(data)
    await interaction.response.send_message("🛑 Hourly cat posting disabled."); log_action(interaction.user, "/removehourlycatchannel", interaction.channel)

# -------------------------
# User info & avatar
# -------------------------
@tree.command(name="avatar", description="Show a user's avatar")
async def slash_avatar(interaction: Interaction, user: Optional[discord.User] = None):
    user = user or interaction.user
    embed = Embed(title=f"Avatar — {user}", color=discord.Color.green()); embed.set_image(url=user.display_avatar.url)
    await interaction.response.send_message(embed=embed); log_action(interaction.user, "/avatar", interaction.channel)

@tree.command(name="userinfo", description="Show a user's info")
async def slash_userinfo(interaction: Interaction, user: Optional[discord.Member] = None):
    if not interaction.guild: return await interaction.response.send_message("Guild only.", ephemeral=True)
    user = user or interaction.user
    roles = ", ".join(r.mention for r in user.roles if r.name != "@everyone")
    embed = Embed(title=f"User Info — {user}", color=discord.Color.purple())
    embed.add_field(name="ID", value=str(user.id), inline=False)
    embed.add_field(name="Joined", value=str(user.joined_at) if user.joined_at else "N/A", inline=False)
    embed.add_field(name="Created", value=str(user.created_at), inline=False)
    embed.add_field(name="Roles", value=roles or "None", inline=False)
    embed.set_thumbnail(url=user.display_avatar.url)
    await interaction.response.send_message(embed=embed); log_action(interaction.user, "/userinfo", interaction.channel)

# -------------------------
# Restart commands
# -------------------------
@tree.command(name="restart", description="Restart bot by closing process (Render will restart it)")
async def slash_restart(interaction: Interaction):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    await interaction.response.send_message("🔄 Restarting bot (closing process)...", ephemeral=True)
    log_action(interaction.user, "/restart", interaction.channel)
    await bot.close()

@tree.command(name="restart_render", description="Trigger Render deploy (requires RENDER_API_KEY & RENDER_SERVICE_ID)")
async def slash_restart_render(interaction: Interaction):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    if not RENDER_API_KEY or not RENDER_SERVICE_ID:
        return await interaction.response.send_message("Render API credentials not configured.", ephemeral=True)
    url = f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/deploys"
    headers = {"Authorization": f"Bearer {RENDER_API_KEY}", "Accept":"application/json", "Content-Type":"application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json={}) as resp:
            text = await resp.text()
            if resp.status in (200,201,202):
                await interaction.response.send_message("🔄 Render deploy triggered. Service will restart.", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ Failed to trigger deploy: {resp.status}\n{text}", ephemeral=True)
    log_action(interaction.user, "/restart_render", interaction.channel)

# -------------------------
# Refresh commands (resync slash)
# -------------------------
@tree.command(name="refresh_commands", description="Force-refresh slash commands with Discord")
async def slash_refresh_commands(interaction: Interaction):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    try:
        await bot.tree.sync(); await interaction.response.send_message("✅ Commands refreshed with Discord.", ephemeral=True); log_action(interaction.user, "/refresh_commands", interaction.channel)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error refreshing: {e}", ephemeral=True)

# -------------------------
# Eval, debug, trace_logs (owner/pookie)
# -------------------------
def is_owner_or_pookie(user_id: int) -> bool:
    return is_owner(user_id) or is_pookie(user_id)

@tree.command(name="debug", description="Owner/pookie: show bot debug info")
async def slash_debug(interaction: Interaction):
    if not is_owner_or_pookie(interaction.user.id):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    started_iso = data.get("_started_at")
    started = datetime.fromisoformat(started_iso) if started_iso else datetime.utcnow()
    uptime = datetime.utcnow() - started
    try:
        p = psutil.Process(os.getpid())
        mem = f"{p.memory_info().rss/1024**2:.2f} MB"
        cpu = f"{psutil.cpu_percent(interval=0.1):.2f}%"
    except Exception:
        mem = "N/A"; cpu = "N/A"
    embed = Embed(title="Bot Debug", color=discord.Color.green())
    embed.add_field(name="Uptime", value=str(uptime).split(".")[0], inline=False)
    embed.add_field(name="Guilds", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="Users (approx)", value=str(sum(g.member_count for g in bot.guilds)), inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency*1000)} ms", inline=True)
    embed.add_field(name="Memory", value=mem, inline=True)
    embed.add_field(name="CPU", value=cpu, inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True); log_action(interaction.user, "/debug", interaction.channel)

@tree.command(name="eval", description="Owner-only: evaluate Python code (use carefully!)")
@app_commands.describe(code="Python code to evaluate (supports await)")
async def slash_eval(interaction: Interaction, *, code: str):
    if not is_owner(interaction.user.id):
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    code = code.strip("` \n")
    wrapped = "async def __aexec():\n" + textwrap.indent(code, "    ")
    env = {"bot": bot, "discord": discord, "commands": commands, "asyncio": asyncio, "aiohttp": aiohttp, "data": data, "save_data": save_data}
    str_out = ""
    try:
        exec(wrapped, env)
        func = env["__aexec"]
        start = time.perf_counter()
        result = await asyncio.wait_for(func(), timeout=8.0)
        duration = (time.perf_counter() - start) * 1000
        str_out = f"Result (took {duration:.2f}ms):\n{repr(result)}"
    except asyncio.TimeoutError:
        str_out = "Execution timed out (8s)."
    except Exception:
        str_out = traceback.format_exc()
    if len(str_out) > 1900:
        fn = f"eval_{int(time.time())}.txt"
        with open(fn, "w", encoding="utf-8") as f:
            f.write(str_out)
        await interaction.response.send_message("Output too long, sending file.", ephemeral=True)
        await interaction.followup.send(file=discord.File(fn))
        try: os.remove(fn)
        except Exception: pass
    else:
        await interaction.response.send_message(f"```py\n{str_out}\n```", ephemeral=True)
    log_action(interaction.user, "/eval", interaction.channel)

@tree.command(name="trace_logs", description="Owner/pookie: show last lines of the bot log")
async def slash_trace_logs(interaction: Interaction, lines: int = 20):
    if not is_owner_or_pookie(interaction.user.id):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    try:
        with open(LOGFILE, "r", encoding="utf-8") as f:
            all_lines = f.readlines()[-max(1, min(500, lines)):]
        content = "".join(all_lines)
    except FileNotFoundError:
        return await interaction.response.send_message("No log file found.", ephemeral=True)
    if len(content) > 1900:
        import io
        with io.StringIO(content) as file:
            await interaction.response.send_message("Logs are long — sending as file.", ephemeral=True)
            await interaction.followup.send(file=discord.File(file, "trace_logs.txt"))
    else:
        await interaction.response.send_message(f"```log\n{content}\n```", ephemeral=True)
    log_action(interaction.user, "/trace_logs", interaction.channel)

# -------------------------
# Custom commands + interactive Add Modal
# -------------------------
class AddCmdModal(discord.ui.Modal, title="Add Custom Command"):
    name_input = discord.ui.TextInput(label="Command name (single word)", placeholder="hello", max_length=50)
    response_input = discord.ui.TextInput(label="Response (use {user} to mention)", style=discord.TextStyle.paragraph, max_length=2000)
    def __init__(self, invoking_user_id: int):
        super().__init__()
        self.invoking_user_id = invoking_user_id
    async def on_submit(self, interaction: Interaction):
        if not has_staff(interaction.user.id):
            return await interaction.response.send_message("No permission.", ephemeral=True)
        name = self.name_input.value.strip().lower()
        if not name.isalnum():
            return await interaction.response.send_message("Name must be alphanumeric (no spaces).", ephemeral=True)
        data.setdefault("custom_commands", {})[name] = self.response_input.value
        save_data(data)
        await interaction.response.send_message(f"✅ Custom command `{name}` added.", ephemeral=True)
        log_action(interaction.user, "/add_custom_via_modal", interaction.channel)

class AddCmdView(discord.ui.View):
    @discord.ui.button(label="Add custom command", style=discord.ButtonStyle.green)
    async def add_button(self, button: discord.ui.Button, interaction: Interaction):
        if not has_staff(interaction.user.id):
            return await interaction.response.send_message("No permission.", ephemeral=True)
        modal = AddCmdModal(interaction.user.id)
        await interaction.response.send_modal(modal)

@tree.command(name="open_add_command", description="Open a button to add custom commands (Admin/Pookie only)")
async def slash_open_add_command(interaction: Interaction):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    view = AddCmdView(); await interaction.response.send_message("Click to add a custom command (admin/pookie only).", view=view, ephemeral=True)

@tree.command(name="add_custom_command", description="Admin/Pookie: add custom command")
async def slash_add_custom(interaction: Interaction, name: str, response: str):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    name = name.strip().lower(); data.setdefault("custom_commands", {})[name] = response; save_data(data)
    await interaction.response.send_message(f"✅ Custom command `{name}` added."); log_action(interaction.user, "/add_custom_command", interaction.channel)

@tree.command(name="remove_custom_command", description="Admin/Pookie: remove custom command")
async def slash_remove_custom(interaction: Interaction, name: str):
    if not has_staff(interaction.user.id): return await interaction.response.send_message("No permission.", ephemeral=True)
    name = name.strip().lower()
    if name in data.get("custom_commands", {}):
        data["custom_commands"].pop(name); save_data(data)
        await interaction.response.send_message(f"🗑️ Removed custom command `{name}`.")
    else:
        await interaction.response.send_message("No such custom command.")
    log_action(interaction.user, f"/remove_custom_command {name}", interaction.channel)

@tree.command(name="list_custom_commands", description="List custom commands")
async def slash_list_custom(interaction: Interaction):
    cc = data.get("custom_commands", {})
    if not cc: return await interaction.response.send_message("No custom commands set.")
    lines = [f"`{k}` → {v[:100]}{'...' if len(v)>100 else ''}" for k,v in cc.items()]
    await interaction.response.send_message("Custom commands:\n" + "\n".join(lines)); log_action(interaction.user, "/list_custom_commands", interaction.channel)

# -------------------------
# Prefix versions: purge (example), say already above, etc.
# -------------------------
@bot.command(name="purge")
@commands.has_permissions(manage_messages=True)
async def prefix_purge(ctx: commands.Context, amount: int):
    amount = max(1, min(100, amount))
    deleted = await ctx.channel.purge(limit=amount+1)
    await ctx.send(f"🧹 Deleted {len(deleted)-1} messages.", delete_after=5)
    log_action(ctx.author, f"?purge {amount}", ctx.channel)

# -------------------------
# Error handling
# -------------------------
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission for this command.")
    elif isinstance(error, commands.CheckFailure):
        await ctx.send("❌ Check failed (permissions).")
    else:
        logger.exception("Unhandled command error")
        await ctx.send("❌ An error occurred. Check bot logs.")

# -------------------------
# Ensure owner in admins on start
# -------------------------
if __name__ == "__main__":
    # ensure owner is admin
    if str(OWNER_ID) not in data.get("admins", []):
        data.setdefault("admins", []).append(str(OWNER_ID)); save_data(data)
    if not data.get("_started_at"):
        data["_started_at"] = datetime.utcnow().isoformat(); save_data(data)
    # run bot
    bot.run(DISCORD_TOKEN)
