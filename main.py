# main.py
# Full single-file Discord bot with Flask keepalive (Render-ready)
# Features included (summary):
# - Prefix "?" and slash "/" commands
# - Owner/Admin/Pookie systems (owner ID from env, defaults to your ID)
# - Blacklist and blocked-words with bypass protection
# - AFK system (notify when mentioned) and /afk, ?afk
# - Triggers (exact-word auto-responder) add/remove/show
# - Snipe / Esnipe (deleted & edited messages) with ⬅️/➡️ buttons
# - Cat commands: /cat, ?cat, set daily 11:00 IST channel, set hourly channel
# - Logging: set_log_channel, disable_log_channel, logs (view)
# - Moderation: /ban (member), ?ban (ID or mention), /kick, ?kick, purge
# - Say (no pings) and say_admin (pings allowed)
# - Showcommands interactive (shows only commands user can use)
# - Askforcommand to notify owner via DM and log
# - Restart (exit process) & refresh commands (sync)
# - JSON single-file persistence: data.json
# - Flask keepalive on port from environment or 8080
# - Starts tasks inside on_ready to avoid "no running event loop" error

import os, json, re, random, asyncio, atexit, sys, platform, secrets
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from threading import Thread
from typing import Optional, List, Dict, Any

import aiohttp
import discord
from discord.ext import commands, tasks
from discord import app_commands
from flask import Flask

# ------------------------- ENV & CONFIG -------------------------
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN not set in environment variables")

def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        # try to extract digits if user pasted "OWNER_ID = 123"
        digits = "".join(ch for ch in raw if ch.isdigit())
        if digits:
            return int(digits)
        return default

OWNER_ID = _int_env("OWNER_ID", 1319292111325106296)  # your default owner id
# add extra default admin IDs requested earlier
DEFAULT_EXTRA_ADMINS = {1380315427992768633, 909468887098216499}

CAT_API_KEY = os.getenv("CAT_API_KEY", "").strip()
RENDER_API_KEY = os.getenv("RENDER_API_KEY", "").strip()
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "").strip()
TZ_NAME = os.getenv("TZ", "Asia/Kolkata").strip() or "Asia/Kolkata"

try:
    BOT_TZ = ZoneInfo(TZ_NAME)
except Exception:
    BOT_TZ = ZoneInfo("Asia/Kolkata")

PREFIX = "?"
DATA_FILE = "data.json"
SNIPES_KEEP = 50
LOGS_KEEP = 2000

# ------------------------- FLASK KEEPALIVE -------------------------
FLASK_PORT = int(os.getenv("PORT", "8080"))
flask_app = Flask("bot_keepalive")

@flask_app.route("/")
def alive():
    return "OK", 200

def run_flask():
    # run in separate thread to not block bot
    flask_app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, use_reloader=False)

Thread(target=run_flask, daemon=True).start()

# ------------------------- BOT SETUP -------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.messages = True

bot = commands.Bot(command_prefix=commands.when_mentioned_or(PREFIX), intents=intents, help_command=None)
tree = bot.tree

# ------------------------- STORAGE -------------------------
DEFAULT_DATA = {
    "admins": [],              # list of user ids (int)
    "pookies": [],             # list of user ids (int)
    "blacklist": [],           # list of user ids (int)
    "blocked_words": [],       # list of strings
    "triggers": {},            # word -> reply
    "log_channel": None,       # int or None
    "cat_channel": None,       # daily 11:00 IST channel id
    "hourly_cat_channel": None,# hourly channel id
    "logs": [],                # list of log entries (dict)
    "afk": {}                  # user_id (str) -> {"reason": str, "since": iso}
}

def _init_data():
    if not os.path.exists(DATA_FILE):
        data = DEFAULT_DATA.copy()
        # default admins: owner + extras
        data["admins"] = list({OWNER_ID} | DEFAULT_EXTRA_ADMINS)
        save_data(data)
    return load_data()

def load_data() -> Dict[str, Any]:
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = DEFAULT_DATA.copy()
    # ensure keys
    for k, v in DEFAULT_DATA.items():
        if k not in d:
            d[k] = v.copy() if isinstance(v, (list, dict)) else v
    # ensure owner and default extras in admins
    admins = set(map(int, d.get("admins", [])))
    admins.add(OWNER_ID)
    admins |= DEFAULT_EXTRA_ADMINS
    d["admins"] = list(admins)
    return d

def save_data(d: Dict[str, Any]):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)

DATA = _init_data()

def reload_data():
    global DATA
    DATA = load_data()
    return DATA

# ------------------------- UTILITIES -------------------------
def is_owner(u: discord.abc.User) -> bool:
    return int(u.id) == int(OWNER_ID)

def is_admin_user(u: discord.abc.User) -> bool:
    d = reload_data()
    return int(u.id) in set(map(int, d.get("admins", []))) or is_owner(u)

def is_pookie_user(u: discord.abc.User) -> bool:
    d = reload_data()
    return int(u.id) in set(map(int, d.get("pookies", []))) or is_admin_user(u)

def is_blacklisted_user(u: discord.abc.User) -> bool:
    d = reload_data()
    return int(u.id) in set(map(int, d.get("blacklist", [])))

def sanitize_no_mentions(text: str) -> str:
    return text.replace("@", "@\u200b")

def add_file_log(kind: str, message: str):
    d = reload_data()
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, "message": message}
    d.setdefault("logs", [])
    d["logs"].append(entry)
    if len(d["logs"]) > LOGS_KEEP:
        d["logs"] = d["logs"][-LOGS_KEEP:]
    save_data(d)

