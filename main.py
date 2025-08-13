import discord
from discord.ext import commands, tasks
from discord import app_commands, ButtonStyle
from discord.ui import Button, View
import json, random, asyncio, os, datetime, aiohttp, threading
from flask import Flask

# -------------------------------
# FLASK KEEP-ALIVE
# -------------------------------
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

threading.Thread(target=run_flask).start()

# -------------------------------
# LOAD ENV & DATA
# -------------------------------
TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
DATA_FILE = "data.json"

if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w") as f:
        json.dump({
            "admins": [],
            "pookie_users": [],
            "pookie_keys": [],
            "blacklist": [],
            "blocked_words": [],
            "logs": [],
            "auto_responders": {},
            "daily_cat_channel": None
        }, f, indent=4)

with open(DATA_FILE) as f:
    data = json.load(f)

def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

# -------------------------------
# BOT SETUP
# -------------------------------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix=['?', '/'], intents=intents)
tree = bot.tree

OWNER_ID = 1319292111325106296  # Replace with your ID

# -------------------------------
# HELPER FUNCTIONS
# -------------------------------
def is_admin(user_id):
    return user_id == OWNER_ID or user_id in data["admins"] or user_id in data["pookie_users"]

def log_command(user, command, channel):
    entry = {
        "user": user.id,
        "name": user.name,
        "command": command,
        "channel": channel.name,
        "time": datetime.datetime.utcnow().isoformat()
    }
    data["logs"].append(entry)
    save_data()

# -------------------------------
# EVENTS
# -------------------------------
snipes = {}  # channel_id: (author, content, time)
esnipes = {}  # channel_id: list of edits

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    if not daily_cat_task.is_running():
        daily_cat_task.start()

@bot.event
async def on_message_delete(message):
    snipes[message.channel.id] = (message.author, message.content, datetime.datetime.utcnow())
    log_command(message.author, f"Deleted message: {message.content}", message.channel)
    await bot.process_commands(message)

@bot.event
async def on_message_edit(before, after):
    if before.content != after.content:
        if after.channel.id not in esnipes:
            esnipes[after.channel.id] = []
        esnipes[after.channel.id].append((before.author, before.content, after.content, datetime.datetime.utcnow()))
        if len(esnipes[after.channel.id]) > 20:
            esnipes[after.channel.id].pop(0)
        log_command(after.author, f"Edited message from '{before.content}' to '{after.content}'", after.channel)
    await bot.process_commands(after)

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    # BLOCKED WORDS CHECK
    msg_lower = ''.join([c.lower() for c in message.content if c.isalnum()])
    for word in data["blocked_words"]:
        word_clean = ''.join([c.lower() for c in word if c.isalnum()])
        if word_clean in msg_lower:
            await message.delete()
            await message.channel.send("This word is not allowed here.", delete_after=5)
            log_command(message.author, f"Used blocked word: {word}", message.channel)
            return
    # AUTO RESPONDER
    for trigger, response in data["auto_responders"].items():
        if trigger.lower() in message.content.lower():
            await message.channel.send(response)
            break
    await bot.process_commands(message)

# -------------------------------
# DAILY CAT POSTING
# -------------------------------
@tasks.loop(hours=24)
async def daily_cat_task():
    channel_id = data.get("daily_cat_channel")
    if channel_id:
        channel = bot.get_channel(channel_id)
        if channel:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://api.thecatapi.com/v1/images/search") as resp:
                    if resp.status == 200:
                        info = await resp.json()
                        await channel.send(info[0]['url'])

# -------------------------------
# FUN COMMANDS
# -------------------------------
@bot.command()
async def flipcoin(ctx):
    result = random.choice(["Heads", "Tails"])
    await ctx.send(result)
    log_command(ctx.author, "flipcoin", ctx.channel)

@bot.command()
async def dadjoke(ctx):
    async with aiohttp.ClientSession() as session:
        async with session.get("https://icanhazdadjoke.com/", headers={"Accept":"application/json"}) as r:
            res = await r.json()
            await ctx.send(res["joke"])
    log_command(ctx.author, "dadjoke", ctx.channel)

@bot.command()
async def rolldice(ctx):
    await ctx.send(f"🎲 You rolled a {random.randint(1,6)}")
    log_command(ctx.author, "rolldice", ctx.channel)

@bot.command()
async def cat(ctx):
    async with aiohttp.ClientSession() as session:
        async with session.get("https://api.thecatapi.com/v1/images/search") as r:
            info = await r.json()
            await ctx.send(info[0]['url'])
    log_command(ctx.author, "cat", ctx.channel)

# -------------------------------
# SAY COMMANDS
# -------------------------------
@bot.command()
async def say(ctx, *, message):
    message = message.replace("@everyone", "").replace("@here", "")
    await ctx.send(message)
    log_command(ctx.author, "say", ctx.channel)

@bot.command()
async def say_admin(ctx, *, message):
    if is_admin(ctx.author.id):
        await ctx.send(message)
        log_command(ctx.author, "say_admin", ctx.channel)
    else:
        await ctx.send("You are not an admin.", delete_after=5)

