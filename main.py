import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import json
import asyncio
from flask import Flask
from threading import Thread
import random
import aiohttp
from dotenv import load_dotenv
import re

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CAT_API_KEY = os.getenv("CAT_API_KEY")  # Optional for cat pics

OWNER_ID = 1319292111325106296

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='?', intents=intents)
tree = bot.tree

# Flask uptime server for Render or Replit
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    Thread(target=run).start()

# JSON files for persistent data
ADMIN_FILE = "admins.json"
BLACKLIST_FILE = "blacklist.json"
BAN_FILE = "bans.json"
LOGS_FILE = "logs.json"
BLOCKED_WORDS_FILE = "blocked_words.json"
POOKIE_KEYS_FILE = "pookie_keys.json"
POOKIE_USERS_FILE = "pookie_users.json"
CAT_CHANNEL_FILE = "cat_channel.json"

def load_json(file, default):
    try:
        with open(file, "r") as f:
            return json.load(f)
    except:
        return default

def save_json(file, data):
    with open(file, "w") as f:
        json.dump(data, f, indent=4)

admins = load_json(ADMIN_FILE, [OWNER_ID])
blacklist = load_json(BLACKLIST_FILE, [])
banned_users = load_json(BAN_FILE, [])
logs = load_json(LOGS_FILE, [])
blocked_words = load_json(BLOCKED_WORDS_FILE, [])
pookie_keys = load_json(POOKIE_KEYS_FILE, [])
pookie_users = load_json(POOKIE_USERS_FILE, [])
cat_channel_data = load_json(CAT_CHANNEL_FILE, {"channel_id": None})

# -- Utilities --

def is_owner(user_id):
    return user_id == OWNER_ID

def is_admin(user_id):
    return is_owner(user_id) or user_id in admins

def is_pookie(user_id):
    return user_id in pookie_users or is_owner(user_id)

def has_full_access(user_id):
    # Pookie users have full access
    return is_pookie(user_id)

def is_blacklisted(user_id):
    return user_id in blacklist

def is_banned(user_id):
    return user_id in banned_users

def log_command(user_id, username, command_name, channel_name):
    logs.append({
        "user_id": user_id,
        "username": username,
        "command": command_name,
        "channel": channel_name,
        "timestamp": discord.utils.utcnow().isoformat()
    })
    if len(logs) > 1000:
        logs.pop(0)
    save_json(LOGS_FILE, logs)

def contains_blocked_word(message_content):
    # Normalize message: lowercase, remove spaces and common symbols
    normalized = re.sub(r"[^a-zA-Z0-9]", "", message_content.lower())
    for word in blocked_words:
        w = word.lower()
        if w in normalized:
            return True
    return False

# -- Decorators --

def admin_only():
    async def predicate(ctx):
        if has_full_access(ctx.author.id) or is_admin(ctx.author.id):
            return True
        else:
            await ctx.send("You need admin or pookie access to use this command.")
            return False
    return commands.check(predicate)

def owner_only():
    async def predicate(ctx):
        if is_owner(ctx.author.id):
            return True
        else:
            await ctx.send("Only the owner can use this command.")
            return False
    return commands.check(predicate)

def pookie_only():
    async def predicate(ctx):
        if is_pookie(ctx.author.id):
            return True
        else:
            await ctx.send("Only pookie users can use this command.")
            return False
    return commands.check(predicate)

# -- Events --

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        await tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print(f"Slash sync error: {e}")
    # Start daily cat task
    daily_cat_task.start()

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if is_blacklisted(message.author.id) or is_banned(message.author.id):
        return

    # Blocked words check
    if contains_blocked_word(message.content):
        try:
            await message.delete()
            await message.channel.send(f"{message.author.mention}, This word is not allowed here.", delete_after=10)
            return
        except Exception:
            pass
    await bot.process_commands(message)

# -- Commands --

# Prefix and slash commands examples:

@bot.command(name="ping")
@admin_only()
async def ping(ctx):
    log_command(ctx.author.id, str(ctx.author), "ping", ctx.channel.name)
    await ctx.send(f"Pong! {round(bot.latency * 1000)}ms")