async def send_log_embed(guild: Optional[discord.Guild], title: str, description: str, color=discord.Color.blurple(), fields: Optional[List[tuple]] = None):
    d = reload_data()
    ch_id = d.get("log_channel")
    if not ch_id:
        return
    ch = bot.get_channel(ch_id)
    if not ch:
        # maybe in guild
        if guild:
            ch = guild.get_channel(ch_id) if hasattr(guild, "get_channel") else None
    if not ch:
        return
    emb = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
    if fields:
        for n, v, i in fields:
            emb.add_field(name=n, value=v, inline=i)
    try:
        await ch.send(embed=emb)
    except Exception:
        pass

def exact_word_present(text: str, word: str) -> bool:
    pattern = r"\b" + re.escape(word) + r"\b"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None

# ------------------------- SNIPES -------------------------
SNIPES: Dict[int, List[Dict[str, Any]]] = {}
ESNIPES: Dict[int, List[Dict[str, Any]]] = {}

def push_snipe(store: Dict[int, List[Dict[str, Any]]], channel_id: int, entry: Dict[str, Any]):
    lst = store.setdefault(channel_id, [])
    lst.append(entry)
    if len(lst) > SNIPES_KEEP:
        del lst[0]

class SnipeView(discord.ui.View):
    def __init__(self, items: List[Dict[str, Any]]):
        super().__init__(timeout=60)
        self.items = items
        self.idx = len(items) - 1

    def make_embed(self):
        data = self.items[self.idx]
        emb = discord.Embed(title=f"Snipe [{self.idx+1}/{len(self.items)}]", color=discord.Color.blurple())
        if "content" in data:
            emb.add_field(name="Content", value=data.get("content", "*empty*")[:1024], inline=False)
        else:
            emb.add_field(name="Before", value=data.get("before","")[:1024] or "*empty*", inline=False)
            emb.add_field(name="After", value=data.get("after","")[:1024] or "*empty*", inline=False)
        emb.set_author(name=data.get("author_tag","Unknown"), icon_url=data.get("avatar_url",""))
        emb.set_footer(text=f"{data.get('time','')}")
        return emb

    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.secondary)
    async def prev(self, inter: discord.Interaction, btn: discord.ui.Button):
        if self.idx > 0:
            self.idx -= 1
        await inter.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="➡️", style=discord.ButtonStyle.secondary)
    async def nxt(self, inter: discord.Interaction, btn: discord.ui.Button):
        if self.idx < len(self.items)-1:
            self.idx += 1
        await inter.response.edit_message(embed=self.make_embed(), view=self)

# ------------------------- CAT FETCH -------------------------
async def fetch_random_cat(session: aiohttp.ClientSession) -> Optional[str]:
    headers = {}
    if CAT_API_KEY:
        headers["x-api-key"] = CAT_API_KEY
    try:
        async with session.get("https://api.thecatapi.com/v1/images/search", headers=headers, timeout=20) as r:
            if r.status == 200:
                js = await r.json()
                if isinstance(js, list) and js:
                    return js[0].get("url")
    except Exception:
        return None
    # fallback
    return "https://cataas.com/cat"

