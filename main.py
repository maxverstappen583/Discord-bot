# =========================
# main.py
# =========================
import os, re, json, time, asyncio, aiohttp, traceback, psutil, platform, math
from collections import defaultdict, deque
from datetime import datetime, timedelta
import pytz

import discord
from discord.ext import commands, tasks
from discord import app_commands

from flask import Flask
from threading import Thread

# ---------------------------
# ENV / CONSTANTS
# ---------------------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN not set")

OWNER_ID = int(os.getenv("OWNER_ID", "1319292111325106296"))
# Default extra admins you mentioned:
DEFAULT_ADMINS = {1319292111325106296, 1380315427992768633, 909468887098216499}

RENDER_API_KEY   = os.getenv("RENDER_API_KEY", "").strip()
RENDER_SERVICE_ID= os.getenv("RENDER_SERVICE_ID", "").strip()

CAT_API_KEY = os.getenv("CAT_API_KEY", "").strip()  # TheCatAPI (optional but recommended)
TZ_NAME     = os.getenv("TZ", "Asia/Kolkata")
IST_TZ      = pytz.timezone(TZ_NAME)

DATA_FILE   = "data.json"
SNIPES_KEEP = 50          # how many to keep per channel
ESNIPES_KEEP= 50
SPAM_WINDOW = 7           # seconds (default; can be overridden via automod)
SPAM_THRESHOLD = 5        # msgs in window (default; can be overridden)
DEFAULT_TIMEOUT_SECS = 300

# ---------------------------
# INTENTS / BOT
# ---------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds  = True
intents.presences = False
bot = commands.Bot(command_prefix="?", intents=intents, help_command=None)

# ---------------------------
# STORAGE
# ---------------------------
def load_data():
    if not os.path.exists(DATA_FILE):
        base = {
            "admins": list(DEFAULT_ADMINS),
            "pookies": [],
            "trusted": [],
            "blacklist": [],
            "blocked_words": [],
            "automod": {
                "enabled": True,
                "anti_link": {"enabled": True, "action": "delete"},
                "anti_invite": {"enabled": True, "action": "delete"},
                "blocked_words": {"enabled": True, "action": "delete"},
                "anti_spam": {"enabled": True, "window": SPAM_WINDOW, "threshold": SPAM_THRESHOLD, "action": "timeout", "duration": DEFAULT_TIMEOUT_SECS},
                "trusted_bypass": True
            },
            "log_channel": {},              # guild_id -> channel_id
            "cat_daily_channel": {},        # guild_id -> channel_id
            "cat_hourly_channels": {},      # guild_id -> [channel_ids]
            "triggers": {},                 # guild_id -> { word: reply }
            "warns": {},                    # guild_id -> { user_id: [ {reason, mod, ts} ] }
            "temp_roles": []                # [{guild_id,user_id,role_id,expires}]
        }
        save_data(base)
        return base
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(d):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, DATA_FILE)

data = load_data()

# quick refs
def guild_map(dct_name):
    return data.get(dct_name, {})

def get_log_channel_id(gid:int):
    return data.get("log_channel", {}).get(str(gid))

def set_log_channel_id(gid:int, cid:int|None):
    data.setdefault("log_channel", {})
    if cid is None:
        data["log_channel"].pop(str(gid), None)
    else:
        data["log_channel"][str(gid)] = cid
    save_data(data)

def trigger_map(gid:int):
    data.setdefault("triggers", {})
    data["triggers"].setdefault(str(gid), {})
    return data["triggers"][str(gid)]

def warns_map(gid:int):
    data.setdefault("warns", {})
    data["warns"].setdefault(str(gid), {})
    return data["warns"][str(gid)]

def hourly_cat_map(gid:int):
    data.setdefault("cat_hourly_channels", {})
    data["cat_hourly_channels"].setdefault(str(gid), [])
    return data["cat_hourly_channels"][str(gid)]

# ---------------------------
# PERMS HELPERS
# ---------------------------
def is_owner(u:discord.abc.User):
    return u.id == OWNER_ID

def is_pookie(u:discord.abc.User):
    return is_owner(u) or (u.id in set(data.get("pookies", [])))

def is_admin(u:discord.abc.User):
    return is_pookie(u) or (u.id in set(data.get("admins", [])))

def is_trusted(u:discord.abc.User):
    return u.id in set(data.get("trusted", []))

def is_blacklisted(u:discord.abc.User):
    return u.id in set(data.get("blacklist", []))

def mod_user(u:discord.abc.User):
    return is_admin(u) or is_pookie(u)

# Global command block for blacklist
@bot.check
async def not_blacklisted(ctx:commands.Context):
    return not is_blacklisted(ctx.author)

def AM(color=0x2B2D31, title=None, desc=None):
    e = discord.Embed(color=color, timestamp=datetime.utcnow())
    if title: e.title = title
    if desc:  e.description = desc
    return e

def snowflake_age(sf:int):
    # approximate from timestamp within snowflake
    try:
        ts = ((sf >> 22) + 1420070400000) / 1000
        dt = datetime.utcfromtimestamp(ts)
        return dt
    except:
        return None

# ---------------------------
# KEEPALIVE (Flask)
# ---------------------------
app = Flask("bot_keepalive")

@app.route("/")
def index():
    return "OK", 200

@app.route("/health")
def health():
    return "healthy", 200

def run_flask():
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

Thread(target=run_flask, daemon=True).start()

# ---------------------------
# PRESENCE / UPTIME
# ---------------------------
start_time = time.time()

async def set_streaming_presence():
    # Purple streaming presence (Twitch link required for purple look)
    activity = discord.Streaming(name="Max Verstappen", url="https://www.twitch.tv/max")
    await bot.change_presence(status=discord.Status.dnd, activity=activity)

def human_timedelta(seconds:int):
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)

# ---------------------------
# SNIPES / ESNIPES
# ---------------------------
snipes   : dict[int, deque] = defaultdict(lambda: deque(maxlen=SNIPES_KEEP))   # channel_id -> deque of dict
esnipes  : dict[int, deque] = defaultdict(lambda: deque(maxlen=ESNIPES_KEEP))  # channel_id -> deque of dict

class SnipeView(discord.ui.View):
    def __init__(self, items:list[dict], kind:str):
        super().__init__(timeout=60)
        self.items = items
        self.index = max(0, len(items)-1)  # start at latest
        self.kind = kind  # "delete" or "edit"

    def build_embed(self):
        item = self.items[self.index]
        color = 0xFF5555 if self.kind == "delete" else 0x55AAFF
        e = AM(color=color, title=f"{'Deleted' if self.kind=='delete' else 'Edited'} message {self.index+1}/{len(self.items)}")
        e.add_field(name="Author", value=f"{item['author']} ({item['author_id']})", inline=False)
        e.add_field(name="Channel", value=f"<#{item['channel_id']}>", inline=True)
        e.add_field(name="Message ID", value=str(item.get("message_id","?")), inline=True)
        e.add_field(name="When", value=f"<t:{int(item['ts'])}:R>", inline=True)
        if self.kind == "edit":
            e.add_field(name="Before", value=item.get("before","(empty)")[:1024], inline=False)
            e.add_field(name="After",  value=item.get("after","(empty)")[:1024], inline=False)
        else:
            e.add_field(name="Content", value=item.get("content","(empty)")[:1024], inline=False)
        if att := item.get("attachment"):
            e.add_field(name="Attachment", value=att, inline=False)
        if del_by := item.get("deleted_by"):
            e.add_field(name="Deleted by", value=del_by, inline=False)
        return e

    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction:discord.Interaction, button:discord.ui.Button):
        if interaction.user is None: return
        self.index = max(0, self.index-1)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction:discord.Interaction, button:discord.ui.Button):
        if interaction.user is None: return
        self.index = min(len(self.items)-1, self.index+1)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

