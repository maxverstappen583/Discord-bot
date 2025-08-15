# main.py
import os
import json
import random
import asyncio
from datetime import datetime, timedelta
import threading

import aiohttp
import discord
from discord.ext import commands, tasks
from discord import app_commands, ui
from flask import Flask

# -------------------- CONFIG --------------------
OWNER_ID = 1319292111325106296
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "REPLACE_ME_IN_RENDER")
CAT_API_KEY = os.getenv("CAT_API_KEY", "")  # optional
PORT = int(os.getenv("PORT", 8080))         # Render will provide PORT

# -------------------- FLASK (Render keeps service alive) --------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "OK"

def _run_flask():
    app.run(host="0.0.0.0", port=PORT)

# start Flask in background thread
threading.Thread(target=_run_flask, daemon=True).start()

# -------------------- FILE STORAGE --------------------
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

def _path(name: str) -> str:
    return os.path.join(DATA_DIR, name + ".json")

def load_json(name: str, default):
    p = _path(name)
    if not os.path.exists(p):
        with open(p, "w") as f:
            json.dump(default, f)
        return default
    with open(p, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return default

def save_json(name: str, data):
    with open(_path(name), "w") as f:
        json.dump(data, f, indent=2)

admins          = load_json("admins", {})                # {user_id: true}
pookies         = load_json("pookies", {})               # {user_id: true}
blacklist       = load_json("blacklist", {})             # {user_id: true}
blocked_words   = load_json("blocked_words", [])         # ["badword", ...]
triggers        = load_json("triggers", {})              # {"hello":"hi"}
logs            = load_json("logs", [])                  # [ ... entries ... ]
cfg             = load_json("config", {                  # misc config
    "log_channel_id": None,
    "cat_channel_id": None,
    "last_cat_post_ist": None  # "YYYY-MM-DD"
})

# -------------------- BOT SETUP --------------------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="?", intents=intents, help_command=None)
tree = bot.tree

# -------------------- PERMISSION HELPERS --------------------
def is_owner(u: int | discord.abc.User) -> bool:
    uid = u.id if hasattr(u, "id") else int(u)
    return uid == OWNER_ID

def is_admin_user(u: discord.abc.User) -> bool:
    return is_owner(u) or str(u.id) in admins or str(u.id) in pookies

def is_pookie_user(u: discord.abc.User) -> bool:
    return str(u.id) in pookies

def is_blacklisted_user(u: discord.abc.User) -> bool:
    return str(u.id) in blacklist

def can_use_mod(u: discord.abc.User) -> bool:
    return is_owner(u) or str(u.id) in admins or str(u.id) in pookies

# app_commands checks (for slash)
def slash_check_not_blacklisted():
    async def predicate(inter: discord.Interaction):
        if is_blacklisted_user(inter.user):
            await inter.response.send_message("You are blacklisted from using commands.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

def slash_check_admin_like():
    async def predicate(inter: discord.Interaction):
        if not can_use_mod(inter.user):
            await inter.response.send_message("You don't have permission to use this command.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

# -------------------- LOGGING --------------------
def log_entry(kind: str, detail: dict):
    entry = {
        "time": datetime.utcnow().isoformat(),
        "kind": kind,
        "detail": detail
    }
    logs.append(entry)
    save_json("logs", logs)

async def send_log_embed(guild: discord.Guild | None, title: str, description: str):
    channel_id = cfg.get("log_channel_id")
    if not channel_id or not guild:
        return
    ch = guild.get_channel(int(channel_id))
    if not ch:
        return
    embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
    embed.timestamp = datetime.utcnow()
    try:
        await ch.send(embed=embed)
    except Exception:
        pass

# -------------------- SNIPE CACHES --------------------
# per-channel ring buffers (max 10)
SNIPE_MAX = 10
snipe_cache = {}   # channel_id -> list of {author, content, time}
esnipe_cache = {}  # channel_id -> list of {author, before, after, time}

def _push_cache(cache: dict, channel_id: int, payload: dict, maxlen=SNIPE_MAX):
    arr = cache.get(channel_id, [])
    arr.insert(0, payload)
    if len(arr) > maxlen:
        arr.pop()
    cache[channel_id] = arr

# -------------------- EVENTS --------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is online")
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} slash commands")
    except Exception as e:
        print("Slash sync error:", e)
    # start daily cat loop
    daily_cat_tick.start()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # blocked words (mods bypass)
    if not can_use_mod(message.author):
        low = message.content.lower()
        for w in blocked_words:
            if w.lower() in low:
                try:
                    await message.delete()
                except Exception:
                    pass
                await message.channel.send("That word is not allowed here.")
                log_entry("blocked_word", {
                    "user": message.author.id, "word": w, "channel": message.channel.id
                })
                await send_log_embed(message.guild, "Blocked Word",
                                     f"{message.author.mention} used a blocked word in {message.channel.mention}.")
                return

    # auto triggers (simple contains, case-insensitive)
    low = message.content.lower()
    for trig, resp in triggers.items():
        if trig.lower() in low:
            await message.channel.send(resp)
            break

    await bot.process_commands(message)

@bot.event
async def on_message_delete(message: discord.Message):
    if message.author and not message.author.bot:
        _push_cache(snipe_cache, message.channel.id, {
            "author": str(message.author),
            "content": message.content or "[embed/attachment]",
            "time": datetime.utcnow().isoformat()
        })
        log_entry("delete", {"author": message.author.id, "channel": message.channel.id, "content": message.content})
        await send_log_embed(message.guild, "Message Deleted",
                             f"**Author:** {message.author} in {message.channel.mention}\n**Content:** {message.content or '[embed/attachment]'}")

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author and not before.author.bot and before.content != after.content:
        _push_cache(esnipe_cache, before.channel.id, {
            "author": str(before.author),
            "before": before.content or "[embed/attachment]",
            "after":  after.content  or "[embed/attachment]",
            "time": datetime.utcnow().isoformat()
        })
        log_entry("edit", {"author": before.author.id, "channel": before.channel.id})
        await send_log_embed(before.guild, "Message Edited",
                             f"**Author:** {before.author} in {before.channel.mention}\n"
                             f"**Before:** {before.content or '[embed/attachment]'}\n"
                             f"**After:** {after.content or '[embed/attachment]'}")

@bot.event
async def on_member_join(member: discord.Member):
    log_entry("join", {"user": member.id, "guild": member.guild.id})
    await send_log_embed(member.guild, "Member Joined", f"{member.mention} joined.")

@bot.event
async def on_member_remove(member: discord.Member):
    log_entry("leave", {"user": member.id, "guild": member.guild.id})
    await send_log_embed(member.guild, "Member Left", f"{member} left.")

# -------------------- UTILITIES --------------------
def ist_now():
    # IST = UTC + 5:30 (no DST)
    return datetime.utcnow() + timedelta(hours=5, minutes=30)

# -------------------- DAILY CAT (11:00 IST) --------------------
@tasks.loop(minutes=1)
async def daily_cat_tick():
    ch_id = cfg.get("cat_channel_id")
    if not ch_id:
        return
    now_ist = ist_now()
    # fire exactly 11:00 IST, once/day
    if now_ist.hour == 11 and now_ist.minute == 0:
        last = cfg.get("last_cat_post_ist")  # "YYYY-MM-DD"
        today = now_ist.strftime("%Y-%m-%d")
        if last == today:
            return
        channel = bot.get_channel(int(ch_id))
        if not channel:
            return
        # fetch cat
        url = "https://api.thecatapi.com/v1/images/search"
        headers = {"x-api-key": CAT_API_KEY} if CAT_API_KEY else {}
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, headers=headers) as resp:
                    data = await resp.json()
                    cat_url = (data[0]["url"] if data else None) or "https://cataas.com/cat"
        except Exception:
            cat_url = "https://cataas.com/cat"
        await channel.send(cat_url)
        cfg["last_cat_post_ist"] = today
        save_json("config", cfg)

# -------------------- VIEWS (Snipe navigation) --------------------
class NavView(ui.View):
    def __init__(self, items: list[dict], title: str):
        super().__init__(timeout=60)
        self.items = items
        self.idx = 0
        self.title = title

    def make_embed(self):
        item = self.items[self.idx]
        if self.title == "Snipe":
            desc = f"**Author:** {item['author']}\n**Content:** {item['content']}\n**Time:** {item['time']}"
        else:
            desc = (f"**Author:** {item['author']}\n"
                    f"**Before:** {item['before']}\n"
                    f"**After:** {item['after']}\n"
                    f"**Time:** {item['time']}")
        e = discord.Embed(title=f"{self.title} ({self.idx+1}/{len(self.items)})",
                          description=desc, color=discord.Color.dark_teal())
        return e

    @ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        if self.idx > 0:
            self.idx -= 1
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def fwd(self, interaction: discord.Interaction, button: ui.Button):
        if self.idx < len(self.items) - 1:
            self.idx += 1
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

# -------------------- PREFIX COMMANDS --------------------
# Admin & Pookie management (owner only for add/remove)
@bot.command()
async def add_admin(ctx: commands.Context, user: discord.User):
    if not is_owner(ctx.author):
        return await ctx.send("Only the owner can do that.")
    admins[str(user.id)] = True
    save_json("admins", admins)
    await ctx.send(f"{user.mention} added as admin.")

@bot.command()
async def remove_admin(ctx: commands.Context, user: discord.User):
    if not is_owner(ctx.author):
        return await ctx.send("Only the owner can do that.")
    admins.pop(str(user.id), None)
    save_json("admins", admins)
    await ctx.send(f"{user.mention} removed from admin.")

@bot.command()
async def list_admins(ctx: commands.Context):
    mentions = [f"<@{uid}>" for uid in admins.keys()]
    if not mentions:
        return await ctx.send("No admins set.")
    await ctx.send("Admins: " + ", ".join(mentions))

@bot.command()
async def add_pookie(ctx: commands.Context, user: discord.User):
    if not is_owner(ctx.author):
        return await ctx.send("Only the owner can do that.")
    pookies[str(user.id)] = True
    save_json("pookies", pookies)
    await ctx.send(f"{user.mention} added as pookie.")

@bot.command()
async def remove_pookie(ctx: commands.Context, user: discord.User):
    if not is_owner(ctx.author):
        return await ctx.send("Only the owner can do that.")
    pookies.pop(str(user.id), None)
    save_json("pookies", pookies)
    await ctx.send(f"{user.mention} removed from pookie.")

@bot.command()
async def list_pookie(ctx: commands.Context):
    mentions = [f"<@{uid}>" for uid in pookies.keys()]
    await ctx.send("Pookies: " + (", ".join(mentions) if mentions else "None"))

# moderation
@bot.command()
async def ban(ctx: commands.Context, user: discord.User, *, reason: str = "No reason"):
    if not can_use_mod(ctx.author):
        return await ctx.send("No permission.")
    try:
        await ctx.guild.ban(user, reason=reason, delete_message_days=0)
        await ctx.send(f"Banned {user.mention}.")
        await send_log_embed(ctx.guild, "Ban", f"{user.mention} banned by {ctx.author.mention}\nReason: {reason}")
    except Exception as e:
        await ctx.send(f"Failed: {e}")

@bot.command()
async def kick(ctx: commands.Context, user: discord.User, *, reason: str = "No reason"):
    if not can_use_mod(ctx.author):
        return await ctx.send("No permission.")
    try:
        await ctx.guild.kick(user, reason=reason)
        await ctx.send(f"Kicked {user.mention}.")
        await send_log_embed(ctx.guild, "Kick", f"{user.mention} kicked by {ctx.author.mention}\nReason: {reason}")
    except Exception as e:
        await ctx.send(f"Failed: {e}")

@bot.command()
async def blacklist_user(ctx: commands.Context, user: discord.User):
    if not can_use_mod(ctx.author):
        return await ctx.send("No permission.")
    blacklist[str(user.id)] = True
    save_json("blacklist", blacklist)
    await ctx.send(f"{user.mention} has been blacklisted.")

@bot.command()
async def unblacklist_user(ctx: commands.Context, user: discord.User):
    if not can_use_mod(ctx.author):
        return await ctx.send("No permission.")
    blacklist.pop(str(user.id), None)
    save_json("blacklist", blacklist)
    await ctx.send(f"{user.mention} removed from blacklist.")

# config: log channel & cat channel
@bot.command()
async def setlogchannel(ctx: commands.Context, channel: discord.TextChannel):
    if not can_use_mod(ctx.author):
        return await ctx.send("No permission.")
    cfg["log_channel_id"] = channel.id
    save_json("config", cfg)
    await ctx.send(f"Log channel set to {channel.mention}")

@bot.command()
async def setcatchannel(ctx: commands.Context, channel: discord.TextChannel):
    if not can_use_mod(ctx.author):
        return await ctx.send("No permission.")
    cfg["cat_channel_id"] = channel.id
    save_json("config", cfg)
    await ctx.send(f"Daily cat channel set to {channel.mention}")

# fun
@bot.command()
async def cat(ctx: commands.Context):
    url = "https://api.thecatapi.com/v1/images/search"
    headers = {"x-api-key": CAT_API_KEY} if CAT_API_KEY else {}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, headers=headers) as resp:
                data = await resp.json()
                cat_url = (data[0]["url"] if data else None) or "https://cataas.com/cat"
    except Exception:
        cat_url = "https://cataas.com/cat"
    await ctx.send(cat_url)

