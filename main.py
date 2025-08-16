# ================================================================
#  Discord Bot — Full Master Script (Slash + Prefix `?`)
#  Features:
#   • Admin/Pookie/Owner permissions
#   • Blacklist, banned words with bypass detection
#   • Ban/Kick commands
#   • Say (no pings) / Say_Admin (pings allowed)
#   • Logs (view last N), optional log channel
#   • Triggers (exact-match words) with {user} mention replacement
#   • Snipe / Esnipe
#   • Fun commands (8ball, jokes, dad jokes, rps, coin, dice, quote,
#     roast, compliment, reverse, choose, fact)
#   • User info & avatar
#   • Cat command + daily cat 11:00 IST + hourly cat channel
#   • Flask keepalive server for Render/UptimeRobot
#   • JSON persistence (single file: botdata.json)
#
#  Env Vars (Render):
#    DISCORD_BOT_TOKEN  (required)
#    CAT_API_KEY        (optional, for TheCatAPI)
#
#  Made for: Owner ID 1319292111325106296
# ================================================================

import os
import json
import random
import asyncio
from datetime import datetime
import pytz

import aiohttp
import discord
from discord.ext import commands, tasks
from discord import app_commands, AllowedMentions, Embed, Interaction

from flask import Flask
from threading import Thread


# ===========================
# Configuration
# ===========================
OWNER_ID = 1319292111325106296
PREFIX = "?"

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
CAT_API_KEY = os.getenv("CAT_API_KEY", "")

# Intents: we use all for simplicity (members, message content, etc.)
intents = discord.Intents.all()
bot = commands.Bot(command_prefix=PREFIX, intents=intents)
tree = bot.tree  # slash commands registry

DATA_FILE = "botdata.json"

# Snipe caches (per-channel)
snipe_cache = {}    # channel_id -> (content, author, timestamp)
esnipe_cache = {}   # channel_id -> (before_content, after_content, author, timestamp)

# Allowed mentions:
#   - Public messages (say): no @everyone, no roles, no user mentions
#   - Admin say: allow mentions
ALLOW_NONE = AllowedMentions(everyone=False, roles=False, users=False)
ALLOW_ALL  = AllowedMentions(everyone=True, roles=True, users=True)

# Flask app for uptime monitoring
app = Flask(__name__)

@app.route("/")
def home():
    return "OK - bot is running"

def run_flask():
    # On Render, any port works for simple health checks; 8080 is common
    app.run(host="0.0.0.0", port=8080)

# Start Flask in background thread immediately
Thread(target=run_flask, daemon=True).start()


# ===========================
# Persistent Storage Helpers
# ===========================
DEFAULT_DATA = {
    "admins": [],               # list of user_ids (str)
    "pookies": [],              # list of user_ids (str)
    "blacklist": [],            # list of user_ids (str)
    "blocked_words": [],        # list of words/phrases
    "logs": [],                 # list of entries {user, user_id, command, channel, time}
    "log_channel": None,        # channel id (str) or None
    "triggers": {},             # {word: reply}
    "daily_cat_channel": None,  # channel id (str) for daily 11:00 IST
    "hourly_cat_channel": None  # channel id (str) for hourly cats
}

def load_data():
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w") as f:
            json.dump(DEFAULT_DATA, f, indent=4)
        return json.loads(json.dumps(DEFAULT_DATA))
    with open(DATA_FILE, "r") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            data = json.loads(json.dumps(DEFAULT_DATA))
    # Ensure keys exist (future-proof)
    for k, v in DEFAULT_DATA.items():
        if k not in data:
            data[k] = v
    return data

def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

data = load_data()


# ===========================
# Permission Helpers
# ===========================
def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID

def is_pookie(user_id: int) -> bool:
    return str(user_id) in data["pookies"]

def is_admin(user_id: int) -> bool:
    return str(user_id) in data["admins"]

def has_staff_access(user_id: int) -> bool:
    # Owner has full; Pookie > Admin > regular
    return is_owner(user_id) or is_pookie(user_id) or is_admin(user_id)

def is_blacklisted_user(user_id: int) -> bool:
    return str(user_id) in data["blacklist"]