@tree.command(name="ping", description="Check bot latency")
@app_commands.checks.has_permissions(administrator=True)
async def slash_ping(interaction: discord.Interaction):
    if is_blacklisted(interaction.user.id):
        await interaction.response.send_message("You are blacklisted.", ephemeral=True)
        return
    log_command(interaction.user.id, str(interaction.user), "ping", interaction.channel.name)
    await interaction.response.send_message(f"Pong! {round(bot.latency * 1000)}ms")

@bot.command(name="pong")
async def pong(ctx):
    log_command(ctx.author.id, str(ctx.author), "pong", ctx.channel.name)
    await ctx.send("Ping!")

@tree.command(name="pong", description="Respond with ping")
async def slash_pong(interaction: discord.Interaction):
    if is_blacklisted(interaction.user.id):
        await interaction.response.send_message("You are blacklisted.", ephemeral=True)
        return
    log_command(interaction.user.id, str(interaction.user), "pong", interaction.channel.name)
    await interaction.response.send_message("Ping!")

@bot.command(name="cat")
async def cat(ctx):
    log_command(ctx.author.id, str(ctx.author), "cat", ctx.channel.name)
    headers = {}
    if CAT_API_KEY:
        headers["x-api-key"] = CAT_API_KEY
    url = "https://api.thecatapi.com/v1/images/search"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                await ctx.send(data[0]["url"])
            else:
                await ctx.send("Couldn't fetch a cat image right now.")

@tree.command(name="cat", description="Send a random cat image")
async def slash_cat(interaction: discord.Interaction):
    if is_blacklisted(interaction.user.id):
        await interaction.response.send_message("You are blacklisted.", ephemeral=True)
        return
    log_command(interaction.user.id, str(interaction.user), "cat", interaction.channel.name)
    headers = {}
    if CAT_API_KEY:
        headers["x-api-key"] = CAT_API_KEY
    url = "https://api.thecatapi.com/v1/images/search"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                await interaction.response.send_message(data[0]["url"])
            else:
                await interaction.response.send_message("Couldn't fetch a cat image right now.")

@bot.command(name="8ball")
async def eightball(ctx, *, question: str):
    log_command(ctx.author.id, str(ctx.author), "8ball", ctx.channel.name)
    responses = [
        "It is certain.", "Without a doubt.", "You may rely on it.",
        "Ask again later.", "Better not tell you now.", "My reply is no.",
        "Very doubtful."
    ]
    await ctx.send(f"🎱 Question: {question}\nAnswer: {random.choice(responses)}")

@tree.command(name="8ball", description="Ask the magic 8ball a question")
@app_commands.describe(question="Your question")
async def slash_eightball(interaction: discord.Interaction, question: str):
    log_command(interaction.user.id, str(interaction.user), "8ball", interaction.channel.name)
    responses = [
        "It is certain.", "Without a doubt.", "You may rely on it.",
        "Ask again later.", "Better not tell you now.", "My reply is no.",
        "Very doubtful."
    ]
    await interaction.response.send_message(f"🎱 Question: {question}\nAnswer: {random.choice(responses)}")

@bot.command(name="joke")
async def joke(ctx):
    log_command(ctx.author.id, str(ctx.author), "joke", ctx.channel.name)
    jokes = [
        "Why don't scientists trust atoms? Because they make up everything!",
        "Why did the bicycle fall over? Because it was two-tired!",
        "I told my computer I needed a break, and it said no problem—it would go to sleep.",
        "Why do programmers prefer dark mode? Because light attracts bugs!",
        "What do you call fake spaghetti? An impasta!"
    ]
    await ctx.send(random.choice(jokes))

@tree.command(name="joke", description="Tell a random joke")
async def slash_joke(interaction: discord.Interaction):
    log_command(interaction.user.id, str(interaction.user), "joke", interaction.channel.name)
    jokes = [
        "Why don't scientists trust atoms? Because they make up everything!",
        "Why did the bicycle fall over? Because it was two-tired!",
        "I told my computer I needed a break, and it said no problem—it would go to sleep.",
        "Why do programmers prefer dark mode? Because light attracts bugs!",
        "What do you call fake spaghetti? An impasta!"
    ]
    await interaction.response.send_message(random.choice(jokes))