@bot.command()
async def eightball(ctx: commands.Context, *, question: str):
    answers = [
        "Yes", "No", "Maybe", "Definitely", "Ask again later",
        "It is certain", "Very doubtful", "Without a doubt", "Not a chance"
    ]
    await ctx.send(f"🎱 {random.choice(answers)}")

@bot.command()
async def joke(ctx: commands.Context):
    jokes = [
        "Why did the developer go broke? Because they used up all their cache.",
        "I would tell you a UDP joke… but you might not get it.",
        "There are 10 types of people: those who understand binary and those who don't."
    ]
    await ctx.send(random.choice(jokes))

@bot.command()
async def dadjoke(ctx: commands.Context):
    dads = [
        "I'm reading a book about anti-gravity. It's impossible to put down!",
        "I used to play piano by ear, now I use my hands.",
        "I would avoid the sushi if I were you. It’s a little fishy."
    ]
    await ctx.send(random.choice(dads))

@bot.command()
async def rps(ctx: commands.Context, choice: str):
    options = ["rock", "paper", "scissors"]
    c = choice.lower()
    if c not in options:
        return await ctx.send("Choose rock, paper, or scissors.")
    bot_c = random.choice(options)
    if c == bot_c:
        result = "Tie!"
    elif (c == "rock" and bot_c == "scissors") or (c == "paper" and bot_c == "rock") or (c == "scissors" and bot_c == "paper"):
        result = "You win!"
    else:
        result = "You lose!"
    await ctx.send(f"You: **{c}** | Me: **{bot_c}** → {result}")