# ===========================
# Logging Helper
# ===========================
def log_command(user: discord.abc.User, command_name: str, channel: discord.abc.GuildChannel | str):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = {
        "user": str(user),
        "user_id": str(user.id),
        "command": command_name,
        "channel": str(channel),
        "time": ts
    }
    data["logs"].append(entry)
    # Retain last 1000 only
    if len(data["logs"]) > 1000:
        data["logs"] = data["logs"][-1000:]
    save_data()

    # Mirror to log channel if configured
    log_ch_id = data.get("log_channel")
    if log_ch_id:
        ch = bot.get_channel(int(log_ch_id))
        if ch:
            embed = Embed(title="Command Log", color=discord.Color.blurple())
            embed.add_field(name="User", value=f"{user} (`{user.id}`)", inline=False)
            embed.add_field(name="Command", value=command_name, inline=False)
            embed.add_field(name="Channel", value=str(channel), inline=False)
            embed.set_footer(text=ts)
            # Fire and forget (no await here)
            asyncio.create_task(ch.send(embed=embed))


# ===========================
# Blocked Words Check
#  - bypass-insensitive: compares alphanumeric-only
# ===========================
def contains_blocked_word(raw_content: str) -> bool:
    if not data["blocked_words"]:
        return False
    compact = "".join(ch for ch in raw_content.lower() if ch.isalnum())
    for word in data["blocked_words"]:
        w = "".join(ch for ch in word.lower() if ch.isalnum())
        if w and w in compact:
            return True
    return False


# ===========================
# Events
# ===========================
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    try:
        await tree.sync()
        print("✅ Slash commands synced.")
    except Exception as e:
        print("Slash sync error:", e)

    daily_cat_task.start()
    hourly_cat_task.start()


@bot.event
async def on_message(message: discord.Message):
    # Ignore bots, DMs processed too (but many commands require guild)
    if message.author.bot:
        return

    # Blacklist: ignore blacklisted users entirely
    if is_blacklisted_user(message.author.id):
        return

    # Blocked words (delete and warn)
    if contains_blocked_word(message.content):
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        await message.channel.send("🚫 That word isn't allowed here.", delete_after=5)
        return

    # Trigger system (exact match only; replace {user})
    if message.guild:  # only in guilds
        content = message.content.strip().lower()
        for word, reply in data["triggers"].items():
            if content == word.strip().lower():
                out = reply.replace("{user}", message.author.mention)
                await message.channel.send(out, allowed_mentions=ALLOW_ALL)
                break

    # Finally, pass through to prefix commands
    await bot.process_commands(message)


@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    snipe_cache[message.channel.id] = (message.content, message.author, datetime.utcnow())


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot or not before.guild:
        return
    esnipe_cache[before.channel.id] = (before.content, after.content, before.author, datetime.utcnow())
    # Re-run blocked words on edit (like moderation bots)
    if contains_blocked_word(after.content):
        try:
            await after.delete()
        except discord.Forbidden:
            pass
        await before.channel.send("🚫 That word isn't allowed here.", delete_after=5)


@bot.event
async def on_member_join(member: discord.Member):
    log_command(member, "member_join", member.guild)


@bot.event
async def on_member_remove(member: discord.Member):
    log_command(member, "member_leave", member.guild)


# ===========================
# Utility: send_cat helper
# ===========================
async def fetch_cat_url() -> str | None:
    url = "https://api.thecatapi.com/v1/images/search"
    headers = {"x-api-key": CAT_API_KEY} if CAT_API_KEY else {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=15) as r:
                if r.status == 200:
                    payload = await r.json()
                    if isinstance(payload, list) and payload:
                        return payload[0].get("url")
    except Exception:
        return None
    return None


# ===========================
# Tasks: Daily & Hourly Cats
# ===========================
@tasks.loop(minutes=1)
async def daily_cat_task():
    """
    Fires at 11:00 Asia/Kolkata (IST) once per minute check.
    """
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist)
    if now.hour == 11 and now.minute == 0:
        ch_id = data.get("daily_cat_channel")
        if not ch_id:
            return
        ch = bot.get_channel(int(ch_id))
        if not isinstance(ch, discord.TextChannel):
            return
        url = await fetch_cat_url()
        if url:
            try:
                await ch.send(f"📅 **Daily Cat (11:00 IST)**\n{url}")
            except Exception:
                pass


