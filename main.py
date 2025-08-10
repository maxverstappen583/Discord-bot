import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import json
from flask import Flask
from threading import Thread
import aiohttp
import random
from datetime import datetime, timedelta
import asyncio

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="?", intents=intents)
tree = bot.tree

ADMIN_ID = 1319292111325106296

DATA_FOLDER = "data"
os.makedirs(DATA_FOLDER, exist_ok=True)

ADMIN_FILE = f"{DATA_FOLDER}/admins.json"
BANLIST_FILE = f"{DATA_FOLDER}/banned.json"
BLACKLIST_FILE = f"{DATA_FOLDER}/blacklist.json"
BLOCKED_WORDS_FILE = f"{DATA_FOLDER}/blocked_words.json"
POOKIE_USERS_FILE = f"{DATA_FOLDER}/pookie_users.json"
LOGS_FILE = f"{DATA_FOLDER}/logs.json"
CAT_CHANNEL_FILE = f"{DATA_FOLDER}/cat_channel.json"

# Load or create JSON data
def load_json(filename):
    if not os.path.isfile(filename):
        with open(filename, "w") as f:
            json.dump({}, f)
    with open(filename, "r") as f:
        return json.load(f)

def save_json(filename, data):
    with open(filename, "w") as f:
        json.dump(data, f, indent=4)

admins = load_json(ADMIN_FILE)
banned_users = load_json(BANLIST_FILE)
blacklist = load_json(BLACKLIST_FILE)
blocked_words = load_json(BLOCKED_WORDS_FILE)
pookie_users = load_json(POOKIE_USERS_FILE)
logs = load_json(LOGS_FILE)
cat_channel_data = load_json(CAT_CHANNEL_FILE)

# Ensure owner is admin + pookie by default
admins[str(ADMIN_ID)] = True
pookie_users[str(ADMIN_ID)] = True

save_json(ADMIN_FILE, admins)
save_json(POOKIE_USERS_FILE, pookie_users)

# Permission checks
def is_owner(user):
    return user.id == ADMIN_ID

def is_admin(user):
    return str(user.id) in admins

def is_pookie(user):
    return str(user.id) in pookie_users

def can_use_mod_commands(user):
    return is_owner(user) or is_admin(user) or is_pookie(user)

def is_blacklisted(user):
    return str(user.id) in blacklist or str(user.id) in banned_users

# Logging command usage
def log_command(user_id, command_name, channel_name):
    entry = {
        "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "user": str(user_id),
        "command": command_name,
        "channel": channel_name
    }
    if "entries" not in logs:
        logs["entries"] = []
    logs["entries"].append(entry)
    # Keep only last 1000 logs for size limit
    logs["entries"] = logs["entries"][-1000:]
    save_json(LOGS_FILE, logs)

# Flask app for uptime
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# BLOCKED WORDS detection with bypass (simplistic)
def normalize_text(text):
    # Lowercase and remove common spaces/symbols for bypass check
    return "".join(c for c in text.lower() if c.isalnum())

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if is_blacklisted(message.author):
        await message.delete()
        return

    normalized = normalize_text(message.content)
    for word in blocked_words:
        if word in normalized:
            try:
                await message.delete()
            except:
                pass
            await message.channel.send(f"{message.author.mention} This word is not allowed here.")
            return

    await bot.process_commands(message)

# Prefix commands

@bot.command()
async def say(ctx, *, text):
    # Public say: block mentions
    if any(mention in text for mention in ["@everyone", "@here", "<@&"]):
        return await ctx.send("Mentions are not allowed in this command.")
    await ctx.send(text)
    log_command(ctx.author.id, "say", ctx.channel.name)

@bot.command()
async def say_admin(ctx, *, text):
    # Admin say: allow mentions
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("You don't have permission.")
    await ctx.send(text)
    log_command(ctx.author.id, "say_admin", ctx.channel.name)

@bot.command()
async def hi(ctx):
    await ctx.send(f"Hello {ctx.author.mention}!")
    log_command(ctx.author.id, "hi", ctx.channel.name)

@bot.command()
async def ping(ctx):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("No permission.")
    await ctx.send("Pong!")
    log_command(ctx.author.id, "ping", ctx.channel.name)

