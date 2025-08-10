import discord
from discord.ext import commands, tasks
from discord import app_commands
import random
import json
import os
import aiohttp
from flask import Flask
from threading import Thread

# ========== Config ==========
OWNER_ID = 1319292111325106296
DATA_DIR = "data"
BLACKLIST_FILE = os.path.join(DATA_DIR, "blacklist.json")
BANLIST_FILE = os.path.join(DATA_DIR, "banlist.json")
POOKIE_USERS_FILE = os.path.join(DATA_DIR, "pookie_users.json")
BLOCKED_WORDS_FILE = os.path.join(DATA_DIR, "blocked_words.json")
LOGS_FILE = os.path.join(DATA_DIR, "logs.json")

CAT_API_KEY = os.getenv("CAT_API_KEY")  # Optional for cat images API

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="?", intents=intents)
tree = bot.tree

# ========== Ensure data directory & files ==========
os.makedirs(DATA_DIR, exist_ok=True)
for file in [BLACKLIST_FILE, BANLIST_FILE, POOKIE_USERS_FILE, BLOCKED_WORDS_FILE, LOGS_FILE]:
    if not os.path.exists(file):
        with open(file, "w") as f:
            json.dump({}, f)

# ========== Load JSON data ==========
def load_json(path):
    with open(path, "r") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=4)

blacklist = load_json(BLACKLIST_FILE)
banned_users = load_json(BANLIST_FILE)
pookie_users = load_json(POOKIE_USERS_FILE)
blocked_words = load_json(BLOCKED_WORDS_FILE)
logs = load_json(LOGS_FILE)

# ========== Helper checks ==========

def is_owner(user):
    return user.id == OWNER_ID

def is_admin(user):
    return str(user.id) in pookie_users or is_owner(user)

def is_pookie(user):
    return str(user.id) in pookie_users

def can_use_mod_commands(user):
    return is_admin(user) or is_pookie(user) or is_owner(user)

def is_blacklisted(user):
    return str(user.id) in blacklist

def is_banned(user):
    return str(user.id) in banned_users

def log_command(user_id, command_name, channel_name):
    from datetime import datetime
    logs[str(len(logs)+1)] = {
        "user_id": user_id,
        "command": command_name,
        "channel": channel_name,
        "timestamp": datetime.utcnow().isoformat()
    }
    save_json(LOGS_FILE, logs)

# ========== Flask uptime server for Render ==========
app = Flask('')

@app.route('/')
def home():
    return "Bot is running."

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# ========== Blocked word detection (with bypass detection) ==========
def normalize_text(text):
    # Lowercase and remove common symbol substitutions (simple)
    substitutions = {
        '@': 'a',
        '4': 'a',
        '3': 'e',
        '1': 'i',
        '!': 'i',
        '0': 'o',
        '$': 's',
        '+': 't',
        '7': 't'
    }
    text = text.lower()
    for k, v in substitutions.items():
        text = text.replace(k, v)
    # Remove spaces and some symbols to catch bypass
    return ''.join(c for c in text if c.isalnum())

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Check blacklist and ban
    if is_blacklisted(message.author):
        try:
            await message.delete()
            await message.channel.send(f"{message.author.mention}, you are blacklisted.", delete_after=10)
        except:
            pass
        return

    if is_banned(message.author):
        try:
            await message.delete()
            await message.channel.send(f"{message.author.mention}, you are banned.", delete_after=10)
        except:
            pass
        return

    # Check blocked words with bypass protection
    normalized = normalize_text(message.content)
    for word in blocked_words.keys():
        if word in normalized:
            try:
                await message.delete()
                await message.channel.send(f"{message.author.mention} This word is not allowed here.", delete_after=8)
            except:
                pass
            return

    await bot.process_commands(message)

# ========== Prefix commands ==========

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

# Cat command with optional API

@bot.command()
async def cat(ctx):
    if CAT_API_KEY:
        url = "https://api.thecatapi.com/v1/images/search"
        headers = {"x-api-key": CAT_API_KEY}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data:
                        await ctx.send(data[0]["url"])
                        log_command(ctx.author.id, "cat", ctx.channel.name)
                        return
    # Fallback: random cat images from public source
    cat_images = [
        "https://cdn2.thecatapi.com/images/MTY3ODIyMQ.jpg",
        "https://cdn2.thecatapi.com/images/MTY3OTIzMQ.jpg",
        "https://cdn2.thecatapi.com/images/MTY3OTI5MA.jpg"
    ]
    await ctx.send(random.choice(cat_images))
    log_command(ctx.author.id, "cat", ctx.channel.name)