@bot.command()
async def coinflip(ctx: commands.Context):
    await ctx.send(random.choice(["Heads", "Tails"]))

@bot.command()
async def rolldice(ctx: commands.Context, sides: int = 6):
    if sides < 2 or sides > 1000:
        return await ctx.send("Choose sides between 2 and 1000.")
    await ctx.send(f"🎲 {random.randint(1, sides)}")

# say / say_admin
@bot.command()
async def say(ctx: commands.Context, *, text: str):
    if "@everyone" in text or "@here" in text:
        return await ctx.send("No mass mentions.")
    await ctx.send(text)

@bot.command()
async def say_admin(ctx: commands.Context, *, text: str):
    if not can_use_mod(ctx.author):
        return await ctx.send("No permission.")
    await ctx.send(text)

# user info & avatar
@bot.command()
async def userinfo(ctx: commands.Context, user: discord.User | None = None):
    user = user or ctx.author
    e = discord.Embed(title=f"{user}", color=discord.Color.blue())
    e.add_field(name="ID", value=user.id)
    e.set_thumbnail(url=user.avatar.url if user.avatar else discord.Embed.Empty)
    await ctx.send(embed=e)

@bot.command()
async def avatar(ctx: commands.Context, user: discord.User | None = None):
    user = user or ctx.author
    if user.avatar:
        await ctx.send(user.avatar.url)
    else:
        await ctx.send("No avatar.")

