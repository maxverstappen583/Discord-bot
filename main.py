# main.py

import discord
from discord.ext import commands, tasks
from discord import app_commands, ui
import json, random, asyncio, os, datetime
from flask import Flask
from threading import Thread
import aiohttp

# -------------------- Flask Uptime Server --------------------
app = Flask("")

@app.route("/")
def home():
    return "Bot is running!"

def run():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

keep_alive()

# -------------------- Bot Setup --------------------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix='?', intents=intents, help_command=None)
tree = bot.tree

OWNER_ID = 1319292111325106296

# -------------------- JSON Storage --------------------
def load_json(file, default):
    if not os.path.exists(file):
        with open(file, 'w') as f:
            json.dump(default, f)
    with open(file, 'r') as f:
        return json.load(f)

def save_json(file, data):
    with open(file, 'w') as f:
        json.dump(data, f, indent=4)

blacklist = load_json("blacklist.json", {})
admins = load_json("admins.json", {})
pookie = load_json("pookie.json", {})
logs = load_json("logs.json", [])
blocked_words = load_json("blocked_words.json", [])
triggers = load_json("triggers.json", {})
cat_channel_id = load_json("cat_channel.json", {}).get("channel_id")
snipes = {}
esnipes = {}

# -------------------- Utility Functions --------------------
def is_owner(user):
    return user.id == OWNER_ID

def is_admin(user):
    return str(user.id) in admins or is_owner(user)

def is_pookie(user):
    return str(user.id) in pookie

def can_use_mod_commands(user):
    return is_owner(user) or is_admin(user) or is_pookie(user)

def log_command(user, command, channel):
    logs.append({
        "user": str(user.id),
        "command": command,
        "channel": str(channel),
        "time": str(datetime.datetime.now())
    })
    save_json("logs.json", logs)

# -------------------- Events --------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is online!")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(e)

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Save snipes
    if message.content:
        snipes[message.channel.id] = message

    # Blocked words
    for word in blocked_words:
        if word.lower() in message.content.lower():
            await message.delete()
            await message.channel.send("This word is not allowed here.")
            return

    # Auto responder
    for trigger, response in triggers.items():
        if trigger.lower() in message.content.lower():
            await message.channel.send(response)
            return

    await bot.process_commands(message)

@bot.event
async def on_message_edit(before, after):
    if after.content:
        esnipes[after.channel.id] = after

# -------------------- Commands --------------------
# ----- Admin/Pookie Commands -----
@bot.command()
async def add_admin(ctx, user: discord.User):
    if not is_owner(ctx.author):
        return await ctx.send("You cannot use this command.")
    admins[str(user.id)] = True
    save_json("admins.json", admins)
    await ctx.send(f"{user.mention} is now an admin.")

@bot.command()
async def remove_admin(ctx, user: discord.User):
    if not is_owner(ctx.author):
        return await ctx.send("You cannot use this command.")
    admins.pop(str(user.id), None)
    save_json("admins.json", admins)
    await ctx.send(f"{user.mention} is no longer an admin.")

@bot.command()
async def list_admins(ctx):
    mentions = []
    for uid in admins:
        member = ctx.guild.get_member(int(uid))
        if member:
            mentions.append(member.mention)
    if OWNER_ID not in [int(uid) for uid in admins]:
        owner = ctx.guild.get_member(OWNER_ID)
        if owner:
            mentions.append(owner.mention)
    await ctx.send("Admins: " + " ".join(mentions))

@bot.command()
async def add_pookie(ctx, user: discord.User):
    if not is_owner(ctx.author):
        return await ctx.send("Only owner can add pookie.")
    pookie[str(user.id)] = True
    save_json("pookie.json", pookie)
    await ctx.send(f"{user.mention} is now a Pookie.")