@bot.command(name="dadjoke")
async def dadjoke(ctx):
    log_command(ctx.author.id, str(ctx.author), "dadjoke", ctx.channel.name)
    jokes = [
        "I'm reading a book about anti-gravity. It's impossible to put down!",
        "Why don't skeletons fight each other? They don't have the guts.",
        "I would avoid the sushi if I was you. It’s a little fishy."
    ]
    await ctx.send(random.choice(jokes))

@tree.command(name="dadjoke", description="Tell a dad joke")
async def slash_dadjoke(interaction: discord.Interaction):
    log_command(interaction.user.id, str(interaction.user), "dadjoke", interaction.channel.name)
    jokes = [
        "I'm reading a book about anti-gravity. It's impossible to put down!",
        "Why don't skeletons fight each other? They don't have the guts.",
        "I would avoid the sushi if I was you. It’s a little fishy."
    ]
    await interaction.response.send_message(random.choice(jokes))

@bot.command(name="roll")
async def roll(ctx, sides: int = 6):
    if sides < 2:
        await ctx.send("You need at least 2 sides to roll a dice!")
        return
    result = random.randint(1, sides)
    log_command(ctx.author.id, str(ctx.author), "roll", ctx.channel.name)
    await ctx.send(f"🎲 You rolled a {result} on a {sides}-sided dice.")

@tree.command(name="roll", description="Roll a dice")
@app_commands.describe(sides="Number of sides")
async def slash_roll(interaction: discord.Interaction, sides: int = 6):
    if sides < 2:
        await interaction.response.send_message("You need at least 2 sides to roll a dice!")
        return
    result = random.randint(1, sides)
    log_command(interaction.user.id, str(interaction.user), "roll", interaction.channel.name)
    await interaction.response.send_message(f"🎲 You rolled a {result} on a {sides}-sided dice.")

@bot.command(name="flip")
async def flip(ctx):
    result = random.choice(["Heads", "Tails"])
    log_command(ctx.author.id, str(ctx.author), "flip", ctx.channel.name)
    await ctx.send(f"🪙 It's {result}!")

@tree.command(name="flip", description="Flip a coin")
async def slash_flip(interaction: discord.Interaction):
    result = random.choice(["Heads", "Tails"])
    log_command(interaction.user.id, str(interaction.user), "flip", interaction.channel.name)
    await interaction.response.send_message(f"🪙 It's {result}!")

@bot.command(name="rps")
async def rps(ctx, choice: str):
    choices = ["rock", "paper", "scissors"]
    user_choice = choice.lower()
    if user_choice not in choices:
        await ctx.send("Please choose rock, paper, or scissors.")
        return
    bot_choice = random.choice(choices)
    if user_choice == bot_choice:
        result = "It's a tie!"
    elif (user_choice == "rock" and bot_choice == "scissors") or \
         (user_choice == "paper" and bot_choice == "rock") or \
         (user_choice == "scissors" and bot_choice == "paper"):
        result = "You win!"
    else:
        result = "You lose!"
    log_command(ctx.author.id, str(ctx.author), "rps", ctx.channel.name)
    await ctx.send(f"You chose **{user_choice}**. I chose **{bot_choice}**. {result}")

@tree.command(name="rps", description="Play rock paper scissors")
@app_commands.describe(choice="Your choice: rock, paper, or scissors")
async def slash_rps(interaction: discord.Interaction, choice: str):
    choices = ["rock", "paper", "scissors"]
    user_choice = choice.lower()
    if user_choice not in choices:
        await interaction.response.send_message("Please choose rock, paper, or scissors.")
        return
    bot_choice = random.choice(choices)
    if user_choice == bot_choice:
        result = "It's a tie!"
    elif (user_choice == "rock" and bot_choice == "scissors") or \
         (user_choice == "paper" and bot_choice == "rock") or \
         (user_choice == "scissors" and bot_choice == "paper"):
        result = "You win!"
    else:
        result = "You lose!"
    log_command(interaction.user.id, str(interaction.user), "rps", interaction.channel.name)
    await interaction.response.send_message(f"You chose **{user_choice}**. I chose **{bot_choice}**. {result}")