# show commands (prefix)
@bot.command()
async def showcommands(ctx: commands.Context):
    # visible prefix commands
    all_cmds = [c.name for c in bot.commands if not c.hidden]
    if not can_use_mod(ctx.author):
        hide = {"ban","kick","blacklist_user","unblacklist_user","add_admin","remove_admin","add_pookie","remove_pookie","setlogchannel","setcatchannel"}
        cmds = [c for c in all_cmds if c not in hide]
    else:
        cmds = all_cmds
    await ctx.send("Commands: " + ", ".join(sorted(cmds)))

# triggers (prefix)
@bot.command()
async def showtrigger(ctx: commands.Context):
    if not triggers:
        return await ctx.send("No triggers set.")
    lines = [f"`{k}` → {v}" for k, v in triggers.items()]
    await ctx.send("\n".join(lines))

@bot.command()
async def addtrigger(ctx: commands.Context, trigger: str, *, response: str):
    if not can_use_mod(ctx.author):
        return await ctx.send("No permission.")
    triggers[trigger] = response
    save_json("triggers", triggers)
    await ctx.send(f"Added trigger `{trigger}`.")

@bot.command()
async def removetrigger(ctx: commands.Context, trigger: str):
    if not can_use_mod(ctx.author):
        return await ctx.send("No permission.")
    triggers.pop(trigger, None)
    save_json("triggers", triggers)
    await ctx.send(f"Removed trigger `{trigger}`.")

