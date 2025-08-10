import os
import json
import random
import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask
from threading import Thread
import aiohttp

# --------- Constants ---------

OWNER_ID = 1319292111325106296
PREFIX = "?"
LOGS_FILE = "logs.json"
ADMINS_FILE = "admins.json"
POOKIE_FILE = "pookie.json"
BLACKLIST_FILE = "blacklist.json"
BLOCKED_WORDS_FILE = "blocked_words.json"

# --------- Load or initialize JSON data ---------

def load_json(filename):
    try:
        with open(filename, "r") as f:
            return json.load(f)
    except:
        return {}

def save_json(filename, data):
    with open(filename, "w") as f:
        json.dump(data, f, indent=4)

admins = load_json(ADMINS_FILE)    # {user_id_str: true}
pookie_users = load_json(POOKIE_FILE)
blacklist = load_json(BLACKLIST_FILE)
blocked_words = load_json(BLOCKED_WORDS_FILE)
logs = load_json(LOGS_FILE)
if "entries" not in logs:
    logs["entries"] = []

# --------- Bot and intents ---------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)
tree = bot.tree

# --------- Helpers ---------

def is_owner(user):
    return user.id == OWNER_ID

def is_admin(user):
    return str(user.id) in admins or is_owner(user) or is_pookie(user)

def is_pookie(user):
    return str(user.id) in pookie_users or is_owner(user)

def can_use_mod_commands(user):
    return is_admin(user) or is_pookie(user) or is_owner(user)

def is_blacklisted(user):
    return str(user.id) in blacklist

def log_command(user_id, command, channel):
    logs["entries"].append({
        "user_id": str(user_id),
        "command": command,
        "channel": channel,
        "timestamp": discord.utils.utcnow().isoformat()
    })
    # keep last 100 logs only
    logs["entries"] = logs["entries"][-100:]
    save_json(LOGS_FILE, logs)

# --------- Blocked words bypass check ---------

def message_contains_blocked_word(message_content):
    content = message_content.lower()
    content_clean = "".join(c for c in content if c.isalnum())
    for bw in blocked_words.keys():
        bw_clean = "".join(c for c in bw if c.isalnum())
        if bw_clean in content_clean:
            return True
    return False

# --------- Commands ---------

# Admin commands for managing admins and pookie

@bot.command(name="addadmin")
async def addadmin(ctx, user: discord.User):
    if not is_owner(ctx.author):
        return await ctx.send("Only owner can add admins.")
    admins[str(user.id)] = True
    save_json(ADMINS_FILE, admins)
    await ctx.send(f"{user.mention} is now an admin.")
    log_command(ctx.author.id, "addadmin", ctx.channel.name)

@bot.command(name="removeadmin")
async def removeadmin(ctx, user: discord.User):
    if not is_owner(ctx.author):
        return await ctx.send("Only owner can remove admins.")
    if str(user.id) in admins:
        del admins[str(user.id)]
        save_json(ADMINS_FILE, admins)
        await ctx.send(f"{user.mention} removed from admins.")
    else:
        await ctx.send("User is not an admin.")
    log_command(ctx.author.id, "removeadmin", ctx.channel.name)

@bot.command(name="showadmins")
async def showadmins(ctx):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("No permission.")
    mentions = [f"<@{uid}>" for uid in admins.keys()]
    mentions.append(f"<@{OWNER_ID}> (Owner)")
    await ctx.send("Admins:\n" + " ".join(mentions))
    log_command(ctx.author.id, "showadmins", ctx.channel.name)

@bot.command(name="addpookie")
async def addpookie(ctx, user: discord.User):
    if not is_owner(ctx.author):
        return await ctx.send("Only owner can add pookie users.")
    pookie_users[str(user.id)] = True
    save_json(POOKIE_FILE, pookie_users)
    await ctx.send(f"{user.mention} is now a pookie user.")
    log_command(ctx.author.id, "addpookie", ctx.channel.name)

@bot.command(name="removepookie")
async def removepookie(ctx, user: discord.User):
    if not is_owner(ctx.author):
        return await ctx.send("Only owner can remove pookie users.")
    if str(user.id) in pookie_users:
        del pookie_users[str(user.id)]
        save_json(POOKIE_FILE, pookie_users)
        await ctx.send(f"{user.mention} removed from pookie users.")
    else:
        await ctx.send("User is not a pookie user.")
    log_command(ctx.author.id, "removepookie", ctx.channel.name)

