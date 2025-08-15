# main.py
import os
import json
import random
import asyncio
import threading
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
import discord
from discord.ext import commands, tasks
from discord import app_commands, ui
from flask import Flask

# -------------------- CONFIG --------------------
OWNER_ID = 1319292111325106296
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", None)
CAT_API_KEY = os.getenv("CAT_API_KEY", "")
PORT = int(os.getenv("PORT", 8080))  # Render will provide

# -------------------- FLASK (uptime) --------------------
app = Flask("uptime")

@app.route("/")
def home():
    return "OK"

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

# run flask in background thread
threading.Thread(target=run_flask, daemon=True).start()

# -------------------- DATA STORAGE --------------------
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

def path_for(name: str) -> str:
    return os.path.join(DATA_DIR, f"{name}.json")

def load_json(name: str, default):
    p = path_for(name)
    if not os.path.exists(p):
        with open(p, "w") as f:
            json.dump(default, f)
        return default
    with open(p, "r") as f:
        try:
            return json.load(f)
        except Exception:
            return default

def save_json(name: str, data):
    p = path_for(name)
    with open(p, "w") as f:
        json.dump(data, f, indent=2)

admins = load_json("admins", {})            # { "user_id": true }
pookies = load_json("pookies", {})          # { "user_id": true }
blacklist = load_json("blacklist", {})      # { "user_id": true }
blocked_words = load_json("blocked_words", [])  # ["bad", "worse"]
triggers = load_json("triggers", {})        # { "hello": "hi!" }
logs = load_json("logs", [])                # list of log entries
cfg = load_json("config", {"log_channel_id": None, "cat_channel_id": None, "last_cat_date_ist": None})

# -------------------- BOT SETUP --------------------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="?", intents=intents, help_command=None)
tree = bot.tree

# -------------------- HELPERS --------------------
def is_owner_user(user: discord.abc.User | int) -> bool:
    uid = user.id if hasattr(user, "id") else int(user)
    return uid == OWNER_ID

def is_admin_user(user: discord.abc.User | int) -> bool:
    uid = user.id if hasattr(user, "id") else int(user)
    return is_owner_user(uid) or str(uid) in admins or str(uid) in pookies

def is_pookie_user(user: discord.abc.User | int) -> bool:
    uid = user.id if hasattr(user, "id") else int(user)
    return str(uid) in pookies

def is_blacklisted_user(user: discord.abc.User | int) -> bool:
    uid = user.id if hasattr(user, "id") else int(user)
    return str(uid) in blacklist

def log_add(kind: str, detail: dict):
    entry = {"time": datetime.utcnow().isoformat(), "kind": kind, "detail": detail}
    logs.append(entry)
    save_json("logs", logs)

async def send_log_embed(guild: Optional[discord.Guild], title: str, description: str):
    ch_id = cfg.get("log_channel_id")
    if not guild or not ch_id:
        return
    try:
        ch = guild.get_channel(int(ch_id))
        if not ch:
            return
        embed = discord.Embed(title=title, description=description, color=discord.Color.blurple(), timestamp=datetime.utcnow())
        await ch.send(embed=embed)
    except Exception:
        pass

# -------------------- SNIPE / ESNIPE --------------------
SNIPE_MAX = 10
snipe_cache = {}   # channel_id -> list of dicts
esnipe_cache = {}  # channel_id -> list of dicts

def push_snipe(channel_id: int, payload: dict):
    arr = snipe_cache.get(str(channel_id), [])
    arr.insert(0, payload)
    if len(arr) > SNIPE_MAX:
        arr.pop()
    snipe_cache[str(channel_id)] = arr

def push_esnipe(channel_id: int, payload: dict):
    arr = esnipe_cache.get(str(channel_id), [])
    arr.insert(0, payload)
    if len(arr) > SNIPE_MAX:
        arr.pop()
    esnipe_cache[str(channel_id)] = arr

# -------------------- IST helper --------------------
def now_ist() -> datetime:
    return datetime.utcnow() + timedelta(hours=5, minutes=30)

# -------------------- DAILY CAT TASK --------------------
@tasks.loop(minutes=1)
async def daily_cat_job():
    ch_id = cfg.get("cat_channel_id")
    if not ch_id:
        return
    now = now_ist()
    if now.hour == 11 and now.minute == 0:
        last_date = cfg.get("last_cat_date_ist")
        today = now.strftime("%Y-%m-%d")
        if last_date == today:
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
        ch = bot.get_channel(int(ch_id))
        if ch:
            try:
                await ch.send(cat_url)
            except Exception:
                pass
        cfg["last_cat_date_ist"] = today
        save_json("config", cfg)

