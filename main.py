import os, re, json, asyncio, traceback, aiohttp, time, psutil, platform, math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from discord import app_commands

from typing import Optional, Dict, Any, List, Tuple

from flask import Flask
from threading import Thread

# =========================
# ====== CONFIG/ENV =======
# =========================

def env_str(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if v else default

def env_int(name: str, default: int) -> int:
    raw = env_str(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        # if user accidentally set "OWNER_ID = 123" instead of "123"
        digits = "".join(ch for ch in raw if ch.isdigit())
        if digits:
            return int(digits)
        return default

TOKEN = env_str("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN not set")

OWNER_ID = env_int("OWNER_ID", 1319292111325106296)
CAT_API_KEY = env_str("CAT_API_KEY")
RENDER_API_KEY = env_str("RENDER_API_KEY")  # optional (for remote restarts via API; not used unless you wire it)
RENDER_SERVICE_ID = env_str("RENDER_SERVICE_ID")  # optional
TZ_NAME = env_str("TZ", "Asia/Kolkata")

try:
    LOCAL_TZ = ZoneInfo(TZ_NAME)
except Exception:
    LOCAL_TZ = ZoneInfo("Asia/Kolkata")

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
INTENTS.guilds = True
INTENTS.reactions = True
INTENTS.presences = False

BOT_PREFIX = "?"
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=INTENTS, help_command=None)
tree = bot.tree

LAUNCH_TIME = time.time()

# Presence: DND + Streaming “Max Verstappen”
STREAMING_ACTIVITY = discord.Streaming(name="Max Verstappen", url="https://www.twitch.tv/maxverstappen")

# =========================
# ====== PERSISTENCE ======
# =========================

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

FILES = {
    "core": os.path.join(DATA_DIR, "core.json"),
    "logs": os.path.join(DATA_DIR, "logs.json"),
    "snipes": os.path.join(DATA_DIR, "snipes.json"),
    "triggers": os.path.join(DATA_DIR, "triggers.json"),
    "warns": os.path.join(DATA_DIR, "warns.json"),
    "afk": os.path.join(DATA_DIR, "afk.json"),
}

DEFAULTS = {
    "core": {
        "admins": [OWNER_ID, 1380315427992768633, 909468887098216499],   # <- your requested default admins persistently
        "pookies": [],
        "blacklist": [],             # user IDs not allowed to use bot
        "blocked_words": [],         # exact-word blocks (word-boundary)
        "log_channel_id": None,
        "daily_cat_channel_id": None,
        "hourly_cat_channel_id": None,
        "hourly_cat_enabled": False,
        "default_trigger_reply": "Okay!",
    },
    "logs": {
        "entries": []               # append dict entries; we will cap locally when reading
    },
    "snipes": {
        "deleted": {},              # {guild_id:{channel_id:[entries...]}}
        "edited": {}                # same
    },
    "triggers": {
        # per guild triggers
        # guild_id: [{"word":"max","reply":"Hi"}, ...]
    },
    "warns": {
        # guild_id: {user_id: [{"reason":"...", "mod_id":123, "ts":...}, ...]}
    },
    "afk": {
        # guild_id: {user_id: {"reason":"...", "since":...} }
    },
}

def load_json(name: str) -> Dict[str, Any]:
    path = FILES[name]
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULTS[name], f, indent=2)
        return json.loads(json.dumps(DEFAULTS[name]))
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # corrupt -> reset
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULTS[name], f, indent=2)
        return json.loads(json.dumps(DEFAULTS[name]))

def save_json(name: str, data: Dict[str, Any]) -> None:
    path = FILES[name]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

core = load_json("core")
logs = load_json("logs")
snipes = load_json("snipes")
triggers = load_json("triggers")
warns = load_json("warns")
afkdb = load_json("afk")

def now_ts() -> float:
    return time.time()

def now_dt_tz() -> datetime:
    return datetime.now(tz=LOCAL_TZ)

def ts_str(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else now_ts(), tz=LOCAL_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")

# =========================
# ====== PERMISSIONS ======
# =========================

def is_owner(user: discord.abc.User) -> bool:
    return int(user.id) == int(OWNER_ID)

def is_admin(user: discord.Member) -> bool:
    if is_owner(user):
        return True
    return int(user.id) in core.get("admins", [])

def is_pookie(user: discord.Member) -> bool:
    if is_owner(user) or is_admin(user):
        return True
    return int(user.id) in core.get("pookies", [])

def is_blacklisted(user: discord.abc.User) -> bool:
    return int(user.id) in core.get("blacklist", [])

def ensure_guild_dict(d: Dict[str, Any], guild_id: int) -> Dict[str, Any]:
    gid = str(guild_id)
    if gid not in d:
        d[gid] = {}
    return d[gid]

def add_log(kind: str, detail: Dict[str, Any]):
    """Append to persistent logs and also post to log channel if set."""
    entry = {
        "ts": now_ts(),
        "kind": kind,
        "detail": detail
    }
    logs["entries"].append(entry)
    # optional cap to ~50k entries to avoid gigantic files
    if len(logs["entries"]) > 50000:
        logs["entries"] = logs["entries"][-40000:]
    save_json("logs", logs)
    # post to channel if configured
    lc_id = core.get("log_channel_id")
    if lc_id:
        chan = bot.get_channel(int(lc_id))
        if chan:
            try:
                emb = discord.Embed(title=f"Log: {kind}", color=discord.Color.blurple(), timestamp=datetime.utcnow())
                # flatten detail
                for k, v in detail.items():
                    s = str(v)
                    if len(s) > 1024: s = s[:1020] + "..."
                    emb.add_field(name=str(k), value=s, inline=False)
                asyncio.create_task(chan.send(embed=emb))
            except Exception:
                pass

async def log_command(ctx_or_inter: Any, cmd_name: str, args_text: str = ""):
    try:
        user = ctx_or_inter.user if isinstance(ctx_or_inter, discord.Interaction) else ctx_or_inter.author
        guild = ctx_or_inter.guild
        channel = ctx_or_inter.channel
        add_log("command", {
            "user_id": user.id,
            "user": f"{user}#{getattr(user, 'discriminator', '')}",
            "guild_id": getattr(guild, "id", None),
            "guild": getattr(guild, "name", None),
            "channel_id": getattr(channel, "id", None),
            "channel": getattr(channel, "name", None),
            "cmd": cmd_name,
            "args": args_text,
            "time": ts_str()
        })
    except Exception:
        pass

# Decorators to auto-log commands
def log_prefix_command(name: str):
    def decorator(func):
        async def wrapper(ctx: commands.Context, *args, **kwargs):
            await log_command(ctx, name, ctx.message.content)
            return await func(ctx, *args, **kwargs)
        wrapper.__name__ = func.__name__
        return bot.command(name=name)(wrapper)
    return decorator

def log_slash_command(name: str):
    def decorator(func):
        @app_commands.command(name=name)
        async def wrapped(interaction: discord.Interaction, *args, **kwargs):
            # build args_text for slash
            args_text = " ".join([f"{k}={v}" for k, v in kwargs.items()])
            await log_command(interaction, f"/{name}", args_text)
            return await func(interaction, *args, **kwargs)
        return wrapped
    return decorator

# =========================
# ====== KEEP-ALIVE =======
# =========================

flask_app = Flask("bot_keepalive")

@flask_app.route("/")
def index():
    return "OK", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))