# Say commands

@bot.command()
async def say(ctx, *, text):
    if any(mention in text for mention in ["@everyone", "@here", "<@&"]):
        return await ctx.send("Mentions are not allowed in this command.")
    await ctx.send(text)
    log_command(ctx.author.id, "say", ctx.channel.name)

@bot.command()
async def say_admin(ctx, *, text):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("You do not have permission to use this command.")
    await ctx.send(text)
    log_command(ctx.author.id, "say_admin", ctx.channel.name)

# Admin commands

@bot.command()
async def ban(ctx, user: discord.User):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("You do not have permission to ban.")
    banned_users[str(user.id)] = True
    save_json(BANLIST_FILE, banned_users)
    await ctx.send(f"{user} has been banned.")
    log_command(ctx.author.id, "ban", ctx.channel.name)

@bot.command()
async def unban(ctx, user: discord.User):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("You do not have permission to unban.")
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
        return await ctx.send("You do not have permission to kick.")
    try:
        await user.kick()
        await ctx.send(f"{user} has been kicked.")
    except Exception as e:
        await ctx.send(f"Failed to kick user: {e}")
    log_command(ctx.author.id, "kick", ctx.channel.name)

@bot.command()
async def blacklist(ctx, user: discord.User):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("You do not have permission to blacklist.")
    blacklist[str(user.id)] = True
    save_json(BLACKLIST_FILE, blacklist)
    await ctx.send(f"{user} has been blacklisted.")
    log_command(ctx.author.id, "blacklist", ctx.channel.name)

@bot.command()
async def unblacklist(ctx, user: discord.User):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("You do not have permission to unblacklist.")
    if str(user.id) in blacklist:
        del blacklist[str(user.id)]
        save_json(BLACKLIST_FILE, blacklist)
        await ctx.send(f"{user} has been removed from blacklist.")
    else:
        await ctx.send(f"{user} is not blacklisted.")
    log_command(ctx.author.id, "unblacklist", ctx.channel.name)

# Show commands available for the user (prefix and slash)

@bot.command(name="showcommands")
async def showcommands(ctx):
    cmds = []
    # Always available
    public_cmds = ["roll_dice", "flip_coin", "eight_ball", "joke", "dadjoke", "rps", "cat", "say", "userinfo", "avatar"]
    # Admin commands
    admin_cmds = ["ban", "unban", "kick", "blacklist", "unblacklist", "say_admin", "blocked_words", "add_blocked_word", "remove_blocked_word"]

    if can_use_mod_commands(ctx.author):
        cmds = public_cmds + admin_cmds
    else:
        cmds = public_cmds
    await ctx.send(f"Available commands:\n" + ", ".join(cmds))
    log_command(ctx.author.id, "showcommands", ctx.channel.name)

# User info and avatar

@bot.command()
async def userinfo(ctx, user: discord.User = None):
    user = user or ctx.author
    embed = discord.Embed(title=f"User Info - {user}", color=discord.Color.blue())
    embed.set_thumbnail(url=user.avatar.url if user.avatar else user.default_avatar.url)
    embed.add_field(name="ID", value=user.id)
    embed.add_field(name="Bot?", value=user.bot)
    embed.add_field(name="Created At", value=user.created_at.strftime("%Y-%m-%d %H:%M:%S"))
    await ctx.send(embed=embed)
    log_command(ctx.author.id, "userinfo", ctx.channel.name)

@bot.command()
async def avatar(ctx, user: discord.User = None):
    user = user or ctx.author
    embed = discord.Embed(title=f"{user}'s Avatar", color=discord.Color.green())
    embed.set_image(url=user.avatar.url if user.avatar else user.default_avatar.url)
    await ctx.send(embed=embed)
    log_command(ctx.author.id, "avatar", ctx.channel.name)

# ========== Slash commands ==========
# Register same commands as slash commands with similar permission logic

@tree.command(name="roll_dice", description="Roll a dice")
async def roll_dice_slash(interaction: discord.Interaction):
    result = random.randint(1, 6)
    await interaction.response.send_message(f"🎲 You rolled a {result}!")
    log_command(interaction.user.id, "roll_dice", interaction.channel.name)

@tree.command(name="flip_coin", description="Flip a coin")
async def flip_coin_slash(interaction: discord.Interaction):
    result = random.choice(["Heads", "Tails"])
    await interaction.response.send_message(f"🪙 It's {result}!")
    log_command(interaction.user.id, "flip_coin", interaction.channel.name)