@bot.command(name="listpookie")
async def listpookie(ctx):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("No permission.")
    mentions = [f"<@{uid}>" for uid in pookie_users.keys()]
    await ctx.send("Pookie users:\n" + " ".join(mentions) if mentions else "No pookie users found.")
    log_command(ctx.author.id, "listpookie", ctx.channel.name)

# Blacklist commands

@bot.command(name="blacklist")
async def blacklist_user(ctx, user: discord.User):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("No permission.")
    blacklist[str(user.id)] = True
    save_json(BLACKLIST_FILE, blacklist)
    await ctx.send(f"{user.mention} is blacklisted.")
    log_command(ctx.author.id, "blacklist", ctx.channel.name)

@bot.command(name="unblacklist")
async def unblacklist_user(ctx, user: discord.User):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("No permission.")
    if str(user.id) in blacklist:
        del blacklist[str(user.id)]
        save_json(BLACKLIST_FILE, blacklist)
        await ctx.send(f"{user.mention} removed from blacklist.")
    else:
        await ctx.send("User is not blacklisted.")
    log_command(ctx.author.id, "unblacklist", ctx.channel.name)

# Ban, Unban, Kick commands

@bot.command(name="ban")
async def ban(ctx, user: discord.User, *, reason=None):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("No permission.")
    if is_owner(user):
        return await ctx.send("Cannot ban the owner.")
    try:
        await ctx.guild.ban(user, reason=reason)
        await ctx.send(f"{user} has been banned.")
        log_command(ctx.author.id, "ban", ctx.channel.name)
    except Exception as e:
        await ctx.send(f"Error banning user: {e}")

@bot.command(name="unban")
async def unban(ctx, user: discord.User):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("No permission.")
    banned_users = await ctx.guild.bans()
    user_found = None
    for ban_entry in banned_users:
        if ban_entry.user.id == user.id:
            user_found = ban_entry.user
            break
    if user_found:
        await ctx.guild.unban(user_found)
        await ctx.send(f"{user} has been unbanned.")
        log_command(ctx.author.id, "unban", ctx.channel.name)
    else:
        await ctx.send("User is not banned.")

@bot.command(name="kick")
async def kick(ctx, user: discord.User, *, reason=None):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("No permission.")
    try:
        await ctx.guild.kick(user, reason=reason)
        await ctx.send(f"{user} has been kicked.")
        log_command(ctx.author.id, "kick", ctx.channel.name)
    except Exception as e:
        await ctx.send(f"Error kicking user: {e}")

# Blocked words commands

@bot.command(name="addblockedword")
async def addblockedword(ctx, *, word):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("No permission.")
    blocked_words[word.lower()] = True
    save_json(BLOCKED_WORDS_FILE, blocked_words)
    await ctx.send(f"Added blocked word: {word}")
    log_command(ctx.author.id, "addblockedword", ctx.channel.name)

@bot.command(name="removeblockedword")
async def removeblockedword(ctx, *, word):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("No permission.")
    if word.lower() in blocked_words:
        del blocked_words[word.lower()]
        save_json(BLOCKED_WORDS_FILE, blocked_words)
        await ctx.send(f"Removed blocked word: {word}")
    else:
        await ctx.send("Word not found in blocked list.")
    log_command(ctx.author.id, "removeblockedword", ctx.channel.name)

@bot.command(name="showblockedwords")
async def showblockedwords(ctx):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("No permission.")
    words = ", ".join(blocked_words.keys()) if blocked_words else "No blocked words."
    await ctx.send(f"Blocked words:\n{words}")
    log_command(ctx.author.id, "showblockedwords", ctx.channel.name)

# Logs command

@bot.command(name="logs")
async def logs_cmd(ctx, limit: int = 10):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("No permission.")
    entries = logs.get("entries", [])[-limit:]
    embed = discord.Embed(title=f"Last {limit} logs", color=discord.Color.orange())
    for entry in reversed(entries):
        user = bot.get_user(int(entry["user_id"]))
        user_name = user.name if user else entry["user_id"]
        embed.add_field(name=f"{entry['command']} by {user_name}",
                        value=f"Channel: {entry['channel']}\nAt: {entry['timestamp']}",
                        inline=False)
    await ctx.send(embed=embed)