# ---------------------------
# LOGGING
# ---------------------------
async def send_log(guild:discord.Guild, embed:discord.Embed):
    cid = get_log_channel_id(guild.id)
    if not cid: return
    ch = guild.get_channel(cid)
    if not ch:
        try:
            ch = await guild.fetch_channel(cid)
        except: 
            return
    try:
        await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except:
        pass

def account_age_str(user:discord.abc.User):
    created = snowflake_age(user.id)
    if not created:
        return "N/A"
    delta = datetime.utcnow() - created
    days = delta.days
    return f"{days} days (created <t:{int(created.timestamp())}:R>)"

# ---------------------------
# AUTOMOD HELPERS
# ---------------------------
link_regex = re.compile(r"(https?://|www\.)", re.I)
invite_regex = re.compile(r"(discord\.gg/|discord\.com/invite/)", re.I)

recent_msgs: dict[int, dict[int, deque]] = defaultdict(lambda: defaultdict(lambda: deque(maxlen=30)))
# recent_msgs[guild_id][user_id] -> deque of timestamps

async def apply_action(message:discord.Message, action:str, duration:int|None, reason:str):
    if action == "delete":
        try:
            await message.delete()
        except: pass
    elif action == "timeout":
        try:
            secs = duration or DEFAULT_TIMEOUT_SECS
            until = discord.utils.utcnow() + timedelta(seconds=secs)
            await message.author.timeout(until, reason=reason)
        except: pass
    # "warn" will just log as warn without extra action (handled where called)

def automod_cfg():
    return data.get("automod", {
        "enabled": True,
        "anti_link": {"enabled": True, "action": "delete"},
        "anti_invite": {"enabled": True, "action": "delete"},
        "blocked_words": {"enabled": True, "action": "delete"},
        "anti_spam": {"enabled": True, "window": SPAM_WINDOW, "threshold": SPAM_THRESHOLD, "action": "timeout", "duration": DEFAULT_TIMEOUT_SECS},
        "trusted_bypass": True
    })

# ---------------------------
# TRIGGERS (auto-responder)
# ---------------------------
def word_match(full_text:str, word:str) -> bool:
    # match as whole word (case-insensitive)
    pattern = re.compile(rf"\b{re.escape(word)}\b", re.I)
    return bool(pattern.search(full_text))

# ---------------------------
# CAT HELPERS
# ---------------------------
async def fetch_cat_url(session:aiohttp.ClientSession):
    # mostly images, occasionally videos
    params = {"size": "med", "limit": 1}
    url = "https://api.thecatapi.com/v1/images/search?mime_types=jpg,png,gif,mp4"
    headers = {}
    if CAT_API_KEY:
        headers["x-api-key"] = CAT_API_KEY
    async with session.get(url, headers=headers, params=params, timeout=20) as r:
        if r.status == 200:
            arr = await r.json()
            if isinstance(arr, list) and arr:
                item = arr[0]
                return item.get("url")
    return None

# ---------------------------
# TEMP ROLE HOUSEKEEPING
# ---------------------------
async def temp_role_worker():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.utcnow().isoformat()
        changed = False
        remaining = []
        for tr in data.get("temp_roles", []):
            try:
                exp = datetime.fromisoformat(tr["expires"])
                if datetime.utcnow() >= exp:
                    g = bot.get_guild(int(tr["guild_id"]))
                    if g:
                        mem = g.get_member(int(tr["user_id"])) or (await g.fetch_member(int(tr["user_id"])))
                        role = g.get_role(int(tr["role_id"]))
                        if mem and role:
                            try: await mem.remove_roles(role, reason="Temp role expired")
                            except: pass
                    changed = True
                else:
                    remaining.append(tr)
            except:
                # keep if malformed? safer to drop
                changed = True
        if changed:
            data["temp_roles"] = remaining
            save_data(data)
        await asyncio.sleep(30)

# ---------------------------
# BOT EVENTS
# ---------------------------
@bot.event
async def on_ready():
    try:
        await set_streaming_presence()
        await bot.tree.sync()
    except Exception:
        pass
    print(f"Logged in as {bot.user} (ID: {bot.user.id}) | Guilds: {len(bot.guilds)}")
    # start background tasks after login
    if not daily_cat_task.is_running():
        daily_cat_task.start()
    if not hourly_cat_task.is_running():
        hourly_cat_task.start()
    bot.loop.create_task(temp_role_worker())

# Logging events
@bot.event
async def on_member_join(member:discord.Member):
    e = AM(0x00CC88, "Member Joined")
    e.set_author(name=str(member), icon_url=getattr(member.display_avatar, "url", discord.Embed.Empty))
    e.add_field(name="User", value=f"{member.mention}\n{member} ({member.id})", inline=False)
    e.add_field(name="Account Age", value=account_age_str(member), inline=True)
    e.add_field(name="Member Count", value=str(member.guild.member_count), inline=True)
    e.add_field(name="Joined", value=f"<t:{int(member.joined_at.timestamp())}:F>" if member.joined_at else "N/A", inline=False)
    await send_log(member.guild, e)

@bot.event
async def on_member_remove(member:discord.Member):
    e = AM(0xCC0000, "Member Left")
    e.set_author(name=str(member), icon_url=getattr(member.display_avatar, "url", discord.Embed.Empty))
    e.add_field(name="User", value=f"{member} ({member.id})", inline=False)
    e.add_field(name="Account Age", value=account_age_str(member), inline=True)
    e.add_field(name="Time in Server", value="N/A" if not member.joined_at else f"{human_timedelta((datetime.utcnow()-member.joined_at.replace(tzinfo=None)).total_seconds())}", inline=True)
    e.add_field(name="Member Count", value=str(member.guild.member_count), inline=True)
    await send_log(member.guild, e)