@tasks.loop(hours=1)
async def hourly_cat_task():
    """
    Sends one cat every hour to the configured hourly channel (if set).
    """
    ch_id = data.get("hourly_cat_channel")
    if not ch_id:
        return
    ch = bot.get_channel(int(ch_id))
    if not isinstance(ch, discord.TextChannel):
        return
    url = await fetch_cat_url()
    if url:
        try:
            await ch.send(f"⏰ **Hourly Cat!**\n{url}")
        except Exception:
            pass


# ===========================
# Decorators / Checks
# ===========================
def staff_only_slash():
    async def predicate(interaction: Interaction) -> bool:
        if not has_staff_access(interaction.user.id):
            await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

def owner_only_slash():
    async def predicate(interaction: Interaction) -> bool:
        if not is_owner(interaction.user.id):
            await interaction.response.send_message("❌ Owner only.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)


# ===========================
# Slash: Admin / Pookie Management
# ===========================
@tree.command(name="add_admin", description="Owner: add an admin user")
@owner_only_slash()
@app_commands.describe(user="User to add as admin")
async def add_admin_slash(interaction: Interaction, user: discord.User):
    if str(user.id) not in data["admins"]:
        data["admins"].append(str(user.id))
        save_data()
    await interaction.response.send_message(f"✅ Added {user.mention} as admin.")
    log_command(interaction.user, "/add_admin", interaction.channel)


@tree.command(name="remove_admin", description="Owner: remove an admin user")
@owner_only_slash()
@app_commands.describe(user="User to remove from admin")
async def remove_admin_slash(interaction: Interaction, user: discord.User):
    if str(user.id) in data["admins"]:
        data["admins"].remove(str(user.id))
        save_data()
    await interaction.response.send_message(f"✅ Removed {user.mention} from admin.")
    log_command(interaction.user, "/remove_admin", interaction.channel)


@tree.command(name="show_admins", description="Show all admin users")
async def show_admins_slash(interaction: Interaction):
    if not data["admins"]:
        await interaction.response.send_message("No admins set.")
        return
    mentions = [f"<@{uid}>" for uid in data["admins"]]
    await interaction.response.send_message("👑 Admins:\n" + "\n".join(mentions))


@tree.command(name="add_pookie", description="Owner/Admin: add a Pookie user")
@staff_only_slash()
@app_commands.describe(user="User to add as Pookie")
async def add_pookie_slash(interaction: Interaction, user: discord.User):
    if str(user.id) not in data["pookies"]:
        data["pookies"].append(str(user.id))
        save_data()
    await interaction.response.send_message(f"✅ Added {user.mention} as **Pookie**.")
    log_command(interaction.user, "/add_pookie", interaction.channel)


@tree.command(name="remove_pookie", description="Owner/Admin: remove a Pookie user")
@staff_only_slash()
@app_commands.describe(user="User to remove from Pookie")
async def remove_pookie_slash(interaction: Interaction, user: discord.User):
    if str(user.id) in data["pookies"]:
        data["pookies"].remove(str(user.id))
        save_data()
    await interaction.response.send_message(f"✅ Removed {user.mention} from **Pookie**.")
    log_command(interaction.user, "/remove_pookie", interaction.channel)


@tree.command(name="list_pookie", description="List all Pookie users")
async def list_pookie_slash(interaction: Interaction):
    if not data["pookies"]:
        await interaction.response.send_message("No Pookie users set.")
        return
    mentions = [f"<@{uid}>" for uid in data["pookies"]]
    await interaction.response.send_message("🍪 **Pookie users:**\n" + "\n".join(mentions))


# ===========================
# Slash: Moderation
# ===========================
@tree.command(name="blacklist", description="Admin/Pookie: add a user to blacklist")
@staff_only_slash()
@app_commands.describe(user="User to blacklist")
async def blacklist_slash(interaction: Interaction, user: discord.User):
    if str(user.id) not in data["blacklist"]:
        data["blacklist"].append(str(user.id))
        save_data()
    await interaction.response.send_message(f"🚫 {user.mention} has been blacklisted.")
    log_command(interaction.user, "/blacklist", interaction.channel)


@tree.command(name="unblacklist", description="Admin/Pookie: remove a user from blacklist")
@staff_only_slash()
@app_commands.describe(user="User to unblacklist")
async def unblacklist_slash(interaction: Interaction, user: discord.User):
    if str(user.id) in data["blacklist"]:
        data["blacklist"].remove(str(user.id))
        save_data()
    await interaction.response.send_message(f"✅ {user.mention} removed from blacklist.")
    log_command(interaction.user, "/unblacklist", interaction.channel)


@tree.command(name="add_blocked_word", description="Admin/Pookie: add blocked word")
@staff_only_slash()
@app_commands.describe(word="Word/phrase to block")
async def add_blocked_word_slash(interaction: Interaction, word: str):
    if word not in data["blocked_words"]:
        data["blocked_words"].append(word)
        save_data()
    await interaction.response.send_message(f"🚫 Added blocked word: `{word}`")
    log_command(interaction.user, "/add_blocked_word", interaction.channel)


@tree.command(name="remove_blocked_word", description="Admin/Pookie: remove blocked word")
@staff_only_slash()
@app_commands.describe(word="Word/phrase to remove from blocked list")
async def remove_blocked_word_slash(interaction: Interaction, word: str):
    try:
        data["blocked_words"].remove(word)
        save_data()
        await interaction.response.send_message(f"✅ Removed blocked word: `{word}`")
    except ValueError:
        await interaction.response.send_message("Word not in list.")
    log_command(interaction.user, "/remove_blocked_word", interaction.channel)


@tree.command(name="show_blocked_words", description="Show blocked words")
async def show_blocked_words_slash(interaction: Interaction):
    if not data["blocked_words"]:
        await interaction.response.send_message("No blocked words configured.")
    else:
        await interaction.response.send_message("🚫 **Blocked words:**\n" + ", ".join(f"`{w}`" for w in data["blocked_words"]))


@tree.command(name="ban", description="Admin/Pookie: ban a member")
@staff_only_slash()
@app_commands.describe(user="Member to ban", reason="Reason")
async def ban_slash(interaction: Interaction, user: discord.User, reason: str = "No reason provided"):
    if not interaction.guild:
        return await interaction.response.send_message("This works only in servers.", ephemeral=True)
    try:
        await interaction.guild.ban(user, reason=reason, delete_message_days=0)
        await interaction.response.send_message(f"🔨 Banned {user.mention}. Reason: {reason}")
        log_command(interaction.user, f"/ban {user.id}", interaction.channel)
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to ban that user.", ephemeral=True)


@tree.command(name="kick", description="Admin/Pookie: kick a member")
@staff_only_slash()
@app_commands.describe(member="Member to kick", reason="Reason")
async def kick_slash(interaction: Interaction, member: discord.Member, reason: str = "No reason provided"):
    if not interaction.guild:
        return await interaction.response.send_message("This works only in servers.", ephemeral=True)
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(f"👢 Kicked {member.mention}. Reason: {reason}")
        log_command(interaction.user, f"/kick {member.id}", interaction.channel)
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to kick that member.", ephemeral=True)


# ===========================
# Slash: Say (public/admin)
# ===========================
@tree.command(name="say", description="Public say (pings disabled)")
@app_commands.describe(message="Message to send")
async def say_slash(interaction: Interaction, message: str):
    # Strip obvious ping tokens (defense-in-depth; allowed_mentions blocks anyway)
    safe = message.replace("@everyone", "everyone").replace("@here", "here")
    await interaction.response.send_message(safe, allowed_mentions=ALLOW_NONE)
    log_command(interaction.user, "/say", interaction.channel)


@tree.command(name="say_admin", description="Admin/Pookie say (pings allowed)")
@staff_only_slash()
@app_commands.describe(message="Message to send (mentions allowed)")
async def say_admin_slash(interaction: Interaction, message: str):
    await interaction.response.send_message(message, allowed_mentions=ALLOW_ALL)
    log_command(interaction.user, "/say_admin", interaction.channel)


# ===========================
# Slash: Triggers (exact match)
# ===========================
@tree.command(name="trigger_add", description="Admin/Pookie: add/update a trigger reply")
@staff_only_slash()
@app_commands.describe(word="Exact word to match", reply="Reply (use {user} to mention the author)")
async def trigger_add_slash(interaction: Interaction, word: str, reply: str):
    data["triggers"][word] = reply
    save_data()
    await interaction.response.send_message(f"✅ Trigger set: `{word}` → `{reply}`")
    log_command(interaction.user, "/trigger_add", interaction.channel)


@tree.command(name="trigger_remove", description="Admin/Pookie: remove a trigger")
@staff_only_slash()
@app_commands.describe(word="Exact word to remove")
async def trigger_remove_slash(interaction: Interaction, word: str):
    if word in data["triggers"]:
        data["triggers"].pop(word)
        save_data()
        await interaction.response.send_message(f"🗑️ Removed trigger `{word}`")
    else:
        await interaction.response.send_message("No such trigger.")
    log_command(interaction.user, "/trigger_remove", interaction.channel)


@tree.command(name="showtrigger", description="Show all exact-match triggers")
async def showtrigger_slash(interaction: Interaction):
    if not data["triggers"]:
        await interaction.response.send_message("No triggers set.")
    else:
        lines = [f"`{k}` → {v}" for k, v in data["triggers"].items()]
        await interaction.response.send_message("**Triggers:**\n" + "\n".join(lines))


# ===========================
# Slash: Logs & Log Channel
# ===========================
@tree.command(name="logs", description="Show the last N logs")
@app_commands.describe(amount="How many logs (default 10)")
async def logs_slash(interaction: Interaction, amount: int = 10):
    # Only staff can view logs
    if not has_staff_access(interaction.user.id):
        return await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
    amount = max(1, min(50, amount))
    logs = data["logs"][-amount:]
    if not logs:
        return await interaction.response.send_message("No logs yet.")
    embed = Embed(title=f"Last {amount} logs", color=discord.Color.blue())
    for entry in logs:
        embed.add_field(
            name=entry["command"],
            value=f"**User:** {entry['user']} (`{entry['user_id']}`)\n**Channel:** {entry['channel']}\n**Time:** {entry['time']}",
            inline=False
        )
    await interaction.response.send_message(embed=embed)
    log_command(interaction.user, f"/logs {amount}", interaction.channel)


@tree.command(name="set_log_channel", description="Admin/Pookie: set a channel to mirror command logs")
@staff_only_slash()
@app_commands.describe(channel="Channel to receive log embeds")
async def set_log_channel_slash(interaction: Interaction, channel: discord.TextChannel):
    data["log_channel"] = str(channel.id)
    save_data()
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}")
    log_command(interaction.user, "/set_log_channel", interaction.channel)