# -------------------- NAV VIEW --------------------
class NavView(ui.View):
    def __init__(self, items: list[dict], title: str):
        super().__init__(timeout=120)
        self.items = items
        self.idx = 0
        self.title = title

    def make_embed(self) -> discord.Embed:
        item = self.items[self.idx]
        if self.title == "Snipe":
            desc = f"**Author:** {item.get('author')}\n**Content:** {item.get('content')}\n**Time:** {item.get('time')}"
        else:
            desc = (f"**Author:** {item.get('author')}\n**Before:** {item.get('before')}\n"
                    f"**After:** {item.get('after')}\n**Time:** {item.get('time')}")
        embed = discord.Embed(title=f"{self.title} ({self.idx+1}/{len(self.items)})", description=desc, color=discord.Color.dark_teal())
        return embed

    @ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        if self.idx > 0:
            self.idx -= 1
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def forward(self, interaction: discord.Interaction, button: ui.Button):
        if self.idx < len(self.items)-1:
            self.idx += 1
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

# -------------------- BOT EVENTS --------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is online")
    try:
        await tree.sync()
        print("Slash commands synced")
    except Exception as e:
        print("Slash sync failed:", e)
    if not daily_cat_job.is_running():
        daily_cat_job.start()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Blocked words (mods bypass)
    if not is_admin_user(message.author):
        low = message.content.lower()
        for w in blocked_words:
            if w.lower() in low:
                try:
                    await message.delete()
                except Exception:
                    pass
                await message.channel.send("That word is not allowed here.", delete_after=5)
                log_add("blocked_word", {"user": message.author.id, "word": w, "channel": message.channel.id})
                await send_log_embed(message.guild, "Blocked word", f"{message.author.mention} used blocked word `{w}` in {message.channel.mention}")
                return

    # Auto-responder triggers (simple contains)
    low = message.content.lower()
    for trig, resp in triggers.items():
        if trig.lower() in low:
            try:
                await message.channel.send(resp)
            except Exception:
                pass
            break

    await bot.process_commands(message)

@bot.event
async def on_message_delete(message: discord.Message):
    if message.author and not message.author.bot:
        push_snipe(message.channel.id, {"author": str(message.author), "content": message.content or "[embed/attachment]", "time": datetime.utcnow().isoformat()})
        log_add("delete", {"author": message.author.id, "channel": message.channel.id, "content": message.content})
        await send_log_embed(message.guild, "Message deleted", f"Author: {message.author} in {message.channel.mention}\nContent: {message.content or '[embed/attachment]'}")

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author and not before.author.bot and before.content != after.content:
        push_esnipe(before.channel.id, {"author": str(before.author), "before": before.content or "[embed/attachment]", "after": after.content or "[embed/attachment]", "time": datetime.utcnow().isoformat()})
        log_add("edit", {"author": before.author.id, "channel": before.channel.id})
        await send_log_embed(before.guild, "Message edited", f"Author: {before.author} in {before.channel.mention}\nBefore: {before.content or '[embed/attachment]'}\nAfter: {after.content or '[embed/attachment]'}")

@bot.event
async def on_member_join(member: discord.Member):
    log_add("join", {"user": member.id, "guild": member.guild.id})
    await send_log_embed(member.guild, "Member joined", f"{member.mention} joined.")

@bot.event
async def on_member_remove(member: discord.Member):
    log_add("leave", {"user": member.id, "guild": member.guild.id})
    await send_log_embed(member.guild, "Member left", f"{member} left.")

# -------------------- CORE LOGIC FUNCTIONS (used by both prefix & slash) --------------------
async def do_add_admin(invoker: discord.abc.User, target: discord.User):
    if not is_owner_user(invoker):
        return False, "Only owner can add admins."
    admins[str(target.id)] = True
    save_json("admins", admins)
    log_add("add_admin", {"by": invoker.id, "target": target.id})
    return True, f"{target.mention} added as admin."

async def do_remove_admin(invoker: discord.abc.User, target: discord.User):
    if not is_owner_user(invoker):
        return False, "Only owner can remove admins."
    admins.pop(str(target.id), None)
    save_json("admins", admins)
    log_add("remove_admin", {"by": invoker.id, "target": target.id})
    return True, f"{target.mention} removed from admins."