# Say commands

@bot.command(name="say")
async def say(ctx, *, message):
    # block pings in public say
    if "@everyone" in message or "@here" in message or "<@&" in message or "<@" in message:
        return await ctx.send("You cannot mention people in this command.")
    if message_contains_blocked_word(message):
        return await ctx.send("Message contains blocked words.")
    await ctx.send(message)
    log_command(ctx.author.id, "say", ctx.channel.name)

@bot.command(name="say_admin")
async def say_admin(ctx, *, message):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("No permission.")
    # admin say can ping
    await ctx.send(message)
    log_command(ctx.author.id, "say_admin", ctx.channel.name)

# Fun commands

@bot.command(name="8ball")
async def eight_ball(ctx, *, question):
    responses = [
        "It is certain.", "Without a doubt.", "You may rely on it.",
        "Ask again later.", "Cannot predict now.", "Don't count on it.",
        "Very doubtful."
    ]
    answer = random.choice(responses)
    await ctx.send(f"🎱 Question: {question}\nAnswer: {answer}")
    log_command(ctx.author.id, "8ball", ctx.channel.name)

@bot.command(name="joke")
async def joke(ctx):
    jokes = [
        "Why don't scientists trust atoms? Because they make up everything!",
        "I told my wife she was drawing her eyebrows too high. She looked surprised.",
        "What do you call fake spaghetti? An impasta!"
    ]
    await ctx.send(random.choice(jokes))
    log_command(ctx.author.id, "joke", ctx.channel.name)

@bot.command(name="dadjoke")
async def dadjoke(ctx):
    dad_jokes = [
        "I'm reading a book on anti-gravity. It's impossible to put down!",
        "Why did the scarecrow win an award? Because he was outstanding in his field!",
        "I would tell you a joke about construction, but I'm still working on it."
    ]
    await ctx.send(random.choice(dad_jokes))
    log_command(ctx.author.id, "dadjoke", ctx.channel.name)

@bot.command(name="cat")
async def cat(ctx):
    api_key = os.getenv("CAT_API_KEY")
    if not api_key:
        return await ctx.send("CAT_API_KEY is not set.")
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.thecatapi.com/v1/images/search?api_key={api_key}") as resp:
            if resp.status != 200:
                return await ctx.send("Failed to get cat image.")
            data = await resp.json()
            await ctx.send(data[0]["url"])
    log_command(ctx.author.id, "cat", ctx.channel.name)

@bot.command(name="rps")
async def rps(ctx, choice: str):
    choices = ["rock", "paper", "scissors"]
    choice = choice.lower()
    if choice not in choices:
        return await ctx.send(f"Choose one: {', '.join(choices)}")
    bot_choice = random.choice(choices)
    result = ""
    if choice == bot_choice:
        result = "It's a tie!"
    elif (choice == "rock" and bot_choice == "scissors") or \
         (choice == "scissors" and bot_choice == "paper") or \
         (choice == "paper" and bot_choice == "rock"):
        result = "You win!"
    else:
        result = "You lose!"
    await ctx.send(f"You chose {choice}, bot chose {bot_choice}. {result}")
    log_command(ctx.author.id, "rps", ctx.channel.name)

@bot.command(name="flipcoin")
async def flipcoin(ctx):
    result = random.choice(["Heads", "Tails"])
    await ctx.send(f"The coin landed on: {result}")
    log_command(ctx.author.id, "flipcoin", ctx.channel.name)

@bot.command(name="rolldice")
async def rolldice(ctx, sides: int = 6):
    if sides < 2:
        return await ctx.send("Dice must have at least 2 sides.")
    result = random.randint(1, sides)
    await ctx.send(f"🎲 You rolled a {result} on a {sides}-sided dice.")
    log_command(ctx.author.id, "rolldice", ctx.channel.name)

# User info and avatar commands