@tree.command(name="remove_log_channel", description="Admin/Pookie: disable log channel mirroring")
@staff_only_slash()
async def remove_log_channel_slash(interaction: Interaction):
    data["log_channel"] = None
    save_data()
    await interaction.response.send_message("🛑 Log channel mirroring disabled.")
    log_command(interaction.user, "/remove_log_channel", interaction.channel)


# ===========================
# Slash: User Info / Avatar
# ===========================
@tree.command(name="avatar", description="Show a user's avatar")
@app_commands.describe(user="User (optional)")
async def avatar_slash(interaction: Interaction, user: discord.User | None = None):
    user = user or interaction.user
    embed = Embed(title=f"Avatar — {user}", color=discord.Color.green())
    embed.set_image(url=user.display_avatar.url)
    await interaction.response.send_message(embed=embed)
    log_command(interaction.user, "/avatar", interaction.channel)


@tree.command(name="userinfo", description="Show a user's info")
@app_commands.describe(user="User (optional)")
async def userinfo_slash(interaction: Interaction, user: discord.Member | None = None):
    if not interaction.guild:
        return await interaction.response.send_message("Guild only.", ephemeral=True)
    user = user or interaction.user
    roles = ", ".join(r.mention for r in user.roles if r.name != "@everyone")
    embed = Embed(title=f"User Info — {user}", color=discord.Color.purple())
    embed.add_field(name="ID", value=str(user.id), inline=False)
    embed.add_field(name="Joined", value=str(user.joined_at) if user.joined_at else "N/A", inline=False)
    embed.add_field(name="Created", value=str(user.created_at), inline=False)
    embed.add_field(name="Roles", value=roles or "None", inline=False)
    embed.set_thumbnail(url=user.display_avatar.url)
    await interaction.response.send_message(embed=embed)
    log_command(interaction.user, "/userinfo", interaction.channel)


