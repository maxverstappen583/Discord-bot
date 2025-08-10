import os
import re
import discord
from discord.ext import commands, tasks
from discord import app_commands
import random
import aiohttp
from flask import Flask
import threading
import json
from datetime import datetime

# --- Flask uptime server ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_webserver():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = threading.Thread(target=run_webserver)
    t.start()

# --- JSON helpers ---
def load_json(filename):
    try:
        with open(filename, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_json(filename, data):
    with open(filename, 'w') as f:
        json.dump(data, f, indent=4)

# --- Persistent storage ---
admins = load_json('admins.json')
blacklist = load_json('blacklist.json')
blocked_words = load_json('blocked_words.json')
pookie_users = load_json('pookie_users.json')
pookie_keys = load_json('pookie_keys.json')
logs = load_json('logs.json')

OWNER_ID = 1319292111325106296

# Make sure owner is admin
if str(OWNER_ID) not in admins:
    admins[str(OWNER_ID)] = True
    save_json('admins.json', admins)

# --- Intents and bot ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='?', intents=intents)

# --- Utility functions ---
def is_admin(user: discord.User):
    return admins.get(str(user.id), False) or is_pookie(user)

def is_blacklisted(user: discord.User):
    return blacklist.get(str(user.id), False)

def is_pookie(user: discord.User):
    return pookie_users.get(str(user.id), False)

def is_blocked_word(msg_content: str):
    msg = msg_content.lower()
    for word in blocked_words:
        # Basic bypass check: remove spaces/symbols
        pattern = re.sub(r'\W+', '', word.lower())
        check_msg = re.sub(r'\W+', '', msg)
        if pattern in check_msg:
            return True
    return False

def add_log(user_id, username, command_name, channel_id, channel_name):
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "user_id": user_id,
        "username": username,
        "command": command_name,
        "channel_id": channel_id,
        "channel_name": channel_name
    }
    logs.append(entry)
    # Keep last 100 logs max
    if len(logs) > 100:
        logs.pop(0)
    save_json('logs.json', logs)

# --- Decorators ---
def admin_only():
    async def predicate(ctx):
        if is_admin(ctx.author):
            return True
        await ctx.send("You need to be an admin to use this command.")
        return False
    return commands.check(predicate)

def pookie_only():
    async def predicate(ctx):
        if is_pookie(ctx.author):
            return True
        await ctx.send("You need pookie access to use this command.")
        return False
    return commands.check(predicate)

def not_blacklisted():
    async def predicate(ctx):
        if is_blacklisted(ctx.author):
            await ctx.send("You are blacklisted and cannot use commands.")
            return False
        return True
    return commands.check(predicate)

def admin_only_slash(interaction: discord.Interaction):
    return is_admin(interaction.user)

def pookie_only_slash(interaction: discord.Interaction):
    return is_pookie(interaction.user)

def not_blacklisted_slash(interaction: discord.Interaction):
    return not is_blacklisted(interaction.user)

# --- Events ---
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    keep_alive()
    try:
        await bot.tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print(f"Slash sync error: {e}")

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if is_blacklisted(message.author):
        try:
            await message.delete()
        except:
            pass
        return

    # Blocked words detection
    if is_blocked_word(message.content):
        try:
            await message.delete()
            await message.channel.send(f"{message.author.mention} This word is not allowed here.", delete_after=10)
        except:
            pass
        return

    await bot.process_commands(message)

# --- Commands ---

# --- Moderation ---
@bot.command()
@admin_only()
async def ban(ctx, member: discord.Member, *, reason=None):
    await member.ban(reason=reason)
    await ctx.send(f"{member} was banned. Reason: {reason}")
    add_log(ctx.author.id, str(ctx.author), "ban", ctx.channel.id, ctx.channel.name)

@bot.command()
@admin_only()
async def unban(ctx, user_id: int):
    user = await bot.fetch_user(user_id)
    await ctx.guild.unban(user)
    await ctx.send(f"{user} was unbanned.")
    add_log(ctx.author.id, str(ctx.author), "unban", ctx.channel.id, ctx.channel.name)

@bot.command()
@admin_only()
async def kick(ctx, member: discord.Member, *, reason=None):
    await member.kick(reason=reason)
    await ctx.send(f"{member} was kicked. Reason: {reason}")
    add_log(ctx.author.id, str(ctx.author), "kick", ctx.channel.id, ctx.channel.name)

@bot.command()
@admin_only()
async def blacklist(ctx, user: discord.User):
    blacklist[str(user.id)] = True
    save_json('blacklist.json', blacklist)
    await ctx.send(f"{user} added to blacklist.")
    add_log(ctx.author.id, str(ctx.author), "blacklist", ctx.channel.id, ctx.channel.name)