# snipe/esnipe (prefix)
@bot.command()
async def snipe(ctx: commands.Context):
    items = snipe_cache.get(ctx.channel.id, [])
    if not items:
        return await ctx.send("Nothing to snipe.")
    view = NavView(items, "Snipe")
    await ctx.send(embed=view.make_embed(), view=view)

@bot.command()
async def esnipe(ctx: commands.Context):
    items = esnipe_cache.get(ctx.channel.id, [])
    if not items:
        return await ctx.send("Nothing to e-snipe.")
    view = NavView(items, "E-Snipe")
    await ctx.send(embed=view.make_embed(), view=view)

# -------------------- SLASH COMMANDS --------------------
@tree.command(name="showcommands", description="Show the commands you can use")
@slash_check_not_blacklisted()
async def slash_showcommands(inter: discord.Interaction):
    all_slash = [
        "showcommands","avatar","userinfo","cat","coinflip","rolldice","rps",
        "eightball","joke","dadjoke","snipe","esnipe","showtrigger"
    ]
    admin_slash = ["addtrigger","removetrigger","setlogchannel","setcatchannel","ban","kick",
                   "blacklist_user","unblacklist_user","add_admin","remove_admin","add_pookie","remove_pookie","list_pookie","list_admins"]
    if can_use_mod(inter.user):
        visible = all_slash + admin_slash
    else:
        visible = all_slash
    await inter.response.send_message("Slash Commands: " + ", ".join(sorted(visible)), ephemeral=True)

@tree.command(name="avatar", description="Get a user's avatar")
@slash_check_not_blacklisted()
async def slash_avatar(inter: discord.Interaction, user: discord.User | None = None):
    user = user or inter.user
    await inter.response.send_message(user.avatar.url if user.avatar else "No avatar.", ephemeral=False)

@tree.command(name="userinfo", description="Get basic info about a user")
@slash_check_not_blacklisted()
async def slash_userinfo(inter: discord.Interaction, user: discord.User | None = None):
    user = user or inter.user
    e = discord.Embed(title=f"{user}", color=discord.Color.blue())
    e.add_field(name="ID", value=user.id)
    e.set_thumbnail(url=user.avatar.url if user.avatar else discord.Embed.Empty)
    await inter.response.send_message(embed=e)

@tree.command(name="cat", description="Random cat image")
@slash_check_not_blacklisted()
async def slash_cat(inter: discord.Interaction):
    url = "https://api.thecatapi.com/v1/images/search"
    headers = {"x-api-key": CAT_API_KEY} if CAT_API_KEY else {}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, headers=headers) as resp:
                data = await resp.json()
                cat_url = (data[0]["url"] if data else None) or "https://cataas.com/cat"
    except Exception:
        cat_url = "https://cataas.com/cat"
    await inter.response.send_message(cat_url)

@tree.command(name="coinflip", description="Flip a coin")
@slash_check_not_blacklisted()
async def slash_coin(inter: discord.Interaction):
    await inter.response.send_message(random.choice(["Heads","Tails"]))

@tree.command(name="rolldice", description="Roll a dice with N sides")
@slash_check_not_blacklisted()
async def slash_roll(inter: discord.Interaction, sides: app_commands.Range[int, 2, 1000]):
    await inter.response.send_message(f"🎲 {random.randint(1, sides)}")

@tree.command(name="rps", description="Rock, Paper, Scissors")
@slash_check_not_blacklisted()
async def slash_rps(inter: discord.Interaction, choice: app_commands.Choice[str]):
    c = choice.value.lower()
    options = ["rock","paper","scissors"]
    bot_c = random.choice(options)
    if c == bot_c:
        result = "Tie!"
    elif (c == "rock" and bot_c == "scissors") or (c == "paper" and bot_c == "rock") or (c == "scissors" and bot_c == "paper"):
        result = "You win!"
    else:
        result = "You lose!"
    await inter.response.send_message(f"You: **{c}** | Me: **{bot_c}** → {result}")