def start_flask():
    t = Thread(target=run_flask, daemon=True)
    t.start()

# =========================
# ======= UTILITIES =======
# =========================

def human_timedelta(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, s = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {s}s"
    hours, m = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {m}m"
    days, h = divmod(hours, 24)
    return f"{days}d {h}h"

def parse_duration(text: str) -> Optional[int]:
    """
    parse "10m", "2h", "3d" into seconds
    """
    m = re.fullmatch(r"(\d+)([smhd])", text.lower().strip())
    if not m: return None
    val = int(m.group(1))
    unit = m.group(2)
    mult = {"s":1, "m":60, "h":3600, "d":86400}[unit]
    return val * mult

def boundary_regex(word: str) -> re.Pattern:
    # exact word match with word boundaries; escape special chars
    pattern = r"\b" + re.escape(word) + r"\b"
    return re.compile(pattern, flags=re.IGNORECASE)

async def fetch_cat_url(session: aiohttp.ClientSession) -> Optional[str]:
    headers = {"x-api-key": CAT_API_KEY} if CAT_API_KEY else {}
    url = "https://api.thecatapi.com/v1/images/search"
    try:
        async with session.get(url, headers=headers, timeout=20) as r:
            if r.status != 200:
                return None
            data = await r.json()
            if isinstance(data, list) and data:
                return data[0].get("url")
            return None
    except Exception:
        return None

# =========================
# ======= PRESENCE ========
# =========================

@bot.event
async def on_ready():
    await bot.change_presence(status=discord.Status.dnd, activity=STREAMING_ACTIVITY)
    # sync tree safely
    try:
        await tree.sync()
    except Exception:
        pass
    print(f"{bot.user} is online, DND, and streaming Max Verstappen! Synced slash commands.")
    start_flask()
    # start tasks
    hourly_cat_post.start()
    daily_cat_conditional.start()

# =========================
# ======== LOGGING ========
# =========================

@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot:
        return
    # store snipe
    gid = str(message.guild.id) if message.guild else "dm"
    cid = str(message.channel.id)
    snipes["deleted"].setdefault(gid, {}).setdefault(cid, [])
    snipes["deleted"][gid][cid].append({
        "author_id": message.author.id,
        "author_name": str(message.author),
        "content": message.content or "",
        "avatar": str(message.author.display_avatar.url) if hasattr(message.author, "display_avatar") else "",
        "ts": now_ts()
    })
    # cap list size
    if len(snipes["deleted"][gid][cid]) > 50:
        snipes["deleted"][gid][cid] = snipes["deleted"][gid][cid][-50:]
    save_json("snipes", snipes)

    add_log("message_delete", {
        "author_id": message.author.id,
        "author": str(message.author),
        "guild_id": getattr(message.guild, "id", None),
        "channel_id": getattr(message.channel, "id", None),
        "content": message.content,
        "time": ts_str()
    })

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot:
        return
    if before.content == after.content:
        return
    gid = str(before.guild.id) if before.guild else "dm"
    cid = str(before.channel.id)
    snipes["edited"].setdefault(gid, {}).setdefault(cid, [])
    snipes["edited"][gid][cid].append({
        "author_id": before.author.id,
        "author_name": str(before.author),
        "before": before.content or "",
        "after": after.content or "",
        "avatar": str(before.author.display_avatar.url) if hasattr(before.author, "display_avatar") else "",
        "ts": now_ts()
    })
    if len(snipes["edited"][gid][cid]) > 50:
        snipes["edited"][gid][cid] = snipes["edited"][gid][cid][-50:]
    save_json("snipes", snipes)

    add_log("message_edit", {
        "author_id": before.author.id,
        "author": str(before.author),
        "guild_id": getattr(before.guild, "id", None),
        "channel_id": getattr(before.channel, "id", None),
        "before": before.content,
        "after": after.content,
        "time": ts_str()
    })

@bot.event
async def on_member_join(member: discord.Member):
    add_log("member_join", {
        "member_id": member.id,
        "member": str(member),
        "guild_id": member.guild.id,
        "time": ts_str()
    })

@bot.event
async def on_member_remove(member: discord.Member):
    add_log("member_leave", {
        "member_id": member.id,
        "member": str(member),
        "guild_id": member.guild.id,
        "time": ts_str()
    })

# =========================
# ======== AFK SYS ========
# =========================

@tree.command(name="afk", description="Set yourself AFK with an optional reason.")
@app_commands.describe(reason="Reason you're AFK")
async def slash_afk(interaction: discord.Interaction, reason: Optional[str] = None):
    gid = str(interaction.guild_id)
    g = afkdb.setdefault(gid, {})
    g[str(interaction.user.id)] = {"reason": reason or "AFK", "since": now_ts()}
    save_json("afk", afkdb)
    await interaction.response.send_message(f"You're now AFK: **{g[str(interaction.user.id)]['reason']}**", ephemeral=True)
    await log_command(interaction, "/afk", reason or "")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Clear AFK if user talks
    gid = str(getattr(message.guild, "id", "dm"))
    if gid in afkdb and str(message.author.id) in afkdb[gid]:
        afkdb[gid].pop(str(message.author.id), None)
        save_json("afk", afkdb)
        try:
            await message.channel.send(f"Welcome back {message.author.mention}, AFK removed.", delete_after=5)
        except Exception:
            pass

    # Notify if mentioning AFK people
    if message.mentions:
        for u in message.mentions:
            if gid in afkdb and str(u.id) in afkdb[gid]:
                data = afkdb[gid][str(u.id)]
                since = human_timedelta(now_ts() - data.get("since", now_ts()))
                try:
                    await message.reply(f"{u.mention} is AFK: **{data.get('reason','AFK')}** — {since} ago.")
                except Exception:
                    pass

    # Blocked words check (exact word match)
    blocked_words = core.get("blocked_words", [])
    if blocked_words:
        lower = message.content.lower()
        for w in blocked_words:
            pat = boundary_regex(w)
            if pat.search(lower):
                try:
                    await message.delete()
                except Exception:
                    pass
                add_log("blocked_word_delete", {
                    "guild_id": getattr(message.guild, "id", None),
                    "channel_id": getattr(message.channel, "id", None),
                    "user_id": message.author.id,
                    "word": w,
                    "content": message.content,
                    "time": ts_str()
                })
                return

    await bot.process_commands(message)

# =========================
# ====== TRIGGERS =========
# =========================

TRIGGER_CAT = "Triggers"

def get_guild_triggers(guild_id: int) -> List[Dict[str, Any]]:
    return triggers.get(str(guild_id), [])

def set_guild_triggers(guild_id: int, arr: List[Dict[str, Any]]):
    triggers[str(guild_id)] = arr
    save_json("triggers", triggers)

@tree.command(name="trigger_add", description="(Admin) Add an auto-responder trigger")
@app_commands.describe(word="Exact word to match", reply="What the bot should say")
async def slash_trigger_add(interaction: discord.Interaction, word: str, reply: str):
    if not is_admin(interaction.user) and not is_pookie(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    arr = get_guild_triggers(interaction.guild_id)
    if any(t["word"].lower() == word.lower() for t in arr):
        return await interaction.response.send_message("Trigger already exists.", ephemeral=True)
    arr.append({"word": word, "reply": reply})
    set_guild_triggers(interaction.guild_id, arr)
    await interaction.response.send_message(f"Added trigger **{word}** → **{reply}**")

@tree.command(name="trigger_remove", description="(Admin) Remove an auto-responder trigger")
@app_commands.describe(word="Exact word to remove")
async def slash_trigger_remove(interaction: discord.Interaction, word: str):
    if not is_admin(interaction.user) and not is_pookie(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    arr = get_guild_triggers(interaction.guild_id)
    new = [t for t in arr if t["word"].lower() != word.lower()]
    set_guild_triggers(interaction.guild_id, new)
    await interaction.response.send_message(f"Removed trigger **{word}**")

@tree.command(name="trigger_list", description="List triggers")
async def slash_trigger_list(interaction: discord.Interaction):
    arr = get_guild_triggers(interaction.guild_id)
    if not arr:
        return await interaction.response.send_message("No triggers set.")
    text = "\n".join([f"- **{t['word']}** → {t['reply']}" for t in arr])
    await interaction.response.send_message(f"**Triggers:**\n{text}")

@bot.listen("on_message")
async def triggers_listener(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    arr = get_guild_triggers(message.guild.id)
    if not arr:
        return
    content = message.content.strip()
    if not content:
        return
    # exact word match only (e.g., "max" only fires for "max", not "max is best")
    for t in arr:
        pat = boundary_regex(t["word"])
        if pat.fullmatch(content.strip(),):
            try:
                await message.channel.send(t["reply"].replace("{user}", message.author.mention))
                add_log("trigger_fire", {
                    "guild_id": message.guild.id,
                    "channel_id": message.channel.id,
                    "user_id": message.author.id,
                    "trigger": t["word"],
                    "reply": t["reply"],
                    "time": ts_str()
                })
            except Exception:
                pass
            break

# =========================
# ======= LOG CHANNEL =====
# =========================

@tree.command(name="set_log_channel", description="(Admin) Set the log channel")
@app_commands.describe(channel="Channel to send logs")
async def slash_set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_admin(interaction.user) and not is_pookie(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    core["log_channel_id"] = int(channel.id)
    save_json("core", core)
    await interaction.response.send_message(f"Log channel set to {channel.mention}")
    add_log("config_change", {"what": "log_channel_id", "value": channel.id, "by": interaction.user.id, "time": ts_str()})

@log_prefix_command("set_log_channel")
async def set_log_channel_prefix(ctx: commands.Context, channel: discord.TextChannel):
    if not is_admin(ctx.author) and not is_pookie(ctx.author):
        return await ctx.reply("No permission.")
    core["log_channel_id"] = int(channel.id)
    save_json("core", core)
    await ctx.reply(f"Log channel set to {channel.mention}")
    add_log("config_change", {"what": "log_channel_id", "value": channel.id, "by": ctx.author.id, "time": ts_str()})

# =========================
# ====== FUN COMMANDS =====
# =========================

FUN_CAT = "Fun"

@tree.command(name="cat", description="Random cat image")
async def slash_cat(interaction: discord.Interaction):
    async with aiohttp.ClientSession() as session:
        url = await fetch_cat_url(session)
    if not url:
        return await interaction.response.send_message("Couldn't fetch a cat right now.")
    await interaction.response.send_message(url)

@log_prefix_command("cat")
async def cat_prefix(ctx: commands.Context):
    async with aiohttp.ClientSession() as session:
        url = await fetch_cat_url(session)
    await ctx.reply(url if url else "Couldn't fetch a cat right now.")

EIGHTBALL_ANSWERS = [
    "Yes.", "No.", "Maybe.", "Definitely.", "Absolutely not.", "Ask again later.", "It is certain.",
    "Very doubtful.", "Without a doubt.", "Better not tell you now.", "Most likely.", "Outlook not so good."
]

@tree.command(name="8ball", description="Magic 8-ball")
@app_commands.describe(question="Ask the 8-ball a question")
async def slash_8ball(interaction: discord.Interaction, question: str):
    import random
    await interaction.response.send_message(f"🎱 {random.choice(EIGHTBALL_ANSWERS)}")

@log_prefix_command("8ball")
async def eightball_prefix(ctx: commands.Context, *, question: str):
    import random
    await ctx.reply(f"🎱 {random.choice(EIGHTBALL_ANSWERS)}")

JOKES = [
    "Why did the scarecrow get promoted? He was outstanding in his field!",
    "I told my computer I needed a break, and it said 'No problem — I’ll go to sleep.'",
]
DADJOKES = [
    "I would tell you a construction joke, but I’m still working on it.",
    "I used to be a baker, then I couldn't make enough dough.",
]

@tree.command(name="joke", description="Random joke")
async def slash_joke(interaction: discord.Interaction):
    import random
    await interaction.response.send_message(random.choice(JOKES))

@log_prefix_command("joke")
async def joke_prefix(ctx: commands.Context):
    import random
    await ctx.reply(random.choice(JOKES))

@tree.command(name="dadjoke", description="Random dad joke")
async def slash_dadjoke(interaction: discord.Interaction):
    import random
    await interaction.response.send_message(random.choice(DADJOKES))

@log_prefix_command("dadjoke")
async def dadjoke_prefix(ctx: commands.Context):
    import random
    await ctx.reply(random.choice(DADJOKES))

@tree.command(name="rps", description="Rock Paper Scissors")
@app_commands.describe(choice="rock/paper/scissors")
async def slash_rps(interaction: discord.Interaction, choice: app_commands.Choice[str]):
    import random
    bot_choice = random.choice(["rock","paper","scissors"])
    user_choice = choice.value
    outcome = "draw"
    if (user_choice, bot_choice) in [("rock","scissors"),("paper","rock"),("scissors","paper")]:
        outcome = "you win"
    elif user_choice != bot_choice:
        outcome = "you lose"
    await interaction.response.send_message(f"You: **{user_choice}** | Bot: **{bot_choice}** → {outcome}")

# set fixed choices
slash_rps.__dict__["_params"] = []
choice_param = app_commands.Parameter(name="choice", description="Choose one", required=True)
setattr(choice_param, "type", discord.AppCommandOptionType.string)
# We cannot mutate .choices directly; define choices via decorator in newer versions.
# To keep compatibility, override with transformer:
@slash_rps.autocomplete("choice")
async def rps_autocomplete(interaction: discord.Interaction, current: str):
    opts = ["rock","paper","scissors"]
    return [app_commands.Choice(name=o, value=o) for o in opts if current.lower() in o]

@log_prefix_command("rps")
async def rps_prefix(ctx: commands.Context, choice: str):
    import random
    choice = choice.lower()
    if choice not in ("rock","paper","scissors"):
        return await ctx.reply("Choose rock, paper, or scissors.")
    bot_choice = random.choice(["rock","paper","scissors"])
    outcome = "draw"
    if (choice, bot_choice) in [("rock","scissors"),("paper","rock"),("scissors","paper")]:
        outcome = "you win"
    elif choice != bot_choice:
        outcome = "you lose"
    await ctx.reply(f"You: **{choice}** | Bot: **{bot_choice}** → {outcome}")

@tree.command(name="coinflip", description="Flip a coin")
async def slash_coinflip(interaction: discord.Interaction):
    import random
    await interaction.response.send_message(f"Heads" if random.random()<0.5 else "Tails")

@log_prefix_command("coinflip")
async def coinflip_prefix(ctx: commands.Context):
    import random
    await ctx.reply("Heads" if random.random()<0.5 else "Tails")

@tree.command(name="rolldice", description="Roll a 6-sided die")
async def slash_rolldice(interaction: discord.Interaction):
    import random
    await interaction.response.send_message(f"🎲 {random.randint(1,6)}")

@log_prefix_command("rolldice")
async def rolldice_prefix(ctx: commands.Context):
    import random
    await ctx.reply(f"🎲 {random.randint(1,6)}")

# =========================
# ======= SAY (NO PING) ===
# =========================

@tree.command(name="say", description="Make the bot say something (no pings)")
@app_commands.describe(text="What should I say?")
async def slash_say(interaction: discord.Interaction, text: str):
    # strip mentions to avoid ping
    sanitized = discord.utils.remove_markdown(text)
    sanitized = re.sub(r"<@!?(\d+)>", r"@\1", sanitized)
    sanitized = re.sub(r"<@&(\d+)>", r"@\1", sanitized)
    await interaction.response.send_message("Sent.", ephemeral=True)
    await interaction.channel.send(sanitized)

@log_prefix_command("say")
async def say_prefix(ctx: commands.Context, *, text: str):
    sanitized = discord.utils.remove_markdown(text)
    sanitized = re.sub(r"<@!?(\d+)>", r"@\1", sanitized)
    sanitized = re.sub(r"<@&(\d+)>", r"@\1", sanitized)
    await ctx.message.delete()
    await ctx.send(sanitized)

@tree.command(name="say_admin", description="(Admin) Say (mentions allowed)")
@app_commands.describe(text="What should I say (mentions allowed)?")
async def slash_say_admin(interaction: discord.Interaction, text: str):
    if not is_admin(interaction.user) and not is_pookie(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    await interaction.response.send_message("Sent.", ephemeral=True)
    await interaction.channel.send(text)

@log_prefix_command("say_admin")
async def say_admin_prefix(ctx: commands.Context, *, text: str):
    if not is_admin(ctx.author) and not is_pookie(ctx.author):
        return await ctx.reply("No permission.")
    await ctx.message.delete()
    await ctx.send(text)

# =========================
# ======= AVATAR/INFO =====
# =========================

@tree.command(name="avatar", description="Show a user's avatar")
@app_commands.describe(user="User (mention) or leave empty for yourself")
async def slash_avatar(interaction: discord.Interaction, user: Optional[discord.User] = None):
    user = user or interaction.user
    emb = discord.Embed(title=f"Avatar of {user}", color=discord.Color.blurple())
    emb.set_image(url=user.display_avatar.url)
    await interaction.response.send_message(embed=emb)

@log_prefix_command("avatar")
async def avatar_prefix(ctx: commands.Context, user: Optional[discord.User] = None):
    user = user or ctx.author
    emb = discord.Embed(title=f"Avatar of {user}", color=discord.Color.blurple())
    emb.set_image(url=user.display_avatar.url)
    await ctx.reply(embed=emb)

@tree.command(name="userinfo", description="User info by mention or ID")
@app_commands.describe(user_id="Optional user ID (if not mentioning)")
async def slash_userinfo(interaction: discord.Interaction, user_id: Optional[str] = None, user: Optional[discord.User] = None):
    target = None
    if user:
        target = user
    elif user_id and user_id.isdigit():
        try:
            target = await bot.fetch_user(int(user_id))
        except Exception:
            pass
    else:
        target = interaction.user
    if not target:
        return await interaction.response.send_message("User not found.", ephemeral=True)
    emb = discord.Embed(title=f"User info: {target}", color=discord.Color.green())
    emb.add_field(name="ID", value=str(target.id))
    emb.add_field(name="Bot", value=str(target.bot))
    emb.add_field(name="Created", value=str(target.created_at.replace(tzinfo=timezone.utc)))
    if interaction.guild:
        m = interaction.guild.get_member(target.id)
        if m:
            emb.add_field(name="Joined", value=str(m.joined_at.replace(tzinfo=timezone.utc)))
            roles = [r.mention for r in m.roles if r.name != "@everyone"]
            emb.add_field(name="Roles", value=", ".join(roles) if roles else "None", inline=False)
    emb.set_thumbnail(url=target.display_avatar.url)
    await interaction.response.send_message(embed=emb)

@log_prefix_command("userinfo")
async def userinfo_prefix(ctx: commands.Context, user: Optional[discord.User] = None):
    inter = type("X", (), {"guild": ctx.guild, "user": ctx.author})
    return await slash_userinfo.__wrapped__(interaction=inter, user=user, user_id=None)  # reuse logic

@tree.command(name="guildinfo", description="Show info for a guild by ID")
@app_commands.describe(guild_id="Guild/server ID")
async def slash_guildinfo(interaction: discord.Interaction, guild_id: str):
    if not guild_id.isdigit():
        return await interaction.response.send_message("Invalid guild ID.", ephemeral=True)
    g = bot.get_guild(int(guild_id))
    if not g:
        return await interaction.response.send_message("I'm not in that guild (or cannot access it).", ephemeral=True)
    emb = discord.Embed(title=f"Guild info: {g.name}", color=discord.Color.gold())
    emb.add_field(name="ID", value=str(g.id))
    emb.add_field(name="Owner", value=str(g.owner))
    emb.add_field(name="Created", value=str(g.created_at.replace(tzinfo=timezone.utc)))
    emb.add_field(name="Members", value=str(g.member_count))
    cats = sum(1 for c in g.channels if isinstance(c, discord.CategoryChannel))
    txts = sum(1 for c in g.channels if isinstance(c, discord.TextChannel))
    vcos = sum(1 for c in g.channels if isinstance(c, discord.VoiceChannel))
    emb.add_field(name="Channels", value=f"{cats} categories | {txts} text | {vcos} voice", inline=False)
    emb.add_field(name="Roles", value=str(len(g.roles)))
    emb.add_field(name="Boosts", value=str(g.premium_subscription_count or 0))
    emb.add_field(name="Verification", value=str(g.verification_level))
    if g.features:
        emb.add_field(name="Features", value=", ".join(g.features), inline=False)
    # Try to create an invite (needs permission)
    invite_txt = "N/A"
    try:
        # find any text channel we can create invite in
        for ch in g.text_channels:
            if ch.permissions_for(g.me).create_instant_invite:
                inv = await ch.create_invite(max_age=3600, max_uses=1, unique=True)
                invite_txt = inv.url
                break
    except Exception:
        pass
    emb.add_field(name="Invite (1h, 1 use)", value=invite_txt, inline=False)
    emb.set_thumbnail(url=g.icon.url if g.icon else discord.Embed.Empty)
    await interaction.response.send_message(embed=emb)

@log_prefix_command("guildinfo")
async def guildinfo_prefix(ctx: commands.Context, guild_id: str):
    inter = type("X", (), {"guild": ctx.guild})
    return await slash_guildinfo.__wrapped__(interaction=inter, guild_id=guild_id)

@tree.command(name="servers", description="List servers the bot is in (owner only)")
async def slash_servers(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    text = "\n".join([f"- {g.name} (`{g.id}`) — {g.member_count} members" for g in bot.guilds])
    await interaction.response.send_message(text or "No servers.")

@log_prefix_command("servers")
async def servers_prefix(ctx: commands.Context):
    if not is_owner(ctx.author):
        return await ctx.reply("Owner only.")
    text = "\n".join([f"- {g.name} (`{g.id}`) — {g.member_count} members" for g in bot.guilds])
    await ctx.reply(text or "No servers.")

# =========================
# ===== MODERATION =========
# =========================

def mod_check(u: discord.Member) -> bool:
    return is_owner(u) or is_admin(u) or is_pookie(u)

# ?ban: accepts mention OR ID; can ban by ID even if not present
@log_prefix_command("ban")
@commands.has_permissions(ban_members=True)
async def ban_prefix(ctx: commands.Context, target: str, *, reason: Optional[str] = None):
    if not mod_check(ctx.author):
        return await ctx.reply("No permission.")
    user_obj: Optional[discord.abc.User] = None
    if target.isdigit():
        try:
            user_obj = await bot.fetch_user(int(target))
        except Exception:
            pass
    elif ctx.message.mentions:
        user_obj = ctx.message.mentions[0]
    if not user_obj:
        return await ctx.reply("User not found by ID/mention.")
    try:
        await ctx.guild.ban(discord.Object(id=user_obj.id), reason=reason)
        await ctx.reply(f"Banned **{user_obj}** | Reason: {reason or 'N/A'}")
        add_log("ban", {
            "guild_id": ctx.guild.id, "mod_id": ctx.author.id, "target_id": user_obj.id, "reason": reason, "time": ts_str()
        })
    except Exception as e:
        await ctx.reply(f"Failed to ban: {e}")

@tree.command(name="ban", description="Ban a member (server member only)")
@app_commands.describe(member="Member to ban", reason="Reason")
async def slash_ban(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
    if not mod_check(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    try:
        await member.ban(reason=reason)
        await interaction.response.send_message(f"Banned **{member}** | Reason: {reason or 'N/A'}")
        add_log("ban", {
            "guild_id": interaction.guild_id, "mod_id": interaction.user.id, "target_id": member.id, "reason": reason, "time": ts_str()
        })
    except Exception as e:
        await interaction.response.send_message(f"Failed to ban: {e}", ephemeral=True)

@log_prefix_command("kick")
@commands.has_permissions(kick_members=True)
async def kick_prefix(ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None):
    if not mod_check(ctx.author):
        return await ctx.reply("No permission.")
    try:
        await member.kick(reason=reason)
        await ctx.reply(f"Kicked **{member}** | Reason: {reason or 'N/A'}")
        add_log("kick", {"guild_id": ctx.guild.id, "mod_id": ctx.author.id, "target_id": member.id, "reason": reason, "time": ts_str()})
    except Exception as e:
        await ctx.reply(f"Failed to kick: {e}")

@tree.command(name="kick", description="Kick a member")
@app_commands.describe(member="Member to kick", reason="Reason")
async def slash_kick(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
    if not mod_check(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(f"Kicked **{member}** | Reason: {reason or 'N/A'}")
        add_log("kick", {"guild_id": interaction.guild_id, "mod_id": interaction.user.id, "target_id": member.id, "reason": reason, "time": ts_str()})
    except Exception as e:
        await interaction.response.send_message(f"Failed to kick: {e}", ephemeral=True)

@tree.command(name="mute", description="Timeout a member for duration (e.g., 10m, 2h)")
@app_commands.describe(member="Member", duration="e.g., 10m / 2h / 4d", reason="Reason")
async def slash_mute(interaction: discord.Interaction, member: discord.Member, duration: str, reason: Optional[str] = None):
    if not mod_check(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    seconds = parse_duration(duration)
    if not seconds:
        return await interaction.response.send_message("Use duration like 10m / 2h / 4d.", ephemeral=True)
    until = datetime.utcnow().replace(tzinfo=timezone.utc) + timedelta(seconds=seconds)
    try:
        await member.timeout(until, reason=reason)
        await interaction.response.send_message(f"Timed out **{member}** for {duration}. Reason: {reason or 'N/A'}")
        add_log("timeout", {"guild_id": interaction.guild_id, "mod_id": interaction.user.id, "target_id": member.id, "duration": duration, "reason": reason, "time": ts_str()})
    except Exception as e:
        await interaction.response.send_message(f"Failed to timeout: {e}", ephemeral=True)

@tree.command(name="lock", description="Lock this channel (deny @everyone Send Messages)")
async def slash_lock(interaction: discord.Interaction):
    if not mod_check(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    overwrites = interaction.channel.overwrites_for(interaction.guild.default_role)
    overwrites.send_messages = False
    await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrites)
    await interaction.response.send_message("Channel locked.")
    add_log("lock_channel", {"guild_id": interaction.guild_id, "channel_id": interaction.channel.id, "mod_id": interaction.user.id, "time": ts_str()})

@tree.command(name="unlock", description="Unlock this channel")
async def slash_unlock(interaction: discord.Interaction):
    if not mod_check(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    overwrites = interaction.channel.overwrites_for(interaction.guild.default_role)
    overwrites.send_messages = None
    await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrites)
    await interaction.response.send_message("Channel unlocked.")
    add_log("unlock_channel", {"guild_id": interaction.guild_id, "channel_id": interaction.channel.id, "mod_id": interaction.user.id, "time": ts_str()})

# Warns (persistent)
def get_warns(gid: int, uid: int) -> List[Dict[str, Any]]:
    return warns.setdefault(str(gid), {}).setdefault(str(uid), [])

def add_warn(gid: int, uid: int, mod_id: int, reason: str):
    arr = get_warns(gid, uid)
    arr.append({"reason": reason, "mod_id": mod_id, "ts": now_ts()})
    save_json("warns", warns)
    add_log("warn", {"guild_id": gid, "target_id": uid, "mod_id": mod_id, "reason": reason, "time": ts_str()})

@tree.command(name="warn", description="Warn a user")
@app_commands.describe(member="Member to warn", reason="Reason")
async def slash_warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    if not mod_check(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    add_warn(interaction.guild_id, member.id, interaction.user.id, reason)
    await interaction.response.send_message(f"Warned **{member}** for: {reason}")

@tree.command(name="showwarns", description="Show warns of a user")
@app_commands.describe(member="Member to view warns for")
async def slash_showwarns(interaction: discord.Interaction, member: discord.Member):
    arr = get_warns(interaction.guild_id, member.id)
    if not arr:
        return await interaction.response.send_message("No warns.")
    text = "\n".join([f"- {i+1}. {w['reason']} (by <@{w['mod_id']}>, {ts_str(w['ts'])})" for i,w in enumerate(arr)])
    await interaction.response.send_message(text)

@tree.command(name="removewarn", description="Remove a warn by index")
@app_commands.describe(member="Member", index="Warn number (1..n)")
async def slash_removewarn(interaction: discord.Interaction, member: discord.Member, index: int):
    if not mod_check(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    arr = get_warns(interaction.guild_id, member.id)
    if not arr or index<1 or index>len(arr):
        return await interaction.response.send_message("Invalid index.")
    removed = arr.pop(index-1)
    save_json("warns", warns)
    await interaction.response.send_message(f"Removed warn: {removed['reason']}")

# =========================
# ====== ROLE MGMT ========
# =========================

@tree.command(name="giverole", description="(Admin/Pookie) Give a role")
@app_commands.describe(member="Member", role="Role")
async def slash_giverole(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    if not mod_check(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    try:
        await member.add_roles(role, reason=f"by {interaction.user}")
        await interaction.response.send_message(f"Gave {role.mention} to {member.mention}")
        add_log("give_role", {"guild_id": interaction.guild_id, "mod_id": interaction.user.id, "target_id": member.id, "role_id": role.id, "time": ts_str()})
    except Exception as e:
        await interaction.response.send_message(f"Failed: {e}", ephemeral=True)

@tree.command(name="removerole", description="(Admin/Pookie) Remove a role")
@app_commands.describe(member="Member", role="Role")
async def slash_removerole(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    if not mod_check(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    try:
        await member.remove_roles(role, reason=f"by {interaction.user}")
        await interaction.response.send_message(f"Removed {role.mention} from {member.mention}")
        add_log("remove_role", {"guild_id": interaction.guild_id, "mod_id": interaction.user.id, "target_id": member.id, "role_id": role.id, "time": ts_str()})
    except Exception as e:
        await interaction.response.send_message(f"Failed: {e}", ephemeral=True)

@tree.command(name="giverole_temp", description="(Admin/Pookie) Give a role temporarily")
@app_commands.describe(member="Member", role="Role", duration="e.g., 10m / 2h / 4d")
async def slash_giverole_temp(interaction: discord.Interaction, member: discord.Member, role: discord.Role, duration: str):
    if not mod_check(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    seconds = parse_duration(duration)
    if not seconds:
        return await interaction.response.send_message("Use duration like 10m / 2h / 4d.", ephemeral=True)
    try:
        await member.add_roles(role, reason=f"temp by {interaction.user} for {duration}")
        await interaction.response.send_message(f"Gave {role.mention} to {member.mention} for {duration}")
        add_log("give_role_temp", {"guild_id": interaction.guild_id, "mod_id": interaction.user.id, "target_id": member.id, "role_id": role.id, "duration": duration, "time": ts_str()})
    except Exception as e:
        return await interaction.response.send_message(f"Failed: {e}", ephemeral=True)

    async def remove_later():
        await asyncio.sleep(seconds)
        try:
            await member.remove_roles(role, reason=f"temp expired ({duration})")
            add_log("temp_role_expired", {"guild_id": interaction.guild_id, "target_id": member.id, "role_id": role.id, "time": ts_str()})
        except Exception:
            pass

    asyncio.create_task(remove_later())

# =========================
# ======= ADMIN/POOKIE ====
# =========================

@tree.command(name="addadmin", description="(Owner) Add admin by ID")
async def slash_addadmin(interaction: discord.Interaction, user_id: str):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    if not user_id.isdigit():
        return await interaction.response.send_message("Invalid ID.", ephemeral=True)
    uid = int(user_id)
    if uid not in core["admins"]:
        core["admins"].append(uid)
        save_json("core", core)
    await interaction.response.send_message(f"Added admin: <@{uid}>")

@tree.command(name="removeadmin", description="(Owner) Remove admin by ID")
async def slash_removeadmin(interaction: discord.Interaction, user_id: str):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    if not user_id.isdigit():
        return await interaction.response.send_message("Invalid ID.", ephemeral=True)
    uid = int(user_id)
    if uid in core["admins"]:
        core["admins"].remove(uid)
        save_json("core", core)
    await interaction.response.send_message(f"Removed admin: <@{uid}>")

@tree.command(name="showadmins", description="List admins")
async def slash_showadmins(interaction: discord.Interaction):
    ids = core.get("admins", [])
    if not ids:
        return await interaction.response.send_message("No admins set.")
    await interaction.response.send_message("Admins:\n" + "\n".join([f"- <@{i}> (`{i}`)" for i in ids]))

@tree.command(name="addpookie", description="(Owner/Admin) Add pookie by ID")
async def slash_addpookie(interaction: discord.Interaction, user_id: str):
    if not is_owner(interaction.user) and not is_admin(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    if not user_id.isdigit():
        return await interaction.response.send_message("Invalid ID.", ephemeral=True)
    uid = int(user_id)
    if uid not in core["pookies"]:
        core["pookies"].append(uid)
        save_json("core", core)
    await interaction.response.send_message(f"Added pookie: <@{uid}>")

@tree.command(name="removepookie", description="(Owner/Admin) Remove pookie by ID")
async def slash_removepookie(interaction: discord.Interaction, user_id: str):
    if not is_owner(interaction.user) and not is_admin(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    if not user_id.isdigit():
        return await interaction.response.send_message("Invalid ID.", ephemeral=True)
    uid = int(user_id)
    if uid in core["pookies"]:
        core["pookies"].remove(uid)
        save_json("core", core)
    await interaction.response.send_message(f"Removed pookie: <@{uid}>")

@tree.command(name="listpookie", description="List pookies")
async def slash_listpookie(interaction: discord.Interaction):
    ids = core.get("pookies", [])
    if not ids:
        return await interaction.response.send_message("No pookies set.")
    await interaction.response.send_message("Pookies:\n" + "\n".join([f"- <@{i}> (`{i}`)" for i in ids]))

# =========================
# ========= BLACKLIST =====
# =========================

@tree.command(name="blacklist", description="(Owner/Admin) Blacklist a user ID")
async def slash_blacklist(interaction: discord.Interaction, user_id: str):
    if not is_owner(interaction.user) and not is_admin(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    if not user_id.isdigit():
        return await interaction.response.send_message("Invalid ID.", ephemeral=True)
    uid = int(user_id)
    if uid not in core["blacklist"]:
        core["blacklist"].append(uid)
        save_json("core", core)
    await interaction.response.send_message(f"Blacklisted <@{uid}>")

@tree.command(name="unblacklist", description="(Owner/Admin) Unblacklist a user ID")
async def slash_unblacklist(interaction: discord.Interaction, user_id: str):
    if not is_owner(interaction.user) and not is_admin(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    if not user_id.isdigit():
        return await interaction.response.send_message("Invalid ID.", ephemeral=True)
    uid = int(user_id)
    if uid in core["blacklist"]:
        core["blacklist"].remove(uid)
        save_json("core", core)
    await interaction.response.send_message(f"Unblacklisted <@{uid}>")

@tree.command(name="blockword", description="(Owner/Admin) Add a blocked word (exact match)")
async def slash_blockword(interaction: discord.Interaction, word: str):
    if not is_owner(interaction.user) and not is_admin(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    if word.lower() not in [w.lower() for w in core["blocked_words"]]:
        core["blocked_words"].append(word)
        save_json("core", core)
    await interaction.response.send_message(f"Blocked exact word: **{word}**")

@tree.command(name="unblockword", description="(Owner/Admin) Remove a blocked word")
async def slash_unblockword(interaction: discord.Interaction, word: str):
    if not is_owner(interaction.user) and not is_admin(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    core["blocked_words"] = [w for w in core["blocked_words"] if w.lower() != word.lower()]
    save_json("core", core)
    await interaction.response.send_message(f"Unblocked: **{word}**")

@tree.command(name="listblocked", description="List blocked words")
async def slash_listblocked(interaction: discord.Interaction):
    ws = core.get("blocked_words", [])
    if not ws:
        return await interaction.response.send_message("No blocked words.")
    await interaction.response.send_message("Blocked words:\n" + ", ".join(ws))

# =========================
# ========= SNIPES =========
# =========================

class Pager(discord.ui.View):
    def __init__(self, entries: List[Dict[str, Any]]):
        super().__init__(timeout=60)
        self.entries = entries
        self.idx = len(entries)-1 if entries else -1

    def fmt(self) -> discord.Embed:
        if self.idx < 0 or not self.entries:
            return discord.Embed(title="Nothing to show", color=discord.Color.dark_gray())
        e = self.entries[self.idx]
        emb = discord.Embed(title=f"#{self.idx+1}/{len(self.entries)}", color=discord.Color.orange())
        author = e.get("author_name","unknown")
        emb.add_field(name="Author", value=author, inline=True)
        t = e.get("ts", now_ts())
        emb.add_field(name="Time", value=ts_str(t), inline=True)
        content = e.get("content") or f"{e.get('before','')} → {e.get('after','')}"
        emb.add_field(name="Content", value=content[:1024] if content else "(no text)", inline=False)
        avatar = e.get("avatar")
        if avatar:
            emb.set_thumbnail(url=avatar)
        return emb

    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.secondary)
    async def left(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.idx > 0:
            self.idx -= 1
        await interaction.response.edit_message(embed=self.fmt(), view=self)

    @discord.ui.button(label="➡️", style=discord.ButtonStyle.secondary)
    async def right(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.idx < len(self.entries)-1:
            self.idx += 1
        await interaction.response.edit_message(embed=self.fmt(), view=self)

@tree.command(name="snipe", description="Show deleted messages in this channel")
async def slash_snipe(interaction: discord.Interaction):
    gid = str(interaction.guild_id)
    cid = str(interaction.channel_id)
    arr = snipes.get("deleted", {}).get(gid, {}).get(cid, [])
    view = Pager(arr)
    await interaction.response.send_message(embed=view.fmt(), view=view, ephemeral=False)

@tree.command(name="esnipe", description="Show edited messages in this channel")
async def slash_esnipe(interaction: discord.Interaction):
    gid = str(interaction.guild_id)
    cid = str(interaction.channel_id)
    arr = snipes.get("edited", {}).get(gid, {}).get(cid, [])
    view = Pager(arr)
    await interaction.response.send_message(embed=view.fmt(), view=view, ephemeral=False)

# =========================
# ======= SHOWCOMMANDS ====
# =========================

from discord.ui import View, Button

CATEGORIES = {
    "Fun": ["cat","8ball","joke","dadjoke","rps","coinflip","rolldice"],
    "Utility": ["avatar","userinfo","guildinfo","servers","showcommands","logs"],
    "Moderation": ["ban","kick","mute","lock","unlock","warn","showwarns","removewarn","giverole","removerole","giverole_temp"],
    "Config": ["set_log_channel","trigger_add","trigger_remove","trigger_list","setdailycatchannel","sethourlycatchannel","hourlycat_on","hourlycat_off"],
    "Pookie/Admin": ["addpookie","removepookie","listpookie","addadmin","removeadmin","showadmins","blacklist","unblacklist","blockword","unblockword","listblocked"],
    "Messaging": ["say","say_admin","askforcommand"],
    "Maintenance": ["restart","refresh","debug","eval"],
    "Snipes": ["snipe","esnipe"]
}

def visible_commands_for(member: discord.Member) -> Dict[str, List[str]]:
    out = {}
    for cat, cmds in CATEGORIES.items():
        vis = []
        for c in cmds:
            if c in ["addadmin","removeadmin","servers","eval","restart","refresh"] and not is_owner(member):
                continue
            if c in ["addpookie","removepookie","giverole","removerole","giverole_temp","warn","removewarn","mute","lock","unlock","blacklist","unblacklist","blockword","unblockword","set_log_channel","hourlycat_on","hourlycat_off","setdailycatchannel","sethourlycatchannel"] and not mod_check(member):
                continue
            vis.append(c)
        if vis:
            out[cat] = vis
    return out

@tree.command(name="showcommands", description="Interactive commands browser")
async def slash_showcommands(interaction: discord.Interaction):
    cats = visible_commands_for(interaction.user)
    if not cats:
        return await interaction.response.send_message("You cannot use any commands here.", ephemeral=True)

    view = View(timeout=60)
    state = {"cat": None}

    async def update(cat_name: str):
        cmds = cats[cat_name]
        text = "\n".join([f"• `{c}`" for c in cmds])
        content = f"**{cat_name} Commands**\n{text}\n\n*(Tap another category)*"
        await msg.edit(content=content, view=view)

    for cat_name in cats.keys():
        b = Button(label=cat_name, style=discord.ButtonStyle.primary)
        async def cb(inter: discord.Interaction, cat=cat_name):
            await inter.response.defer()
            await update(cat)
        b.callback = cb
        view.add_item(b)

    await interaction.response.send_message("Select a category:", view=view)
    msg = await interaction.original_response()

# Prefix alias
@log_prefix_command("showcommands")
async def showcommands_prefix(ctx: commands.Context):
    inter = type("X", (), {"user": ctx.author})
    return await slash_showcommands.__wrapped__(interaction=inter)

# =========================
# ====== LOG VIEWING ======
# =========================

@tree.command(name="logs", description="Show recent logs (default 10, max 50)")
@app_commands.describe(amount="How many (1-50)")
async def slash_logs(interaction: discord.Interaction, amount: Optional[int] = 10):
    n = max(1, min(50, amount or 10))
    recent = logs.get("entries", [])[-n:]
    lines = []
    for e in recent:
        lines.append(f"- [{e['kind']}] {e['detail'].get('time','')} :: {e['detail']}")
    text = "\n".join(lines) or "No logs."
    if len(text) > 1900:
        text = text[-1900:]
    await interaction.response.send_message(f"**Last {n} logs:**\n{text}")

@log_prefix_command("logs")
async def logs_prefix(ctx: commands.Context, amount: Optional[int] = None):
    amt = 10
    if amount is None:
        # parse from message content tail
        parts = ctx.message.content.strip().split()
        if len(parts)>=2 and parts[-1].isdigit():
            amt = int(parts[-1])
    else:
        amt = amount
    inter = type("X", (), {"response": type("R", (), {"send_message": lambda *_a, **_k: None})})
    return await slash_logs.__wrapped__(interaction=ctx, amount=amt)

# =========================
# ===== DAILY / HOURLY CAT
# =========================

@tree.command(name="setdailycatchannel", description="(Admin/Pookie) Set channel for daily 11:00 IST cat")
async def slash_set_daily_cat(interaction: discord.Interaction, channel: discord.TextChannel):
    if not mod_check(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    core["daily_cat_channel_id"] = int(channel.id)
    save_json("core", core)
    await interaction.response.send_message(f"Daily cat channel set to {channel.mention}")

@tree.command(name="sethourlycatchannel", description="(Admin/Pookie) Set channel for hourly cats")
async def slash_set_hourly_cat(interaction: discord.Interaction, channel: discord.TextChannel):
    if not mod_check(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    core["hourly_cat_channel_id"] = int(channel.id)
    core["hourly_cat_enabled"] = True
    save_json("core", core)
    await interaction.response.send_message(f"Hourly cat channel set to {channel.mention} and enabled.")

@tree.command(name="hourlycat_on", description="(Admin/Pookie) Enable hourly cats")
async def slash_hourly_on(interaction: discord.Interaction):
    if not mod_check(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    core["hourly_cat_enabled"] = True
    save_json("core", core)
    await interaction.response.send_message("Hourly cats enabled.")

@tree.command(name="hourlycat_off", description="(Admin/Pookie) Disable hourly cats")
async def slash_hourly_off(interaction: discord.Interaction):
    if not mod_check(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    core["hourly_cat_enabled"] = False
    save_json("core", core)
    await interaction.response.send_message("Hourly cats disabled.")

@tasks.loop(hours=1)
async def hourly_cat_post():
    if not core.get("hourly_cat_enabled"):
        return
    ch_id = core.get("hourly_cat_channel_id")
    if not ch_id:
        return
    ch = bot.get_channel(int(ch_id))
    if not ch:
        return
    async with aiohttp.ClientSession() as session:
        url = await fetch_cat_url(session)
    if url:
        try:
            await ch.send(url)
            add_log("hourly_cat", {"channel_id": ch.id, "time": ts_str()})
        except Exception:
            pass

@hourly_cat_post.before_loop
async def before_hourly():
    await bot.wait_until_ready()

@tasks.loop(minutes=1)
async def daily_cat_conditional():
    """Fire at 11:00 IST."""
    await bot.wait_until_ready()
    ch_id = core.get("daily_cat_channel_id")
    if not ch_id:
        return
    now = datetime.now(tz=LOCAL_TZ)
    if now.hour == 11 and now.minute == 0:
        ch = bot.get_channel(int(ch_id))
        if ch:
            async with aiohttp.ClientSession() as session:
                url = await fetch_cat_url(session)
            if url:
                try:
                    await ch.send(url)
                    add_log("daily_cat", {"channel_id": ch.id, "time": ts_str()})
                except Exception:
                    pass
        await asyncio.sleep(60)  # avoid multiple sends inside same minute

# =========================
# ====== ASK FOR COMMAND ==
# =========================

@tree.command(name="askforcommand", description="Ask the owner for a new command/request")
@app_commands.describe(request="Describe what you want")
async def slash_askforcommand(interaction: discord.Interaction, request: str):
    owner = bot.get_user(OWNER_ID) or await bot.fetch_user(OWNER_ID)
    # DM owner
    try:
        await owner.send(
            f"**Command Request**\nFrom: {interaction.user} (`{interaction.user.id}`)\n"
            f"Guild: {interaction.guild.name if interaction.guild else 'DM'} (`{interaction.guild_id}`)\n"
            f"Channel: #{interaction.channel.name} (`{interaction.channel_id}`)\n"
            f"Request: {request}"
        )
    except Exception:
        pass
    # Ping owner in log channel if exists
    lc_id = core.get("log_channel_id")
    if lc_id:
        chan = bot.get_channel(int(lc_id))
        if chan:
            try:
                await chan.send(f"<@{OWNER_ID}> New request from **{interaction.user}**: {request}")
            except Exception:
                pass
    add_log("askforcommand", {
        "from_id": interaction.user.id, "guild_id": interaction.guild_id, "channel_id": interaction.channel_id,
        "text": request, "time": ts_str()
    })
    await interaction.response.send_message("Sent your request to the owner. Thanks!")

@log_prefix_command("askforcommand")
async def askforcommand_prefix(ctx: commands.Context, *, request: str):
    inter = type("X", (), {"user": ctx.author, "guild": ctx.guild, "guild_id": getattr(ctx.guild, "id", None), "channel_id": ctx.channel.id, "channel": ctx.channel})
    return await slash_askforcommand.__wrapped__(interaction=inter, request=request)

# =========================
# ===== MAINTENANCE =======
# =========================

@tree.command(name="refresh", description="(Owner) Refresh application commands")
async def slash_refresh(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    try:
        synced = await tree.sync()
        await interaction.response.send_message(f"Synced {len(synced)} commands.")
        add_log("refresh", {"by": interaction.user.id, "count": len(synced), "time": ts_str()})
    except Exception as e:
        await interaction.response.send_message(f"Failed: {e}", ephemeral=True)

@tree.command(name="restart", description="(Owner) Restart the bot (Render will bring it back)")
async def slash_restart(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    await interaction.response.send_message("Restarting…")
    add_log("restart", {"by": interaction.user.id, "time": ts_str()})
    await asyncio.sleep(1)
    os._exit(0)  # Render restarts container

@tree.command(name="debug", description="(Owner) Debug info")
async def slash_debug(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    proc = psutil.Process(os.getpid())
    mem = proc.memory_info().rss / (1024*1024)
    cpu = psutil.cpu_percent(interval=0.2)
    up = human_timedelta(time.time() - LAUNCH_TIME)
    gcount = len(bot.guilds)
    await interaction.response.send_message(
        f"Uptime: {up}\nGuilds: {gcount}\nCPU: {cpu:.1f}%\nRAM: {mem:.1f} MB\nPy: {platform.python_version()} | discord.py: {discord.__version__}"
    )

@tree.command(name="eval", description="(Owner) Evaluate python code")
@app_commands.describe(code="Python code (dangerous!)")
async def slash_eval(interaction: discord.Interaction, code: str):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    env = {"bot": bot, "discord": discord, "asyncio": asyncio, "os": os, "ctx": interaction}
    try:
        result = eval(code, env)
        if asyncio.iscoroutine(result):
            result = await result
        await interaction.response.send_message(f"```py\n{result}\n```")
    except Exception as e:
        await interaction.response.send_message(f"```py\n{e}\n```", ephemeral=True)

# =========================
# ====== PREFIX SHORTS ====
# =========================

@log_prefix_command("ping")
async def ping_prefix(ctx: commands.Context):
    await ctx.reply(f"Pong! {round(bot.latency*1000)} ms")

# =========================
# ===== BOT STARTUP =======
# =========================

bot.remove_command("help")  # ensure no collision

bot.run(TOKEN)