@bot.command()
async def avatar(ctx, user: discord.User = None):
    user = user or ctx.author
    embed = discord.Embed(title=f"{user}'s Avatar")
    embed.set_image(url=user.display_avatar.url)
    await ctx.send(embed=embed)
    log_command(ctx.author.id, "avatar", ctx.channel.name)

@bot.command()
async def userinfo(ctx, user: discord.User = None):
    user = user or ctx.author
    member = ctx.guild.get_member(user.id)
    embed = discord.Embed(title=f"User info: {user}")
    embed.add_field(name="ID", value=user.id)
    if member:
        embed.add_field(name="Top role", value=member.top_role.name)
        embed.add_field(name="Joined", value=member.joined_at.strftime("%Y-%m-%d %H:%M:%S") if member.joined_at else "Unknown")
    embed.set_thumbnail(url=user.display_avatar.url)
    await ctx.send(embed=embed)
    log_command(ctx.author.id, "userinfo", ctx.channel.name)

# Ban/kick/blacklist commands (owner/admin/pookie only)

@bot.command()
async def ban(ctx, user: discord.User):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("You don't have permission.")
    banned_users[str(user.id)] = True
    save_json(BANLIST_FILE, banned_users)
    await ctx.send(f"{user} has been banned.")
    log_command(ctx.author.id, "ban", ctx.channel.name)

@bot.command()
async def unban(ctx, user: discord.User):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("You don't have permission.")
    if str(user.id) in banned_users:
        del banned_users[str(user.id)]
        save_json(BANLIST_FILE, banned_users)
        await ctx.send(f"{user} has been unbanned.")
    else:
        await ctx.send(f"{user} is not banned.")
    log_command(ctx.author.id, "unban", ctx.channel.name)

@bot.command()
async def kick(ctx, user: discord.Member):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("You don't have permission.")
    try:
        await user.kick()
        await ctx.send(f"{user} has been kicked.")
    except Exception as e:
        await ctx.send(f"Failed to kick: {e}")
    log_command(ctx.author.id, "kick", ctx.channel.name)

@bot.command()
async def blacklist_add(ctx, user: discord.User):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("You don't have permission.")
    blacklist[str(user.id)] = True
    save_json(BLACKLIST_FILE, blacklist)
    await ctx.send(f"{user} added to blacklist.")
    log_command(ctx.author.id, "blacklist_add", ctx.channel.name)

@bot.command()
async def blacklist_remove(ctx, user: discord.User):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("You don't have permission.")
    if str(user.id) in blacklist:
        del blacklist[str(user.id)]
        save_json(BLACKLIST_FILE, blacklist)
        await ctx.send(f"{user} removed from blacklist.")
    else:
        await ctx.send(f"{user} is not blacklisted.")
    log_command(ctx.author.id, "blacklist_remove", ctx.channel.name)

@bot.command()
async def show_blacklist(ctx):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("You don't have permission.")
    if not blacklist:
        return await ctx.send("Blacklist is empty.")
    users = [f"<@{uid}>" for uid in blacklist.keys()]
    await ctx.send("Blacklisted users:\n" + "\n".join(users))

# Blocked words management (admin/pookie/owner only)

@bot.command()
async def add_blocked_word(ctx, *, word):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("You don't have permission.")
    blocked_words[word.lower()] = True
    save_json(BLOCKED_WORDS_FILE, blocked_words)
    await ctx.send(f"Added blocked word: {word}")

@bot.command()
async def remove_blocked_word(ctx, *, word):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("You don't have permission.")
    if word.lower() in blocked_words:
        del blocked_words[word.lower()]
        save_json(BLOCKED_WORDS_FILE, blocked_words)
        await ctx.send(f"Removed blocked word: {word}")
    else:
        await ctx.send("Word not found.")

@bot.command()
async def show_blocked_words(ctx):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("You don't have permission.")
    if not blocked_words:
        return await ctx.send("No blocked words set.")
    await ctx.send("Blocked words:\n" + ", ".join(blocked_words.keys()))

# Pookie system (owner only)

@bot.command()
async def add_pookie(ctx, user: discord.User):
    if not is_owner(ctx.author):
        return await ctx.send("You don't have permission.")
    pookie_users[str(user.id)] = True
    save_json(POOKIE_USERS_FILE, pookie_users)
    await ctx.send(f"Added {user} as pookie user.")