# ------------------------- EVENTS & LOGGING -------------------------
@bot.event
async def on_ready():
    # sync commands once
    try:
        await tree.sync()
    except Exception as e:
        print("Slash sync error:", e)
    # presence
    await bot.change_presence(status=discord.Status.dnd, activity=discord.Streaming(name="Max Verstappen", url="https://twitch.tv/max"))
    # start scheduled tasks now that loop is running
    if not minute_scheduler.is_running():
        minute_scheduler.start()
    print(f"Ready: {bot.user} | Guilds: {len(bot.guilds)}")
    add_file_log("system", f"Bot ready: {bot.user} | guilds={len(bot.guilds)}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Clear AFK if author was AFK
    d = reload_data()
    if str(message.author.id) in d.get("afk", {}):
        d["afk"].pop(str(message.author.id), None)
        save_data(d)
        try:
            await message.channel.send(f"✅ Welcome back {message.author.mention}. AFK removed.")
        except Exception:
            pass

    # If mentions include AFK users notify
    if message.mentions:
        for u in message.mentions:
            info = d.get("afk", {}).get(str(u.id))
            if info:
                reason = info.get("reason", "AFK")
                since = info.get("since")
                try:
                    ts = int(datetime.fromisoformat(since).timestamp())
                    await message.reply(f"{u.mention} is AFK: **{sanitize_no_mentions(reason)}** (since <t:{ts}:R>)", mention_author=False)
                except Exception:
                    await message.reply(f"{u.mention} is AFK: **{sanitize_no_mentions(reason)}**", mention_author=False)

    # Blocked words (bypass protection: compact characters)
    content_compact = re.sub(r"[\s\-\_\.]", "", message.content.lower())
    for w in d.get("blocked_words", []):
        wc = re.sub(r"[\s\-\_\.]", "", w.lower())
        if wc and wc in content_compact:
            try:
                await message.delete()
            except Exception:
                pass
            await send_log_embed(message.guild, "Blocked Word", f"{message.author.mention} used blocked word `{w}` in {message.channel.mention}")
            add_file_log("blocked_word", f"{message.author} used blocked word {w} in {message.channel}")
            return

    # Triggers (exact whole word)
    for word, reply in d.get("triggers", {}).items():
        if exact_word_present(message.content, word):
            out = reply.replace("{user}", message.author.mention)
            try:
                await message.channel.send(out)
            except Exception:
                pass
            break

    await bot.process_commands(message)

@bot.event
async def on_message_delete(message: discord.Message):
    if message.author and message.author.bot:
        return
    push_snipe(SNIPES, message.channel.id, {
        "author_id": message.author.id if message.author else None,
        "author_tag": str(message.author) if message.author else "Unknown",
        "avatar_url": getattr(message.author.display_avatar, "url", ""),
        "content": message.content or "",
        "time": datetime.now(timezone.utc).isoformat()
    })
    await send_log_embed(message.guild, "Message Deleted", f"**{message.author}** deleted a message in {message.channel.mention}\n```{sanitize_no_mentions(message.content)[:1000]}```", discord.Color.red())
    add_file_log("delete", f"{message.author} deleted message in {message.channel}: {message.content[:200]}")

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author and before.author.bot:
        return
    if before.content == after.content:
        return
    push_snipe(ESNIPES, before.channel.id, {
        "author_id": before.author.id if before.author else None,
        "author_tag": str(before.author) if before.author else "Unknown",
        "avatar_url": getattr(before.author.display_avatar, "url", ""),
        "before": before.content,
        "after": after.content,
        "time": datetime.now(timezone.utc).isoformat()
    })
    await send_log_embed(before.guild, "Message Edited", f"**{before.author}** edited a message in {before.channel.mention}\n**Before:**\n```{sanitize_no_mentions(before.content)[:800]}```\n**After:**\n```{sanitize_no_mentions(after.content)[:800]}```", discord.Color.orange())
    add_file_log("edit", f"{before.author} edited message in {before.channel}")

# ------------------------- SNIPE COMMANDS -------------------------
@tree.command(name="snipe", description="Show the most recent deleted messages (this channel).")
async def slash_snipe(inter: discord.Interaction):
    items = SNIPES.get(inter.channel_id, [])
    if not items:
        return await inter.response.send_message("Nothing to snipe here.", ephemeral=True)
    view = SnipeView(items)
    await inter.response.send_message(embed=view.make_embed(), view=view)

@tree.command(name="esnipe", description="Show the most recent edited messages (this channel).")
async def slash_esnipe(inter: discord.Interaction):
    items = ESNIPES.get(inter.channel_id, [])
    if not items:
        return await inter.response.send_message("No edits to snipe here.", ephemeral=True)
    view = SnipeView(items)
    await inter.response.send_message(embed=view.make_embed(), view=view)

# ------------------------- SCHEDULED TASKS -------------------------
@tasks.loop(minutes=1)
async def minute_scheduler():
    # runs every minute; handle daily 11:00 IST and hourly cat
    now = datetime.now(BOT_TZ)
    d = reload_data()
    # Daily at 11:00 IST
    if d.get("cat_channel"):
        try:
            if now.hour == 11 and now.minute == 0:
                ch = bot.get_channel(d["cat_channel"])
                if ch:
                    async with aiohttp.ClientSession() as s:
                        url = await fetch_random_cat(s)
                    if url:
                        await ch.send(url)
                    await asyncio.sleep(60)
        except Exception:
            pass
    # Hourly at minute 0
    if d.get("hourly_cat_channel"):
        try:
            if now.minute == 0:
                ch2 = bot.get_channel(d["hourly_cat_channel"])
                if ch2:
                    async with aiohttp.ClientSession() as s:
                        url = await fetch_random_cat(s)
                    if url:
                        await ch2.send(url)
                    await asyncio.sleep(60)
        except Exception:
            pass

# ------------------------- COMMAND HELPERS / CHECKS -------------------------
def owner_or_admin_check():
    async def predicate(inter: discord.Interaction):
        if is_admin_user(inter.user) or is_owner(inter.user):
            return True
        await inter.response.send_message("Admins only.", ephemeral=True)
        return False
    return app_commands.check(predicate)

def admin_or_pookie_check():
    async def predicate(inter: discord.Interaction):
        if is_pookie_user(inter.user) or is_admin_user(inter.user) or is_owner(inter.user):
            return True
        await inter.response.send_message("Admins or Pookie only.", ephemeral=True)
        return False
    return app_commands.check(predicate)

def blacklist_check():
    async def predicate(inter: discord.Interaction):
        if is_blacklisted_user(inter.user):
            await inter.response.send_message("You are blacklisted.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

# ------------------------- MODERATION COMMANDS -------------------------
@tree.command(name="ban", description="Ban a member (must be in server).")
@admin_or_pookie_check()
@app_commands.describe(member="Member to ban", reason="Reason")
async def slash_ban(inter: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
    try:
        await member.ban(reason=reason or "No reason provided", delete_message_days=0)
        await inter.response.send_message(f"Banned {member.mention}.")
        await send_log_embed(inter.guild, "Ban", f"{inter.user.mention} banned {member.mention}\nReason: {sanitize_no_mentions(reason or 'No reason')}", discord.Color.red())
        add_file_log("mod", f"{inter.user} banned {member}")
    except Exception as e:
        await inter.response.send_message(f"Failed to ban: {e}", ephemeral=True)

@bot.command(name="ban")
async def prefix_ban(ctx: commands.Context, target: str, *, reason: str = ""):
    if not is_pookie_user(ctx.author) and not is_admin_user(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("Admins or Pookie only.", mention_author=False)
    uid = None
    m = re.match(r"<@!?(\d+)>", target)
    if m:
        uid = int(m.group(1))
    else:
        try:
            uid = int(target)
        except Exception:
            return await ctx.reply("Provide mention or ID.", mention_author=False)
    member = ctx.guild.get_member(uid)
    if member:
        try:
            await member.ban(reason=reason or "No reason", delete_message_days=0)
            await ctx.reply(f"Banned {member.mention}.", mention_author=False)
            await send_log_embed(ctx.guild, "Ban", f"{ctx.author.mention} banned {member.mention}\nReason: {sanitize_no_mentions(reason or 'No reason')}", discord.Color.red())
            add_file_log("mod", f"{ctx.author} banned {member}")
        except Exception as e:
            await ctx.reply(f"Failed to ban: {e}", mention_author=False)
    else:
        # ban by ID
        try:
            await ctx.guild.ban(discord.Object(id=uid), reason=reason or "No reason", delete_message_days=0)
            await ctx.reply(f"Banned <@{uid}> (by ID).", mention_author=False)
            await send_log_embed(ctx.guild, "Ban (ID)", f"{ctx.author.mention} banned <@{uid}> by ID", discord.Color.red())
            add_file_log("mod", f"{ctx.author} banned id {uid}")
        except Exception as e:
            await ctx.reply(f"Failed to ban by ID: {e}", mention_author=False)

@tree.command(name="kick", description="Kick a member.")
@admin_or_pookie_check()
@app_commands.describe(member="Member to kick", reason="Reason")
async def slash_kick(inter: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
    try:
        await member.kick(reason=reason or "No reason")
        await inter.response.send_message(f"Kicked {member.mention}.")
        await send_log_embed(inter.guild, "Kick", f"{inter.user.mention} kicked {member.mention}\nReason: {sanitize_no_mentions(reason or 'No reason')}", discord.Color.orange())
        add_file_log("mod", f"{inter.user} kicked {member}")
    except Exception as e:
        await inter.response.send_message(f"Failed to kick: {e}", ephemeral=True)

@bot.command(name="kick")
async def prefix_kick(ctx: commands.Context, target: str, *, reason: str = ""):
    if not is_pookie_user(ctx.author) and not is_admin_user(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("Admins or Pookie only.", mention_author=False)
    member = None
    m = re.match(r"<@!?(\d+)>", target)
    if m:
        member = ctx.guild.get_member(int(m.group(1)))
    else:
        if target.isdigit():
            member = ctx.guild.get_member(int(target))
    if not member:
        return await ctx.reply("Member not found in server.", mention_author=False)
    try:
        await member.kick(reason=reason or "No reason")
        await ctx.reply(f"Kicked {member.mention}.", mention_author=False)
        await send_log_embed(ctx.guild, "Kick", f"{ctx.author.mention} kicked {member.mention}\nReason: {sanitize_no_mentions(reason or 'No reason')}", discord.Color.orange())
        add_file_log("mod", f"{ctx.author} kicked {member}")
    except Exception as e:
        await ctx.reply(f"Failed to kick: {e}", mention_author=False)

# Purge
@tree.command(name="purge", description="Delete up to 100 messages (admin/pookie).")
@admin_or_pookie_check()
@app_commands.describe(amount="How many messages to delete (1-100)")
async def slash_purge(inter: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    try:
        deleted = await inter.channel.purge(limit=amount)
        await inter.response.send_message(f"Deleted {len(deleted)} messages.", ephemeral=True)
        await send_log_embed(inter.guild, "Purge", f"{inter.user.mention} purged {len(deleted)} messages in {inter.channel.mention}")
        add_file_log("mod", f"{inter.user} purged {len(deleted)} messages in {inter.channel}")
    except Exception as e:
        await inter.response.send_message(f"Failed: {e}", ephemeral=True)

@bot.command(name="purge")
async def prefix_purge(ctx: commands.Context, amount: int):
    if not is_pookie_user(ctx.author):
        return await ctx.reply("Admins or Pookie only.", mention_author=False)
    amount = max(1, min(100, amount))
    try:
        deleted = await ctx.channel.purge(limit=amount)
        m = await ctx.send(f"Deleted {len(deleted)} messages.")
        await asyncio.sleep(3)
        await m.delete()
        await send_log_embed(ctx.guild, "Purge", f"{ctx.author.mention} purged {len(deleted)} messages in {ctx.channel.mention}")
        add_file_log("mod", f"{ctx.author} purged {len(deleted)}")
    except Exception as e:
        await ctx.reply(f"Failed: {e}", mention_author=False)

# ------------------------- SAY COMMANDS -------------------------
@tree.command(name="say", description="Bot repeats text (no pings).")
@blacklist_check()
@app_commands.describe(text="Text to send (pings disabled)")
async def slash_say(inter: discord.Interaction, text: str):
    safe = sanitize_no_mentions(text)
    await inter.response.send_message("Sent.", ephemeral=True)
    await inter.channel.send(safe, allowed_mentions=discord.AllowedMentions.none())
    add_file_log("command", f"{inter.user} used /say")

@bot.command(name="say")
async def prefix_say(ctx: commands.Context, *, text: str):
    if is_blacklisted_user(ctx.author):
        return await ctx.reply("You are blacklisted.", mention_author=False)
    safe = sanitize_no_mentions(text)
    await ctx.send(safe, allowed_mentions=discord.AllowedMentions.none())
    add_file_log("command", f"{ctx.author} used ?say")

@tree.command(name="say_admin", description="Admin say (pings allowed).")
@admin_or_pookie_check()
@app_commands.describe(text="Text to send (pings allowed)")
async def slash_say_admin(inter: discord.Interaction, text: str):
    await inter.response.send_message("Sent.", ephemeral=True)
    await inter.channel.send(text)
    add_file_log("command", f"{inter.user} used /say_admin")

# ------------------------- ADMIN & POOKIE MANAGEMENT -------------------------
@tree.command(name="add_admin", description="Add an admin (owner only).")
@app_commands.check(lambda inter: inter.user.id == OWNER_ID)
@app_commands.describe(user="User to make admin")
async def slash_add_admin(inter: discord.Interaction, user: discord.User):
    d = reload_data()
    if user.id not in d["admins"]:
        d["admins"].append(int(user.id))
        save_data(d)
    await inter.response.send_message(f"Added {user.mention} as admin.")

@tree.command(name="remove_admin", description="Remove an admin (owner only).")
@app_commands.check(lambda inter: inter.user.id == OWNER_ID)
@app_commands.describe(user="Admin user to remove")
async def slash_remove_admin(inter: discord.Interaction, user: discord.User):
    d = reload_data()
    if user.id == OWNER_ID:
        return await inter.response.send_message("Cannot remove the owner.", ephemeral=True)
    if user.id in d["admins"]:
        d["admins"].remove(int(user.id))
        save_data(d)
        return await inter.response.send_message(f"Removed {user.mention} from admins.")
    await inter.response.send_message("User not an admin.", ephemeral=True)

@tree.command(name="show_admins", description="List admins (owner/admin only).")
@owner_or_admin_check()
async def slash_show_admins(inter: discord.Interaction):
    d = reload_data()
    mentions = [f"<@{uid}>" for uid in d.get("admins", [])]
    await inter.response.send_message("Admins:\n" + "\n".join(mentions), ephemeral=True)

@tree.command(name="addpookie", description="Add pookie user (owner/admin).")
@owner_or_admin_check()
@app_commands.describe(user="User to add as pookie")
async def slash_add_pookie(inter: discord.Interaction, user: discord.User):
    d = reload_data()
    if user.id not in d["pookies"]:
        d["pookies"].append(int(user.id))
        save_data(d)
    await inter.response.send_message(f"Added {user.mention} as pookie.")

@tree.command(name="removepookie", description="Remove pookie (owner/admin).")
@owner_or_admin_check()
@app_commands.describe(user="User to remove from pookie")
async def slash_remove_pookie(inter: discord.Interaction, user: discord.User):
    d = reload_data()
    if user.id in d["pookies"]:
        d["pookies"].remove(int(user.id))
        save_data(d)
        return await inter.response.send_message(f"Removed {user.mention} from pookie.")
    await inter.response.send_message("User not a pookie.", ephemeral=True)

@tree.command(name="listpookie", description="List pookie users (owner/admin).")
@owner_or_admin_check()
async def slash_list_pookie(inter: discord.Interaction):
    d = reload_data()
    mentions = [f"<@{uid}>" for uid in d.get("pookies", [])]
    await inter.response.send_message("Pookies:\n" + ("\n".join(mentions) if mentions else "None"), ephemeral=True)

# ------------------------- BLACKLIST -------------------------
@tree.command(name="blacklist", description="Blacklist user from using commands (owner/admin).")
@owner_or_admin_check()
@app_commands.describe(user="User to blacklist")
async def slash_blacklist(inter: discord.Interaction, user: discord.User):
    d = reload_data()
    if int(user.id) not in d["blacklist"]:
        d["blacklist"].append(int(user.id))
        save_data(d)
    await inter.response.send_message(f"Blacklisted {user.mention}")

@tree.command(name="unblacklist", description="Remove user from blacklist (owner/admin).")
@owner_or_admin_check()
@app_commands.describe(user="User to unblacklist")
async def slash_unblacklist(inter: discord.Interaction, user: discord.User):
    d = reload_data()
    if int(user.id) in d["blacklist"]:
        d["blacklist"].remove(int(user.id))
        save_data(d)
        return await inter.response.send_message(f"Unblacklisted {user.mention}")
    await inter.response.send_message("User not blacklisted.", ephemeral=True)

# ------------------------- BLOCKED WORDS -------------------------
@tree.command(name="blocked_add", description="Add a blocked word (admin).")
@owner_or_admin_check()
@app_commands.describe(word="Exact word to block")
async def slash_blocked_add(inter: discord.Interaction, word: str):
    d = reload_data()
    w = word.strip().lower()
    if w and w not in d["blocked_words"]:
        d["blocked_words"].append(w)
        save_data(d)
    await inter.response.send_message(f"Blocked word `{w}` added.")

@tree.command(name="blocked_remove", description="Remove blocked word (admin).")
@owner_or_admin_check()
@app_commands.describe(word="Word to remove")
async def slash_blocked_remove(inter: discord.Interaction, word: str):
    d = reload_data()
    w = word.strip().lower()
    if w in d["blocked_words"]:
        d["blocked_words"].remove(w)
        save_data(d)
        return await inter.response.send_message(f"Blocked word `{w}` removed.")
    await inter.response.send_message("Word not found.", ephemeral=True)

@tree.command(name="blocked_list", description="List blocked words.")
@owner_or_admin_check()
async def slash_blocked_list(inter: discord.Interaction):
    d = reload_data()
    words = d.get("blocked_words", [])
    if not words:
        return await inter.response.send_message("No blocked words.")
    await inter.response.send_message("Blocked words:\n" + "\n".join(f"`{w}`" for w in words))

# ------------------------- LOGGING CHANNEL & LOGS -------------------------
@tree.command(name="set_log_channel", description="Set channel that receives logs (owner/admin).")
@owner_or_admin_check()
@app_commands.describe(channel="Text channel for logs")
async def slash_set_log_channel(inter: discord.Interaction, channel: discord.TextChannel):
    d = reload_data()
    d["log_channel"] = int(channel.id)
    save_data(d)
    await inter.response.send_message(f"Log channel set to {channel.mention}")
    await send_log_embed(inter.guild, "Log channel set", f"{inter.user.mention} set logs to {channel.mention}")

@tree.command(name="disable_log_channel", description="Disable logs.")
@owner_or_admin_check()
async def slash_disable_log_channel(inter: discord.Interaction):
    d = reload_data()
    d["log_channel"] = None
    save_data(d)
    await inter.response.send_message("Log channel disabled.")

@tree.command(name="logs", description="Show recent logs (owner/admin).")
@owner_or_admin_check()
@app_commands.describe(count="How many to show (1-50)")
async def slash_logs(inter: discord.Interaction, count: app_commands.Range[int, 1, 50] = 10):
    d = reload_data()
    logs = d.get("logs", [])
    if not logs:
        return await inter.response.send_message("No logs.", ephemeral=True)
    last = logs[-count:]
    text = "\n".join(f"`{e['ts']}` **{e['kind']}** — {e['message']}"[:1900] for e in last)
    await inter.response.send_message(text or "Empty.", ephemeral=True)

# ------------------------- CAT CHANNELS -------------------------
@tree.command(name="setcatchannel", description="Set daily 11:00 IST cat channel (owner/admin).")
@owner_or_admin_check()
@app_commands.describe(channel="Text channel")
async def slash_set_cat_channel(inter: discord.Interaction, channel: discord.TextChannel):
    d = reload_data()
    d["cat_channel"] = int(channel.id)
    save_data(d)
    await inter.response.send_message(f"Daily cat channel set to {channel.mention} (11:00 IST)")
    await send_log_embed(inter.guild, "Daily cat channel set", f"{inter.user.mention} set daily cat channel to {channel.mention}")

@tree.command(name="sethourlycatchannel", description="Set hourly cat channel (owner/admin).")
@owner_or_admin_check()
@app_commands.describe(channel="Text channel")
async def slash_set_hourly_cat_channel(inter: discord.Interaction, channel: discord.TextChannel):
    d = reload_data()
    d["hourly_cat_channel"] = int(channel.id)
    save_data(d)
    await inter.response.send_message(f"Hourly cat channel set to {channel.mention}")
    await send_log_embed(inter.guild, "Hourly cat channel set", f"{inter.user.mention} set hourly cat channel to {channel.mention}")

@tree.command(name="cat", description="Get a random cat image.")
@blacklist_check()
async def slash_cat_cmd(inter: discord.Interaction):
    await inter.response.defer()
    async with aiohttp.ClientSession() as s:
        url = await fetch_random_cat(s)
    if not url:
        return await inter.followup.send("Couldn't fetch a cat right now.")
    await inter.followup.send(url)

@bot.command(name="cat")
async def prefix_cat(ctx: commands.Context):
    if is_blacklisted_user(ctx.author):
        return await ctx.reply("You are blacklisted.", mention_author=False)
    async with aiohttp.ClientSession() as s:
        url = await fetch_random_cat(s)
    await ctx.send(url)
    add_file_log("cat", f"{ctx.author} requested cat")

# ------------------------- FUN COMMANDS -------------------------
@tree.command(name="8ball", description="Ask the magic 8-ball.")
@blacklist_check()
@app_commands.describe(question="Your question")
async def slash_8ball(inter: discord.Interaction, question: str):
    answers = ["Yes.", "No.", "Maybe.", "Absolutely!", "Ask again later.", "Definitely not.", "Probably.", "Unlikely."]
    await inter.response.send_message(f"🎱 {random.choice(answers)}")

@bot.command(name="8ball")
async def prefix_8ball(ctx: commands.Context, *, question: str):
    answers = ["Yes.", "No.", "Maybe.", "Absolutely!", "Ask again later.", "Definitely not.", "Probably.", "Unlikely."]
    await ctx.reply(f"🎱 {random.choice(answers)}", mention_author=False)

@tree.command(name="joke", description="Tell a joke.")
@blacklist_check()
async def slash_joke(inter: discord.Interaction):
    jokes = ["I told my computer I needed a break, and it said 'No problem — I'll go to sleep.'", "Why do programmers prefer dark mode? Because light attracts bugs.", "There are 10 types of people: those who understand binary and those who don't."]
    await inter.response.send_message(random.choice(jokes))

@tree.command(name="dadjoke", description="Tell a dad joke.")
@blacklist_check()
async def slash_dadjoke(inter: discord.Interaction):
    jokes = ["I used to play piano by ear, but now I use my hands.", "Why don't eggs tell jokes? They'd crack each other up."]
    await inter.response.send_message(random.choice(jokes))

@tree.command(name="coinflip", description="Flip a coin.")
@blacklist_check()
async def slash_coin(inter: discord.Interaction):
    await inter.response.send_message("Heads" if random.random() < 0.5 else "Tails")

@tree.command(name="rolldice", description="Roll a dice (1-6).")
@blacklist_check()
async def slash_dice(inter: discord.Interaction):
    await inter.response.send_message(f"🎲 {random.randint(1,6)}")

@app_commands.choices(choice=[
    app_commands.Choice(name="rock", value="rock"),
    app_commands.Choice(name="paper", value="paper"),
    app_commands.Choice(name="scissors", value="scissors")
])
@tree.command(name="rps", description="Rock Paper Scissors")
@blacklist_check()
async def slash_rps(inter: discord.Interaction, choice: app_commands.Choice[str]):
    bot_choice = random.choice(["rock","paper","scissors"])
    user = choice.value
    result = "draw"
    if (user, bot_choice) in [("rock","scissors"), ("paper","rock"), ("scissors","paper")]:
        result = "you win!"
    elif user != bot_choice:
        result = "you lose!"
    await inter.response.send_message(f"You: **{user}**, Bot: **{bot_choice}** → {result}")

# ------------------------- TRIGGERS -------------------------
@tree.command(name="trigger_add", description="Add exact-word trigger (admin).")
@owner_or_admin_check()
@app_commands.describe(word="Exact word", reply="Reply text (use {user} to mention)")
async def slash_trigger_add(inter: discord.Interaction, word: str, reply: str):
    d = reload_data()
    d.setdefault("triggers", {})
    d["triggers"][word.lower()] = reply
    save_data(d)
    await inter.response.send_message(f"Trigger for `{word}` added.")

@tree.command(name="trigger_remove", description="Remove trigger (admin).")
@owner_or_admin_check()
@app_commands.describe(word="Word to remove")
async def slash_trigger_remove(inter: discord.Interaction, word: str):
    d = reload_data()
    if word.lower() in d.get("triggers", {}):
        d["triggers"].pop(word.lower(), None)
        save_data(d)
        return await inter.response.send_message(f"Removed trigger `{word}`")
    await inter.response.send_message("Trigger not found.", ephemeral=True)

@tree.command(name="showtrigger", description="Show triggers (admin).")
@owner_or_admin_check()
async def slash_show_trigger(inter: discord.Interaction):
    d = reload_data()
    t = d.get("triggers", {})
    if not t:
        return await inter.response.send_message("No triggers.")
    text = "\n".join(f"`{k}` → `{v}`" for k, v in t.items())
    await inter.response.send_message(text[:1900])

# ------------------------- AFK -------------------------
@tree.command(name="afk", description="Set AFK with reason.")
async def slash_afk(inter: discord.Interaction, reason: str = "AFK"):
    d = reload_data()
    d.setdefault("afk", {})
    d["afk"][str(inter.user.id)] = {"reason": reason, "since": datetime.now(timezone.utc).isoformat()}
    save_data(d)
    await inter.response.send_message(f"AFK set: **{sanitize_no_mentions(reason)}**", ephemeral=True)

@tree.command(name="afk_clear", description="Clear your AFK.")
async def slash_afk_clear(inter: discord.Interaction):
    d = reload_data()
    if str(inter.user.id) in d.get("afk", {}):
        d["afk"].pop(str(inter.user.id), None)
        save_data(d)
        return await inter.response.send_message("AFK removed.", ephemeral=True)
    await inter.response.send_message("You were not AFK.", ephemeral=True)

# ------------------------- SHOWCOMMANDS -------------------------
CATEGORIES = {
    "Fun": ["cat","8ball","joke","dadjoke","coinflip","rolldice","rps","avatar","userinfo"],
    "Moderation": ["ban","kick","purge","say","say_admin","blocked_add","blocked_remove"],
    "Management": ["add_admin","remove_admin","show_admins","addpookie","removepookie","listpookie","blacklist","unblacklist","set_log_channel","disable_log_channel","logs"],
    "Logging": ["set_log_channel","disable_log_channel","logs"],
    "Cats": ["cat","setcatchannel","sethourlycatchannel"]
}

class ShowView(discord.ui.View):
    def __init__(self, user: discord.User):
        super().__init__(timeout=60)
        self.user = user

    @discord.ui.select(placeholder="Pick a category", options=[discord.SelectOption(label=k) for k in CATEGORIES.keys()])
    async def select_cat(self, inter: discord.Interaction, select: discord.ui.Select):
        cat = select.values[0]
        items = CATEGORIES.get(cat, [])
        filtered = []
        for c in items:
            # check admin-only commands and hide if not admin
            if c in ["add_admin","remove_admin","addpookie","removepookie","listpookie","blacklist","unblacklist","set_log_channel","disable_log_channel","logs"]:
                if not is_admin_user(inter.user):
                    continue
            filtered.append(c)
        txt = f"**{cat}**\n" + (", ".join(f"`/{x}`" for x in filtered) if filtered else "No commands you can use here.")
        await inter.response.edit_message(content=txt, view=self)

@tree.command(name="showcommands", description="Interactive list of commands you can use.")
@blacklist_check()
async def slash_showcommands(inter: discord.Interaction):
    view = ShowView(inter.user)
    await inter.response.send_message("Choose a category:", view=view, ephemeral=True)

# ------------------------- ASK FOR COMMAND -------------------------
@tree.command(name="askforcommand", description="Ask owner for a command (DM + log).")
@blacklist_check()
@app_commands.describe(request="Describe command you want")
async def slash_askforcommand(inter: discord.Interaction, request: str):
    owner = await bot.fetch_user(OWNER_ID)
    content = f"**Command request** from {inter.user} ({inter.user.id}) in {inter.guild.name if inter.guild else 'DM'}:\n{request}"
    try:
        await owner.send(content)
    except Exception:
        pass
    await send_log_embed(inter.guild, "Command Request", content)
    await inter.response.send_message("Sent your request to the owner (or logged it).", ephemeral=True)

# ------------------------- DEBUG / REFRESH / RESTART -------------------------
@tree.command(name="refreshcommands", description="Refresh slash commands (owner/admin).")
@owner_or_admin_check()
async def slash_refresh(inter: discord.Interaction):
    try:
        await tree.sync()
        await inter.response.send_message("Slash commands refreshed.", ephemeral=True)
        add_file_log("admin", f"{inter.user} refreshed slash commands")
    except Exception as e:
        await inter.response.send_message(f"Failed: {e}", ephemeral=True)

@tree.command(name="restart", description="Restart the bot process (owner/admin).")
@owner_or_admin_check()
async def slash_restart(inter: discord.Interaction):
    await inter.response.send_message("Restarting...", ephemeral=True)
    add_file_log("admin", f"{inter.user} requested restart")
    # If Render set up, process exit will restart container; otherwise will restart on host
    os._exit(0)

@tree.command(name="debug", description="Show debug info (owner/admin).")
@owner_or_admin_check()
async def slash_debug(inter: discord.Interaction):
    mem = "N/A"
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        mem = f"{proc.memory_info().rss/(1024*1024):.1f} MiB"
    except Exception:
        mem = "psutil not installed"
    emb = discord.Embed(title="Debug", color=discord.Color.teal())
    emb.add_field(name="Uptime", value=str(datetime.now(timezone.utc).isoformat()), inline=False)
    emb.add_field(name="Guilds", value=str(len(bot.guilds)))
    emb.add_field(name="Latency", value=f"{round(bot.latency*1000)} ms")
    emb.add_field(name="Memory", value=mem)
    emb.add_field(name="Python", value=platform.python_version())
    emb.add_field(name="d.py", value=discord.__version__)
    await inter.response.send_message(embed=emb, ephemeral=True)

# ------------------------- AVATAR / USERINFO / GUILDINFO -------------------------
@tree.command(name="avatar", description="Show a user's avatar.")
@blacklist_check()
async def slash_avatar(inter: discord.Interaction, user: Optional[discord.User] = None):
    u = user or inter.user
    emb = discord.Embed(title=f"{u} avatar")
    emb.set_image(url=u.display_avatar.url)
    await inter.response.send_message(embed=emb)

@tree.command(name="userinfo", description="Show user information (mention or id).")
@blacklist_check()
async def slash_userinfo(inter: discord.Interaction, user: Optional[discord.User] = None, user_id: Optional[str] = None):
    target = user
    if not target and user_id:
        try:
            target = await bot.fetch_user(int(user_id))
        except Exception:
            pass
    if not target:
        target = inter.user
    emb = discord.Embed(title=f"User info: {target}")
    emb.add_field(name="ID", value=str(target.id))
    emb.add_field(name="Bot?", value=str(target.bot))
    emb.set_thumbnail(url=target.display_avatar.url)
    await inter.response.send_message(embed=emb)

@tree.command(name="guildinfo", description="Show info about a guild by ID (owner/admin).")
@owner_or_admin_check()
async def slash_guildinfo(inter: discord.Interaction, guild_id: str):
    try:
        gid = int(guild_id)
    except Exception:
        return await inter.response.send_message("Invalid guild ID.", ephemeral=True)
    g = bot.get_guild(gid)
    if not g:
        return await inter.response.send_message("Not in that guild.", ephemeral=True)
    invite = None
    try:
        for ch in g.text_channels:
            if ch.permissions_for(g.me).create_instant_invite:
                inv = await ch.create_invite(max_age=3600, max_uses=1, unique=True)
                invite = str(inv)
                break
    except Exception:
        invite = None
    emb = discord.Embed(title=f"Guild: {g.name}", color=discord.Color.gold())
    emb.add_field(name="ID", value=str(g.id))
    emb.add_field(name="Owner", value=f"{g.owner} ({g.owner_id})")
    emb.add_field(name="Members", value=str(g.member_count))
    emb.add_field(name="Channels", value=f"{len(g.text_channels)} text / {len(g.voice_channels)} voice")
    emb.add_field(name="Created", value=str(g.created_at))
    if invite:
        emb.add_field(name="Invite (1 hour)", value=invite)
    await inter.response.send_message(embed=emb, ephemeral=True)

# ------------------------- SNIPE PREFIX (optional) -------------------------
@bot.command(name="snipe")
async def snipe_prefix(ctx: commands.Context):
    items = SNIPES.get(ctx.channel.id, [])
    if not items:
        return await ctx.reply("Nothing to snipe here.", mention_author=False)
    view = SnipeView(items)
    m = await ctx.reply(embed=view.make_embed(), view=view, mention_author=False)
    # ephemeral not available in prefix

# ------------------------- COMMANDS MISSING (say_admin prefix, avatar prefix, userinfo prefix) -------------------------
@bot.command(name="say_admin")
async def say_admin_prefix(ctx: commands.Context, *, text: str):
    if not is_admin_user(ctx.author) and not is_pookie_user(ctx.author):
        return await ctx.reply("Admins only.", mention_author=False)
    await ctx.send(text)
    add_file_log("command", f"{ctx.author} used ?say_admin")
    await send_log_embed(ctx.guild, "Say Admin", f"{ctx.author.mention} used say_admin in {ctx.channel.mention}\n```{text[:900]}```")

@bot.command(name="avatar")
async def avatar_prefix(ctx: commands.Context, user: Optional[discord.User] = None):
    u = user or ctx.author
    emb = discord.Embed(title=f"{u} avatar")
    emb.set_image(url=u.display_avatar.url)
    await ctx.send(embed=emb)

@bot.command(name="userinfo")
async def userinfo_prefix(ctx: commands.Context, user: Optional[discord.Member] = None):
    m = user or ctx.author
    roles = [r.name for r in m.roles if r.name != "@everyone"]
    emb = discord.Embed(title=f"{m} — Member Info")
    emb.add_field(name="ID", value=str(m.id))
    emb.add_field(name="Created", value=str(m.created_at))
    if m.joined_at:
        emb.add_field(name="Joined", value=str(m.joined_at))
    emb.add_field(name="Roles", value=", ".join(roles) or "None")
    await ctx.send(embed=emb)

# ------------------------- AUTO-RESPONDER TRIGGER PREFIX COMMANDS -------------------------
@bot.command(name="trigger_add")
async def prefix_trigger_add(ctx: commands.Context, word: str, *, reply: str):
    if not is_admin_user(ctx.author):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    d.setdefault("triggers", {})
    d["triggers"][word.lower()] = reply
    save_data(d)
    await ctx.reply(f"Trigger `{word}` added.", mention_author=False)

@bot.command(name="trigger_remove")
async def prefix_trigger_remove(ctx: commands.Context, word: str):
    if not is_admin_user(ctx.author):
        return await ctx.reply("Admins only.", mention_author=False)
    d = reload_data()
    if word.lower() in d.get("triggers", {}):
        d["triggers"].pop(word.lower(), None)
        save_data(d)
        return await ctx.reply(f"Trigger `{word}` removed.", mention_author=False)
    await ctx.reply("Not found.", mention_author=False)

# ------------------------- WRAP UP & RUN -------------------------
# Ensure data persisted on exit
def _on_exit():
    try:
        save_data(DATA)
    except Exception:
        pass

atexit.register(_on_exit)

# Main runner
async def main():
    try:
        await bot.start(DISCORD_TOKEN)
    except KeyboardInterrupt:
        await bot.close()
    except Exception as e:
        print("Bot start error:", e)
        raise

if __name__ == "__main__":
    # sanity: ensure data file exists and owner/admins present
    d = reload_data()
    if OWNER_ID not in d["admins"]:
        d["admins"].append(int(OWNER_ID))
        for aid in DEFAULT_EXTRA_ADMINS:
            if aid not in d["admins"]:
                d["admins"].append(int(aid))
        save_data(d)
    asyncio.run(main())