# ===========================
# Slash: Snipe / Esnipe
# ===========================
@tree.command(name="snipe", description="Show last deleted message in this channel")
async def snipe_slash(interaction: Interaction):
    data_t = snipe_cache.get(interaction.channel.id)
    if not data_t:
        return await interaction.response.send_message("Nothing to snipe.", ephemeral=True)
    content, author, tstamp = data_t
    embed = Embed(title="Snipe", description=content or "*<no content>*", color=discord.Color.red())
    embed.set_footer(text=f"By {author} at {tstamp.strftime('%H:%M:%S UTC')}")
    await interaction.response.send_message(embed=embed)
    log_command(interaction.user, "/snipe", interaction.channel)


@tree.command(name="esnipe", description="Show last edit in this channel")
async def esnipe_slash(interaction: Interaction):
    data_t = esnipe_cache.get(interaction.channel.id)
    if not data_t:
        return await interaction.response.send_message("Nothing to esnipe.", ephemeral=True)
    before, after, author, tstamp = data_t
    embed = Embed(
        title="E-Snipe",
        description=f"**Before:** {before or '*<no content>*'}\n**After:** {after or '*<no content>*'}",
        color=discord.Color.orange()
    )
    embed.set_footer(text=f"By {author} at {tstamp.strftime('%H:%M:%S UTC')}")
    await interaction.response.send_message(embed=embed)
    log_command(interaction.user, "/esnipe", interaction.channel)