# Say commands

@bot.command(name="say")
async def say(ctx, *, text):
    if is_blacklisted(ctx.author.id):
        return
    # Remove mentions for normal say
    text = re.sub(r"<@!?&?\d+>", "[ping blocked]", text)
    log_command(ctx.author.id, str(ctx.author), "say", ctx.channel.name)
    await ctx.send(text)

@bot.command(name="say_admin")
@owner_only()
async def say_admin(ctx, *, text):
    log_command(ctx.author.id, str(ctx.author), "say_admin", ctx.channel.name)
    await ctx.send(text)

# User info and avatar

@bot.command(name="userinfo")
async def userinfo(ctx, user: discord.User = None):
    user = user or ctx.author
    embed = discord.Embed(title=f"User Info - {user}", color=discord.Color.green())
    embed.set_thumbnail(url=user.avatar.url if user.avatar else user.default_avatar.url)
    embed.add_field(name="ID", value=user.id)
    embed.add_field(name="Bot?", value=user.bot)
    embed.add_field(name="Created at", value=user.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"))
    if isinstance(ctx.channel, discord.TextChannel):
        member = ctx.guild.get_member(user.id)
        if member:
            embed.add_field(name="Joined server at", value=member.joined_at.strftime("%Y-%m-%d %H:%M:%S UTC"))
    await ctx.send(embed=embed)

@tree.command(name="userinfo", description="Get info about a user")
@app_commands.describe(user="The user to get info about")
async def slash_userinfo(interaction: discord.Interaction, user: discord.User = None):
    user = user or interaction.user
    embed = discord.Embed(title=f"User Info - {user}", color=discord.Color.green())
    embed.set_thumbnail(url=user.avatar.url if user.avatar else user.default_avatar.url)
    embed.add_field(name="ID", value=user.id)
    embed.add_field(name="Bot?", value=user.bot)
    embed.add_field(name="Created at", value=user.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"))
    guild = interaction.guild
    member = guild.get_member(user.id) if guild else None
    if member:
        embed.add_field(name="Joined server at", value=member.joined_at.strftime("%Y-%m-%d %H:%M:%S UTC"))
    await interaction.response.send_message(embed=embed)

@bot.command(name="avatar")
async def avatar(ctx, user: discord.User = None):
    user = user or ctx.author
    embed = discord.Embed(title=f"{user}'s avatar")
    embed.set_image(url=user.avatar.url if user.avatar else user.default_avatar.url)
    await ctx.send(embed=embed)

@tree.command(name="avatar", description="Get a user's avatar")
@app_commands.describe(user="User to get avatar")
async def slash_avatar(interaction: discord.Interaction, user: discord.User = None):
    user = user or interaction.user
    embed = discord.Embed(title=f"{user}'s avatar")
    embed.set_image(url=user.avatar.url if user.avatar else user.default_avatar.url)
    await interaction.response.send_message(embed=embed)

# Admin only commands

@bot.command(name="blacklist")
@owner_only()
async def blacklist_add(ctx, user: discord.User):
    if user.id in blacklist:
        await ctx.send("User is already blacklisted.")
        return
    blacklist.append(user.id)
    save_json(BLACKLIST_FILE, blacklist)
    await ctx.send(f"User {user} added to blacklist.")

@bot.command(name="unblacklist")
@owner_only()
async def unblacklist(ctx, user: discord.User):
    if user.id not in blacklist:
        await ctx.send("User is not blacklisted.")
        return
    blacklist.remove(user.id)
    save_json(BLACKLIST_FILE, blacklist)
    await ctx.send(f"User {user} removed from blacklist.")

@bot.command(name="ban")
@owner_only()
async def ban(ctx, user: discord.User, *, reason=None):
    if user.id in banned_users:
        await ctx.send("User is already banned.")
        return
    banned_users.append(user.id)
    save_json(BAN_FILE, banned_users)
    await ctx.guild.ban(user, reason=reason)
    await ctx.send(f"{user} has been banned. Reason: {reason}")

@bot.command(name="unban")
@owner_only()
async def unban(ctx, user: discord.User):
    if user.id not in banned_users:
        await ctx.send("User is not banned.")
        return
    banned_users.remove(user.id)
    save_json(BAN_FILE, banned_users)
    await ctx.guild.unban(user)
    await ctx.send(f"{user} has been unbanned.")

@bot.command(name="addadmin")
@owner_only()
async def addadmin(ctx, user: discord.User):
    if user.id in admins:
        await ctx.send("User is already an admin.")
        return
    admins.append(user.id)
    save_json(ADMIN_FILE, admins)
    await ctx.send(f"User {user} added as admin.")

@bot.command(name="removeadmin")
@owner_only()
async def removeadmin(ctx, user: discord.User):
    if user.id not in admins:
        await ctx.send("User is not an admin.")
        return
    admins.remove(user.id)
    save_json(ADMIN_FILE, admins)
    await ctx.send(f"User {user} removed from admins.")

@bot.command(name="showadmins")
@admin_only()
async def showadmins(ctx):
    if not admins:
        await ctx.send("No admins set.")
        return
    mentions = []
    for aid in admins:
        member = ctx.guild.get_member(aid)
        if member:
            mentions.append(str(member))
        else:
            mentions.append(f"User ID: {aid}")
    await ctx.send("Admins:\n" + "\n".join(mentions))

@bot.command(name="addblockedword")
@owner_only()
async def addblockedword(ctx, *, word):
    word = word.lower().strip()
    if word in blocked_words:
        await ctx.send("That word is already blocked.")
        return
    blocked_words.append(word)
    save_json(BLOCKED_WORDS_FILE, blocked_words)
    await ctx.send(f"Blocked word added: {word}")

@bot.command(name="removeblockedword")
@owner_only()
async def removeblockedword(ctx, *, word):
    word = word.lower().strip()
    if word not in blocked_words:
        await ctx.send("That word is not blocked.")
        return
    blocked_words.remove(word)
    save_json(BLOCKED_WORDS_FILE, blocked_words)
    await ctx.send(f"Blocked word removed: {word}")

@bot.command(name="showblockedwords")
@owner_only()
async def showblockedwords(ctx):
    if not blocked_words:
        await ctx.send("No blocked words set.")
        return
    await ctx.send("Blocked words:\n" + ", ".join(blocked_words))

@bot.command(name="logs")
@admin_only()
async def show_logs(ctx, limit: int = 10):
    last_logs = logs[-limit:]
    if not last_logs:
        await ctx.send("No logs available.")
        return
    embed = discord.Embed(title=f"Last {limit} Command Logs", color=discord.Color.blue())
    for log in last_logs:
        embed.add_field(
            name=f"{log['username']} used {log['command']}",
            value=f"Channel: {log['channel']} at {log['timestamp']}",
            inline=False)
    await ctx.send(embed=embed)

# -- Pookie system --

@bot.command(name="createpookiekey")
@owner_only()
async def create_pookie_key(ctx):
    key = ''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=8))
    pookie_keys.append(key)
    save_json(POOKIE_KEYS_FILE, pookie_keys)
    await ctx.send(f"Pookie key created: `{key}` (One-time use)")