# -------------------- PREFIX COMMANDS --------------------
# admin/pookie
@bot.command(name="add_admin")
async def cmd_add_admin(ctx: commands.Context, user: discord.User):
    ok, msg = await do_add_admin(ctx.author, user)
    await ctx.send(msg)

@bot.command(name="remove_admin")
async def cmd_remove_admin(ctx: commands.Context, user: discord.User):
    ok, msg = await do_remove_admin(ctx.author, user)
    await ctx.send(msg)

@bot.command(name="list_admins")
async def cmd_list_admins(ctx: commands.Context):
    mentions = [f"<@{uid}>" for uid in admins.keys()]
    if not mentions:
        return await ctx.send("No admins set.")
    await ctx.send("Admins: " + ", ".join(mentions))

@bot.command(name="add_pookie")
async def cmd_add_pookie(ctx: commands.Context, user: discord.User):
    if not is_owner_user(ctx.author):
        return await ctx.send("Only owner.")
    pookies[str(user.id)] = True
    save_json("pookies", pookies)
    log_add("add_pookie", {"by": ctx.author.id, "target": user.id})
    await ctx.send(f"{user.mention} added as pookie.")

@bot.command(name="remove_pookie")
async def cmd_remove_pookie(ctx: commands.Context, user: discord.User):
    if not is_owner_user(ctx.author):
        return await ctx.send("Only owner.")
    pookies.pop(str(user.id), None)
    save_json("pookies", pookies)
    log_add("remove_pookie", {"by": ctx.author.id, "target": user.id})
    await ctx.send(f"{user.mention} removed from pookie.")

@bot.command(name="list_pookie")
async def cmd_list_pookie(ctx: commands.Context):
    mentions = [f"<@{uid}>" for uid in pookies.keys()]
    await ctx.send("Pookies: " + (", ".join(mentions) if mentions else "None"))

# moderation
@bot.command(name="ban")
async def cmd_ban(ctx: commands.Context, user: discord.User, *, reason: str = "No reason"):
    if not is_admin_user(ctx.author):
        return await ctx.send("No permission.")
    try:
        await ctx.guild.ban(user, reason=reason, delete_message_days=0)
        await ctx.send(f"Banned {user.mention}.")
        log_add("ban", {"by": ctx.author.id, "target": user.id, "reason": reason})
        await send_log_embed(ctx.guild, "Ban", f"{user.mention} banned by {ctx.author.mention}\nReason: {reason}")
    except Exception as e:
        await ctx.send(f"Failed: {e}")

@bot.command(name="kick")
async def cmd_kick(ctx: commands.Context, user: discord.User, *, reason: str = "No reason"):
    if not is_admin_user(ctx.author):
        return await ctx.send("No permission.")
    try:
        member = ctx.guild.get_member(user.id)
        if not member:
            return await ctx.send("User not in server.")
        await member.kick(reason=reason)
        await ctx.send(f"Kicked {user.mention}.")
        log_add("kick", {"by": ctx.author.id, "target": user.id, "reason": reason})
        await send_log_embed(ctx.guild, "Kick", f"{user.mention} kicked by {ctx.author.mention}\nReason: {reason}")
    except Exception as e:
        await ctx.send(f"Failed: {e}")

@bot.command(name="blacklist")
async def cmd_blacklist(ctx: commands.Context, user: discord.User):
    if not is_admin_user(ctx.author):
        return await ctx.send("No permission.")
    blacklist[str(user.id)] = True
    save_json("blacklist", blacklist)
    await ctx.send(f"{user.mention} blacklisted.")
    log_add("blacklist", {"by": ctx.author.id, "target": user.id})

@bot.command(name="unblacklist")
async def cmd_unblacklist(ctx: commands.Context, user: discord.User):
    if not is_admin_user(ctx.author):
        return await ctx.send("No permission.")
    blacklist.pop(str(user.id), None)
    save_json("blacklist", blacklist)
    await ctx.send(f"{user.mention} removed from blacklist.")
    log_add("unblacklist", {"by": ctx.author.id, "target": user.id})

# set log/cat channel
@bot.command(name="setlogchannel")
async def cmd_setlogchannel(ctx: commands.Context, channel: discord.TextChannel):
    if not is_admin_user(ctx.author):
        return await ctx.send("No permission.")
    cfg["log_channel_id"] = channel.id
    save_json("config", cfg)
    await ctx.send(f"Log channel set to {channel.mention}.")