@tree.command(name="eight_ball", description="Ask the magic 8-ball")
@app_commands.describe(question="Your question")
async def eight_ball_slash(interaction: discord.Interaction, question: str):
    answers = [
        "It is certain.", "It is decidedly so.", "Without a doubt.", "Yes – definitely.",
        "You may rely on it.", "As I see it, yes.", "Most likely.", "Outlook good.",
        "Yes.", "Signs point to yes.", "Reply hazy, try again.", "Ask again later.",
        "Better not tell you now.", "Cannot predict now.", "Concentrate and ask again.",
        "Don't count on it.", "My reply is no.", "My sources say no.",
        "Outlook not so good.", "Very doubtful."
    ]
    response = random.choice(answers)
    await interaction.response.send_message(f"🎱 Question: {question}\nAnswer: {response}")
    log_command(interaction.user.id, "8ball", interaction.channel.name)

@tree.command(name="joke", description="Get a random joke")
async def joke_slash(interaction: discord.Interaction):
    jokes = [
        "Why don't scientists trust atoms? Because they make up everything!",
        "Why did the scarecrow win an award? Because he was outstanding in his field!",
        "What do you call fake spaghetti? An impasta!"
    ]
    await interaction.response.send_message(random.choice(jokes))
    log_command(interaction.user.id, "joke", interaction.channel.name)

@tree.command(name="dadjoke", description="Get a dad joke")
async def dadjoke_slash(interaction: discord.Interaction):
    dad_jokes = [
        "I'm reading a book on anti-gravity. It's impossible to put down!",
        "Why did the math book look sad? Because it had too many problems.",
        "Did you hear about the restaurant on the moon? Great food, no atmosphere."
    ]
    await interaction.response.send_message(random.choice(dad_jokes))
    log_command(interaction.user.id, "dadjoke", interaction.channel.name)

@tree.command(name="rps", description="Play rock-paper-scissors")
@app_commands.describe(choice="Your choice")
async def rps_slash(interaction: discord.Interaction, choice: str):
    choices = ["rock", "paper", "scissors"]
    if choice.lower() not in choices:
        await interaction.response.send_message(f"Invalid choice. Choose from {choices}.")
        return
    bot_choice = random.choice(choices)
    if choice == bot_choice:
        result = "It's a tie!"
    elif (choice == "rock" and bot_choice == "scissors") or \
         (choice == "scissors" and bot_choice == "paper") or \
         (choice == "paper" and bot_choice == "rock"):
        result = "You win!"
    else:
        result = "You lose!"
    await interaction.response.send_message(f"You chose {choice}, I chose {bot_choice}. {result}")
    log_command(interaction.user.id, "rps", interaction.channel.name)

@tree.command(name="cat", description="Show a random cat image")
async def cat_slash(interaction: discord.Interaction):
    if CAT_API_KEY:
        url = "https://api.thecatapi.com/v1/images/search"
        headers = {"x-api-key": CAT_API_KEY}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data:
                        await interaction.response.send_message(data[0]["url"])
                        log_command(interaction.user.id, "cat", interaction.channel.name)
                        return
    cat_images = [
        "https://cdn2.thecatapi.com/images/MTY3ODIyMQ.jpg",
        "https://cdn2.thecatapi.com/images/MTY3OTIzMQ.jpg",
        "https://cdn2.thecatapi.com/images/MTY3OTI5MA.jpg"
    ]
    await interaction.response.send_message(random.choice(cat_images))
    log_command(interaction.user.id, "cat", interaction.channel.name)

@tree.command(name="say", description="Say something (no mentions allowed)")
@app_commands.describe(text="Text to say")
async def say_slash(interaction: discord.Interaction, text: str):
    if any(mention in text for mention in ["@everyone", "@here", "<@&"]):
        await interaction.response.send_message("Mentions are not allowed.", ephemeral=True)
        return
    await interaction.response.send_message(text)
    log_command(interaction.user.id, "say", interaction.channel.name)

@tree.command(name="say_admin", description="Say something as admin")
@app_commands.describe(text="Text to say")
async def say_admin_slash(interaction: discord.Interaction, text: str):
    if not can_use_mod_commands(interaction.user):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return
    await interaction.response.send_message(text)
    log_command(interaction.user.id, "say_admin", interaction.channel.name)

# Admin mod slash commands (ban, kick, blacklist etc)