@bot.event
async def on_member_update(before:discord.Member, after:discord.Member):
    # roles added / removed
    b = set(before.roles)
    a = set(after.roles)
    added = a - b
    removed = b - a
    if added:
        e = AM(0x3388FF, "Roles Added")
        e.add_field(name="User", value=f"{after} ({after.id})", inline=False)
        e.add_field(name="Added", value=", ".join(r.mention for r in added), inline=False)
        e.add_field(name="Account Age", value=account_age_str(after), inline=True)
        await send_log(after.guild, e)
    if removed:
        e = AM(0xFF8833, "Roles Removed")
        e.add_field(name="User", value=f"{after} ({after.id})", inline=False)
        e.add_field(name="Removed", value=", ".join(r.name for r in removed), inline=False)
        e.add_field(name="Account Age", value=account_age_str(after), inline=True)
        await send_log(after.guild, e)

@bot.event
async def on_member_ban(guild:discord.Guild, user:discord.User):
    e = AM(0x990000, "User Banned")
    e.add_field(name="User", value=f"{user} ({user.id})", inline=False)
    e.add_field(name="Account Age", value=account_age_str(user), inline=True)
    await send_log(guild, e)

@bot.event
async def on_member_unban(guild:discord.Guild, user:discord.User):
    e = AM(0x33AA33, "User Unbanned")
    e.add_field(name="User", value=f"{user} ({user.id})", inline=False)
    await send_log(guild, e)

@bot.event
async def on_message_delete(msg:discord.Message):
    if not msg.guild or msg.author.bot:
        return
    att = msg.attachments[0].url if msg.attachments else None
    snipes[msg.channel.id].append({
        "author": str(msg.author),
        "author_id": msg.author.id,
        "channel_id": msg.channel.id,
        "content": msg.content or "",
        "attachment": att,
        "message_id": msg.id,
        "ts": time.time(),
        "deleted_by": None  # unknown unless audit logs; skip to avoid rate limits
    })
    e = AM(0xCC4444, "Message Deleted")
    e.add_field(name="User", value=f"{msg.author} ({msg.author.id})", inline=False)
    e.add_field(name="Channel", value=f"{msg.channel.mention}", inline=True)
    e.add_field(name="Message ID", value=str(msg.id), inline=True)
    e.add_field(name="Age", value=f"{human_timedelta(time.time()-msg.created_at.timestamp())}", inline=True)
    if msg.content:
        e.add_field(name="Content", value=msg.content[:1000], inline=False)
    if att:
        e.add_field(name="Attachment", value=att, inline=False)
    await send_log(msg.guild, e)

@bot.event
async def on_message_edit(before:discord.Message, after:discord.Message):
    if not before.guild or before.author.bot or before.content == after.content:
        return
    esnipes[before.channel.id].append({
        "author": str(before.author),
        "author_id": before.author.id,
        "channel_id": before.channel.id,
        "before": before.content or "",
        "after":  after.content or "",
        "message_id": before.id,
        "ts": time.time()
    })
    e = AM(0x4488CC, "Message Edited")
    e.add_field(name="User", value=f"{before.author} ({before.author.id})", inline=False)
    e.add_field(name="Channel", value=f"{before.channel.mention}", inline=True)
    e.add_field(name="Message ID", value=str(before.id), inline=True)
    e.add_field(name="Before", value=(before.content or "(empty)")[:800], inline=False)
    e.add_field(name="After",  value=(after.content  or "(empty)")[:800], inline=False)
    await send_log(before.guild, e)

# ---------------------------
# AUTOMOD (on_message)
# ---------------------------
async def handle_automod(message:discord.Message):
    if message.author.bot or not message.guild:
        return
    cfg = automod_cfg()
    if not cfg.get("enabled", True):
        return
    if cfg.get("trusted_bypass", True) and is_trusted(message.author):
        return

    content = message.content or ""
    reason = None
    action_to_apply = None
    duration = None

    # anti-invite
    if cfg["anti_invite"]["enabled"] and invite_regex.search(content):
        action_to_apply = cfg["anti_invite"]["action"]
        reason = "Automod: Discord invite link"
    # anti-link
    elif cfg["anti_link"]["enabled"] and link_regex.search(content):
        action_to_apply = cfg["anti_link"]["action"]
        reason = "Automod: Link detected"
    # blocked words
    elif cfg["blocked_words"]["enabled"] and data.get("blocked_words"):
        for w in data["blocked_words"]:
            if re.search(rf"\b{re.escape(w)}\b", content, re.I):
                action_to_apply = cfg["blocked_words"]["action"]
                reason = f"Automod: Blocked word ({w})"
                break
    # anti-spam
    if not action_to_apply and cfg["anti_spam"]["enabled"]:
        rm = recent_msgs[message.guild.id][message.author.id]
        now = time.time()
        rm.append(now)
        window = cfg["anti_spam"].get("window", SPAM_WINDOW)
        thresh = cfg["anti_spam"].get("threshold", SPAM_THRESHOLD)
        while rm and now - rm[0] > window:
            rm.popleft()
        if len(rm) >= thresh:
            action_to_apply = cfg["anti_spam"].get("action", "timeout")
            duration = cfg["anti_spam"].get("duration", DEFAULT_TIMEOUT_SECS)
            reason = f"Automod: Spam (>{thresh} msgs/{window}s)"

    if action_to_apply:
        await apply_action(message, action_to_apply, duration, reason)
        e = AM(0xAA00AA, "Automod Action")
        e.add_field(name="User", value=f"{message.author} ({message.author.id})", inline=False)
        e.add_field(name="Channel", value=message.channel.mention, inline=True)
        e.add_field(name="Action", value=action_to_apply + (f" ({duration}s)" if duration else ""), inline=True)
        e.add_field(name="Reason", value=reason or "Automod", inline=False)
        if message.content:
            e.add_field(name="Content", value=message.content[:800], inline=False)
        await send_log(message.guild, e)