# fix choices for rps parameter
slash_rps.parameters[0].choices = [
    app_commands.Choice(name="rock", value="rock"),
    app_commands.Choice(name="paper", value="paper"),
    app_commands.Choice(name="scissors", value="scissors")
]

@tree.command(name="eightball", description="Ask the magic 8-ball")
@slash_check_not_blacklisted()
async def slash_eightball(inter: discord.Interaction, question: str):
    answers = [
        "Yes","No","Maybe","Definitely","Ask again later",
        "It is certain","Very doubtful","Without a doubt","Not a chance"
    ]
    await inter.response.send_message(f"🎱 {random.choice(answers)}")

@tree.command(name="joke", description="Random joke")
@slash_check_not_blacklisted()
async def slash_joke(inter: discord.Interaction):
    jokes = [
        "Why did the developer go broke? Because they used up all their cache.",
        "I would tell you a UDP joke… but you might not get it.",
        "There are 10 types of people: those who understand binary and those who don't."
    ]
    await inter.response.send_message(random.choice(jokes))

@tree.command(name="dadjoke", description="Random dad joke")
@slash_check_not_blacklisted()
async def slash_dadjoke(inter: discord.Interaction):
    dads = [
        "I'm reading a book about anti-gravity. It's impossible to put down!",
        "I used to play piano by ear, now I use my hands.",
        "I would avoid the sushi if I were you. It’s a little fishy."
    ]
    await inter.response.send_message(random.choice(dads))

# snipe / esnipe (slash)
@tree.command(name="snipe", description="Show recently deleted messages in this channel")
@slash_check_not_blacklisted()
async def slash_snipe(inter: discord.Interaction):
    items = snipe_cache.get(inter.channel.id, [])
    if not items:
        return await inter.response.send_message("Nothing to snipe.")
    view = NavView(items, "Snipe")
    await inter.response.send_message(embed=view.make_embed(), view=view)

@tree.command(name="esnipe", description="Show recent edits in this channel")
@slash_check_not_blacklisted()
async def slash_esnipe(inter: discord.Interaction):
    items = esnipe_cache.get(inter.channel.id, [])
    if not items:
        return await inter.response.send_message("Nothing to e-snipe.")
    view = NavView(items, "E-Snipe")
    await inter.response.send_message(embed=view.make_embed(), view=view)

# triggers (slash)
@tree.command(name="showtrigger", description="Show all auto-responder triggers")
@slash_check_not_blacklisted()
async def slash_showtrigger(inter: discord.Interaction):
    if not triggers:
        return await inter.response.send_message("No triggers set.")
    lines = [f"`{k}` → {v}" for k, v in triggers.items()]
    await inter.response.send_message("\n".join(lines))

@tree.command(name="addtrigger", description="Add a trigger → response")
@slash_check_admin_like()
async def slash_addtrigger(inter: discord.Interaction, trigger: str, response: str):
    triggers[trigger] = response
    save_json("triggers", triggers)
    await inter.response.send_message(f"Added trigger `{trigger}`.")

@tree.command(name="removetrigger", description="Remove a trigger")
@slash_check_admin_like()
async def slash_removetrigger(inter: discord.Interaction, trigger: str):
    triggers.pop(trigger, None)
    save_json("triggers", triggers)
    await inter.response.send_message(f"Removed trigger `{trigger}`.")

# admin/pookie + moderation (slash)
@tree.command(name="add_admin", description="Owner: add an admin")
@slash_check_admin_like()
async def slash_add_admin(inter: discord.Interaction, user: discord.User):
    if not is_owner(inter.user):
        return await inter.response.send_message("Owner only.", ephemeral=True)
    admins[str(user.id)] = True
    save_json("admins", admins)
    await inter.response.send_message(f"Added {user.mention} as admin.")

@tree.command(name="remove_admin", description="Owner: remove an admin")
@slash_check_admin_like()
async def slash_remove_admin(inter: discord.Interaction, user: discord.User):
    if not is_owner(inter.user):
        return await inter.response.send_message("Owner only.", ephemeral=True)
    admins.pop(str(user.id), None)
    save_json("admins", admins)
    await inter.response.send_message(f"Removed {user.mention} from admin.")

