# main.py
# Full single-file Discord bot with prefix + slash, admin/pookie, logs, AFK, snipe/esnipe (with buttons),
# exact-word triggers, cat (random/daily/hourly), guild/user info, avatar, moderation, Render restart,
# command refresh, Flask uptime keepalive, and JSON persistence in one data.json.
#
# Python 3.11+ recommended. Works with discord.py 2.4+.

import os, json, asyncio, traceback, aiohttp, re, platform, psutil, time, signal
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, List

import discord
from discord.ext import commands, tasks
from discord import app_commands

# ------------- Flask Keepalive (for Render UptimeRobot pings) -------------
from flask import Flask
from threading import Thread
keepalive_app = Flask("bot_keepalive")

@keepalive_app.route("/")
def root_alive():
    return "OK", 200

def run_flask():
    port = int(os.getenv("PORT", "10000"))
    keepalive_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

Thread(target=run_flask, daemon=True).start()
# -------------------------------------------------------------------------

# --------------------------- ENV & CONSTANTS ------------------------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN not set")

def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        # If someone accidentally pasted "OWNER_ID = 123" as value, try to pull digits.
        digits = "".join(ch for ch in raw if ch.isdigit())
        if digits:
            return int(digits)
        raise

OWNER_ID = _int_env("OWNER_ID", 1319292111325106296)  # You as default owner
# Hard-default extra admins you asked:
DEFAULT_EXTRA_ADMINS = {1380315427992768633, 909468887098216499}

RENDER_API_KEY = os.getenv("RENDER_API_KEY", "").strip()       # optional
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "").strip() # optional
CAT_API_KEY = os.getenv("CAT_API_KEY", "").strip()             # optional
TZ_NAME = os.getenv("TZ", "Asia/Kolkata").strip() or "Asia/Kolkata"
BOT_TZ = ZoneInfo(TZ_NAME)  # will raise if invalid; use valid tz like Asia/Kolkata

PREFIX = "?"

DATA_FILE = "data.json"
SNIPES_PER_CHANNEL = 50
LOGS_MAX = 5000  # stored in file (for /logs view); channel logs are separate

STREAM_NAME = "Max Verstappen"
STREAM_URL = "https://twitch.tv/twitch"  # Streaming presence needs a URL

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
INTENTS.guilds = True
INTENTS.reactions = True

# --------------------------- BOT SETUP ------------------------------------
class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned_or(PREFIX),
                         intents=INTENTS,
                         help_command=None)
        self.synced = False
        self.start_time = time.time()

    async def setup_hook(self):
        # Sync commands after login in on_ready to ensure caches are ready
        pass

bot = Bot()
tree = bot.tree

# --------------------------- STORAGE LAYER --------------------------------
DEFAULT_DATA = {
    "admins": [],             # user IDs
    "pookie_users": [],       # user IDs
    "blacklist": [],          # user IDs
    "blocked_words": [],      # exact words
    "log_channel": None,      # channel id
    "cat_channel": None,      # channel id for cat posts
    "hourly_cat_channel": None,  # channel id (permanent hourly)
    "triggers": {},           # {"word": "reply"}
    "logs": [],               # recent logs (capped)
    "afk": {},                # {user_id: {"reason": str, "since": iso str}}
}

def load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        data = DEFAULT_DATA.copy()
        data["admins"] = list(DEFAULT_EXTRA_ADMINS | {OWNER_ID})
        save_data(data)
        return data
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except Exception:
            data = DEFAULT_DATA.copy()
    # Ensure all keys exist
    for k, v in DEFAULT_DATA.items():
        data.setdefault(k, v if not isinstance(v, (list, dict)) else type(v)())
    # Ensure owner & default admins present
    if OWNER_ID not in data["admins"]:
        data["admins"].append(OWNER_ID)
    for aid in DEFAULT_EXTRA_ADMINS:
        if aid not in data["admins"]:
            data["admins"].append(aid)
    save_data(data)
    return data

def save_data(data: Dict[str, Any]):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

DATA = load_data()

# --------------------------- UTILS & CHECKS -------------------------------
def is_owner(user: discord.abc.User) -> bool:
    return int(user.id) == int(OWNER_ID)

def is_admin(user: discord.abc.User) -> bool:
    return int(user.id) in set(DATA["admins"]) or is_owner(user)

def is_pookie(user: discord.abc.User) -> bool:
    return int(user.id) in set(DATA["pookie_users"]) or is_admin(user)

def is_blacklisted(user: discord.abc.User) -> bool:
    return int(user.id) in set(DATA["blacklist"])

def exact_word_present(text: str, word: str) -> bool:
    # Whole word match, case-insensitive, no substring leaks
    pattern = r"\b" + re.escape(word) + r"\b"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None

async def send_log_embed(guild: Optional[discord.Guild], title: str, description: str, color=discord.Color.blurple(), fields: List[tuple]=None):
    ch_id = DATA.get("log_channel")
    if not ch_id:
        return
    channel = None
    if guild:
        channel = guild.get_channel(ch_id) or (bot.get_channel(ch_id) if bot.get_channel(ch_id) and bot.get_channel(ch_id).guild == guild else None)
    if not channel:
        channel = bot.get_channel(ch_id)
    if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    emb = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
    if fields:
        for name, value, inline in fields:
            emb.add_field(name=name, value=value, inline=inline)
    try:
        await channel.send(embed=emb)
    except Exception:
        pass

def add_file_log(kind: str, message: str):
    # small ring buffer
    entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, "message": message}
    DATA["logs"].append(entry)
    if len(DATA["logs"]) > LOGS_MAX:
        DATA["logs"] = DATA["logs"][len(DATA["logs"]) - LOGS_MAX:]
    save_data(DATA)