# ===========================
# Slash: Cat + Channel Setters
# ===========================
@tree.command(name="cat", description="Get a random cat image")
async def cat_slash(interaction: Interaction):
    url = await fetch_cat_url()
    if not url:
        return await interaction.response.send_message("Couldn't fetch a cat right now 😿")
    await interaction.response.send_message(url)
    log_command(interaction.user, "/cat", interaction.channel)


@tree.command(name="setdailycatchannel", description="Admin/Pookie: set daily 11:00 IST cat channel")
@staff_only_slash()
@app_commands.describe(channel="Channel to receive daily cats")
async def setdailycatchannel_slash(interaction: Interaction, channel: discord.TextChannel):
    data["daily_cat_channel"] = str(channel.id)
    save_data()
    await interaction.response.send_message(f"✅ Daily cat channel set to {channel.mention}")
    log_command(interaction.user, "/setdailycatchannel", interaction.channel)


@tree.command(name="sethourlycatchannel", description="Admin/Pookie: set hourly cat channel")
@staff_only_slash()
@app_commands.describe(channel="Channel to receive hourly cats")
async def sethourlycatchannel_slash(interaction: Interaction, channel: discord.TextChannel):
    data["hourly_cat_channel"] = str(channel.id)
    save_data()
    await interaction.response.send_message(f"✅ Hourly cat channel set to {channel.mention}")
    log_command(interaction.user, "/sethourlycatchannel", interaction.channel)


@tree.command(name="removehourlycatchannel", description="Admin/Pookie: stop hourly cat posts")
@staff_only_slash()
async def removehourlycatchannel_slash(interaction: Interaction):
    data["hourly_cat_channel"] = None
    save_data()
    await interaction.response.send_message("🛑 Hourly cat posting disabled.")
    log_command(interaction.user, "/removehourlycatchannel", interaction.channel)


# ===========================
# Slash: Fun Commands (No API keys)
# ===========================
@tree.command(name="eightball", description="Ask the magic 8-ball")
@app_commands.describe(question="Your question")
async def eightball_slash(interaction: Interaction, question: str):
    responses = [
        "It is certain.", "Without a doubt.", "You may rely on it.",
        "Most likely.", "Outlook good.", "Yes.",
        "Reply hazy, try again.", "Ask again later.", "Better not tell you now.",
        "Don't count on it.", "My reply is no.", "Very doubtful."
    ]
    await interaction.response.send_message(f"🎱 {random.choice(responses)}")
    log_command(interaction.user, "/eightball", interaction.channel)