@tree.command(name="ban", description="Ban a user")
@app_commands.describe(user="User to ban")
async def ban_slash(interaction: discord.Interaction, user: discord.User):
    if not can_use_mod_commands(interaction.user):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return
    banned_users[str(user.id)] = True
    save_json(BANLIST_FILE, banned_users)
    await interaction.response.send_message(f"{user} has been banned.")
    log_command(interaction.user.id, "ban", interaction.channel.name)

@tree.command(name="unban", description="Unban a user")
@app_commands.describe(user="User to unban")
async def unban_slash(interaction: discord.Interaction, user: discord.User):
    if not can_use_mod_commands(interaction.user):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return
    if str(user.id) in banned_users:
        del banned_users[str(user.id)]
        save_json(BANLIST_FILE, banned_users)
        await interaction.response.send_message(f"{user} has been unbanned.")
    else:
        await interaction.response.send_message("User is not banned.")
    log_command(interaction.user.id, "unban", interaction.channel.name)

@tree.command(name="kick", description="Kick a user")
@app_commands.describe(user="User to kick")
async def kick_slash(interaction: discord.Interaction, user: discord.Member):
    if not can_use_mod_commands(interaction.user):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return
    try:
        await user.kick()
        await interaction.response.send_message(f"{user} has been kicked.")
    except Exception as e:
        await interaction.response.send_message(f"Failed to kick user: {e}")
    log_command(interaction.user.id, "kick", interaction.channel.name)

@tree.command(name="blacklist", description="Blacklist a user")
@app_commands.describe(user="User to blacklist")
async def blacklist_slash(interaction: discord.Interaction, user: discord.User):
    if not can_use_mod_commands(interaction.user):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return
    blacklist[str(user.id)] = True
    save_json(BLACKLIST_FILE, blacklist)
    await interaction.response.send_message(f"{user} has been blacklisted.")
    log_command(interaction.user.id, "blacklist", interaction.channel.name)

@tree.command(name="unblacklist", description="Remove user from blacklist")
@app_commands.describe(user="User to unblacklist")
async def unblacklist_slash(interaction: discord.Interaction, user: discord.User):
    if not can_use_mod_commands(interaction.user):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return
    if str(user.id) in blacklist:
        del blacklist[str(user.id)]
        save_json(BLACKLIST_FILE, blacklist)
        await interaction.response.send_message(f"{user} removed from blacklist.")
    else:
        await interaction.response.send_message("User not in blacklist.")
    log_command(interaction.user.id, "unblacklist", interaction.channel.name)

# User info and avatar slash commands

@tree.command(name="userinfo", description="Get user info")
@app_commands.describe(user="User to get info about")
async def userinfo_slash(interaction: discord.Interaction, user: discord.User = None):
    user = user or interaction.user
    embed = discord.Embed(title=f"User Info - {user}", color=discord.Color.blue())
    embed.set_thumbnail(url=user.avatar.url if user.avatar else user.default_avatar.url)
    embed.add_field(name="ID", value=user.id)
    embed.add_field(name="Bot?", value=user.bot)
    embed.add_field(name="Created At", value=user.created_at.strftime("%Y-%m-%d %H:%M:%S"))
    await interaction.response.send_message(embed=embed)
    log_command(interaction.user.id, "userinfo", interaction.channel.name)

@tree.command(name="avatar", description="Get user avatar")
@app_commands.describe(user="User to get avatar of")
async def avatar_slash(interaction: discord.Interaction, user: discord.User = None):
    user = user or interaction.user
    embed = discord.Embed(title=f"{user}'s Avatar", color=discord.Color.green())
    embed.set_image(url=user.avatar.url if user.avatar else user.default_avatar.url)
    await interaction.response.send_message(embed=embed)
    log_command(interaction.user.id, "avatar", interaction.channel.name)

@tree.command(name="showcommands", description="Show commands you can use")
async def showcommands_slash(interaction: discord.Interaction):
    public_cmds = ["roll_dice", "flip_coin", "eight_ball", "joke", "dadjoke", "rps", "cat", "say", "userinfo", "avatar"]
    admin_cmds = ["ban", "unban", "kick", "blacklist", "unblacklist", "say_admin"]
    if can_use_mod_commands(interaction.user):
        cmds = public_cmds + admin_cmds
    else:
        cmds = public_cmds
    await interaction.response.send_message("Available commands:\n" + ", ".join(cmds))
    log_command(interaction.user.id, "showcommands", interaction.channel.name)

# ========== Bot events ==========

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

# ========== Run ==========

keep_alive()
bot.run(os.getenv("DISCORD_TOKEN"))