@bot.command(name="setcatchannel")
async def cmd_setcatchannel(ctx: commands.Context, channel: discord.TextChannel):
    if not is_admin_user(ctx.author):
        return await ctx.send("No permission.")
    cfg["cat_channel_id"] = channel.id
    save_json("config", cfg)
    await ctx.send(f"Daily cat channel set to {channel.mention}.")

# fun commands
@bot.command(name="cat")
async def cmd_cat(ctx: commands.Context):
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

@bot.command(name="eightball")
async def cmd_8ball(ctx: commands.Context, *, question: str):
    answers = ["Yes","No","Maybe","Definitely","Ask again later","It is certain","Very doubtful"]
    await ctx.send(f"🎱 {random.choice(answers)}")

@bot.command(name="joke")
async def cmd_joke(ctx: commands.Context):
    jokes = ["Why did the dev go broke? Because they used up all their cache.", "I would tell you a UDP joke but you might not get it."]
    await ctx.send(random.choice(jokes))

@bot.command(name="dadjoke")
async def cmd_dadjoke(ctx: commands.Context):
    dads = ["I'm reading a book on anti-gravity. It's impossible to put down!","Why don't scientists trust atoms? Because they make up everything."]
    await ctx.send(random.choice(dads))

@bot.command(name="rps")
async def cmd_rps(ctx: commands.Context, choice: str):
    options = ["rock","paper","scissors"]
    c = choice.lower()
    if c not in options:
        return await ctx.send("Choose rock, paper or scissors.")
    bot_c = random.choice(options)
    if c == bot_c:
        res = "Tie!"
    elif (c=="rock" and bot_c=="scissors") or (c=="paper" and bot_c=="rock") or (c=="scissors" and bot_c=="paper"):
        res = "You win!"
    else:
        res = "You lose!"
    await ctx.send(f"You: **{c}** | Me: **{bot_c}** → {res}")

@bot.command(name="coinflip")
async def cmd_coinflip(ctx: commands.Context):
    await ctx.send(random.choice(["Heads","Tails"]))

@bot.command(name="rolldice")
async def cmd_rolldice(ctx: commands.Context, sides: int = 6):
    if sides < 2 or sides > 1000:
        return await ctx.send("Choose sides between 2 and 1000.")
    await ctx.send(f"🎲 {random.randint(1, sides)}")

# say commands
@bot.command(name="say")
async def cmd_say(ctx: commands.Context, *, text: str):
    for w in blocked_words:
        if w.lower() in text.lower() and not is_admin_user(ctx.author):
            return await ctx.send("Message contains blocked word.")
    if "@everyone" in text or "@here" in text:
        text = text.replace("@everyone","").replace("@here","")
    await ctx.send(text)

@bot.command(name="say_admin")
async def cmd_say_admin(ctx: commands.Context, *, text: str):
    if not is_admin_user(ctx.author):
        return await ctx.send("No permission.")
    await ctx.send(text)

# userinfo / avatar
@bot.command(name="userinfo")
async def cmd_userinfo(ctx: commands.Context, user: discord.User = None):
    user = user or ctx.author
    e = discord.Embed(title=str(user), color=discord.Color.blue())
    e.add_field(name="ID", value=user.id)
    if user.avatar:
        e.set_thumbnail(url=user.avatar.url)
    await ctx.send(embed=e)

@bot.command(name="avatar")
async def cmd_avatar(ctx: commands.Context, user: discord.User = None):
    user = user or ctx.author
    if user.avatar:
        await ctx.send(user.avatar.url)
    else:
        await ctx.send("No avatar.")

# show commands
@bot.command(name="showcommands")
async def cmd_showcommands(ctx: commands.Context):
    all_cmds = [c.name for c in bot.commands if not c.hidden]
    if not is_admin_user(ctx.author):
        hide = {"ban","kick","blacklist","unblacklist","add_admin","remove_admin","add_pookie","remove_pookie","setlogchannel","setcatchannel"}
        visible = [c for c in all_cmds if c not in hide]
    else:
        visible = all_cmds
    await ctx.send("Commands: " + ", ".join(sorted(visible)))