# -------------------------------
# USER INFO / AVATAR
# -------------------------------
@bot.command()
async def userinfo(ctx, member: discord.Member=None):
    member = member or ctx.author
    embed = discord.Embed(title=f"{member}", color=discord.Color.blue())
    embed.add_field(name="ID", value=member.id)
    embed.add_field(name="Joined", value=member.joined_at)
    embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
    await ctx.send(embed=embed)
    log_command(ctx.author, "userinfo", ctx.channel)

@bot.command()
async def avatar(ctx, member: discord.Member=None):
    member = member or ctx.author
    embed = discord.Embed(title=f"{member}'s Avatar", color=discord.Color.green())
    embed.set_image(url=member.avatar.url if member.avatar else member.default_avatar.url)
    await ctx.send(embed=embed)
    log_command(ctx.author, "avatar", ctx.channel)

# -------------------------------
# SNIPES / E-SNIPES
# -------------------------------
@bot.command()
async def snipe(ctx):
    s = snipes.get(ctx.channel.id)
    if s:
        author, content, time = s
        embed = discord.Embed(title="Last Deleted Message", description=content, color=discord.Color.red())
        embed.set_author(name=str(author))
        embed.set_footer(text=str(time))
        await ctx.send(embed=embed)
        log_command(ctx.author, "snipe", ctx.channel)
    else:
        await ctx.send("No snipes found.")

@bot.command()
async def e_snipe(ctx):
    edits = esnipes.get(ctx.channel.id, [])
    if edits:
        last = edits[-1]
        author, before, after, time = last
        embed = discord.Embed(title="Last Edited Message", color=discord.Color.orange())
        embed.add_field(name="Before", value=before, inline=False)
        embed.add_field(name="After", value=after, inline=False)
        embed.set_author(name=str(author))
        embed.set_footer(text=str(time))
        await ctx.send(embed=embed)
        log_command(ctx.author, "e_snipe", ctx.channel)
    else:
        await ctx.send("No edited messages found.")

# -------------------------------
# ADMIN / POOKIE COMMANDS
# -------------------------------
@bot.command()
async def add_admin(ctx, member: discord.Member):
    if ctx.author.id == OWNER_ID:
        if member.id not in data["admins"]:
            data["admins"].append(member.id)
            save_data()
            await ctx.send(f"{member} added as admin.")
    else:
        await ctx.send("Only owner can add admins.")

@bot.command()
async def remove_admin(ctx, member: discord.Member):
    if ctx.author.id == OWNER_ID:
        if member.id in data["admins"]:
            data["admins"].remove(member.id)
            save_data()
            await ctx.send(f"{member} removed from admins.")
    else:
        await ctx.send("Only owner can remove admins.")

@bot.command()
async def add_pookie(ctx, member: discord.Member):
    if ctx.author.id == OWNER_ID:
        if member.id not in data["pookie_users"]:
            data["pookie_users"].append(member.id)
            save_data()
            await ctx.send(f"{member} added as Pookie user.")
    else:
        await ctx.send("Only owner can add Pookie users.")

@bot.command()
async def remove_pookie(ctx, member: discord.Member):
    if ctx.author.id == OWNER_ID:
        if member.id in data["pookie_users"]:
            data["pookie_users"].remove(member.id)
            save_data()
            await ctx.send(f"{member} removed from Pookie users.")
    else:
        await ctx.send("Only owner can remove Pookie users.")

@bot.command()
async def list_pookie(ctx):
    users = [bot.get_user(uid) for uid in data["pookie_users"]]
    await ctx.send(f"Pookie users: {', '.join([str(u) for u in users if u])}")

# -------------------------------
# MODERATION COMMANDS
# -------------------------------
@bot.command()
async def ban(ctx, member: discord.Member, *, reason=None):
    if is_admin(ctx.author.id):
        await member.ban(reason=reason)
        await ctx.send(f"{member} has been banned.")
        log_command(ctx.author, f"ban {member}", ctx.channel)
    else:
        await ctx.send("You are not allowed to ban users.")

@bot.command()
async def unban(ctx, user_id: int):
    if is_admin(ctx.author.id):
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user)
        await ctx.send(f"{user} has been unbanned.")
        log_command(ctx.author, f"unban {user}", ctx.channel)
    else:
        await ctx.send("You are not allowed to unban users.")

@bot.command()
async def kick(ctx, member: discord.Member, *, reason=None):
    if is_admin(ctx.author.id):
        await member.kick(reason=reason)
        await ctx.send(f"{member} has been kicked.")
        log_command(ctx.author, f"kick {member}", ctx.channel)
    else:
        await ctx.send("You are not allowed to kick users.")

# -------------------------------
# PING / PONG
# -------------------------------
@bot.command()
async def ping(ctx):
    if is_admin(ctx.author.id):
        await ctx.send(f"Pong! {round(bot.latency*1000)}ms")
        log_command(ctx.author, "ping", ctx.channel)

@bot.command()
async def pong(ctx):
    await ctx.send("Pong!")
    log_command(ctx.author, "pong", ctx.channel)

# -------------------------------
# START BOT
# -------------------------------
bot.run(TOKEN)