@bot.command()
@admin_only()
async def unblacklist(ctx, user: discord.User):
    if str(user.id) in blacklist:
        blacklist.pop(str(user.id))
        save_json('blacklist.json', blacklist)
        await ctx.send(f"{user} removed from blacklist.")
        add_log(ctx.author.id, str(ctx.author), "unblacklist", ctx.channel.id, ctx.channel.name)
    else:
        await ctx.send(f"{user} is not blacklisted.")

@bot.command()
@admin_only()
async def show_blacklist(ctx):
    if not blacklist:
        await ctx.send("Blacklist is empty.")
        return
    users = []
    for uid in blacklist:
        user = await bot.fetch_user(int(uid))
        users.append(f"{user} ({uid})")
    await ctx.send("Blacklisted users:\n" + "\n".join(users))

@bot.command()
@admin_only()
async def add_admin(ctx, user: discord.User):
    admins[str(user.id)] = True
    save_json('admins.json', admins)
    await ctx.send(f"{user} added as admin.")
    add_log(ctx.author.id, str(ctx.author), "add_admin", ctx.channel.id, ctx.channel.name)

@bot.command()
@admin_only()
async def remove_admin(ctx, user: discord.User):
    if str(user.id) == str(OWNER_ID):
        await ctx.send("Cannot remove owner from admin.")
        return
    if str(user.id) in admins:
        admins.pop(str(user.id))
        save_json('admins.json', admins)
        await ctx.send(f"{user} removed from admin.")
        add_log(ctx.author.id, str(ctx.author), "remove_admin", ctx.channel.id, ctx.channel.name)
    else:
        await ctx.send(f"{user} is not an admin.")

@bot.command()
@admin_only()
async def show_admins(ctx):
    users = []
    for uid in admins:
        user = await bot.fetch_user(int(uid))
        users.append(f"{user} ({uid})")
    await ctx.send("Admins:\n" + "\n".join(users))

# --- Blocked words management ---
@bot.command()
@admin_only()
async def add_blocked_word(ctx, *, word: str):
    word = word.lower().strip()
    if word in blocked_words:
        await ctx.send("Word already in blocked list.")
        return
    blocked_words.append(word)
    save_json('blocked_words.json', blocked_words)
    await ctx.send(f"Added blocked word: {word}")

@bot.command()
@admin_only()
async def remove_blocked_word(ctx, *, word: str):
    word = word.lower().strip()
    if word not in blocked_words:
        await ctx.send("Word not found in blocked list.")
        return
    blocked_words.remove(word)
    save_json('blocked_words.json', blocked_words)
    await ctx.send(f"Removed blocked word: {word}")

@bot.command()
@admin_only()
async def show_blocked_words(ctx):
    if not blocked_words:
        await ctx.send("No blocked words.")
        return
    await ctx.send("Blocked words:\n" + ", ".join(blocked_words))

# --- Pookie system ---
@bot.command()
@commands.check(lambda ctx: ctx.author.id == OWNER_ID)
async def create_pookie_key(ctx):
    key = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=12))
    pookie_keys[key] = True
    save_json('pookie_keys.json', pookie_keys)
    await ctx.send(f"New pookie key created:\n`{key}`")

@bot.command()
@commands.check(lambda ctx: ctx.author.id == OWNER_ID)
async def delete_pookie_key(ctx, key: str):
    if key in pookie_keys:
        pookie_keys.pop(key)
        save_json('pookie_keys.json', pookie_keys)
        await ctx.send(f"Pookie key `{key}` deleted.")
    else:
        await ctx.send(f"No such pookie key: `{key}`")

@bot.command()
@commands.check(lambda ctx: ctx.author.id == OWNER_ID)
async def list_pookie_users(ctx):
    if not pookie_users:
        await ctx.send("No pookie users.")
        return
    users = []
    for uid in pookie_users:
        user = await bot.fetch_user(int(uid))
        users.append(f"{user} ({uid})")
    await ctx.send("Pookie users:\n" + "\n".join(users))

@bot.command()
@commands.check(lambda ctx: ctx.author.id == OWNER_ID)
async def remove_pookie_user(ctx, user: discord.User):
    if str(user.id) in pookie_users:
        pookie_users.pop(str(user.id))
        save_json('pookie_users.json', pookie_users)
        await ctx.send(f"{user} removed from pookie users.")
    else:
        await ctx.send(f"{user} is not a pookie user.")

@bot.command()
@not_blacklisted()
async def redeem_pookie(ctx, key: str):
    if key in pookie_keys:
        pookie_users[str(ctx.author.id)] = True
        pookie_keys.pop(key)
        save_json('pookie_users.json', pookie_users)
        save_json('pookie_keys.json', pookie_keys)
        await ctx.send(f"{ctx.author.mention} You have been granted pookie access!")
    else:
        await ctx.send("Invalid or already used pookie key.")