# triggers
@bot.command(name="showtrigger")
async def cmd_showtrigger(ctx: commands.Context):
    if not triggers:
        return await ctx.send("No triggers.")
    await ctx.send("\n".join([f"`{k}` -> {v}" for k,v in triggers.items()]))

@bot.command(name="addtrigger")
async def cmd_addtrigger(ctx: commands.Context, trigger: str, *, response: str):
    if not is_admin_user(ctx.author):
        return await ctx.send("No permission.")
    triggers[trigger] = response
    save_json("triggers", triggers)
    await ctx.send(f"Added trigger `{trigger}`")

@bot.command(name="removetrigger")
async def cmd_removetrigger(ctx: commands.Context, trigger: str):
    if not is_admin_user(ctx.author):
        return await ctx.send("No permission.")
    triggers.pop(trigger, None)
    save_json("triggers", triggers)
    await ctx.send(f"Removed trigger `{trigger}`")

# snipe / esnipe prefix uses NavView
@bot.command(name="snipe")
async def cmd_snipe(ctx: commands.Context):
    items = snipe_cache.get(str(ctx.channel.id), [])
    if not items:
        return await ctx.send("Nothing to snipe.")
    view = NavView(items, "Snipe")
    await ctx.send(embed=view.make_embed(), view=view)

@bot.command(name="esnipe")
async def cmd_esnipe(ctx: commands.Context):
    items = esnipe_cache.get(str(ctx.channel.id), [])
    if not items:
        return await ctx.send("Nothing to e-snipe.")
    view = NavView(items, "E-Snipe")
    await ctx.send(embed=view.make_embed(), view=view)

# -------------------- SLASH COMMANDS (discord.py 2.x) --------------------
# small decorator helpers
def slash_not_blacklisted():
    async def pred(inter: discord.Interaction):
        if is_blacklisted_user(inter.user):
            await inter.response.send_message("You are blacklisted.", ephemeral=True)
            return False
        return True
    return app_commands.check(pred)

def slash_admin_like():
    async def pred(inter: discord.Interaction):
        if not is_admin_user(inter.user):
            await inter.response.send_message("No permission.", ephemeral=True)
            return False
        return True
    return app_commands.check(pred)

@tree.command(name="avatar", description="Get user's avatar")
@slash_not_blacklisted()
async def sc_avatar(inter: discord.Interaction, user: Optional[discord.User] = None):
    user = user or inter.user
    await inter.response.send_message(user.avatar.url if user.avatar else "No avatar.", ephemeral=False)

@tree.command(name="userinfo", description="Get info about a user")
@slash_not_blacklisted()
async def sc_userinfo(inter: discord.Interaction, user: Optional[discord.User] = None):
    user = user or inter.user
    e = discord.Embed(title=str(user), color=discord.Color.blue())
    e.add_field(name="ID", value=user.id)
    if user.avatar:
        e.set_thumbnail(url=user.avatar.url)
    await inter.response.send_message(embed=e)

@tree.command(name="cat", description="Random cat image")
@slash_not_blacklisted()
async def sc_cat(inter: discord.Interaction):
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
@slash_not_blacklisted()
async def sc_coin(inter: discord.Interaction):
    await inter.response.send_message(random.choice(["Heads", "Tails"]))

@tree.command(name="rolldice", description="Roll a dice (2-1000)")
@slash_not_blacklisted()
async def sc_roll(inter: discord.Interaction, sides: int):
    if sides < 2 or sides > 1000:
        return await inter.response.send_message("Sides must be between 2 and 1000.", ephemeral=True)
    await inter.response.send_message(f"🎲 {random.randint(1, sides)}")

# RPS: define choices properly via app_commands.choices
@tree.command(name="rps", description="Rock Paper Scissors")
@slash_not_blacklisted()
@app_commands.describe(choice="rock / paper / scissors")
@app_commands.choices(choice=[
    app_commands.Choice(name="rock", value="rock"),
    app_commands.Choice(name="paper", value="paper"),
    app_commands.Choice(name="scissors", value="scissors"),
])
async def sc_rps(inter: discord.Interaction, choice: app_commands.Choice[str]):
    c = choice.value.lower()
    options = ["rock", "paper", "scissors"]
    bot_c = random.choice(options)
    if c == bot_c:
        res = "Tie!"
    elif (c=="rock" and bot_c=="scissors") or (c=="paper" and bot_c=="rock") or (c=="scissors" and bot_c=="paper"):
        res = "You win!"
    else:
        res = "You lose!"
    await inter.response.send_message(f"You: **{c}** | Me: **{bot_c}** → {res}")