@bot.command()
async def remove_pookie(ctx, user: discord.User):
    if not is_owner(ctx.author):
        return await ctx.send("You don't have permission.")
    if str(user.id) in pookie_users:
        del pookie_users[str(user.id)]
        save_json(POOKIE_USERS_FILE, pookie_users)
        await ctx.send(f"Removed {user} from pookie users.")
    else:
        await ctx.send("User is not a pookie user.")

@bot.command()
async def list_pookie(ctx):
    if not is_owner(ctx.author):
        return await ctx.send("You don't have permission.")
    if not pookie_users:
        return await ctx.send("No pookie users.")
    users = [f"<@{uid}>" for uid in pookie_users.keys()]
    await ctx.send("Pookie users:\n" + "\n".join(users))

# Fun commands

@bot.command()
async def roll_dice(ctx):
    result = random.randint(1, 6)
    await ctx.send(f"🎲 You rolled a {result}!")
    log_command(ctx.author.id, "roll_dice", ctx.channel.name)

@bot.command()
async def flip_coin(ctx):
    result = random.choice(["Heads", "Tails"])
    await ctx.send(f"🪙 It's {result}!")
    log_command(ctx.author.id, "flip_coin", ctx.channel.name)

@bot.command()
async def eight_ball(ctx, *, question):
    answers = [
        "It is certain.", "It is decidedly so.", "Without a doubt.", "Yes – definitely.",
        "You may rely on it.", "As I see it, yes.", "Most likely.", "Outlook good.",
        "Yes.", "Signs point to yes.", "Reply hazy, try again.", "Ask again later.",
        "Better not tell you now.", "Cannot predict now.", "Concentrate and ask again.",
        "Don't count on it.", "My reply is no.", "My sources say no.",
        "Outlook not so good.", "Very doubtful."
    ]
    response = random.choice(answers)
    await ctx.send(f"🎱 Question: {question}\nAnswer: {response}")
    log_command(ctx.author.id, "8ball", ctx.channel.name)

@bot.command()
async def joke(ctx):
    jokes = [
        "Why don't scientists trust atoms? Because they make up everything!",
        "Why did the scarecrow win an award? Because he was outstanding in his field!",
        "What do you call fake spaghetti? An impasta!"
    ]
    await ctx.send(random.choice(jokes))
    log_command(ctx.author.id, "joke", ctx.channel.name)

@bot.command()
async def dadjoke(ctx):
    dad_jokes = [
        "I'm reading a book on anti-gravity. It's impossible to put down!",
        "Why did the math book look sad? Because it had too many problems.",
        "Did you hear about the restaurant on the moon? Great food, no atmosphere."
    ]
    await ctx.send(random.choice(dad_jokes))
    log_command(ctx.author.id, "dadjoke", ctx.channel.name)

@bot.command()
async def rps(ctx, choice):
    choices = ["rock", "paper", "scissors"]
    if choice.lower() not in choices:
        return await ctx.send(f"Invalid choice. Choose from {choices}.")
    bot_choice = random.choice(choices)
    if choice == bot_choice:
        result = "It's a tie!"
    elif (choice == "rock" and bot_choice == "scissors") or \
         (choice == "scissors" and bot_choice == "paper") or \
         (choice == "paper" and bot_choice == "rock"):
        result = "You win!"
    else:
        result = "You lose!"
    await ctx.send(f"You chose {choice}, I chose {bot_choice}. {result}")
    log_command(ctx.author.id, "rps", ctx.channel.name)

# Showcommands command - lists commands user can use

@bot.command()
async def showcommands(ctx):
    # List all commands user can run (prefix commands)
    available = []
    for cmd in bot.commands:
        if cmd.name in ["ban", "kick", "blacklist_add", "blacklist_remove", "show_blacklist",
                        "add_blocked_word", "remove_blocked_word", "show_blocked_words",
                        "unban", "say_admin", "ping", "add_pookie", "remove_pookie", "list_pookie"]:
            if can_use_mod_commands(ctx.author):
                available.append(cmd.name)
        else:
            available.append(cmd.name)
    await ctx.send(f"Commands you can use:\n{', '.join(available)}")

# Slash commands