@tree.command(name="coinflip", description="Flip a coin")
async def coinflip_slash(interaction: Interaction):
    await interaction.response.send_message(random.choice(["🪙 Heads", "🪙 Tails"]))
    log_command(interaction.user, "/coinflip", interaction.channel)


@tree.command(name="roll", description="Roll a die (1–6)")
async def roll_slash(interaction: Interaction):
    await interaction.response.send_message(f"🎲 You rolled a **{random.randint(1,6)}**")
    log_command(interaction.user, "/roll", interaction.channel)


@tree.command(name="rps", description="Rock Paper Scissors")
@app_commands.describe(choice="rock, paper, or scissors")
async def rps_slash(interaction: Interaction, choice: app_commands.Choice[str]):
    # To avoid the “choices setter” bug, define choices at decorator-time:
    await interaction.response.send_message("This command was updated; use `/rps2` instead.")
    log_command(interaction.user, "/rps", interaction.channel)

# Proper RPS with static choices:
@tree.command(name="rps2", description="Rock Paper Scissors (fixed)")
@app_commands.describe(choice="Your move")
@app_commands.choices(choice=[
    app_commands.Choice(name="rock", value="rock"),
    app_commands.Choice(name="paper", value="paper"),
    app_commands.Choice(name="scissors", value="scissors"),
])
async def rps2_slash(interaction: Interaction, choice: app_commands.Choice[str]):
    bot_choice = random.choice(["rock","paper","scissors"])
    c = choice.value
    if c == bot_choice:
        result = "Tie!"
    elif (c=="rock" and bot_choice=="scissors") or (c=="scissors" and bot_choice=="paper") or (c=="paper" and bot_choice=="rock"):
        result = "You win!"
    else:
        result = "I win!"
    await interaction.response.send_message(f"You: **{c}**, Me: **{bot_choice}** → **{result}**")
    log_command(interaction.user, "/rps2", interaction.channel)


@tree.command(name="joke", description="Random one-liner")
async def joke_slash(interaction: Interaction):
    jokes = [
        "Why don’t programmers like nature? It has too many bugs.",
        "I would tell you a UDP joke, but you might not get it.",
        "There are 10 kinds of people in the world: those who understand binary and those who don’t.",
        "I told my computer I needed a break, and it said no problem — it’ll go to sleep."
    ]
    await interaction.response.send_message(random.choice(jokes))
    log_command(interaction.user, "/joke", interaction.channel)


@tree.command(name="dadjoke", description="Classic dad joke")
async def dadjoke_slash(interaction: Interaction):
    jokes = [
        "I’m reading a book about anti-gravity. It’s impossible to put down!",
        "Why did the scarecrow win an award? He was outstanding in his field.",
        "I only know 25 letters of the alphabet. I don’t know y.",
        "I used to be a baker, then I kneaded a change."
    ]
    await interaction.response.send_message(random.choice(jokes))
    log_command(interaction.user, "/dadjoke", interaction.channel)


@tree.command(name="quote", description="Random motivational quote")
async def quote_slash(interaction: Interaction):
    quotes = [
        "Believe you can and you're halfway there.",
        "Do or do not. There is no try.",
        "It always seems impossible until it's done.",
        "Dream big and dare to fail."
    ]
    await interaction.response.send_message(random.choice(quotes))
    log_command(interaction.user, "/quote", interaction.channel)


@tree.command(name="compliment", description="Get a wholesome compliment")
async def compliment_slash(interaction: Interaction):
    compliments = [
        "You are doing great! 🌟",
        "Your energy is contagious.",
        "You make the world better just by being in it.",
        "Keep going — you’ve got this!"
    ]
    await interaction.response.send_message(random.choice(compliments))
    log_command(interaction.user, "/compliment", interaction.channel)


@tree.command(name="roast", description="A tiny roast (fun)")
async def roast_slash(interaction: Interaction):
    roasts = [
        "If I wanted to hear from someone with your IQ, I’d talk to a rock.",
        "You’re like a cloud. When you disappear, it’s a beautiful day.",
        "I’d explain it, but I left my crayons at home."
    ]
    await interaction.response.send_message(random.choice(roasts))
    log_command(interaction.user, "/roast", interaction.channel)