# --- Fun commands ---

eight_ball_answers = [
    "It is certain.", "It is decidedly so.", "Without a doubt.", "Yes – definitely.",
    "You may rely on it.", "As I see it, yes.", "Most likely.", "Outlook good.",
    "Yes.", "Signs point to yes.", "Reply hazy, try again.", "Ask again later.",
    "Better not tell you now.", "Cannot predict now.", "Concentrate and ask again.",
    "Don't count on it.", "My reply is no.", "My sources say no.",
    "Outlook not so good.", "Very doubtful."
]

jokes = [
    "Why did the scarecrow win an award? Because he was outstanding in his field!",
    "Why don't scientists trust atoms? Because they make up everything!",
    "I told my wife she was drawing her eyebrows too high. She looked surprised.",
    "Why did the math book look sad? Because it had too many problems."
]

dad_jokes = [
    "I'm reading a book about anti-gravity. It's impossible to put down!",
    "Did you hear about the restaurant on the moon? Great food, no atmosphere.",
    "I would avoid the sushi if I was you. It’s a little fishy.",
    "Want to hear a joke about construction? I'm still working on it."
]

rps_choices = ["rock", "paper", "scissors"]

@bot.command()
@not_blacklisted()
async def eightball(ctx, *, question: str):
    answer = random.choice(eight_ball_answers)
    await ctx.send(f"🎱 Question: {question}\nAnswer: {answer}")
    add_log(ctx.author.id, str(ctx.author), "8ball", ctx.channel.id, ctx.channel.name)

@bot.command()
@not_blacklisted()
async def joke(ctx):
    await ctx.send(random.choice(jokes))
    add_log(ctx.author.id, str(ctx.author), "joke", ctx.channel.id, ctx.channel.name)

@bot.command()
@not_blacklisted()
async def dadjoke(ctx):
    await ctx.send(random.choice(dad_jokes))
    add_log(ctx.author.id, str(ctx.author), "dadjoke", ctx.channel.id, ctx.channel.name)

@bot.command()
@not_blacklisted()
async def rps(ctx, choice: str):
    choice = choice.lower()
    if choice not in rps_choices:
        await ctx.send("Choose rock, paper, or scissors.")
        return
    bot_choice = random.choice(rps_choices)
    if choice == bot_choice:
        result = "It's a tie!"
    elif (choice == "rock" and bot_choice == "scissors") or \
         (choice == "paper" and bot_choice == "rock") or \
         (choice == "scissors" and bot_choice == "paper"):
        result = "You win!"
    else:
        result = "I win!"
    await ctx.send(f"You chose {choice}, I chose {bot_choice}. {result}")
    add_log(ctx.author.id, str(ctx.author), "rps", ctx.channel.id, ctx.channel.name)

@bot.command()
@not_blacklisted()
async def flipcoin(ctx):
    result = random.choice(["Heads", "Tails"])
    await ctx.send(f"The coin landed on {result}.")
    add_log(ctx.author.id, str(ctx.author), "flipcoin", ctx.channel.id, ctx.channel.name)

@bot.command()
@not_blacklisted()
async def rolldice(ctx, sides: int = 6):
    if sides < 2 or sides > 100:
        await ctx.send("Number of sides must be between 2 and 100.")
        return
    result = random.randint(1, sides)
    await ctx.send(f"🎲 Rolled a {sides}-sided dice: {result}")
    add_log(ctx.author.id, str(ctx.author), "rolldice", ctx.channel.id, ctx.channel.name)

@bot.command()
@not_blacklisted()
async def cat(ctx):
    cat_api_key = os.getenv("CAT_API_KEY")
    headers = {}
    if cat_api_key:
        headers["x-api-key"] = cat_api_key
    url = "https://api.thecatapi.com/v1/images/search?limit=1"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            if not data:
                await ctx.send("Could not get a cat image.")
                return
            await ctx.send(data[0]["url"])
    add_log(ctx.author.id, str(ctx.author), "cat", ctx.channel.id, ctx.channel.name)

# --- Say commands ---
@bot.command()
@not_blacklisted()
async def say(ctx, *, message: str):
    # Remove pings
    safe_msg = message.replace('@everyone', '@\u200beveryone').replace('@here', '@\u200bhere')
    await ctx.message.delete()
    await ctx.send(safe_msg)
    add_log(ctx.author.id, str(ctx.author), "say", ctx.channel.id, ctx.channel.name)