@bot.command(name="userinfo")
async def userinfo(ctx, user: discord.User = None):
    user = user or ctx.author
    embed = discord.Embed(title=f"User Info - {user}", color=discord.Color.blue())
    embed.set_thumbnail(url=user.avatar.url if user.avatar else user.default_avatar.url)
    embed.add_field(name="ID", value=user.id)
    embed.add_field(name="Bot?", value=user.bot)
    embed.add_field(name="Created At", value=user.created_at.strftime("%Y-%m-%d %H:%M:%S"))
    embed.add_field(name="Joined At", value=user.joined_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(user, "joined_at") and user.joined_at else "N/A")
    await ctx.send(embed=embed)
    log_command(ctx.author.id, "userinfo", ctx.channel.name)

@bot.command(name="avatar")
async def avatar(ctx, user: discord.User = None):
    user = user or ctx.author
    embed = discord.Embed(title=f"{user}'s Avatar", color=discord.Color.green())
    embed.set_image(url=user.avatar.url if user.avatar else user.default_avatar.url)
    await ctx.send(embed=embed)
    log_command(ctx.author.id, "avatar", ctx.channel.name)

# Showcommands command that shows commands based on role

@bot.command(name="showcommands")
async def showcommands(ctx):
    # Build list depending on permissions
    cmds = []
    # Everyone commands
    cmds += ["8ball", "joke", "dadjoke", "cat", "rps", "flipcoin", "rolldice", "userinfo", "avatar", "showcommands", "say"]
    # Admin or pookie or owner commands
    if can_use_mod_commands(ctx.author):
        cmds += ["ban", "unban", "kick", "blacklist", "unblacklist", "addadmin", "removeadmin", "showadmins",
                 "addpookie", "removepookie", "listpookie", "addblockedword", "removeblockedword", "showblockedwords",
                 "logs", "say_admin"]
    await ctx.send("Commands you can use:\n" + "\n".join(sorted(cmds)))

# --------- Event: check blacklist and blocked words ---------

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if is_blacklisted(message.author):
        try:
            await message.delete()
            await message.channel.send(f"{message.author.mention}, you are blacklisted and cannot use commands.")
        except:
            pass
        return
    # Blocked words detection with bypass checks only for normal users (not admins/pookie/owner)
    if not can_use_mod_commands(message.author):
        if message_contains_blocked_word(message.content):
            try:
                await message.delete()
                await message.channel.send(f"{message.author.mention} This word is not allowed here.")
            except:
                pass
            return
    await bot.process_commands(message)

# --------- Slash Commands ---------

# Utility check for slash commands permission

async def slash_can_use_mod_commands(interaction: discord.Interaction) -> bool:
    user = interaction.user
    return is_admin(user) or is_pookie(user) or is_owner(user)