@tree.command(name="eightball", description="Ask the magic 8-ball")
@slash_not_blacklisted()
async def sc_8ball(inter: discord.Interaction, question: str):
    answers = ["Yes","No","Maybe","Definitely","Ask again later","It is certain","Very doubtful"]
    await inter.response.send_message(f"🎱 {random.choice(answers)}")

@tree.command(name="joke", description="Random joke")
@slash_not_blacklisted()
async def sc_joke(inter: discord.Interaction):
    jokes = ["Why did the dev go broke? Because they used all their cache.","I would tell you a UDP joke but you might not get it."]
    await inter.response.send_message(random.choice(jokes))

@tree.command(name="dadjoke", description="Random dad joke")
@slash_not_blacklisted()
async def sc_dadjoke(inter: discord.Interaction):
    dads = ["I'm reading a book on anti-gravity. It's impossible to put down!","I used to play piano by ear; now I use my hands."]
    await inter.response.send_message(random.choice(dads))

# showcommands slash
@tree.command(name="showcommands", description="Show commands you can use")
@slash_not_blacklisted()
async def sc_showcommands(inter: discord.Interaction):
    # simple static list mirrored to prefix visibility
    base = ["avatar","userinfo","cat","coinflip","rolldice","rps","eightball","joke","dadjoke","showtrigger","snipe","esnipe"]
    admin_only = ["addtrigger","removetrigger","setlogchannel","setcatchannel","ban","kick","blacklist","unblacklist","add_admin","remove_admin","add_pookie","remove_pookie","list_pookie","list_admins"]
    if is_admin_user(inter.user):
        allc = base + admin_only
    else:
        allc = base
    await inter.response.send_message("Available: " + ", ".join(sorted(allc)), ephemeral=True)

# triggers (slash)
@tree.command(name="showtrigger", description="Show auto-responder triggers")
@slash_not_blacklisted()
async def sc_showtrigger(inter: discord.Interaction):
    if not triggers:
        return await inter.response.send_message("No triggers set.")
    lines = [f"`{k}` -> {v}" for k,v in triggers.items()]
    await inter.response.send_message("\n".join(lines), ephemeral=True)

@tree.command(name="addtrigger", description="Add trigger -> response")
@slash_admin_like()
async def sc_addtrigger(inter: discord.Interaction, trigger: str, response: str):
    triggers[trigger] = response
    save_json("triggers", triggers)
    await inter.response.send_message(f"Added trigger `{trigger}`")

@tree.command(name="removetrigger", description="Remove a trigger")
@slash_admin_like()
async def sc_removetrigger(inter: discord.Interaction, trigger: str):
    triggers.pop(trigger, None)
    save_json("triggers", triggers)
    await inter.response.send_message(f"Removed trigger `{trigger}`")

# snipe / esnipe slash
@tree.command(name="snipe", description="Show recently deleted messages in this channel")
@slash_not_blacklisted()
async def sc_snipe(inter: discord.Interaction):
    items = snipe_cache.get(str(inter.channel.id), [])
    if not items:
        return await inter.response.send_message("Nothing to snipe.")
    view = NavView(items, "Snipe")
    await inter.response.send_message(embed=view.make_embed(), view=view)

@tree.command(name="esnipe", description="Show recently edited messages in this channel")
@slash_not_blacklisted()
async def sc_esnipe(inter: discord.Interaction):
    items = esnipe_cache.get(str(inter.channel.id), [])
    if not items:
        return await inter.response.send_message("Nothing to e-snipe.")
    view = NavView(items, "E-Snipe")
    await inter.response.send_message(embed=view.make_embed(), view=view)

# admin/pookie management slash
@tree.command(name="add_admin", description="Owner: add an admin")
@slash_admin_like()
async def sc_add_admin(inter: discord.Interaction, user: discord.User):
    if not is_owner_user(inter.user):
        return await inter.response.send_message("Owner only.", ephemeral=True)
    admins[str(user.id)] = True
    save_json("admins", admins)
    await inter.response.send_message(f"Added {user.mention} as admin.")

@tree.command(name="remove_admin", description="Owner: remove an admin")
@slash_admin_like()
async def sc_remove_admin(inter: discord.Interaction, user: discord.User):
    if not is_owner_user(inter.user):
        return await inter.response.send_message("Owner only.", ephemeral=True)
    admins.pop(str(user.id), None)
    save_json("admins", admins)
    await inter.response.send_message(f"Removed {user.mention} from admins.")