@bot.command(name="deletepookiekey")
@owner_only()
async def delete_pookie_key(ctx, key: str):
    key = key.upper()
    if key not in pookie_keys:
        await ctx.send("That key does not exist.")
        return
    pookie_keys.remove(key)
    save_json(POOKIE_KEYS_FILE, pookie_keys)
    await ctx.send(f"Pookie key `{key}` deleted.")

@bot.command(name="listpookiekeys")
@owner_only()
async def list_pookie_keys(ctx):
    if not pookie_keys:
        await ctx.send("No pookie keys exist.")
        return
    await ctx.send("Pookie keys:\n" + ", ".join(pookie_keys))

@bot.command(name="redeempookie")
async def redeem_pookie(ctx, key: str):
    key = key.upper()
    if key not in pookie_keys:
        await ctx.send("Invalid pookie key.")
        return
    if ctx.author.id in pookie_users:
        await ctx.send("You already have pookie access.")
        return
    pookie_users.append(ctx.author.id)
    pookie_keys.remove(key)
    save_json(POOKIE_USERS_FILE, pookie_users)
    save_json(POOKIE_KEYS_FILE, pookie_keys)
    await ctx.send(f"Congrats {ctx.author.mention}, you now have full pookie access!")

@bot.command(name="removepookieuser")
@owner_only()
async def remove_pookie_user(ctx, user: discord.User):
    if user.id not in pookie_users:
        await ctx.send("User is not a pookie user.")
        return
    pookie_users.remove(user.id)
    save_json(POOKIE_USERS_FILE, pookie_users)
    await ctx.send(f"Pookie access removed from {user}.")