@tree.command(name="say", description="Say something (no mentions)")
@app_commands.describe(text="Text to say")
async def say_slash(interaction: discord.Interaction, text: str):
    if any(mention in text for mention in ["@everyone", "@here", "<@&"]):
        return await interaction.response.send_message("Mentions are not allowed.", ephemeral=True)
    await interaction.response.send_message(text)
    log_command(interaction.user.id, "say_slash", interaction.channel.name)

@tree.command(name="say_admin", description="Say something (admin only, mentions allowed)")
@app_commands.describe(text="Text to say")
async def say_admin_slash(interaction: discord.Interaction, text: str):
    if not can_use_mod_commands(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    await interaction.response.send_message(text)
    log_command(interaction.user.id, "say_admin_slash", interaction.channel.name)

@tree.command(name="hi", description="Say hi")
async def hi_slash(interaction: discord.Interaction):
    await interaction.response.send_message(f"Hello {interaction.user.mention}!")
    log_command(interaction.user.id, "hi_slash", interaction.channel.name)

@tree.command(name="ping", description="Ping (admin only)")
async def ping_slash(interaction: discord.Interaction):
    if not can_use_mod_commands(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    await interaction.response.send_message("Pong!")
    log_command(interaction.user.id, "ping_slash", interaction.channel.name)

@tree.command(name="avatar", description="Show avatar")
@app_commands.describe(user="User to get avatar of")
async def avatar_slash(interaction: discord.Interaction, user: discord.User = None):
    user = user or interaction.user
    embed = discord.Embed(title=f"{user}'s Avatar")
    embed.set_image(url=user.display_avatar.url)
    await interaction.response.send_message(embed=embed)
    log_command(interaction.user.id, "avatar_slash", interaction.channel.name)

@tree.command(name="userinfo", description="Show user info")
@app_commands.describe(user="User to get info of")
async def userinfo_slash(interaction: discord.Interaction, user: discord.User = None):
    user = user or interaction.user
    guild = interaction.guild
    member = guild.get_member(user.id) if guild else None
    embed = discord.Embed(title=f"User info: {user}")
    embed.add_field(name="ID", value=user.id)
    if member:
        embed.add_field(name="Top role", value=member.top_role.name)
        embed.add_field(name="Joined", value=member.joined_at.strftime("%Y-%m-%d %H:%M:%S") if member.joined_at else "Unknown")
    embed.set_thumbnail(url=user.display_avatar.url)
    await interaction.response.send_message(embed=embed)
    log_command(interaction.user.id, "userinfo_slash", interaction.channel.name)

# Ban/kick/blacklist slash commands with permission checks

@tree.command(name="ban", description="Ban a user (admin/pookie/owner only)")
@app_commands.describe(user="User to ban")
async def ban_slash(interaction: discord.Interaction, user: discord.User):
    if not can_use_mod_commands(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    banned_users[str(user.id)] = True
    save_json(BANLIST_FILE, banned_users)
    await interaction.response.send_message(f"{user} has been banned.")
    log_command(interaction.user.id, "ban_slash", interaction.channel.name)

@tree.command(name="unban", description="Unban a user (admin/pookie/owner only)")
@app_commands.describe(user="User to unban")
async def unban_slash(interaction: discord.Interaction, user: discord.User):
    if not can_use_mod_commands(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    if str(user.id) in banned_users:
        del banned_users[str(user.id)]
        save_json(BANLIST_FILE, banned_users)
        await interaction.response.send_message(f"{user} has been unbanned.")
    else:
        await interaction.response.send_message(f"{user} is not banned.")
    log_command(interaction.user.id, "unban_slash", interaction.channel.name)

@tree.command(name="kick", description="Kick a user (admin/pookie/owner only)")
@app_commands.describe(user="User to kick")
async def kick_slash(interaction: discord.Interaction, user: discord.Member):
    if not can_use_mod_commands(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    try:
        await user.kick()
        await interaction.response.send_message(f"{user} has been kicked.")
    except Exception as e:
        await interaction.response.send_message(f"Failed to kick: {e}")
    log_command(interaction.user.id, "kick_slash", interaction.channel.name)

@tree.command(name="blacklist_add", description="Add user to blacklist (admin/pookie/owner only)")
@app_commands.describe(user="User to blacklist")
async def blacklist_add_slash(interaction: discord.Interaction, user: discord.User):
    if not can_use_mod_commands(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    blacklist[str(user.id)] = True
    save_json(BLACKLIST_FILE, blacklist)
    await interaction.response.send_message(f"{user} added to blacklist.")
    log_command(interaction.user.id, "blacklist_add_slash", interaction.channel.name)

@tree.command(name="blacklist_remove", description="Remove user from blacklist (admin/pookie/owner only)")
@app_commands.describe(user="User to remove from blacklist")
async def blacklist_remove_slash(interaction: discord.Interaction, user: discord.User):
    if not can_use_mod_commands(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    if str(user.id) in blacklist:
        del blacklist[str(user.id)]
        save_json(BLACKLIST_FILE, blacklist)
        await interaction.response.send_message(f"{user} removed from blacklist.")
    else:
        await interaction.response.send_message(f"{user} is not blacklisted.")
    log_command(interaction.user.id, "blacklist_remove_slash", interaction.channel.name)

@tree.command(name="show_blacklist", description="Show blacklisted users (admin/pookie/owner only)")
async def show_blacklist_slash(interaction: discord.Interaction):
    if not can_use_mod_commands(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    if not blacklist:
        return await interaction.response.send_message("Blacklist is empty.", ephemeral=True)
    users = [f"<@{uid}>" for uid in blacklist.keys()]
    await interaction.response.send_message("Blacklisted users:\n" + "\n".join(users))

# Blocked words slash commands (admin/pookie/owner only)

@tree.command(name="add_blocked_word", description="Add a blocked word (admin/pookie/owner only)")
@app_commands.describe(word="Word to block")
async def add_blocked_word_slash(interaction: discord.Interaction, word: str):
    if not can_use_mod_commands(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    blocked_words[word.lower()] = True
    save_json(BLOCKED_WORDS_FILE, blocked_words)
    await interaction.response.send_message(f"Added blocked word: {word}")

@tree.command(name="remove_blocked_word", description="Remove a blocked word (admin/pookie/owner only)")
@app_commands.describe(word="Word to unblock")
async def remove_blocked_word_slash(interaction: discord.Interaction, word: str):
    if not can_use_mod_commands(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    if word.lower() in blocked_words:
        del blocked_words[word.lower()]
        save_json(BLOCKED_WORDS_FILE, blocked_words)
        await interaction.response.send_message(f"Removed blocked word: {word}")
    else:
        await interaction.response.send_message("Word not found.")

@tree.command(name="show_blocked_words", description="Show blocked words (admin/pookie/owner only)")
async def show_blocked_words_slash(interaction: discord.Interaction):
    if not can_use_mod_commands(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    if not blocked_words:
        return await interaction.response.send_message("No blocked words set.", ephemeral=True)
    await interaction.response.send_message("Blocked words:\n" + ", ".join(blocked_words.keys()))

# Pookie management slash commands (owner only)

@tree.command(name="add_pookie", description="Add a pookie user (owner only)")
@app_commands.describe(user="User to add")
async def add_pookie_slash(interaction: discord.Interaction, user: discord.User):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    pookie_users[str(user.id)] = True
    save_json(POOKIE_USERS_FILE, pookie_users)
    await interaction.response.send_message(f"Added {user} as pookie user.")

@tree.command(name="remove_pookie", description="Remove a pookie user (owner only)")
@app_commands.describe(user="User to remove")
async def remove_pookie_slash(interaction: discord.Interaction, user: discord.User):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    if str(user.id) in pookie_users:
        del pookie_users[str(user.id)]
        save_json(POOKIE_USERS_FILE, pookie_users)
        await interaction.response.send_message(f"Removed {user} from pookie users.")
    else:
        await interaction.response.send_message("User is not a pookie user.")

@tree.command(name="list_pookie", description="List pookie users (owner only)")
async def list_pookie_slash(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    if not pookie_users:
        return await interaction.response.send_message("No pookie users.", ephemeral=True)
    users = [f"<@{uid}>" for uid in pookie_users.keys()]
    await interaction.response.send_message("Pookie users:\n" + "\n".join(users))

# On ready event

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands.")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

keep_alive()

TOKEN = os.getenv("DISCORD_TOKEN")
bot.run(TOKEN)