@tree.command(name="list_admins", description="List admins")
@slash_not_blacklisted()
async def sc_list_admins(inter: discord.Interaction):
    mentions = [f"<@{uid}>" for uid in admins.keys()]
    await inter.response.send_message("Admins: " + (", ".join(mentions) if mentions else "None"), ephemeral=True)

@tree.command(name="add_pookie", description="Owner: add pookie")
@slash_admin_like()
async def sc_add_pookie(inter: discord.Interaction, user: discord.User):
    if not is_owner_user(inter.user):
        return await inter.response.send_message("Owner only.", ephemeral=True)
    pookies[str(user.id)] = True
    save_json("pookies", pookies)
    await inter.response.send_message(f"Added {user.mention} as pookie.")

@tree.command(name="remove_pookie", description="Owner: remove pookie")
@slash_admin_like()
async def sc_remove_pookie(inter: discord.Interaction, user: discord.User):
    if not is_owner_user(inter.user):
        return await inter.response.send_message("Owner only.", ephemeral=True)
    pookies.pop(str(user.id), None)
    save_json("pookies", pookies)
    await inter.response.send_message(f"Removed {user.mention} from pookie.")

@tree.command(name="list_pookie", description="List pookies")
@slash_not_blacklisted()
async def sc_list_pookie(inter: discord.Interaction):
    mentions = [f"<@{uid}>" for uid in pookies.keys()]
    await inter.response.send_message("Pookies: " + (", ".join(mentions) if mentions else "None"), ephemeral=True)

# moderation slash
@tree.command(name="blacklist_user", description="Blacklist a user")
@slash_admin_like()
async def sc_blacklist(inter: discord.Interaction, user: discord.User):
    blacklist[str(user.id)] = True
    save_json("blacklist", blacklist)
    await inter.response.send_message(f"Blacklisted {user.mention}")

@tree.command(name="unblacklist_user", description="Remove from blacklist")
@slash_admin_like()
async def sc_unblacklist(inter: discord.Interaction, user: discord.User):
    blacklist.pop(str(user.id), None)
    save_json("blacklist", blacklist)
    await inter.response.send_message(f"Unblacklisted {user.mention}")

@tree.command(name="ban", description="Ban a user")
@slash_admin_like()
async def sc_ban(inter: discord.Interaction, user: discord.User, reason: Optional[str] = "No reason"):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    try:
        await inter.guild.ban(user, reason=reason, delete_message_days=0)
        await inter.response.send_message(f"Banned {user.mention}")
        await send_log_embed(inter.guild, "Ban", f"{user.mention} banned by {inter.user.mention}\nReason: {reason}")
    except Exception as e:
        await inter.response.send_message(f"Failed: {e}", ephemeral=True)

@tree.command(name="kick", description="Kick a user")
@slash_admin_like()
async def sc_kick(inter: discord.Interaction, user: discord.User, reason: Optional[str] = "No reason"):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    member = inter.guild.get_member(user.id)
    if not member:
        return await inter.response.send_message("User not in server.", ephemeral=True)
    try:
        await member.kick(reason=reason)
        await inter.response.send_message(f"Kicked {user.mention}")
        await send_log_embed(inter.guild, "Kick", f"{user.mention} kicked by {inter.user.mention}\nReason: {reason}")
    except Exception as e:
        await inter.response.send_message(f"Failed: {e}", ephemeral=True)

@tree.command(name="setlogchannel", description="Set log channel")
@slash_admin_like()
async def sc_setlog(inter: discord.Interaction, channel: discord.TextChannel):
    cfg["log_channel_id"] = channel.id
    save_json("config", cfg)
    await inter.response.send_message(f"Log channel set to {channel.mention}")

@tree.command(name="setcatchannel", description="Set daily cat channel (11:00 IST)")
@slash_admin_like()
async def sc_setcat(inter: discord.Interaction, channel: discord.TextChannel):
    cfg["cat_channel_id"] = channel.id
    save_json("config", cfg)
    await inter.response.send_message(f"Daily cat channel set to {channel.mention}")

# -------------------- RUN --------------------
if not BOT_TOKEN:
    print("ERROR: DISCORD_BOT_TOKEN env var is not set. Set it in Render.")
else:
    bot.run(BOT_TOKEN)