@bot.command()
async def remove_pookie(ctx, user: discord.User):
    if not is_owner(ctx.author):
        return await ctx.send("Only owner can remove pookie.")
    pookie.pop(str(user.id), None)
    save_json("pookie.json", pookie)
    await ctx.send(f"{user.mention} is no longer a Pookie.")

@bot.command()
async def list_pookie(ctx):
    mentions = []
    for uid in pookie:
        member = ctx.guild.get_member(int(uid))
        if member:
            mentions.append(member.mention)
    await ctx.send("Pookies: " + " ".join(mentions))

# ----- Moderation Commands -----
@bot.command()
async def ban(ctx, user: discord.User):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("You cannot use this.")
    await ctx.guild.ban(user)
    log_command(ctx.author, "ban", ctx.channel)
    await ctx.send(f"{user.mention} has been banned.")

@bot.command()
async def unban(ctx, user: discord.User):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("You cannot use this.")
    bans = await ctx.guild.bans()
    for b in bans:
        if b.user.id == user.id:
            await ctx.guild.unban(user)
            await ctx.send(f"{user.mention} has been unbanned.")
            log_command(ctx.author, "unban", ctx.channel)
            return

@bot.command()
async def kick(ctx, user: discord.User):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("You cannot use this.")
    await ctx.guild.kick(user)
    log_command(ctx.author, "kick", ctx.channel)
    await ctx.send(f"{user.mention} has been kicked.")

@bot.command()
async def blacklist_user(ctx, user: discord.User):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("You cannot use this.")
    blacklist[str(user.id)] = True
    save_json("blacklist.json", blacklist)
    await ctx.send(f"{user.mention} has been blacklisted.")

@bot.command()
async def unblacklist_user(ctx, user: discord.User):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("You cannot use this.")
    blacklist.pop(str(user.id), None)
    save_json("blacklist.json", blacklist)
    await ctx.send(f"{user.mention} has been removed from blacklist.")

# ----- Fun Commands -----
@bot.command()
async def cat(ctx):
    CAT_API_KEY = os.environ.get("CAT_API_KEY", "YOUR_CAT_API_KEY")
    url = f"https://api.thecatapi.com/v1/images/search"
    headers = {"x-api-key": CAT_API_KEY}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            if data:
                await ctx.send(data[0]['url'])

@bot.command()
async def eightball(ctx, *, question):
    responses = ["Yes", "No", "Maybe", "Definitely", "Ask again later"]
    await ctx.send(f"🎱 {random.choice(responses)}")

@bot.command()
async def joke(ctx):
    jokes = ["Why did the chicken cross the road? To get to the other side!", "I told my computer I needed a break, it said 'No problem, I'll go to sleep.'"]
    await ctx.send(random.choice(jokes))

@bot.command()
async def dadjoke(ctx):
    dad_jokes = ["I'm reading a book on anti-gravity. It's impossible to put down!", "I would tell you a joke about construction, but I'm still working on it."]
    await ctx.send(random.choice(dad_jokes))

@bot.command()
async def rps(ctx, choice):
    options = ["rock", "paper", "scissors"]
    bot_choice = random.choice(options)
    await ctx.send(f"You chose {choice}, I chose {bot_choice}")

@bot.command()
async def coinflip(ctx):
    await ctx.send(random.choice(["Heads", "Tails"]))

@bot.command()
async def rolldice(ctx, sides: int = 6):
    await ctx.send(f"🎲 You rolled a {random.randint(1, sides)}")

# ----- Say Commands -----
@bot.command()
async def say(ctx, *, message):
    for word in blocked_words:
        if word.lower() in message.lower():
            return await ctx.send("This word is blocked.")
    await ctx.send(message)

@bot.command()
async def say_admin(ctx, *, message):
    if not can_use_mod_commands(ctx.author):
        return await ctx.send("You cannot use this command.")
    await ctx.send(message)

# ----- User Info & Avatar -----
@bot.command()
async def avatar(ctx, user: discord.User = None):
    user = user or ctx.author
    await ctx.send(user.avatar.url)