@tree.command(name="list_admins", description="List admins")
@slash_check_not_blacklisted()
async def slash_list_admins(inter: discord.Interaction):
    mentions = [f"<@{uid}>" for uid in admins.keys()]
    await inter.response.send_message("Admins: " + (", ".join(mentions) if mentions else "None"))

@tree.command(name="add_pookie", description="Owner: add a pookie")
@slash_check_admin_like()
async def slash_add_pookie(inter: discord.Interaction, user: discord.User):
    if not is_owner(inter.user):
        return await inter.response.send_message("Owner only.", ephemeral=True)
    pookies[str(user.id)] = True
    save_json("pookies", pookies)
    await inter.response.send_message(f"Added {user.mention} as pookie.")

@tree.command(name="remove_pookie", description="Owner: remove a pookie")
@slash_check_admin_like()
async def slash_remove_pookie(inter: discord.Interaction, user: discord.User):
    if not is_owner(inter.user):
        return await inter.response.send_message("Owner only.", ephemeral=True)
    pookies.pop(str(user.id), None)
    save_json("pookies", pookies)
    await inter.response.send_message(f"Removed {user.mention} from pookie.")

@tree.command(name="list_pookie", description="List pookies")
@slash_check_not_blacklisted()
async def slash_list_pookie(inter: discord.Interaction):
    mentions = [f"<@{uid}>" for uid in pookies.keys()]
    await inter.response.send_message("Pookies: " + (", ".join(mentions) if mentions else "None"))

@tree.command(name="blacklist_user", description="Blacklist a user")
@slash_check_admin_like()
async def slash_blacklist(inter: discord.Interaction, user: discord.User):
    blacklist[str(user.id)] = True
    save_json("blacklist", blacklist)
    await inter.response.send_message(f"Blacklisted {user.mention}.")

@tree.command(name="unblacklist_user", description="Remove a user from blacklist")
@slash_check_admin_like()
async def slash_unblacklist(inter: discord.Interaction, user: discord.User):
    blacklist.pop(str(user.id), None)
    save_json("blacklist", blacklist)
    await inter.response.send_message(f"Unblacklisted {user.mention}.")

@tree.command(name="ban", description="Ban a user")
@slash_check_admin_like()
async def slash_ban(inter: discord.Interaction, user: discord.User, reason: str = "No reason"):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    try:
        await inter.guild.ban(user, reason=reason, delete_message_days=0)
        await inter.response.send_message(f"Banned {user.mention}.")
        await send_log_embed(inter.guild, "Ban", f"{user.mention} banned by {inter.user.mention}\nReason: {reason}")
    except Exception as e:
        await inter.response.send_message(f"Failed: {e}", ephemeral=True)

@tree.command(name="kick", description="Kick a user")
@slash_check_admin_like()
async def slash_kick(inter: discord.Interaction, user: discord.User, reason: str = "No reason"):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    try:
        member = inter.guild.get_member(user.id)
        if not member:
            return await inter.response.send_message("User not in server.", ephemeral=True)
        await member.kick(reason=reason)
        await inter.response.send_message(f"Kicked {user.mention}.")
        await send_log_embed(inter.guild, "Kick", f"{user.mention} kicked by {inter.user.mention}\nReason: {reason}")
    except Exception as e:
        await inter.response.send_message(f"Failed: {e}", ephemeral=True)

@tree.command(name="setlogchannel", description="Set the logs channel")
@slash_check_admin_like()
async def slash_setlog(inter: discord.Interaction, channel: discord.TextChannel):
    cfg["log_channel_id"] = channel.id
    save_json("config", cfg)
    await inter.response.send_message(f"Log channel set to {channel.mention}")

@tree.command(name="setcatchannel", description="Set the daily cat channel (11:00 IST)")
@slash_check_admin_like()
async def slash_setcat(inter: discord.Interaction, channel: discord.TextChannel):
    cfg["cat_channel_id"] = channel.id
    save_json("config", cfg)
    await inter.response.send_message(f"Daily cat channel set to {channel.mention}")

# -------------------- RUN --------------------
if BOT_TOKEN == "REPLACE_ME_IN_RENDER":
    print("WARNING: DISCORD_BOT_TOKEN not set. Set it in Render → Environment.")
bot.run(BOT_TOKEN)