@bot.command()
@admin_only()
async def say_admin(ctx, *, message: str):
    await ctx.message.delete()
    await ctx.send(message)
    add_log(ctx.author.id, str(ctx.author), "say_admin", ctx.channel.id, ctx.channel.name)

# --- User info & avatar ---
@bot.command()
@not_blacklisted()
async def avatar(ctx, user: discord.User = None):
    user = user or ctx.author
    await ctx.send(user.avatar.url)
    add_log(ctx.author.id, str(ctx.author), "avatar", ctx.channel.id, ctx.channel.name)

@bot.command()
@not_blacklisted()
async def userinfo(ctx, user: discord.User = None):
    user = user or ctx.author
    embed = discord.Embed(title=f"{user}", description=f"ID: {user.id}", color=0x00ff00)
    embed.set_thumbnail(url=user.avatar.url)
    embed.add_field(name="Account Created", value=user.created_at.strftime("%Y-%m-%d"))
    member = ctx.guild.get_member(user.id) if ctx.guild else None
    if member:
        embed.add_field(name="Joined Server", value=member.joined_at.strftime("%Y-%m-%d") if member.joined_at else "Unknown")
    await ctx.send(embed=embed)
    add_log(ctx.author.id, str(ctx.author), "userinfo", ctx.channel.id, ctx.channel.name)

# --- Show commands based on role ---
@bot.command()
async def showcommands(ctx):
    user = ctx.author
    available_cmds = []
    for command in bot.commands:
        # Check blacklist
        if is_blacklisted(user):
            continue
        # Check admin only commands
        if getattr(command.callback, "commands_only_admin", False) and not is_admin(user):
            continue
        # Check pookie only commands
        if getattr(command.callback, "commands_only_pookie", False) and not is_pookie(user):
            continue
        available_cmds.append(f"?{command.name}")
    await ctx.send("Commands you can use:\n" + "\n".join(sorted(available_cmds)))

# --- Slash Commands Setup ---
@bot.event
async def on_ready():
    # Sync slash commands each ready
    await bot.tree.sync()

def slash_admin_only():
    async def predicate(interaction: discord.Interaction):
        if is_admin(interaction.user):
            return True
        await interaction.response.send_message("Admin only command.", ephemeral=True)
        return False
    return app_commands.check(predicate)

def slash_pookie_only():
    async def predicate(interaction: discord.Interaction):
        if is_pookie(interaction.user):
            return True
        await interaction.response.send_message("Pookie only command.", ephemeral=True)
        return False
    return app_commands.check(predicate)

def slash_not_blacklisted():
    async def predicate(interaction: discord.Interaction):
        if not is_blacklisted(interaction.user):
            return True
        await interaction.response.send_message("You are blacklisted.", ephemeral=True)
        return False
    return app_commands.check(predicate)

@bot.tree.command(name="ping", description="Ping the bot")
@slash_admin_only()
async def ping_slash(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")

@bot.tree.command(name="pong", description="Pong command")
@slash_not_blacklisted()
async def pong_slash(interaction: discord.Interaction):
    await interaction.response.send_message("Ping!")

@bot.tree.command(name="avatar", description="Get user avatar")
@slash_not_blacklisted()
@app_commands.describe(user="User to get avatar of")
async def avatar_slash(interaction: discord.Interaction, user: discord.User = None):
    user = user or interaction.user
    await interaction.response.send_message(user.avatar.url)

@bot.tree.command(name="userinfo", description="Get user info")
@slash_not_blacklisted()
@app_commands.describe(user="User to get info of")
async def userinfo_slash(interaction: discord.Interaction, user: discord.User = None):
    user = user or interaction.user
    embed = discord.Embed(title=f"{user}", description=f"ID: {user.id}", color=0x00ff00)
    embed.set_thumbnail(url=user.avatar.url)
    guild = interaction.guild
    if guild:
        member = guild.get_member(user.id)
        if member:
            embed.add_field(name="Joined Server", value=member.joined_at.strftime("%Y-%m-%d") if member.joined_at else "Unknown")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="say", description="Say something without pings")
@slash_not_blacklisted()
@app_commands.describe(message="Message to say")
async def say_slash(interaction: discord.Interaction, message: str):
    safe_msg = message.replace('@everyone', '@\u200beveryone').replace('@here', '@\u200bhere')
    await interaction.response.send_message(safe_msg)

@bot.tree.command(name="say_admin", description="Admin say with pings allowed")
@slash_admin_only()
@app_commands.describe(message="Message to say")
async def say_admin_slash(interaction: discord.Interaction, message: str):
    await interaction.response.send_message(message)

# (Add other slash fun commands similar to prefix if you want...)

# --- Main ---
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    print("Error: DISCORD_TOKEN missing in environment variables!")
    exit(1)

bot.run(TOKEN)