@bot.command()
async def userinfo(ctx, user: discord.User = None):
    user = user or ctx.author
    embed = discord.Embed(title=f"Info for {user}", color=discord.Color.blue())
    embed.add_field(name="ID", value=user.id)
    embed.add_field(name="Name", value=user.name)
    embed.add_field(name="Discriminator", value=user.discriminator)
    embed.set_thumbnail(url=user.avatar.url)
    await ctx.send(embed=embed)

# ----- Show Commands -----
@bot.command()
async def showcommands(ctx):
    cmds = [c.name for c in bot.commands if c.enabled]
    if not can_use_mod_commands(ctx.author):
        cmds = [c for c in cmds if c not in ["ban","kick","unblacklist_user","blacklist_user","add_admin","remove_admin","add_pookie","remove_pookie"]]
    await ctx.send("Available commands: " + ", ".join(cmds))

# -------------------- Slash Commands --------------------
@tree.command(name="avatar", description="Get user's avatar")
async def avatar_slash(interaction: discord.Interaction, user: discord.User = None):
    user = user or interaction.user
    await interaction.response.send_message(user.avatar.url)

# ----- Snipe/E-Snipe -----
class SnipeView(ui.View):
    def __init__(self, messages):
        super().__init__()
        self.messages = messages
        self.index = 0

    @ui.button(label="⬅️", style=discord.ButtonStyle.blurple)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        self.index = max(self.index-1, 0)
        await interaction.response.edit_message(content=f"{self.messages[self.index].author}: {self.messages[self.index].content}")

    @ui.button(label="➡️", style=discord.ButtonStyle.blurple)
    async def forward(self, interaction: discord.Interaction, button: ui.Button):
        self.index = min(self.index+1, len(self.messages)-1)
        await interaction.response.edit_message(content=f"{self.messages[self.index].author}: {self.messages[self.index].content}")

@bot.command()
async def snipe(ctx):
    if ctx.channel.id not in snipes:
        return await ctx.send("No messages to snipe.")
    view = SnipeView([snipes[ctx.channel.id]])
    await ctx.send(f"{snipes[ctx.channel.id].author}: {snipes[ctx.channel.id].content}", view=view)

@bot.command()
async def esnipe(ctx):
    if ctx.channel.id not in esnipes:
        return await ctx.send("No edited messages to snipe.")
    view = SnipeView([esnipes[ctx.channel.id]])
    await ctx.send(f"{esnipes[ctx.channel.id].author}: {esnipes[ctx.channel.id].content}", view=view)

# ----- Auto-Responder -----
@bot.command()
async def showtrigger(ctx):
    msg = ""
    for trigger, response in triggers.items():
        msg += f"Trigger: {trigger} | Response: {response}\n"
    await ctx.send(msg or "No triggers set.")

@bot.command()
async def addtrigger(ctx, trigger, *, response):
    triggers[trigger] = response
    save_json("triggers.json", triggers)
    await ctx.send(f"Added trigger: {trigger}")

@bot.command()
async def removetrigger(ctx, trigger):
    triggers.pop(trigger, None)
    save_json("triggers.json", triggers)
    await ctx.send(f"Removed trigger: {trigger}")

# -------------------- Daily Cat Posting --------------------
@tasks.loop(hours=24)
async def daily_cat():
    if cat_channel_id:
        channel = bot.get_channel(int(cat_channel_id))
        if channel:
            await cat(ctx=channel)

@daily_cat.before_loop
async def before_daily_cat():
    await bot.wait_until_ready()
    now = datetime.datetime.now()
    target = now.replace(hour=11, minute=0, second=0, microsecond=0)
    if now > target:
        target += datetime.timedelta(days=1)
    await asyncio.sleep((target - now).total_seconds())

daily_cat.start()

# -------------------- Run Bot --------------------
TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN")
bot.run(TOKEN)