@bot.command(name="listpookieusers")
@owner_only()
async def list_pookie_users(ctx):
    if not pookie_users:
        await ctx.send("No pookie users.")
        return
    mentions = []
    for uid in pookie_users:
        member = ctx.guild.get_member(uid)
        if member:
            mentions.append(str(member))
        else:
            mentions.append(f"User ID: {uid}")
    await ctx.send("Pookie users:\n" + "\n".join(mentions))

# Showcommands: list commands visible to user

@tree.command(name="showcommands", description="Show commands you can use")
async def slash_showcommands(interaction: discord.Interaction):
    user = interaction.user
    cmds = []

    # Public commands:
    public_cmds = [
        "/pong", "/cat", "/8ball", "/joke", "/dadjoke", "/roll", "/flip", "/rps",
        "/userinfo", "/avatar", "/redeempookie",
        "?pong", "?cat", "?8ball", "?joke", "?dadjoke", "?roll", "?flip", "?rps",
        "?userinfo", "?avatar", "?redeempookie", "?say"
    ]

    # Admin-only commands
    admin_cmds = [
        "?ping", "?blacklist", "?unblacklist", "?ban", "?unban", "?addadmin", "?removeadmin",
        "?showadmins", "?addblockedword", "?removeblockedword", "?showblockedwords", "?logs",
        "?say_admin",
        "?createpookiekey", "?deletepookiekey", "?listpookiekeys",
        "?removepookieuser", "?listpookieusers"
    ]

    if is_owner(user.id) or is_pookie(user.id):
        cmds.extend(public_cmds + admin_cmds)
    elif is_admin(user.id):
        cmds.extend(public_cmds + admin_cmds)
    else:
        cmds.extend(public_cmds)

    await interaction.response.send_message("Commands you can use:\n" + "\n".join(cmds), ephemeral=True)

# -- Daily cat image posting to specific channel --

@tasks.loop(hours=24)
async def daily_cat_task():
    channel_id = cat_channel_data.get("channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return
    headers = {}
    if CAT_API_KEY:
        headers["x-api-key"] = CAT_API_KEY
    url = "https://api.thecatapi.com/v1/images/search"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                await channel.send(data[0]["url"])

@bot.command(name="setcatchannel")
@owner_only()
async def set_cat_channel(ctx, channel: discord.TextChannel):
    cat_channel_data["channel_id"] = channel.id
    save_json(CAT_CHANNEL_FILE, cat_channel_data)
    await ctx.send(f"Daily cat channel set to {channel.mention}")

@bot.command(name="removecatchannel")
@owner_only()
async def remove_cat_channel(ctx):
    cat_channel_data["channel_id"] = None
    save_json(CAT_CHANNEL_FILE, cat_channel_data)
    await ctx.send("Daily cat channel removed.")

# -- Run bot --

keep_alive()
bot.run(TOKEN)