def slash_blacklist_check():
    async def predicate(interaction: discord.Interaction):
        user = interaction.user
        if is_blacklisted(user):
            await interaction.response.send_message("You are blacklisted.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

def slash_mod_check():
    async def predicate(interaction: discord.Interaction):
        if not await slash_can_use_mod_commands(interaction):
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

@tree.command(name="showcommands", description="Show commands you can use")
@slash_blacklist_check()
async def slash_showcommands(interaction: discord.Interaction):
    cmds = []
    cmds += ["8ball", "joke", "dadjoke", "cat", "rps", "flipcoin", "rolldice", "userinfo", "avatar", "showcommands", "say"]
    if await slash_can_use_mod_commands(interaction):
        cmds += ["ban", "unban", "kick", "blacklist", "unblacklist", "addadmin", "removeadmin", "showadmins",
                 "addpookie", "removepookie", "listpookie", "addblockedword", "removeblockedword", "showblockedwords",
                 "logs", "say_admin"]
    await interaction.response.send_message("Commands you can use:\n" + "\n".join(sorted(cmds)), ephemeral=True)

@tree.command(name="ban", description="Ban a user")
@slash_mod_check()
@slash_blacklist_check()
@app_commands.describe(user="User to ban", reason="Reason for ban")
async def slash_ban(interaction: discord.Interaction, user: discord.User, reason: str = None):
    if is_owner(user):
        await interaction.response.send_message("Cannot ban the owner.", ephemeral=True)
        return
    try:
        await interaction.guild.ban(user, reason=reason)
        await interaction.response.send_message(f"{user} has been banned.")
        log_command(interaction.user.id, "ban", interaction.channel.name if interaction.channel else "unknown")
    except Exception as e:
        await interaction.response.send_message(f"Failed to ban user: {e}", ephemeral=True)

@tree.command(name="unban", description="Unban a user")
@slash_mod_check()
@slash_blacklist_check()
@app_commands.describe(user="User to unban")
async def slash_unban(interaction: discord.Interaction, user: discord.User):
    banned_users = await interaction.guild.bans()
    user_found = None
    for ban_entry in banned_users:
        if ban_entry.user.id == user.id:
            user_found = ban_entry.user
            break
    if user_found:
        await interaction.guild.unban(user_found)
        await interaction.response.send_message(f"{user} has been unbanned.")
        log_command(interaction.user.id, "unban", interaction.channel.name if interaction.channel else "unknown")
    else:
        await interaction.response.send_message("User is not banned.", ephemeral=True)

@tree.command(name="kick", description="Kick a user")
@slash_mod_check()
@slash_blacklist_check()
@app_commands.describe(user="User to kick", reason="Reason for kick")
async def slash_kick(interaction: discord.Interaction, user: discord.User, reason: str = None):
    try:
        await interaction.guild.kick(user, reason=reason)
        await interaction.response.send_message(f"{user} has been kicked.")
        log_command(interaction.user.id, "kick", interaction.channel.name if interaction.channel else "unknown")
    except Exception as e:
        await interaction.response.send_message(f"Failed to kick user: {e}", ephemeral=True)

@tree.command(name="blacklist", description="Blacklist a user")
@slash_mod_check()
@slash_blacklist_check()
@app_commands.describe(user="User to blacklist")
async def slash_blacklist(interaction: discord.Interaction, user: discord.User):
    blacklist[str(user.id)] = True
    save_json(BLACKLIST_FILE, blacklist)
    await interaction.response.send_message(f"{user} is blacklisted.")
    log_command(interaction.user.id, "blacklist", interaction.channel.name if interaction.channel else "unknown")

@tree.command(name="unblacklist", description="Remove user from blacklist")
@slash_mod_check()
@slash_blacklist_check()
@app_commands.describe(user="User to remove from blacklist")
async def slash_unblacklist(interaction: discord.Interaction, user: discord.User):
    if str(user.id) in blacklist:
        del blacklist[str(user.id)]
        save_json(BLACKLIST_FILE, blacklist)
        await interaction.response.send_message(f"{user} removed from blacklist.")
    else:
        await interaction.response.send_message("User is not blacklisted.", ephemeral=True)
    log_command(interaction.user.id, "unblacklist", interaction.channel.name if interaction.channel else "unknown")

@tree.command(name="addadmin", description="Add an admin")
@slash_mod_check()
@slash_blacklist_check()
@app_commands.describe(user="User to make admin")
async def slash_addadmin(interaction: discord.Interaction, user: discord.User):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Only owner can add admins.", ephemeral=True)
        return
    admins[str(user.id)] = True
    save_json(ADMINS_FILE, admins)
    await interaction.response.send_message(f"{user} is now an admin.")
    log_command(interaction.user.id, "addadmin", interaction.channel.name if interaction.channel else "unknown")

@tree.command(name="removeadmin", description="Remove an admin")
@slash_mod_check()
@slash_blacklist_check()
@app_commands.describe(user="User to remove admin")
async def slash_removeadmin(interaction: discord.Interaction, user: discord.User):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Only owner can remove admins.", ephemeral=True)
        return
    if str(user.id) in admins:
        del admins[str(user.id)]
        save_json(ADMINS_FILE, admins)
        await interaction.response.send_message(f"{user} removed from admins.")
    else:
        await interaction.response.send_message("User is not an admin.", ephemeral=True)
    log_command(interaction.user.id, "removeadmin", interaction.channel.name if interaction.channel else "unknown")

@tree.command(name="showadmins", description="Show all admins")
@slash_mod_check()
@slash_blacklist_check()
async def slash_showadmins(interaction: discord.Interaction):
    mentions = [f"<@{uid}>" for uid in admins.keys()]
    mentions.append(f"<@{OWNER_ID}> (Owner)")
    await interaction.response.send_message("Admins:\n" + " ".join(mentions))

@tree.command(name="addpookie", description="Add a pookie user")
@slash_mod_check()
@slash_blacklist_check()
@app_commands.describe(user="User to add to pookie")
async def slash_addpookie(interaction: discord.Interaction, user: discord.User):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Only owner can add pookie users.", ephemeral=True)
        return
    pookie_users[str(user.id)] = True
    save_json(POOKIE_FILE, pookie_users)
    await interaction.response.send_message(f"{user} is now a pookie user.")
    log_command(interaction.user.id, "addpookie", interaction.channel.name if interaction.channel else "unknown")

@tree.command(name="removepookie", description="Remove a pookie user")
@slash_mod_check()
@slash_blacklist_check()
@app_commands.describe(user="User to remove from pookie")
async def slash_removepookie(interaction: discord.Interaction, user: discord.User):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Only owner can remove pookie users.", ephemeral=True)
        return
    if str(user.id) in pookie_users:
        del pookie_users[str(user.id)]
        save_json(POOKIE_FILE, pookie_users)
        await interaction.response.send_message(f"{user} removed from pookie users.")
    else:
        await interaction.response.send_message("User is not a pookie user.", ephemeral=True)
    log_command(interaction.user.id, "removepookie", interaction.channel.name if interaction.channel else "unknown")

@tree.command(name="listpookie", description="List pookie users")
@slash_mod_check()
@slash_blacklist_check()
async def slash_listpookie(interaction: discord.Interaction):
    mentions = [f"<@{uid}>" for uid in pookie_users.keys()]
    await interaction.response.send_message("Pookie users:\n" + " ".join(mentions) if mentions else "No pookie users found.")

@tree.command(name="logs", description="Show last 10 logs")
@slash_mod_check()
@slash_blacklist_check()
@app_commands.describe(limit="Number of logs to show")
async def slash_logs(interaction: discord.Interaction, limit: int = 10):
    entries = logs.get("entries", [])[-limit:]
    embed = discord.Embed(title=f"Last {limit} logs", color=discord.Color.orange())
    for entry in reversed(entries):
        user = bot.get_user(int(entry["user_id"]))
        user_name = user.name if user else entry["user_id"]
        embed.add_field(name=f"{entry['command']} by {user_name}",
                        value=f"Channel: {entry['channel']}\nAt: {entry['timestamp']}",
                        inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="say", description="Say something (no pings)")
@slash_blacklist_check()
@app_commands.describe(message="Message to say")
async def slash_say(interaction: discord.Interaction, message: str):
    if "@everyone" in message or "@here" in message or "<@&" in message or "<@" in message:
        await interaction.response.send_message("You cannot mention people in this command.", ephemeral=True)
        return
    if message_contains_blocked_word(message):
        await interaction.response.send_message("Message contains blocked words.", ephemeral=True)
        return
    await interaction.response.send_message(message)
    log_command(interaction.user.id, "say", interaction.channel.name if interaction.channel else "unknown")

@tree.command(name="say_admin", description="Say something (admin only, can ping)")
@slash_mod_check()
@slash_blacklist_check()
@app_commands.describe(message="Message to say")
async def slash_say_admin(interaction: discord.Interaction, message: str):
    await interaction.response.send_message(message)
    log_command(interaction.user.id, "say_admin", interaction.channel.name if interaction.channel else "unknown")

@tree.command(name="userinfo", description="Show user info")
@slash_blacklist_check()
@app_commands.describe(user="User to get info of (optional)")
async def slash_userinfo(interaction: discord.Interaction, user: discord.User = None):
    user = user or interaction.user
    embed = discord.Embed(title=f"User Info - {user}", color=discord.Color.blue())
    embed.set_thumbnail(url=user.avatar.url if user.avatar else user.default_avatar.url)
    embed.add_field(name="ID", value=user.id)
    embed.add_field(name="Bot?", value=user.bot)
    embed.add_field(name="Created At", value=user.created_at.strftime("%Y-%m-%d %H:%M:%S"))
    embed.add_field(name="Joined At", value=user.joined_at.strftime("%Y-%m-%d %H:%M:%S") if user.joined_at else "N/A")
    await interaction.response.send_message(embed=embed)

@tree.command(name="avatar", description="Show user avatar")
@slash_blacklist_check()
@app_commands.describe(user="User to get avatar of (optional)")
async def slash_avatar(interaction: discord.Interaction, user: discord.User = None):
    user = user or interaction.user
    embed = discord.Embed(title=f"{user}'s Avatar", color=discord.Color.green())
    embed.set_image(url=user.avatar.url if user.avatar else user.default_avatar.url)
    await interaction.response.send_message(embed=embed)

# Fun slash commands

@tree.command(name="8ball", description="Ask the magic 8ball")
@slash_blacklist_check()
@app_commands.describe(question="Your question")
async def slash_8ball(interaction: discord.Interaction, question: str):
    responses = [
        "It is certain.", "Without a doubt.", "You may rely on it.",
        "Ask again later.", "Cannot predict now.", "Don't count on it.",
        "Very doubtful."
    ]
    answer = random.choice(responses)
    await interaction.response.send_message(f"🎱 Question: {question}\nAnswer: {answer}")

@tree.command(name="joke", description="Get a random joke")
@slash_blacklist_check()
async def slash_joke(interaction: discord.Interaction):
    jokes = [
        "Why don't scientists trust atoms? Because they make up everything!",
        "I told my wife she was drawing her eyebrows too high. She looked surprised.",
        "What do you call fake spaghetti? An impasta!"
    ]
    await interaction.response.send_message(random.choice(jokes))

@tree.command(name="dadjoke", description="Get a random dad joke")
@slash_blacklist_check()
async def slash_dadjoke(interaction: discord.Interaction):
    dad_jokes = [
        "I'm reading a book on anti-gravity. It's impossible to put down!",
        "Why did the scarecrow win an award? Because he was outstanding in his field!",
        "I would tell you a joke about construction, but I'm still working on it."
    ]
    await interaction.response.send_message(random.choice(dad_jokes))

@tree.command(name="cat", description="Get a random cat image")
@slash_blacklist_check()
async def slash_cat(interaction: discord.Interaction):
    api_key = os.getenv("CAT_API_KEY")
    if not api_key:
        await interaction.response.send_message("CAT_API_KEY is not set.", ephemeral=True)
        return
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.thecatapi.com/v1/images/search?api_key={api_key}") as resp:
            if resp.status != 200:
                await interaction.response.send_message("Failed to get cat image.", ephemeral=True)
                return
            data = await resp.json()
            await interaction.response.send_message(data[0]["url"])

@tree.command(name="rps", description="Play Rock Paper Scissors")
@slash_blacklist_check()
@app_commands.describe(choice="Your choice: rock, paper or scissors")
async def slash_rps(interaction: discord.Interaction, choice: str):
    choices = ["rock", "paper", "scissors"]
    choice = choice.lower()
    if choice not in choices:
        await interaction.response.send_message(f"Choose one: {', '.join(choices)}", ephemeral=True)
        return
    bot_choice = random.choice(choices)
    result = ""
    if choice == bot_choice:
        result = "It's a tie!"
    elif (choice == "rock" and bot_choice == "scissors") or \
         (choice == "scissors" and bot_choice == "paper") or \
         (choice == "paper" and bot_choice == "rock"):
        result = "You win!"
    else:
        result = "You lose!"
    await interaction.response.send_message(f"You chose {choice}, bot chose {bot_choice}. {result}")

@tree.command(name="flipcoin", description="Flip a coin")
@slash_blacklist_check()
async def slash_flipcoin(interaction: discord.Interaction):
    result = random.choice(["Heads", "Tails"])
    await interaction.response.send_message(f"The coin landed on: {result}")

@tree.command(name="rolldice", description="Roll a dice")
@slash_blacklist_check()
@app_commands.describe(sides="Number of sides on the dice")
async def slash_rolldice(interaction: discord.Interaction, sides: int = 6):
    if sides < 2:
        await interaction.response.send_message("Dice must have at least 2 sides.", ephemeral=True)
        return
    result = random.randint(1, sides)
    await interaction.response.send_message(f"🎲 You rolled a {result} on a {sides}-sided dice.")

# --------- Flask Uptime Server for Render ---------

app = Flask("")

@app.route("/")
def home():
    return "Bot is running."

def run():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# --------- On ready event ---------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")
    await tree.sync()
    print("Slash commands synced.")

# --------- Run everything ---------

keep_alive()
bot.run(os.getenv("DISCORD_TOKEN"))