@bot.event
async def on_message(message:discord.Message):
    # triggers (admin-set auto-replies) BEFORE command processing
    if message.guild and not message.author.bot:
        # allow automod
        await handle_automod(message)

        # triggers matching
        tm = trigger_map(message.guild.id)
        for word, reply in tm.items():
            if word_match(message.content, word):
                try:
                    await message.reply(reply, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                except: pass
                break  # one trigger per message

    await bot.process_commands(message)

# ---------------------------
# CHECKERS (for slash)
# ---------------------------
def app_cmd_check_blacklist():
    async def predicate(inter:discord.Interaction):
        if is_blacklisted(inter.user):
            await inter.response.send_message("You are blacklisted from using commands.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

def app_cmd_check_admin():
    async def predicate(inter:discord.Interaction):
        if not is_admin(inter.user):
            await inter.response.send_message("Admin-only command.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

def app_cmd_check_pookie_or_owner():
    async def predicate(inter:discord.Interaction):
        if not is_pookie(inter.user):
            await inter.response.send_message("Pookie/Owner-only command.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

# ---------------------------
# SLASH COMMANDS
# ---------------------------
@bot.tree.command(name="ping", description="Show latency.")
@app_cmd_check_blacklist()
async def slash_ping(inter:discord.Interaction):
    await inter.response.send_message(f"Pong! `{round(bot.latency*1000)} ms`")

@bot.tree.command(name="avatar", description="Show a user's avatar.")
@app_cmd_check_blacklist()
@app_commands.describe(user="Target user (optional)")
async def slash_avatar(inter:discord.Interaction, user:discord.User=None):
    user = user or inter.user
    e = AM(0x5865F2, f"Avatar - {user}")
    e.set_image(url=user.display_avatar.url)
    await inter.response.send_message(embed=e)

@bot.tree.command(name="userinfo", description="Show user info.")
@app_cmd_check_blacklist()
@app_commands.describe(user="Target user (optional)")
async def slash_userinfo(inter:discord.Interaction, user:discord.Member=None):
    user = user or inter.user
    e = AM(0x2B2D31, f"User Info - {user}")
    e.add_field(name="ID", value=str(user.id))
    e.add_field(name="Account Age", value=account_age_str(user))
    if isinstance(user, discord.Member):
        e.add_field(name="Joined", value=f"<t:{int(user.joined_at.timestamp())}:F>" if user.joined_at else "N/A")
        if user.roles:
            e.add_field(name="Roles", value=", ".join(r.mention for r in user.roles[1:]) or "None", inline=False)
    await inter.response.send_message(embed=e)

@bot.tree.command(name="say", description="Make the bot say something (mentions disabled).")
@app_cmd_check_blacklist()
@app_commands.describe(message="What to say")
async def slash_say(inter:discord.Interaction, message:str):
    await inter.response.send_message("Sent.", ephemeral=True)
    await inter.channel.send(message, allowed_mentions=discord.AllowedMentions.none())

@bot.tree.command(name="say_admin", description="Admin say (mentions allowed).")
@app_cmd_check_admin()
@app_commands.describe(message="What to say (mentions allowed)")
async def slash_say_admin(inter:discord.Interaction, message:str):
    await inter.response.send_message("Sent.", ephemeral=True)
    await inter.channel.send(message)

@bot.tree.command(name="purge", description="Delete N messages (max 100).")
@app_cmd_check_admin()
@app_commands.describe(amount="How many (1-100)")
async def slash_purge(inter:discord.Interaction, amount:int):
    if amount < 1 or amount > 100:
        await inter.response.send_message("Choose between 1 and 100.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    deleted = await inter.channel.purge(limit=amount)
    await inter.followup.send(f"Deleted {len(deleted)} messages.", ephemeral=True)

@bot.tree.command(name="ban", description="Ban a member (slash requires the member be in server).")
@app_cmd_check_admin()
@app_commands.describe(member="Member to ban", reason="Reason")
async def slash_ban(inter:discord.Interaction, member:discord.Member, reason:str="No reason"):
    try:
        await member.ban(reason=f"{reason} | by {inter.user}")
        await inter.response.send_message(f"Banned {member} for: {reason}")
    except Exception as e:
        await inter.response.send_message(f"Failed: {e}", ephemeral=True)

@bot.tree.command(name="unban", description="Unban by user ID.")
@app_cmd_check_admin()
@app_commands.describe(user_id="The user's ID")
async def slash_unban(inter:discord.Interaction, user_id:str):
    await inter.response.defer(ephemeral=True)
    try:
        u = await bot.fetch_user(int(user_id))
        await inter.guild.unban(u, reason=f"by {inter.user}")
        await inter.followup.send(f"Unbanned {u}.")
    except Exception as e:
        await inter.followup.send(f"Failed: {e}")

@bot.tree.command(name="kick", description="Kick a member.")
@app_cmd_check_admin()
@app_commands.describe(member="Member to kick", reason="Reason")
async def slash_kick(inter:discord.Interaction, member:discord.Member, reason:str="No reason"):
    try:
        await member.kick(reason=f"{reason} | by {inter.user}")
        await inter.response.send_message(f"Kicked {member} for: {reason}")
    except Exception as e:
        await inter.response.send_message(f"Failed: {e}", ephemeral=True)

@bot.tree.command(name="timeout", description="Timeout a member.")
@app_cmd_check_admin()
@app_commands.describe(member="Member", duration_seconds="Seconds", reason="Reason")
async def slash_timeout(inter:discord.Interaction, member:discord.Member, duration_seconds:int, reason:str="Moderation"):
    try:
        until = discord.utils.utcnow() + timedelta(seconds=duration_seconds)
        await member.timeout(until, reason=f"{reason} | by {inter.user}")
        await inter.response.send_message(f"Timed out {member} for {duration_seconds}s.")
    except Exception as e:
        await inter.response.send_message(f"Failed: {e}", ephemeral=True)

@bot.tree.command(name="lock", description="Lock a channel for @everyone.")
@app_cmd_check_admin()
@app_commands.describe(channel="Channel (optional)")
async def slash_lock(inter:discord.Interaction, channel:discord.TextChannel=None):
    channel = channel or inter.channel
    ov = channel.overwrites_for(inter.guild.default_role)
    ov.send_messages = False
    await channel.set_permissions(inter.guild.default_role, overwrite=ov, reason=f"Locked by {inter.user}")
    await inter.response.send_message(f"Locked {channel.mention}")

@bot.tree.command(name="unlock", description="Unlock a channel.")
@app_cmd_check_admin()
@app_commands.describe(channel="Channel (optional)")
async def slash_unlock(inter:discord.Interaction, channel:discord.TextChannel=None):
    channel = channel or inter.channel
    ov = channel.overwrites_for(inter.guild.default_role)
    ov.send_messages = None
    await channel.set_permissions(inter.guild.default_role, overwrite=ov, reason=f"Unlocked by {inter.user}")
    await inter.response.send_message(f"Unlocked {channel.mention}")

@bot.tree.command(name="role_add", description="Add a role to a user.")
@app_cmd_check_admin()
@app_commands.describe(member="Member", role="Role")
async def slash_role_add(inter:discord.Interaction, member:discord.Member, role:discord.Role):
    try:
        await member.add_roles(role, reason=f"By {inter.user}")
        await inter.response.send_message(f"Added {role.mention} to {member.mention}")
    except Exception as e:
        await inter.response.send_message(f"Failed: {e}", ephemeral=True)

@bot.tree.command(name="role_remove", description="Remove a role from a user.")
@app_cmd_check_admin()
@app_commands.describe(member="Member", role="Role")
async def slash_role_remove(inter:discord.Interaction, member:discord.Member, role:discord.Role):
    try:
        await member.remove_roles(role, reason=f"By {inter.user}")
        await inter.response.send_message(f"Removed {role.mention} from {member.mention}")
    except Exception as e:
        await inter.response.send_message(f"Failed: {e}", ephemeral=True)

@bot.tree.command(name="role_temp", description="Give a role temporarily.")
@app_cmd_check_admin()
@app_commands.describe(member="Member", role="Role", duration_minutes="Duration in minutes")
async def slash_role_temp(inter:discord.Interaction, member:discord.Member, role:discord.Role, duration_minutes:int):
    try:
        await member.add_roles(role, reason=f"Temp role by {inter.user}")
        expires = datetime.utcnow() + timedelta(minutes=duration_minutes)
        data.setdefault("temp_roles", []).append({
            "guild_id": str(inter.guild.id),
            "user_id": str(member.id),
            "role_id": str(role.id),
            "expires": expires.isoformat()
        })
        save_data(data)
        await inter.response.send_message(f"Gave {role.mention} to {member.mention} for {duration_minutes}m.")
    except Exception as e:
        await inter.response.send_message(f"Failed: {e}", ephemeral=True)

# ---- Admin Controls (owner & pookie have highest power) ----
@bot.tree.command(name="add_admin", description="Owner/Pookie: add an admin.")
@app_cmd_check_pookie_or_owner()
@app_commands.describe(user="User to make admin")
async def slash_add_admin(inter:discord.Interaction, user:discord.User):
    s = set(data.get("admins", []))
    s.add(user.id)
    data["admins"] = list(s)
    save_data(data)
    await inter.response.send_message(f"Added **{user}** as admin.")

@bot.tree.command(name="remove_admin", description="Owner/Pookie: remove an admin.")
@app_cmd_check_pookie_or_owner()
@app_commands.describe(user="User to remove from admins")
async def slash_remove_admin(inter:discord.Interaction, user:discord.User):
    s = set(data.get("admins", []))
    s.discard(user.id)
    data["admins"] = list(s)
    save_data(data)
    await inter.response.send_message(f"Removed **{user}** from admins.")

@bot.tree.command(name="show_admins", description="List all admins.")
@app_cmd_check_pookie_or_owner()
async def slash_show_admins(inter:discord.Interaction):
    admins = [f"<@{i}>" for i in data.get("admins", [])]
    await inter.response.send_message("Admins:\n" + ("\n".join(admins) if admins else "None"))

# ---- Pookie ----
@bot.tree.command(name="add_pookie", description="Owner only: add a Pookie (highest power).")
@app_cmd_check_pookie_or_owner()
@app_commands.describe(user="User to make Pookie")
async def slash_add_pookie(inter:discord.Interaction, user:discord.User):
    if not is_owner(inter.user):
        await inter.response.send_message("Only the owner can add/remove Pookies.", ephemeral=True)
        return
    s = set(data.get("pookies", []))
    s.add(user.id)
    data["pookies"] = list(s)
    save_data(data)
    await inter.response.send_message(f"Added **{user}** as Pookie 👑")

@bot.tree.command(name="remove_pookie", description="Owner only: remove a Pookie.")
@app_cmd_check_pookie_or_owner()
@app_commands.describe(user="User to remove from Pookies")
async def slash_remove_pookie(inter:discord.Interaction, user:discord.User):
    if not is_owner(inter.user):
        await inter.response.send_message("Only the owner can add/remove Pookies.", ephemeral=True)
        return
    s = set(data.get("pookies", []))
    s.discard(user.id)
    data["pookies"] = list(s)
    save_data(data)
    await inter.response.send_message(f"Removed **{user}** from Pookies.")

@bot.tree.command(name="list_pookies", description="List Pookie users.")
@app_cmd_check_blacklist()
async def slash_list_pookies(inter:discord.Interaction):
    arr = [f"<@{i}>" for i in data.get("pookies", [])]
    await inter.response.send_message("Pookies:\n" + ("\n".join(arr) if arr else "None"))

# ---- Trusted ----
@bot.tree.command(name="add_trusted", description="Owner/Pookie: add a trusted user (bypass automod).")
@app_cmd_check_pookie_or_owner()
@app_commands.describe(user="User to add as trusted")
async def slash_add_trusted(inter:discord.Interaction, user:discord.User):
    s = set(data.get("trusted", []))
    s.add(user.id)
    data["trusted"] = list(s)
    save_data(data)
    await inter.response.send_message(f"Added **{user}** as trusted.")

@bot.tree.command(name="remove_trusted", description="Owner/Pookie: remove trusted user.")
@app_cmd_check_pookie_or_owner()
@app_commands.describe(user="User to remove")
async def slash_remove_trusted(inter:discord.Interaction, user:discord.User):
    s = set(data.get("trusted", []))
    s.discard(user.id)
    data["trusted"] = list(s)
    save_data(data)
    await inter.response.send_message(f"Removed **{user}** from trusted.")

@bot.tree.command(name="list_trusted", description="List trusted users.")
@app_cmd_check_blacklist()
async def slash_list_trusted(inter:discord.Interaction):
    arr = [f"<@{i}>" for i in data.get("trusted", [])]
    await inter.response.send_message("Trusted:\n" + ("\n".join(arr) if arr else "None"))

# ---- Blacklist ----
@bot.tree.command(name="blacklist", description="Admin: add a user to blacklist.")
@app_cmd_check_admin()
@app_commands.describe(user="User to blacklist")
async def slash_blacklist(inter:discord.Interaction, user:discord.User):
    s = set(data.get("blacklist", []))
    s.add(user.id)
    data["blacklist"] = list(s)
    save_data(data)
    await inter.response.send_message(f"Blacklisted **{user}**.")

@bot.tree.command(name="unblacklist", description="Admin: remove a user from blacklist.")
@app_cmd_check_admin()
@app_commands.describe(user="User to unblacklist")
async def slash_unblacklist(inter:discord.Interaction, user:discord.User):
    s = set(data.get("blacklist", []))
    s.discard(user.id)
    data["blacklist"] = list(s)
    save_data(data)
    await inter.response.send_message(f"Un-blacklisted **{user}**.")

# ---- Blocked words ----
@bot.tree.command(name="add_blocked_word", description="Admin: add a blocked word.")
@app_cmd_check_admin()
@app_commands.describe(word="Word to block")
async def slash_add_blocked(inter:discord.Interaction, word:str):
    arr = set(data.get("blocked_words", []))
    arr.add(word.lower())
    data["blocked_words"] = list(arr)
    save_data(data)
    await inter.response.send_message(f"Added blocked word: `{word}`")

@bot.tree.command(name="remove_blocked_word", description="Admin: remove a blocked word.")
@app_cmd_check_admin()
@app_commands.describe(word="Word to remove")
async def slash_remove_blocked(inter:discord.Interaction, word:str):
    arr = set(data.get("blocked_words", []))
    arr.discard(word.lower())
    data["blocked_words"] = list(arr)
    save_data(data)
    await inter.response.send_message(f"Removed blocked word: `{word}`")

@bot.tree.command(name="show_blocked_words", description="List blocked words.")
@app_cmd_check_blacklist()
async def slash_show_blocked(inter:discord.Interaction):
    arr = data.get("blocked_words", [])
    await inter.response.send_message("Blocked words:\n" + (", ".join(arr) if arr else "None"))

# ---- Automod Config ----
ACTIONS = ["delete", "warn", "timeout"]

@bot.tree.command(name="automod", description="Configure automod.")
@app_cmd_check_admin()
@app_commands.describe(rule="Which rule", enabled="Enable/disable", action="Action", window="Spam window (s)", threshold="Spam msgs", duration="Timeout seconds")
@app_commands.choices(rule=[
    app_commands.Choice(name="anti_link", value="anti_link"),
    app_commands.Choice(name="anti_invite", value="anti_invite"),
    app_commands.Choice(name="blocked_words", value="blocked_words"),
    app_commands.Choice(name="anti_spam", value="anti_spam"),
    app_commands.Choice(name="toggle_all", value="toggle_all")
])
@app_commands.choices(action=[
    app_commands.Choice(name="delete", value="delete"),
    app_commands.Choice(name="warn", value="warn"),
    app_commands.Choice(name="timeout", value="timeout")
])
async def slash_automod(inter:discord.Interaction, rule:app_commands.Choice[str], enabled:bool=None, action:app_commands.Choice[str]=None, window:int=None, threshold:int=None, duration:int=None):
    cfg = automod_cfg()
    r = rule.value
    if r == "toggle_all":
        if enabled is None:
            await inter.response.send_message("Provide enabled=true/false for toggle_all.", ephemeral=True)
            return
        cfg["enabled"] = enabled
        data["automod"] = cfg; save_data(data)
        await inter.response.send_message(f"Automod enabled = **{enabled}**")
        return

    cfg.setdefault(r, {})
    if enabled is not None:
        cfg[r]["enabled"] = enabled
    if action is not None:
        cfg[r]["action"] = action.value
    if r == "anti_spam":
        if window is not None: cfg[r]["window"] = max(2, int(window))
        if threshold is not None: cfg[r]["threshold"] = max(2, int(threshold))
        if duration is not None: cfg[r]["duration"] = max(5, int(duration))
    data["automod"] = cfg
    save_data(data)
    await inter.response.send_message(f"Automod updated: `{r}` -> {cfg[r]}")

# ---- Logs ----
@bot.tree.command(name="set_log_channel", description="Admin: Set log channel here (or specify).")
@app_cmd_check_admin()
@app_commands.describe(channel="Channel (optional)")
async def slash_set_log(inter:discord.Interaction, channel:discord.TextChannel=None):
    channel = channel or inter.channel
    set_log_channel_id(inter.guild.id, channel.id)
    await inter.response.send_message(f"Log channel set to {channel.mention}")

@bot.tree.command(name="disable_log_channel", description="Admin: disable logs.")
@app_cmd_check_admin()
async def slash_disable_log(inter:discord.Interaction):
    set_log_channel_id(inter.guild.id, None)
    await inter.response.send_message("Logs disabled for this server.")

@bot.tree.command(name="check_log_channel", description="Check current log channel.")
@app_cmd_check_blacklist()
async def slash_check_log(inter:discord.Interaction):
    cid = get_log_channel_id(inter.guild.id)
    if not cid:
        await inter.response.send_message("No log channel set.")
    else:
        await inter.response.send_message(f"Log channel: <#{cid}>")

@bot.tree.command(name="logs", description="Show recent logs (count).")
@app_cmd_check_admin()
@app_commands.describe(count="How many last events (1-50)")
async def slash_logs(inter:discord.Interaction, count:int=10):
    count = max(1, min(50, count))
    # We don't store a separate log DB; this command just pings the log channel with a pointer.
    cid = get_log_channel_id(inter.guild.id)
    if not cid:
        await inter.response.send_message("Log channel not set.", ephemeral=True)
        return
    await inter.response.send_message(f"Check the last ~{count} events in <#{cid}>.\n(Events are posted live; use channel history.)", ephemeral=True)

@bot.tree.command(name="log", description="Admin: Show logs related to a specific user (guide).")
@app_cmd_check_admin()
@app_commands.describe(user="Target user")
async def slash_log(inter:discord.Interaction, user:discord.User):
    # Guidance embed (we already send detailed logs as events happen)
    e = AM(0x2B2D31, "Log Lookup")
    e.description = f"Search your log channel for:\n- `{user.id}`\n- Mentions of {user.mention}\n- Message IDs\n\n(Full per-user archival DB would be heavy; current logs contain: user ID, account age, content, message/channel IDs, action type, and time.)"
    await inter.response.send_message(embed=e, ephemeral=True)

# ---- Snipe / Esnipe ----
@bot.tree.command(name="snipe", description="Show recently deleted messages in this channel.")
@app_cmd_check_blacklist()
async def slash_snipe(inter:discord.Interaction):
    items = list(snipes.get(inter.channel.id, []))
    if not items:
        await inter.response.send_message("Nothing to snipe.", ephemeral=True)
        return
    v = SnipeView(items, "delete")
    await inter.response.send_message(embed=v.build_embed(), view=v)

@bot.tree.command(name="esnipe", description="Show recently edited messages in this channel.")
@app_cmd_check_blacklist()
async def slash_esnipe(inter:discord.Interaction):
    items = list(esnipes.get(inter.channel.id, []))
    if not items:
        await inter.response.send_message("Nothing to e-snipe.", ephemeral=True)
        return
    v = SnipeView(items, "edit")
    await inter.response.send_message(embed=v.build_embed(), view=v)

# ---- Triggers (auto-responder) ----
@bot.tree.command(name="trigger_add", description="Admin: add a trigger reply.")
@app_cmd_check_admin()
@app_commands.describe(word="Exact word to match", reply="What bot should reply")
async def slash_trigger_add(inter:discord.Interaction, word:str, reply:str):
    m = trigger_map(inter.guild.id)
    m[word] = reply
    save_data(data)
    await inter.response.send_message(f"Added trigger `{word}` → `{reply}`")

@bot.tree.command(name="trigger_remove", description="Admin: remove a trigger.")
@app_cmd_check_admin()
@app_commands.describe(word="Word to remove")
async def slash_trigger_remove(inter:discord.Interaction, word:str):
    m = trigger_map(inter.guild.id)
    if word in m:
        m.pop(word)
        save_data(data)
        await inter.response.send_message(f"Removed trigger `{word}`")
    else:
        await inter.response.send_message("No such trigger.", ephemeral=True)

@bot.tree.command(name="trigger_list", description="List current triggers.")
@app_cmd_check_blacklist()
async def slash_trigger_list(inter:discord.Interaction):
    m = trigger_map(inter.guild.id)
    if not m:
        await inter.response.send_message("No triggers set.")
        return
    desc = "\n".join([f"`{w}` → `{r}`" for w, r in m.items()])
    await inter.response.send_message(embed=AM(0x5865F2, "Triggers", desc))

# ---- Cats ----
@bot.tree.command(name="cat", description="Send a random cat (image or video).")
@app_cmd_check_blacklist()
async def slash_cat(inter:discord.Interaction):
    await inter.response.defer()
    async with aiohttp.ClientSession() as s:
        url = await fetch_cat_url(s)
    if url:
        await inter.followup.send(url)
    else:
        await inter.followup.send("Couldn't fetch a cat right now.")

@bot.tree.command(name="set_daily_cat_channel", description="Admin: set the daily cat channel (11:00 IST).")
@app_cmd_check_admin()
@app_commands.describe(channel="Channel for daily cat")
async def slash_set_daily_cat(inter:discord.Interaction, channel:discord.TextChannel):
    data.setdefault("cat_daily_channel", {})
    data["cat_daily_channel"][str(inter.guild.id)] = channel.id
    save_data(data)
    await inter.response.send_message(f"Daily cats will go to {channel.mention} at 11:00 {TZ_NAME}.")

@bot.tree.command(name="set_hourly_cat_channel", description="Admin: add this channel for hourly cats.")
@app_cmd_check_admin()
@app_commands.describe(channel="Channel for hourly cats")
async def slash_set_hourly_cat(inter:discord.Interaction, channel:discord.TextChannel):
    arr = hourly_cat_map(inter.guild.id)
    if channel.id not in arr:
        arr.append(channel.id)
        data["cat_hourly_channels"][str(inter.guild.id)] = arr
        save_data(data)
    await inter.response.send_message(f"Hourly cats enabled in {channel.mention}.")

@bot.tree.command(name="stop_hourly_cat", description="Admin: stop hourly cats in this channel.")
@app_cmd_check_admin()
@app_commands.describe(channel="Channel to stop")
async def slash_stop_hourly_cat(inter:discord.Interaction, channel:discord.TextChannel):
    arr = hourly_cat_map(inter.guild.id)
    if channel.id in arr:
        arr.remove(channel.id)
        data["cat_hourly_channels"][str(inter.guild.id)] = arr
        save_data(data)
        await inter.response.send_message(f"Hourly cats disabled in {channel.mention}.")
    else:
        await inter.response.send_message("This channel is not set for hourly cats.", ephemeral=True)

# Schedulers
@tasks.loop(minutes=1)
async def daily_cat_task():
    # runs each minute; posts near 11:00 IST
    now_ist = datetime.now(IST_TZ)
    if now_ist.hour == 11 and now_ist.minute == 0:
        for gid_str, cid in data.get("cat_daily_channel", {}).items():
            g = bot.get_guild(int(gid_str))
            if not g: continue
            ch = g.get_channel(cid)
            if not ch: continue
            try:
                async with aiohttp.ClientSession() as s:
                    url = await fetch_cat_url(s)
                if url:
                    await ch.send(f"🐱 Daily Cat ({TZ_NAME} 11:00):\n{url}")
            except: pass
        await asyncio.sleep(60)  # avoid double post within the same minute

@tasks.loop(hours=1)
async def hourly_cat_task():
    await bot.wait_until_ready()
    for gid_str, arr in data.get("cat_hourly_channels", {}).items():
        g = bot.get_guild(int(gid_str))
        if not g: continue
        for cid in list(arr):
            ch = g.get_channel(cid)
            if not ch: continue
            try:
                async with aiohttp.ClientSession() as s:
                    url = await fetch_cat_url(s)
                if url:
                    await ch.send(f"🐾 Hourly Cat:\n{url}")
            except: pass

# ---- Show Commands (categorized with buttons) ----
CATEGORIES = {
    "Fun": ["cat", "snipe", "esnipe"],
    "Info": ["avatar", "userinfo"],
    "Moderation": ["ban", "unban", "kick", "timeout", "purge", "lock", "unlock", "role_add", "role_remove", "role_temp", "warn", "warn_list", "warn_remove"],
    "Admin": ["say_admin", "set_log_channel", "disable_log_channel", "check_log_channel", "add_blocked_word", "remove_blocked_word", "show_blocked_words", "automod", "trigger_add", "trigger_remove", "trigger_list"],
    "Pookie/Owner": ["add_admin", "remove_admin", "show_admins", "add_trusted", "remove_trusted", "list_trusted", "add_pookie", "remove_pookie", "list_pookies", "restart_service"],
    "Utilities": ["say", "ping", "servers", "serverinfo", "askforcommand"]
}

class ShowCmdsView(discord.ui.View):
    def __init__(self, user:discord.User, accessible:set[str]):
        super().__init__(timeout=120)
        self.user = user
        self.accessible = accessible
        for cat in CATEGORIES.keys():
            self.add_item(ShowCmdButton(cat, self.accessible))

class ShowCmdButton(discord.ui.Button):
    def __init__(self, category:str, accessible:set[str]):
        super().__init__(label=category, style=discord.ButtonStyle.primary)
        self.category = category
        self.accessible = accessible
    async def callback(self, interaction:discord.Interaction):
        if interaction.user.id != interaction.message.interaction.user.id:
            return await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
        cmds = [c for c in CATEGORIES[self.category] if c in self.accessible]
        if not cmds: desc = "_No commands available to you in this category._"
        else: desc = "• " + "\n• ".join(cmds)
        await interaction.response.edit_message(embed=AM(0x5865F2, f"Commands: {self.category}", desc), view=self.view)

def accessible_commands_for(user:discord.abc.User):
    # show only commands they can use (based on perms/pookie/admin/blacklist)
    # we filter by categories definitions above
    accessible = set()
    for cat, names in CATEGORIES.items():
        for name in names:
            # permission gating:
            if name in {"add_admin","remove_admin","show_admins","add_trusted","remove_trusted","list_trusted","add_pookie","remove_pookie","list_pookies","restart_service"}:
                if is_pookie(user):
                    accessible.add(name)
            elif name in {"say_admin","set_log_channel","disable_log_channel","check_log_channel","add_blocked_word","remove_blocked_word","show_blocked_words","automod","trigger_add","trigger_remove","trigger_list","ban","unban","kick","timeout","purge","lock","unlock","role_add","role_remove","role_temp","warn","warn_list","warn_remove"}:
                if is_admin(user):
                    accessible.add(name)
            else:
                # public
                accessible.add(name)
    return accessible

@bot.tree.command(name="showcommands", description="Interactive menu of commands you can use.")
@app_cmd_check_blacklist()
async def slash_showcommands(inter:discord.Interaction):
    acc = accessible_commands_for(inter.user)
    v = ShowCmdsView(inter.user, acc)
    await inter.response.send_message(embed=AM(0x5865F2, "Tap a category to view commands."), view=v, ephemeral=True)

# ---- Warn system ----
@bot.tree.command(name="warn", description="Admin: warn a user.")
@app_cmd_check_admin()
@app_commands.describe(user="User", reason="Reason")
async def slash_warn(inter:discord.Interaction, user:discord.Member, reason:str):
    wm = warns_map(inter.guild.id)
    arr = wm.setdefault(str(user.id), [])
    arr.append({"reason": reason, "mod": inter.user.id, "ts": int(time.time())})
    save_data(data)
    await inter.response.send_message(f"Warned {user.mention}: {reason}")

@bot.tree.command(name="warn_list", description="Show warns for a user.")
@app_cmd_check_admin()
@app_commands.describe(user="User")
async def slash_warn_list(inter:discord.Interaction, user:discord.Member):
    wm = warns_map(inter.guild.id)
    arr = wm.get(str(user.id), [])
    if not arr:
        await inter.response.send_message("No warns.", ephemeral=True)
        return
    lines = []
    for i, w in enumerate(arr, 1):
        lines.append(f"{i}. <t:{w['ts']}:R> by <@{w['mod']}> — {w['reason']}")
    await inter.response.send_message(embed=AM(0xEFBF3E, f"Warns for {user}", "\n".join(lines)[:4000]))

@bot.tree.command(name="warn_remove", description="Remove a warn by index.")
@app_cmd_check_admin()
@app_commands.describe(user="User", index="Warn number (1..N)")
async def slash_warn_remove(inter:discord.Interaction, user:discord.Member, index:int):
    wm = warns_map(inter.guild.id)
    arr = wm.get(str(user.id), [])
    if 1 <= index <= len(arr):
        arr.pop(index-1)
        save_data(data)
        await inter.response.send_message("Removed warn.")
    else:
        await inter.response.send_message("Invalid index.", ephemeral=True)

# ---- Servers list & info ----
@bot.tree.command(name="servers", description="List servers the bot is in.")
@app_cmd_check_pookie_or_owner()
async def slash_servers(inter:discord.Interaction):
    lines = [f"**{len(bot.guilds)} servers**"]
    for g in bot.guilds[:20]:
        lines.append(f"- {g.name} ({g.id}) — {g.member_count} members")
    more = "" if len(bot.guilds)<=20 else f"\n...and {len(bot.guilds)-20} more"
    await inter.response.send_message("\n".join(lines)+more, ephemeral=True)

@bot.tree.command(name="serverinfo", description="Show info about a server (by ID or current).")
@app_cmd_check_admin()
@app_commands.describe(guild_id="Optional server ID (defaults to current)")
async def slash_serverinfo(inter:discord.Interaction, guild_id:str=None):
    g = inter.guild if not guild_id else bot.get_guild(int(guild_id))
    if not g:
        await inter.response.send_message("Guild not found or bot not in it.", ephemeral=True)
        return
    e = AM(0x2B2D31, f"Server Info - {g.name}")
    e.add_field(name="ID", value=str(g.id))
    e.add_field(name="Owner", value=f"{g.owner} ({g.owner_id})" if g.owner_id else "Unknown")
    e.add_field(name="Members", value=str(g.member_count))
    e.add_field(name="Channels", value=f"{len(g.text_channels)} text / {len(g.voice_channels)} voice / {len(g.categories)} categories")
    e.add_field(name="Created", value=f"<t:{int(g.created_at.timestamp())}:F>")
    # Try invite:
    inv = None
    for ch in g.text_channels:
        try:
            inv = await ch.create_invite(max_age=300, max_uses=1, unique=True, reason=f"Requested by {inter.user}")
            break
        except:
            continue
    if inv:
        e.add_field(name="Invite (5m)", value=str(inv))
    await inter.response.send_message(embed=e, ephemeral=True)

# ---- Ask for Command ----
@bot.tree.command(name="askforcommand", description="Ask owner for a command idea.")
@app_cmd_check_blacklist()
@app_commands.describe(idea="Describe what command/feature you want")
async def slash_askforcommand(inter:discord.Interaction, idea:str):
    # ping owner, DM owner, and log
    msg = f"**Command Request** from {inter.user.mention} ({inter.user.id}) in **{inter.guild.name}** ({inter.guild.id}):\n> {idea}"
    try:
        owner = await bot.fetch_user(OWNER_ID)
        await owner.send(msg)
    except: pass
    await send_log(inter.guild, AM(0x5865F2, "Command Request", msg))
    await inter.response.send_message("Sent to owner. Thanks!", ephemeral=True)

# ---- Restart Render Service ----
@bot.tree.command(name="restart_service", description="Owner/Pookie: trigger a new deploy on Render.")
@app_cmd_check_pookie_or_owner()
async def slash_restart_service(inter:discord.Interaction):
    if not (RENDER_API_KEY and RENDER_SERVICE_ID):
        await inter.response.send_message("Missing RENDER_API_KEY or RENDER_SERVICE_ID.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    url = f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/deploys"
    headers = {"Authorization": f"Bearer {RENDER_API_KEY}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, headers=headers, json={"clearCache":True}) as r:
                txt = await r.text()
        await inter.followup.send(f"Triggered deploy on Render.\nResponse: `{txt[:1800]}`", ephemeral=True)
    except Exception as e:
        await inter.followup.send(f"Failed: {e}", ephemeral=True)

# ---------------------------
# PREFIX COMMANDS (mirror key ones)
# ---------------------------
@bot.command(name="say")
async def pc_say(ctx:commands.Context, *, message:str):
    if is_blacklisted(ctx.author): return
    await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())

@bot.command(name="say_admin")
async def pc_say_admin(ctx:commands.Context, *, message:str):
    if not is_admin(ctx.author):
        return await ctx.reply("Admin-only.")
    await ctx.send(message)

@bot.command(name="ban")
async def pc_ban(ctx:commands.Context, user:discord.User|None=None, *, reason:str="No reason"):
    if not is_admin(ctx.author):
        return
    if user is None:
        # allow ID usage: ?ban <id> <reason...>
        parts = ctx.message.content.split()
        if len(parts)>=2:
            try:
                uid = int(parts[1].strip("<@!>"))
                user = await bot.fetch_user(uid)
                reason = " ".join(parts[2:]) if len(parts)>2 else reason
            except: pass
    if user:
        try:
            await ctx.guild.ban(user, reason=f"{reason} | by {ctx.author}")
            await ctx.reply(f"Banned {user} for: {reason}")
        except Exception as e:
            await ctx.reply(f"Failed: {e}")

@bot.command(name="purge")
async def pc_purge(ctx:commands.Context, amount:int):
    if not is_admin(ctx.author):
        return
    amount = max(1, min(100, amount))
    await ctx.channel.purge(limit=amount)
    try: await ctx.message.delete()
    except: pass

# ---------------------------
# DEBUG / UPTIME
# ---------------------------
@bot.tree.command(name="debug", description="Show uptime, system info, guilds.")
@app_cmd_check_admin()
async def slash_debug(inter:discord.Interaction):
    proc = psutil.Process(os.getpid())
    mem = proc.memory_info().rss / (1024**2)
    cpu = psutil.cpu_percent(interval=0.3)
    up = human_timedelta(time.time() - start_time)
    gcount = len(bot.guilds)
    e = AM(0x57F287, "Debug")
    e.add_field(name="Uptime", value=up)
    e.add_field(name="Guilds", value=str(gcount))
    e.add_field(name="CPU%", value=str(cpu))
    e.add_field(name="RAM(MB)", value=f"{mem:.1f}")
    e.add_field(name="Python", value=platform.python_version())
    e.add_field(name="discord.py", value=discord.__version__)
    await inter.response.send_message(embed=e,)ephemeral=# ---------------------------

# LOAD COGS
# ---------------------------
async def load_extensions():
    await bot.load_extension("afk")  # loads afk.py

@bot.event
async def setup_hook():
    await load_extensions()

# ---------------------------
# RUN
# ---------------------------
if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        pass