def sanitize_no_mentions(text: str) -> str:
    # Break @mentions to avoid ping in say
    text = re.sub(r"@","@\u200b", text)
    text = re.sub(r"<@","<@\u200b", text)
    return text

def uptime_str() -> str:
    s = int(time.time() - bot.start_time)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)

async def maybe_create_invite(guild: discord.Guild) -> Optional[str]:
    # Try to create an invite in a text channel the bot can use
    try:
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).create_instant_invite:
                inv = await ch.create_invite(max_age=3600, max_uses=1, unique=True, reason="Requested by owner")
                return str(inv)
    except Exception:
        return None
    return None

# --------------------------- PRESENCE & READY -----------------------------
@bot.event
async def on_ready():
    try:
        if not bot.synced:
            await tree.sync()
            bot.synced = True
    except Exception as e:
        print("Slash sync error:", e)

    activity = discord.Streaming(name=STREAM_NAME, url=STREAM_URL)
    await bot.change_presence(status=discord.Status.dnd, activity=activity)

    add_file_log("system", f"Bot ready as {bot.user} ({bot.user.id}) | guilds={len(bot.guilds)}")
    print(f"Logged in as {bot.user} | {len(bot.guilds)} guilds")

# --------------------------- PERMISSION CHECKS ----------------------------
def slash_check_blacklist():
    async def predicate(inter: discord.Interaction):
        if is_blacklisted(inter.user):
            await inter.response.send_message("You are blacklisted from using commands.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

def slash_check_admin_or_pookie():
    async def predicate(inter: discord.Interaction):
        if is_pookie(inter.user):
            return True
        await inter.response.send_message("Admins or Pookie only.", ephemeral=True)
        return False
    return app_commands.check(predicate)

def slash_check_owner_or_admin():
    async def predicate(inter: discord.Interaction):
        if is_admin(inter.user):
            return True
        await inter.response.send_message("Admins only.", ephemeral=True)
        return False
    return app_commands.check(predicate)

# --------------------------- LOGGING EVENTS -------------------------------
@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot:
        return
    add_file_log("delete", f"{message.author} in #{getattr(message.channel,'name','?')}: {message.content[:200]}")
    await send_log_embed(message.guild, "Message Deleted",
                         f"**Author:** {message.author.mention}\n**Channel:** {message.channel.mention}\n**Content:** {message.content[:1000]}", discord.Color.red())

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot or before.content == after.content:
        return
    add_file_log("edit", f"{before.author} in #{getattr(before.channel,'name','?')}: {before.content[:120]} -> {after.content[:120]}")
    fields = [
        ("Before", before.content[:1024] or "*empty*", False),
        ("After", after.content[:1024] or "*empty*", False)
    ]
    await send_log_embed(before.guild, "Message Edited",
                         f"**Author:** {before.author.mention}\n**Channel:** {before.channel.mention}",
                         discord.Color.orange(), fields)

@bot.event
async def on_member_join(member: discord.Member):
    add_file_log("member", f"JOIN {member} ({member.id}) in {member.guild.name}")
    await send_log_embed(member.guild, "Member Joined", f"{member.mention} joined.", discord.Color.green())

@bot.event
async def on_member_remove(member: discord.Member):
    add_file_log("member", f"LEAVE {member} ({member.id}) in {member.guild.name}")
    await send_log_embed(member.guild, "Member Left", f"{member} left.", discord.Color.dark_gray())

# --------------------------- AFK SYSTEM -----------------------------------
def set_afk(user_id: int, reason: str):
    DATA["afk"][str(user_id)] = {"reason": reason, "since": datetime.now(timezone.utc).isoformat()}
    save_data(DATA)

def clear_afk(user_id: int):
    if str(user_id) in DATA["afk"]:
        del DATA["afk"][str(user_id)]
        save_data(DATA)

def get_afk(user_id: int) -> Optional[Dict[str, str]]:
    return DATA["afk"].get(str(user_id))

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Clear AFK if user speaks
    if get_afk(message.author.id):
        clear_afk(message.author.id)
        try:
            await message.channel.send(f"Welcome back {message.author.mention}, I removed your AFK.", delete_after=6)
        except Exception:
            pass

    # If mentioning someone AFK, notify
    if message.mentions:
        for u in message.mentions:
            info = get_afk(u.id)
            if info:
                since = info.get("since")
                try:
                    since_dt = datetime.fromisoformat(since)
                    ago = int((datetime.now(timezone.utc) - since_dt).total_seconds())
                    mins = ago//60
                    txt = f"{u.mention} is AFK: **{info.get('reason','AFK')}** — {mins}m ago."
                except Exception:
                    txt = f"{u.mention} is AFK: **{info.get('reason','AFK')}**."
                try:
                    await message.reply(txt, mention_author=False, delete_after=10)
                except Exception:
                    pass

    # Blocked words exact match
    lowered = message.content.lower()
    for w in DATA["blocked_words"]:
        if exact_word_present(lowered, w):
            try:
                await message.delete()
            except Exception:
                pass
            await send_log_embed(message.guild, "Blocked Word",
                                 f"{message.author.mention} used blocked word in {message.channel.mention}\nWord: `{w}`")
            return

    # Auto-responder triggers (exact word)
    for word, reply in DATA["triggers"].items():
        if exact_word_present(lowered, word.lower()):
            # Replace {user} with author mention (ping allowed here)
            out = reply.replace("{user}", message.author.mention)
            try:
                await message.channel.send(out)
            except Exception:
                pass
            break

    await bot.process_commands(message)

# --------------------------- SNIPES ---------------------------------------
# Keep small ring buffers of deleted/edited per channel
SNIPES: Dict[int, List[Dict[str, Any]]] = {}   # channel_id -> list of {author_id, content, time, avatar_url, author_tag}
ESNIPES: Dict[int, List[Dict[str, Any]]] = {}  # channel_id -> list of {author_id, before, after, time, ...}

async def push_snipe(channel_id: int, entry: Dict[str, Any], store: Dict[int, List[Dict[str, Any]]]):
    lst = store.setdefault(channel_id, [])
    lst.append(entry)
    if len(lst) > SNIPES_PER_CHANNEL:
        del lst[0]

@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot:
        return
    await push_snipe(message.channel.id, {
        "author_id": message.author.id,
        "author_tag": str(message.author),
        "avatar_url": message.author.display_avatar.url if hasattr(message.author.display_avatar, "url") else "",
        "content": message.content,
        "time": datetime.now(timezone.utc).isoformat()
    }, SNIPES)
    await send_log_embed(message.guild, "Message Deleted",
                         f"**Author:** {message.author.mention}\n**Channel:** {message.channel.mention}\n**Content:** {message.content[:1000]}", discord.Color.red())
    add_file_log("delete", f"{message.author} in #{getattr(message.channel,'name','?')}: {message.content[:200]}")

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot or before.content == after.content:
        return
    await push_snipe(before.channel.id, {
        "author_id": before.author.id,
        "author_tag": str(before.author),
        "avatar_url": before.author.display_avatar.url if hasattr(before.author.display_avatar, "url") else "",
        "before": before.content,
        "after": after.content,
        "time": datetime.now(timezone.utc).isoformat()
    }, ESNIPES)
    add_file_log("edit", f"{before.author} in #{getattr(before.channel,'name','?')}: {before.content[:120]} -> {after.content[:120]}")
    fields = [("Before", before.content[:1024] or "*empty*", False), ("After", after.content[:1024] or "*empty*", False)]
    await send_log_embed(before.guild, "Message Edited",
                         f"**Author:** {before.author.mention}\n**Channel:** {before.channel.mention}",
                         discord.Color.orange(), fields)

class SnipeView(discord.ui.View):
    def __init__(self, items: List[Dict[str, Any]]):
        super().__init__(timeout=30)
        self.items = items
        self.idx = len(items) - 1  # latest

    def embed(self) -> discord.Embed:
        data = self.items[self.idx]
        ts = data.get("time")
        ts_dt = None
        try:
            ts_dt = datetime.fromisoformat(ts)
        except Exception:
            pass
        when = discord.utils.format_dt(ts_dt, style="R") if ts_dt else ts
        emb = discord.Embed(title=f"Snipe [{self.idx+1}/{len(self.items)}]", color=discord.Color.blurple())
        if "content" in data:
            emb.add_field(name="Content", value=data["content"][:1024] or "*empty*", inline=False)
        else:
            emb.add_field(name="Before", value=data.get("before","")[:1024] or "*empty*", inline=False)
            emb.add_field(name="After", value=data.get("after","")[:1024] or "*empty*", inline=False)
        emb.set_author(name=data.get("author_tag","Unknown"), icon_url=data.get("avatar_url",""))
        emb.set_footer(text=f"Deleted/Edited {when}")
        return emb

    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.idx > 0:
            self.idx -= 1
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.idx < len(self.items) - 1:
            self.idx += 1
        await interaction.response.edit_message(embed=self.embed(), view=self)

# Slash snipe
@tree.command(name="snipe", description="Show recently deleted messages in this channel")
@slash_check_blacklist()
async def slash_snipe(inter: discord.Interaction):
    items = SNIPES.get(inter.channel_id, [])
    if not items:
        return await inter.response.send_message("Nothing to snipe here.", ephemeral=True)
    view = SnipeView(items)
    await inter.response.send_message(embed=view.embed(), view=view)

@tree.command(name="esnipe", description="Show recently edited messages in this channel")
@slash_check_blacklist()
async def slash_esnipe(inter: discord.Interaction):
    items = ESNIPES.get(inter.channel_id, [])
    if not items:
        return await inter.response.send_message("No edits to snipe here.", ephemeral=True)
    view = SnipeView(items)
    await inter.response.send_message(embed=view.embed(), view=view)

# --------------------------- CAT FEATURES ---------------------------------
async def fetch_random_cat_url(session: aiohttp.ClientSession) -> Optional[str]:
    # Try TheCatAPI if key provided, else fallback unsplash-like static
    try:
        headers = {}
        if CAT_API_KEY:
            headers["x-api-key"] = CAT_API_KEY
        async with session.get("https://api.thecatapi.com/v1/images/search", headers=headers, timeout=20) as r:
            if r.status == 200:
                data = await r.json()
                if data and isinstance(data, list) and data[0].get("url"):
                    return data[0]["url"]
    except Exception:
        pass
    # Fallback
    return "https://cataas.com/cat"

@tree.command(name="cat", description="Get a random cat picture (sometimes a video)")
@slash_check_blacklist()
async def slash_cat(inter: discord.Interaction):
    await inter.response.defer()
    async with aiohttp.ClientSession() as s:
        url = await fetch_random_cat_url(s)
    await inter.followup.send(url)

@bot.command(name="cat")
async def prefix_cat(ctx: commands.Context):
    if is_blacklisted(ctx.author):
        return await ctx.reply("You are blacklisted.", mention_author=False)
    async with aiohttp.ClientSession() as s:
        url = await fetch_random_cat_url(s)
    await ctx.send(url)

@tree.command(name="setcatchannel", description="Set the daily 11:00 IST cat channel")
@slash_check_owner_or_admin()
async def slash_set_cat_channel(inter: discord.Interaction, channel: discord.TextChannel):
    DATA["cat_channel"] = channel.id
    save_data(DATA)
    await inter.response.send_message(f"Daily cat channel set to {channel.mention} (11:00 IST).")

@tree.command(name="sethourlycatchannel", description="Set a channel to receive a cat every hour (permanent)")
@slash_check_owner_or_admin()
async def slash_set_hourly_cat_channel(inter: discord.Interaction, channel: discord.TextChannel):
    DATA["hourly_cat_channel"] = channel.id
    save_data(DATA)
    await inter.response.send_message(f"Hourly cat channel set to {channel.mention} (every hour).")

@tasks.loop(minutes=1)
async def cat_scheduler():
    # Daily at 11:00 IST
    now = datetime.now(BOT_TZ)
    if DATA.get("cat_channel"):
        if now.hour == 11 and now.minute == 0:
            ch = bot.get_channel(DATA["cat_channel"])
            if isinstance(ch, discord.TextChannel):
                try:
                    async with aiohttp.ClientSession() as s:
                        url = await fetch_random_cat_url(s)
                    await ch.send(url)
                except Exception:
                    pass
                await asyncio.sleep(60)  # avoid double fire within same minute

    # Hourly posts (at minute 0)
    if DATA.get("hourly_cat_channel"):
        if now.minute == 0:
            ch2 = bot.get_channel(DATA["hourly_cat_channel"])
            if isinstance(ch2, discord.TextChannel):
                try:
                    async with aiohttp.ClientSession() as s:
                        url = await fetch_random_cat_url(s)
                    await ch2.send(url)
                except Exception:
                    pass
                await asyncio.sleep(60)

@cat_scheduler.before_loop
async def before_cat_scheduler():
    await bot.wait_until_ready()

cat_scheduler.start()

# --------------------------- MODERATION -----------------------------------
async def do_ban(ctx_or_inter, member: discord.Member, reason: str, slash=False):
    try:
        await member.ban(reason=reason, delete_message_days=0)
        msg = f"Banned {member.mention} | Reason: {reason or 'No reason'}"
        if slash:
            await ctx_or_inter.response.send_message(msg)
        else:
            await ctx_or_inter.reply(msg, mention_author=False)
        await send_log_embed(member.guild, "Ban", msg, discord.Color.red())
        add_file_log("mod", f"Ban {member} by {ctx_or_inter.user if slash else ctx_or_inter.author}")
    except Exception as e:
        if slash:
            await ctx_or_inter.response.send_message(f"Failed to ban: {e}", ephemeral=True)
        else:
            await ctx_or_inter.reply(f"Failed to ban: {e}", mention_author=False)

@tree.command(name="ban", description="Ban a member (server member only)")
@slash_check_admin_or_pookie()
@app_commands.describe(user="Member in this server to ban", reason="Reason")
async def slash_ban(inter: discord.Interaction, user: discord.Member, reason: Optional[str] = None):
    await do_ban(inter, user, reason or "", slash=True)

@bot.command(name="ban")
async def prefix_ban(ctx: commands.Context, target: str, *, reason: str = ""):
    if not is_pookie(ctx.author):
        return await ctx.reply("Admins or Pookie only.", mention_author=False)
    # Accept mention or raw ID
    uid = None
    m = re.match(r"<@!?(\d+)>", target)
    if m:
        uid = int(m.group(1))
    else:
        try:
            uid = int(target)
        except Exception:
            return await ctx.reply("Provide a mention or user ID.", mention_author=False)

    member = ctx.guild.get_member(uid)
    if not member:
        # Can still ban by ID
        try:
            await ctx.guild.ban(discord.Object(id=uid), reason=reason, delete_message_days=0)
            await ctx.reply(f"Banned <@{uid}> | Reason: {reason or 'No reason'}", mention_author=False)
            await send_log_embed(ctx.guild, "Ban (by ID)", f"<@{uid}> banned. Reason: {reason or 'No reason'}", discord.Color.red())
            add_file_log("mod", f"Ban ID {uid} by {ctx.author}")
        except Exception as e:
            await ctx.reply(f"Failed to ban: {e}", mention_author=False)
        return
    await do_ban(ctx, member, reason or "")

@tree.command(name="kick", description="Kick a member")
@slash_check_admin_or_pookie()
@app_commands.describe(user="Member in this server to kick", reason="Reason")
async def slash_kick(inter: discord.Interaction, user: discord.Member, reason: Optional[str] = None):
    try:
        await user.kick(reason=reason)
        await inter.response.send_message(f"Kicked {user.mention} | Reason: {reason or 'No reason'}")
        await send_log_embed(inter.guild, "Kick", f"{user} kicked.", discord.Color.orange())
        add_file_log("mod", f"Kick {user} by {inter.user}")
    except Exception as e:
        await inter.response.send_message(f"Failed to kick: {e}", ephemeral=True)

@bot.command(name="kick")
async def prefix_kick(ctx: commands.Context, member: discord.Member, *, reason: str = ""):
    if not is_pookie(ctx.author):
        return await ctx.reply("Admins or Pookie only.", mention_author=False)
    try:
        await member.kick(reason=reason)
        await ctx.reply(f"Kicked {member.mention} | Reason: {reason or 'No reason'}", mention_author=False)
        await send_log_embed(ctx.guild, "Kick", f"{member} kicked.", discord.Color.orange())
        add_file_log("mod", f"Kick {member} by {ctx.author}")
    except Exception as e:
        await ctx.reply(f"Failed to kick: {e}", mention_author=False)

@tree.command(name="purge", description="Delete N messages (max 100)")
@slash_check_admin_or_pookie()
@app_commands.describe(amount="How many to delete (1-100)")
async def slash_purge(inter: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    try:
        deleted = await inter.channel.purge(limit=amount)
        await inter.response.send_message(f"Deleted {len(deleted)} messages.", ephemeral=True)
        await send_log_embed(inter.guild, "Purge", f"{inter.user.mention} purged {len(deleted)} messages in {inter.channel.mention}")
    except Exception as e:
        await inter.response.send_message(f"Failed to purge: {e}", ephemeral=True)

@bot.command(name="purge")
async def prefix_purge(ctx: commands.Context, amount: int):
    if not is_pookie(ctx.author):
        return await ctx.reply("Admins or Pookie only.", mention_author=False)
    amount = max(1, min(100, amount))
    try:
        deleted = await ctx.channel.purge(limit=amount)
        msg = await ctx.send(f"Deleted {len(deleted)} messages.")
        await asyncio.sleep(3)
        await msg.delete()
        await send_log_embed(ctx.guild, "Purge", f"{ctx.author.mention} purged {len(deleted)} messages in {ctx.channel.mention}")
    except Exception as e:
        await ctx.reply(f"Failed to purge: {e}", mention_author=False)

# --------------------------- SAY / SAY_ADMIN ------------------------------
@tree.command(name="say", description="Say a message (no pings)")
@slash_check_blacklist()
@app_commands.describe(message="Text to say (pings disabled)")
async def slash_say(inter: discord.Interaction, message: str):
    msg = sanitize_no_mentions(message)
    await inter.response.send_message("Sent.", ephemeral=True)
    await inter.channel.send(msg)

@bot.command(name="say")
async def prefix_say(ctx: commands.Context, *, message: str):
    if is_blacklisted(ctx.author):
        return await ctx.reply("You are blacklisted.", mention_author=False)
    msg = sanitize_no_mentions(message)
    await ctx.send(msg)

@tree.command(name="say_admin", description="Say a message (admin/pookie; pings allowed)")
@slash_check_admin_or_pookie()
@app_commands.describe(message="Text to send (pings allowed)", channel="Optional target channel")
async def slash_say_admin(inter: discord.Interaction, message: str, channel: Optional[discord.TextChannel] = None):
    target = channel or inter.channel
    await target.send(message)
    await inter.response.send_message(f"Sent to {target.mention}.", ephemeral=True)

# --------------------------- ADMIN / POOKIE MGMT --------------------------
@tree.command(name="add_admin", description="Add an admin by mention or ID")
@slash_check_owner_or_admin()
@app_commands.describe(user="User to add as admin")
async def slash_add_admin(inter: discord.Interaction, user: discord.User):
    if user.id not in DATA["admins"]:
        DATA["admins"].append(user.id)
        save_data(DATA)
    await inter.response.send_message(f"Added {user.mention} as admin.")

@tree.command(name="remove_admin", description="Remove an admin")
@slash_check_owner_or_admin()
@app_commands.describe(user="Admin user to remove")
async def slash_remove_admin(inter: discord.Interaction, user: discord.User):
    if user.id == OWNER_ID:
        return await inter.response.send_message("Cannot remove the owner.", ephemeral=True)
    if user.id in DATA["admins"]:
        DATA["admins"].remove(user.id)
        save_data(DATA)
        return await inter.response.send_message(f"Removed {user.mention} from admins.")
    await inter.response.send_message("User is not an admin.", ephemeral=True)

@tree.command(name="list_admins", description="List all admins")
@slash_check_owner_or_admin()
async def slash_list_admins(inter: discord.Interaction):
    if not DATA["admins"]:
        return await inter.response.send_message("No admins set.", ephemeral=True)
    mentions = [f"<@{uid}>" for uid in DATA["admins"]]
    await inter.response.send_message("Admins:\n" + "\n".join(mentions))

@tree.command(name="addpookie", description="Add a Pookie user")
@slash_check_owner_or_admin()
@app_commands.describe(user="User to add as Pookie")
async def slash_addpookie(inter: discord.Interaction, user: discord.User):
    if user.id not in DATA["pookie_users"]:
        DATA["pookie_users"].append(user.id)
        save_data(DATA)
    await inter.response.send_message(f"Added {user.mention} to Pookie.")

@tree.command(name="removepookie", description="Remove a Pookie user")
@slash_check_owner_or_admin()
@app_commands.describe(user="User to remove from Pookie")
async def slash_removepookie(inter: discord.Interaction, user: discord.User):
    if user.id in DATA["pookie_users"]:
        DATA["pookie_users"].remove(user.id)
        save_data(DATA)
        return await inter.response.send_message(f"Removed {user.mention} from Pookie.")
    await inter.response.send_message("User is not Pookie.", ephemeral=True)

@tree.command(name="listpookie", description="List Pookie users")
@slash_check_owner_or_admin()
async def slash_listpookie(inter: discord.Interaction):
    if not DATA["pookie_users"]:
        return await inter.response.send_message("No Pookie users.", ephemeral=True)
    mentions = [f"<@{uid}>" for uid in DATA["pookie_users"]]
    await inter.response.send_message("Pookie:\n" + "\n".join(mentions))

# --------------------------- BLACKLIST & BLOCKED WORDS --------------------
@tree.command(name="blacklist", description="Blacklist a user from using commands")
@slash_check_owner_or_admin()
@app_commands.describe(user="User to blacklist")
async def slash_blacklist(inter: discord.Interaction, user: discord.User):
    if user.id not in DATA["blacklist"]:
        DATA["blacklist"].append(user.id)
        save_data(DATA)
    await inter.response.send_message(f"Blacklisted {user.mention}.")

@tree.command(name="unblacklist", description="Remove a user from blacklist")
@slash_check_owner_or_admin()
@app_commands.describe(user="User to unblacklist")
async def slash_unblacklist(inter: discord.Interaction, user: discord.User):
    if user.id in DATA["blacklist"]:
        DATA["blacklist"].remove(user.id)
        save_data(DATA)
        return await inter.response.send_message(f"Unblacklisted {user.mention}.")
    await inter.response.send_message("User not blacklisted.", ephemeral=True)

@tree.command(name="block_word", description="Add a blocked word (exact word match)")
@slash_check_owner_or_admin()
@app_commands.describe(word="Exact word to block")
async def slash_block_word(inter: discord.Interaction, word: str):
    w = word.lower().strip()
    if w and w not in DATA["blocked_words"]:
        DATA["blocked_words"].append(w)
        save_data(DATA)
    await inter.response.send_message(f"Blocked word: `{w}`")

@tree.command(name="unblock_word", description="Remove a blocked word")
@slash_check_owner_or_admin()
@app_commands.describe(word="Exact word to unblock")
async def slash_unblock_word(inter: discord.Interaction, word: str):
    w = word.lower().strip()
    if w in DATA["blocked_words"]:
        DATA["blocked_words"].remove(w)
        save_data(DATA)
        return await inter.response.send_message(f"Unblocked word: `{w}`")
    await inter.response.send_message("Word not in blocked list.", ephemeral=True)

# --------------------------- LOG CHANNEL & LOGS ---------------------------
@tree.command(name="set_log_channel", description="Set the log channel")
@slash_check_owner_or_admin()
async def slash_set_log_channel(inter: discord.Interaction, channel: discord.TextChannel):
    DATA["log_channel"] = channel.id
    save_data(DATA)
    await inter.response.send_message(f"Log channel set to {channel.mention}. Future logs will appear here.")
    await send_log_embed(inter.guild, "Log Channel Set", f"By {inter.user.mention} → {channel.mention}")

@tree.command(name="disable_log_channel", description="Disable log channel")
@slash_check_owner_or_admin()
async def slash_disable_log_channel(inter: discord.Interaction):
    DATA["log_channel"] = None
    save_data(DATA)
    await inter.response.send_message("Log channel disabled.")

@tree.command(name="logs", description="Show recent logs from file")
@slash_check_owner_or_admin()
@app_commands.describe(count="How many to show (1-50)")
async def slash_logs(inter: discord.Interaction, count: app_commands.Range[int, 1, 50] = 10):
    logs = DATA.get("logs", [])
    if not logs:
        return await inter.response.send_message("No logs yet.", ephemeral=True)
    last = logs[-count:]
    text = "\n".join(f"`{e['time']}` **{e['kind']}** — {e['message']}"[:1900] for e in last)
    await inter.response.send_message(text or "Empty.", ephemeral=True)

# --------------------------- INFO COMMANDS --------------------------------
@tree.command(name="avatar", description="Show a user's avatar")
@slash_check_blacklist()
async def slash_avatar(inter: discord.Interaction, user: Optional[discord.User] = None):
    user = user or inter.user
    url = user.display_avatar.url if hasattr(user.display_avatar, "url") else user.avatar.url
    emb = discord.Embed(title=f"{user} avatar", color=discord.Color.blurple())
    emb.set_image(url=url)
    await inter.response.send_message(embed=emb)

@bot.command(name="avatar")
async def prefix_avatar(ctx: commands.Context, user: Optional[discord.User] = None):
    user = user or ctx.author
    url = user.display_avatar.url if hasattr(user.display_avatar, "url") else user.avatar.url
    emb = discord.Embed(title=f"{user} avatar", color=discord.Color.blurple())
    emb.set_image(url=url)
    await ctx.send(embed=emb)

@tree.command(name="userinfo", description="Info about a user (mention or ID)")
@slash_check_blacklist()
async def slash_userinfo(inter: discord.Interaction, user: Optional[discord.User] = None, user_id: Optional[str] = None):
    target = user
    if not target and user_id:
        try:
            target = await bot.fetch_user(int(user_id))
        except Exception:
            pass
    target = target or inter.user
    emb = discord.Embed(title=f"User Info: {target}", color=discord.Color.green())
    emb.add_field(name="ID", value=str(target.id))
    emb.add_field(name="Bot", value=str(target.bot))
    emb.set_thumbnail(url=target.display_avatar.url if hasattr(target.display_avatar, "url") else "")
    await inter.response.send_message(embed=emb)

@tree.command(name="guildinfo", description="Info about a guild by ID (tries to create invite)")
@slash_check_owner_or_admin()
async def slash_guildinfo(inter: discord.Interaction, guild_id: str):
    try:
        gid = int(guild_id)
    except Exception:
        return await inter.response.send_message("Invalid guild ID.", ephemeral=True)
    g = bot.get_guild(gid)
    if not g:
        return await inter.response.send_message("I'm not in that guild.", ephemeral=True)
    invite = await maybe_create_invite(g)
    emb = discord.Embed(title=f"Guild Info: {g.name}", color=discord.Color.gold())
    emb.add_field(name="ID", value=str(g.id))
    emb.add_field(name="Owner", value=f"{g.owner} ({g.owner_id})" if g.owner else str(g.owner_id))
    emb.add_field(name="Members", value=str(g.member_count))
    emb.add_field(name="Channels", value=f"{len(g.text_channels)} text / {len(g.voice_channels)} voice / {len(g.categories)} categories", inline=False)
    emb.add_field(name="Created", value=discord.utils.format_dt(g.created_at, style="R"))
    if invite:
        emb.add_field(name="Invite", value=invite, inline=False)
    await inter.response.send_message(embed=emb, ephemeral=True)

@tree.command(name="servers", description="List servers I'm in (owner/admin only)")
@slash_check_owner_or_admin()
async def slash_servers(inter: discord.Interaction):
    desc = "\n".join(f"- **{g.name}** (`{g.id}`) — {g.member_count} members" for g in bot.guilds) or "No servers."
    await inter.response.send_message(desc, ephemeral=True)

# --------------------------- FUN COMMANDS ---------------------------------
import random

@tree.command(name="8ball", description="Ask the magic 8-ball")
@slash_check_blacklist()
async def slash_8ball(inter: discord.Interaction, question: str):
    answers = ["Yes.", "No.", "Maybe.", "Absolutely!", "Ask again later.", "Definitely not.", "Probably.", "Unlikely."]
    await inter.response.send_message(f"🎱 {random.choice(answers)}")

@bot.command(name="8ball")
async def prefix_8ball(ctx: commands.Context, *, question: str):
    answers = ["Yes.", "No.", "Maybe.", "Absolutely!", "Ask again later.", "Definitely not.", "Probably.", "Unlikely."]
    await ctx.reply(f"🎱 {random.choice(answers)}", mention_author=False)

@tree.command(name="joke", description="Get a joke")
@slash_check_blacklist()
async def slash_joke(inter: discord.Interaction):
    jokes = [
        "Why did the programmer quit his job? Because he didn't get arrays.",
        "I would tell you a UDP joke, but you might not get it.",
        "There are 10 types of people: those who understand binary and those who don't."
    ]
    await inter.response.send_message(random.choice(jokes))

@tree.command(name="dadjoke", description="Get a dad joke")
@slash_check_blacklist()
async def slash_dadjoke(inter: discord.Interaction):
    jokes = [
        "I’m reading a book on anti-gravity. It’s impossible to put down!",
        "I used to hate facial hair... but then it grew on me.",
        "What do you call fake spaghetti? An impasta."
    ]
    await inter.response.send_message(random.choice(jokes))

@tree.command(name="coinflip", description="Flip a coin")
@slash_check_blacklist()
async def slash_coinflip(inter: discord.Interaction):
    await inter.response.send_message("Heads" if random.random() < 0.5 else "Tails")

@tree.command(name="rolldice", description="Roll a dice 1-6")
@slash_check_blacklist()
async def slash_rolldice(inter: discord.Interaction):
    await inter.response.send_message(f"🎲 {random.randint(1,6)}")

@tree.command(name="rps", description="Rock Paper Scissors")
@slash_check_blacklist()
@app_commands.describe(choice="Your move: rock/paper/scissors")
@app_commands.choices(choice=[
    app_commands.Choice(name="rock", value="rock"),
    app_commands.Choice(name="paper", value="paper"),
    app_commands.Choice(name="scissors", value="scissors")
])
async def slash_rps(inter: discord.Interaction, choice: app_commands.Choice[str]):
    bot_choice = random.choice(["rock","paper","scissors"])
    user = choice.value
    result = "draw"
    if (user, bot_choice) in [("rock","scissors"), ("paper","rock"), ("scissors","paper")]:
        result = "you win!"
    elif user != bot_choice:
        result = "you lose!"
    await inter.response.send_message(f"You: **{user}**, Bot: **{bot_choice}** → {result}")

# --------------------------- TRIGGERS (Auto-responder) --------------------
@tree.command(name="trigger_add", description="Admin: add an exact-word trigger")
@slash_check_owner_or_admin()
async def slash_trigger_add(inter: discord.Interaction, word: str, reply: str):
    w = word.strip().lower()
    DATA["triggers"][w] = reply
    save_data(DATA)
    await inter.response.send_message(f"Trigger added: `{w}` → `{reply}`")

@tree.command(name="trigger_remove", description="Admin: remove a trigger")
@slash_check_owner_or_admin()
async def slash_trigger_remove(inter: discord.Interaction, word: str):
    w = word.strip().lower()
    if w in DATA["triggers"]:
        del DATA["triggers"][w]
        save_data(DATA)
        return await inter.response.send_message(f"Removed trigger `{w}`")
    await inter.response.send_message("Trigger not found.", ephemeral=True)

@tree.command(name="showtrigger", description="Show all triggers")
@slash_check_owner_or_admin()
async def slash_showtrigger(inter: discord.Interaction):
    if not DATA["triggers"]:
        return await inter.response.send_message("No triggers set.", ephemeral=True)
    text = "\n".join([f"- `{k}` → `{v}`" for k, v in DATA["triggers"].items()])
    await inter.response.send_message(text[:1900])

# --------------------------- AFK SLASH ------------------------------------
@tree.command(name="afk", description="Set your AFK with reason")
async def slash_afk(inter: discord.Interaction, reason: str):
    set_afk(inter.user.id, reason)
    await inter.response.send_message(f"AFK set: **{reason}**", ephemeral=True)

@tree.command(name="afk_remove", description="Remove your AFK")
async def slash_afk_remove(inter: discord.Interaction):
    clear_afk(inter.user.id)
    await inter.response.send_message("AFK removed.", ephemeral=True)

# --------------------------- SHOWCOMMANDS (interactive) -------------------
CATEGORIES = {
    "Fun": ["cat","8ball","joke","dadjoke","coinflip","rolldice","rps","avatar","userinfo"],
    "Moderation": ["ban","kick","purge","say","say_admin"],
    "Management": ["add_admin","remove_admin","list_admins","addpookie","removepookie","listpookie","blacklist","unblacklist","block_word","unblock_word"],
    "Logging": ["set_log_channel","disable_log_channel","logs"],
    "Info": ["servers","guildinfo"],
    "Snipe": ["snipe","esnipe"],
    "Cats": ["cat","setcatchannel","sethourlycatchannel"],
    "AFK": ["afk","afk_remove"],
    "Owner": ["refreshcommands","restart","debug"]
}

class ShowCmdsView(discord.ui.View):
    def __init__(self, inter_user: discord.User):
        super().__init__(timeout=60)
        self.user = inter_user

    @discord.ui.select(placeholder="Pick a category", options=[discord.SelectOption(label=k) for k in CATEGORIES.keys()])
    async def select_cat(self, interaction: discord.Interaction, select: discord.ui.Select):
        # Filter commands by perms
        cat = select.values[0]
        cmds = CATEGORIES.get(cat, [])
        filtered = []
        for c in cmds:
            if c in ["add_admin","remove_admin","list_admins","addpookie","removepookie","listpookie","blacklist","unblacklist","block_word","unblock_word","set_log_channel","disable_log_channel","logs","servers","guildinfo","refreshcommands","restart","debug","setcatchannel","sethourlycatchannel","purge","say_admin"]:
                if not is_pookie(interaction.user):
                    continue
            filtered.append(c)
        txt = f"**{cat}** commands:\n" + ", ".join(f"`/{x}`" for x in filtered) if filtered else "No commands you can use here."
        await interaction.response.edit_message(content=txt, view=self)

@tree.command(name="showcommands", description="See the commands you can use")
@slash_check_blacklist()
async def slash_showcommands(inter: discord.Interaction):
    view = ShowCmdsView(inter.user)
    await inter.response.send_message("Pick a category:", view=view, ephemeral=True)

# --------------------------- ASK FOR COMMAND ------------------------------
@tree.command(name="askforcommand", description="Ask owner for a new command (pings + DMs owner)")
@slash_check_blacklist()
@app_commands.describe(request="Describe the command you want")
async def slash_askforcommand(inter: discord.Interaction, request: str):
    owner = await bot.fetch_user(OWNER_ID)
    content = f"**Request from {inter.user} ({inter.user.id})** in **{inter.guild.name if inter.guild else 'DM'}**:\n> {request}"
    try:
        await owner.send(content)
    except Exception:
        pass
    try:
        await inter.channel.send(f"<@{OWNER_ID}> new command request:\n{content}")
    except Exception:
        pass
    await inter.response.send_message("Sent your request to the owner. ✅", ephemeral=True)

# --------------------------- RENDER RESTART / REFRESH ---------------------
@tree.command(name="refreshcommands", description="Owner/Admin: refresh slash commands")
@slash_check_owner_or_admin()
async def slash_refresh(inter: discord.Interaction):
    try:
        await tree.sync()
        await inter.response.send_message("Slash commands refreshed ✅", ephemeral=True)
        add_file_log("admin", f"{inter.user} refreshed commands")
    except Exception as e:
        await inter.response.send_message(f"Failed to refresh: `{e}`", ephemeral=True)

@tree.command(name="restart", description="Owner/Admin: restart service on Render (if keys set)")
@slash_check_owner_or_admin()
async def slash_restart(inter: discord.Interaction):
    if not (RENDER_API_KEY and RENDER_SERVICE_ID):
        return await inter.response.send_message("RENDER_API_KEY / RENDER_SERVICE_ID not set.", ephemeral=True)
    await inter.response.send_message("Attempting restart…", ephemeral=True)
    try:
        import base64, json as _json
        # Render restart via PATCH /services/{id}/deploys (or new Deploy hook)
        # Render's public API doesn't have a simple "restart" — easiest is to trigger a new deploy.
        # We'll call Deploys API (v1) if available; otherwise just exit the process so Render restarts it.
        # Fallback: kill self
        os.kill(os.getpid(), signal.SIGTERM)
    except Exception:
        os._exit(0)

@tree.command(name="debug", description="Owner/Admin: debug info")
@slash_check_owner_or_admin()
async def slash_debug(inter: discord.Interaction):
    proc = psutil.Process(os.getpid())
    mem = proc.memory_info().rss / (1024*1024)
    cpu = psutil.cpu_percent(interval=None)
    emb = discord.Embed(title="Debug", color=discord.Color.teal(), timestamp=datetime.now(timezone.utc))
    emb.add_field(name="Uptime", value=uptime_str())
    emb.add_field(name="Guilds", value=str(len(bot.guilds)))
    emb.add_field(name="Latency", value=f"{round(bot.latency*1000)} ms")
    emb.add_field(name="CPU", value=f"{cpu}%")
    emb.add_field(name="RAM", value=f"{mem:.1f} MiB")
    emb.add_field(name="Py", value=platform.python_version())
    emb.add_field(name="d.py", value=discord.__version__)
    await inter.response.send_message(embed=emb, ephemeral=True)

# --------------------------- SIMPLE PING/PONG ------------------------------
@tree.command(name="ping", description="Ping")
async def slash_ping(inter: discord.Interaction):
    await inter.response.send_message(f"Pong! {round(bot.latency*1000)} ms")

@bot.command(name="ping")
async def prefix_ping(ctx: commands.Context):
    await ctx.reply(f"Pong! {round(bot.latency*1000)} ms", mention_author=False)

# --------------------------- START BOT ------------------------------------
async def main():
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