@tree.command(name="reverse", description="Reverse your text")
@app_commands.describe(text="Text to reverse")
async def reverse_slash(interaction: Interaction, text: str):
    await interaction.response.send_message(text[::-1])
    log_command(interaction.user, "/reverse", interaction.channel)


@tree.command(name="choose", description="Choose one option for you")
@app_commands.describe(options="Comma-separated options")
async def choose_slash(interaction: Interaction, options: str):
    parts = [p.strip() for p in options.split(",") if p.strip()]
    if not parts:
        return await interaction.response.send_message("Give me some options separated by commas.")
    await interaction.response.send_message(f"I choose: **{random.choice(parts)}**")
    log_command(interaction.user, "/choose", interaction.channel)


@tree.command(name="fact", description="Random fun fact")
async def fact_slash(interaction: Interaction):
    facts = [
        "Honey never spoils.",
        "Bananas are berries, but strawberries aren’t.",
        "Octopuses have three hearts.",
        "Humans share 60% of their DNA with bananas."
    ]
    await interaction.response.send_message(random.choice(facts))
    log_command(interaction.user, "/fact", interaction.channel)


# ===========================
# Slash: Show Commands
# ===========================
@tree.command(name="showcommands", description="Show only the commands you can use")
async def showcommands_slash(interaction: Interaction):
    # Build a list dynamically based on permissions
    user_id = interaction.user.id
    can_staff = has_staff_access(user_id)

    # All slash commands:
    public_cmds = [
        "cat","roll","coinflip","eightball","joke","dadjoke","quote","compliment","roast",
        "reverse","choose","fact","snipe","esnipe","say","avatar","userinfo","showtrigger",
        "showcommands"
    ]
    staff_cmds = [
        "say_admin","logs",
        "blacklist","unblacklist","add_blocked_word","remove_blocked_word","show_blocked_words",
        "add_pookie","remove_pookie","list_pookie",
        "add_admin","remove_admin","show_admins",
        "ban","kick",
        "set_log_channel","remove_log_channel",
        "setdailycatchannel","sethourlycatchannel","removehourlycatchannel",
        "trigger_add","trigger_remove"
    ]

    viewable = public_cmds + (staff_cmds if can_staff else [])
    await interaction.response.send_message("**Available commands:**\n" + ", ".join(f"`/{c}`" for c in sorted(viewable)))


# ===========================
# Prefix versions (selected)
#  - Keep minimal to avoid duplication; slash is primary
# ===========================
@bot.command(name="say")
async def say_prefix(ctx: commands.Context, *, message: str):
    safe = message.replace("@everyone","everyone").replace("@here","here")
    await ctx.send(safe, allowed_mentions=ALLOW_NONE)
    log_command(ctx.author, "?say", ctx.channel)

@bot.command(name="say_admin")
async def say_admin_prefix(ctx: commands.Context, *, message: str):
    if not has_staff_access(ctx.author.id):
        return
    await ctx.send(message, allowed_mentions=ALLOW_ALL)
    log_command(ctx.author, "?say_admin", ctx.channel)

@bot.command(name="cat")
async def cat_prefix(ctx: commands.Context):
    url = await fetch_cat_url()
    if not url:
        return await ctx.send("Couldn't fetch a cat right now 😿")
    await ctx.send(url)
    log_command(ctx.author, "?cat", ctx.channel)

@bot.command(name="purge")
@commands.has_permissions(manage_messages=True)
async def purge_prefix(ctx: commands.Context, amount: int):
    # You asked earlier for a purge command (max 100)
    amount = max(1, min(100, amount))
    deleted = await ctx.channel.purge(limit=amount+1)  # +1 includes the command message
    await ctx.send(f"🧹 Deleted {len(deleted)-1} messages.", delete_after=5)
    log_command(ctx.author, f"?purge {amount}", ctx.channel)


# ===========================
# Run the bot
# ===========================
if not DISCORD_TOKEN:
    print("ERROR: DISCORD_BOT_TOKEN not set in environment.")
else:
    bot.run(DISCORD_TOKEN)
