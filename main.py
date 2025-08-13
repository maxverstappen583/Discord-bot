# main.py
import discord, random, json, aiohttp, os, asyncio, datetime, threading, flask
from discord.ext import commands, tasks
from discord import app_commands, Interaction, Embed, ButtonStyle
from discord.ui import Button, View, Modal, TextInput

# -------------------- Flask Keep-alive --------------------
app = flask.Flask('')
@app.route('/')
def home(): return "Bot is alive!"
threading.Thread(target=lambda: app.run(host='0.0.0.0', port=8080)).start()

# -------------------- Bot Setup --------------------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix='?', intents=intents)
tree = bot.tree

OWNER_ID = 1319292111325106296
DATA_FILE = "data.json"

# -------------------- JSON Storage --------------------
default_data = {
    "admins": [],
    "pookie_users": [],
    "pookie_keys": [],
    "blacklist": [],
    "blocked_words": [],
    "logs": [],
    "daily_cat_channel": None,
    "auto_responders": [],
    "snipes": {"deleted": [], "edited": []}
}

if not os.path.exists(DATA_FILE):
    with open(DATA_FILE,'w') as f: json.dump(default_data,f,indent=4)

def load_data(): 
    with open(DATA_FILE,'r') as f: return json.load(f)
def save_data(d): 
    with open(DATA_FILE,'w') as f: json.dump(d,f,indent=4)

data = load_data()

# -------------------- Utility Functions --------------------
def is_owner(uid): return uid == OWNER_ID
def is_admin(uid): return uid in data["admins"] or uid in data["pookie_users"] or is_owner(uid)
def is_pookie(uid): return uid in data["pookie_users"]

def log_command(user, action, channel, details=None):
    entry = {
        "user_id": user.id,
        "user_name": str(user),
        "action": action,
        "details": details,
        "channel": str(channel),
        "timestamp": datetime.datetime.utcnow().isoformat()
    }
    data["logs"].append(entry)
    save_data(data)

def log_moderation(action, ctx, target_user, reason=None):
    log_command(ctx.author, action, ctx.channel, f"Target:{target_user}|Reason:{reason}")

# -------------------- Events --------------------
@bot.event
async def on_message_delete(m):
    if m.author.bot: return
    data["snipes"]["deleted"].append({
        "author":str(m.author), "content":m.content,
        "channel":str(m.channel),
        "timestamp":datetime.datetime.utcnow().isoformat()
    })
    data["snipes"]["deleted"]=data["snipes"]["deleted"][-10:]
    log_command(m.author,"message_deleted",m.channel,m.content)
    save_data(data)

@bot.event
async def on_message_edit(before,after):
    if before.author.bot or before.content==after.content: return
    data["snipes"]["edited"].append({
        "author":str(before.author), "before":before.content,
        "after":after.content, "channel":str(before.channel),
        "timestamp":datetime.datetime.utcnow().isoformat()
    })
    data["snipes"]["edited"]=data["snipes"]["edited"][-10:]
    log_command(before.author,"message_edited",before.channel,f"Before:{before.content}|After:{after.content}")
    save_data(data)

@bot.event
async def on_member_join(m): log_command(m,"member_join","N/A","Joined server")
@bot.event
async def on_member_remove(m): log_command(m,"member_leave","N/A","Left server")
@bot.event
async def on_guild_role_create(r): log_command(r.guild.me,"role_created","N/A",f"Role {r.name} created")
@bot.event
async def on_guild_role_delete(r): log_command(r.guild.me,"role_deleted","N/A",f"Role {r.name} deleted")
@bot.event
async def on_guild_channel_create(c): log_command(c.guild.me,"channel_created",c,f"Channel {c.name} created")
@bot.event
async def on_guild_channel_delete(c): log_command(c.guild.me,"channel_deleted",c,f"Channel {c.name} deleted")

# -------------------- Moderation Commands --------------------
@bot.command()
async def ban(ctx, member:discord.Member,*,reason=None):
    if not is_admin(ctx.author.id): return await ctx.send("No permission")
    await member.ban(reason=reason)
    await ctx.send(f"{member} banned.")
    log_moderation("ban",ctx,member,reason)

@bot.command()
async def unban(ctx,user:discord.User):
    if not is_admin(ctx.author.id): return await ctx.send("No permission")
    banned=await ctx.guild.bans()
    for entry in banned:
        if entry.user.id==user.id:
            await ctx.guild.unban(user)
            await ctx.send(f"{user} unbanned.")
            log_moderation("unban",ctx,user)
            return
    await ctx.send("User not in ban list")

@bot.command()
async def kick(ctx, member:discord.Member,*,reason=None):
    if not is_admin(ctx.author.id): return await ctx.send("No permission")
    await member.kick(reason=reason)
    await ctx.send(f"{member} kicked.")
    log_moderation("kick",ctx,member,reason)

@bot.command()
async def purge(ctx, amount:int):
    if not is_admin(ctx.author.id): return await ctx.send("No permission")
    await ctx.channel.purge(limit=amount)
    log_command(ctx.author,"purge",ctx.channel,f"Purged {amount} messages")

# -------------------- Fun Commands --------------------
@bot.command()
async def flipcoin(ctx):
    res=random.choice(["Heads","Tails"])
    await ctx.send(res)
    log_command(ctx.author,"flipcoin",ctx.channel,res)

@bot.command()
async def dadjoke(ctx):
    jokes=["I would tell you a joke about construction, but I'm still working on it.",
           "Why don't eggs tell jokes? They'd crack each other up.",
           "I only know 25 letters of the alphabet… I don't know y."]
    joke=random.choice(jokes)
    await ctx.send(joke)
    log_command(ctx.author,"dadjoke",ctx.channel,joke)

@bot.command()
async def rolldice(ctx,sides:int=6):
    r=random.randint(1,sides)
    await ctx.send(f"🎲 You rolled: {r}")
    log_command(ctx.author,"rolldice",ctx.channel,f"Rolled {r} on {sides} sides")

@bot.command()
async def cat(ctx):
    async with aiohttp.ClientSession() as s:
        async with s.get("https://api.thecatapi.com/v1/images/search") as resp:
            if resp.status!=200: return await ctx.send("Couldn't fetch cat image")
            data_json=await resp.json()
            await ctx.send(data_json[0]["url"])
            log_command(ctx.author,"cat",ctx.channel,"Sent cat image")

# -------------------- Say Commands --------------------
@bot.command()
async def say(ctx,*,msg):
    if ctx.author.id in data["blacklist"]: return await ctx.send("You are blacklisted")
    for w in data["blocked_words"]:
        if w.lower() in msg.lower(): return await ctx.send("This word is not allowed here")
    await ctx.send(msg)
    log_command(ctx.author,"say",ctx.channel,msg)

@bot.command()
async def say_admin(ctx,*,msg):
    if not is_admin(ctx.author.id): return await ctx.send("No permission")
    await ctx.send(msg)
    log_command(ctx.author,"say_admin",ctx.channel,msg)

# -------------------- User Info / Avatar --------------------
@bot.command()
async def userinfo(ctx,member:discord.Member=None):
    member=member or ctx.author
    e=Embed(title=f"{member}'s Info",color=discord.Color.green())
    e.add_field(name="Username",value=str(member),inline=True)
    e.add_field(name="ID",value=member.id,inline=True)
    e.add_field(name="Joined Server",value=member.joined_at,inline=True)
    e.add_field(name="Account Created",value=member.created_at,inline=True)
    e.add_field(name="Roles",value=", ".join([r.name for r in member.roles if r.name!="@everyone"]),inline=False)
    e.set_thumbnail(url=member.avatar.url if member.avatar else "")
    await ctx.send(embed=e)
    log_command(ctx.author,"userinfo",ctx.channel,f"Target:{member}")

@bot.command()
async def avatar(ctx,member:discord.Member=None):
    member=member or ctx.author
    e=Embed(title=f"{member}'s Avatar",color=discord.Color.purple())
    e.set_image(url=member.avatar.url if member.avatar else "")
    await ctx.send(embed=e)
    log_command(ctx.author,"avatar",ctx.channel,f"Target:{member}")

# -------------------- Snipes / E-Snipes --------------------
class SnipeView(View):
    def __init__(self,snipes,index,type):
        super().__init__(timeout=120)
        self.snipes=snipes
        self.index=index
        self.type=type

    @discord.ui.button(label="⬅️",style=ButtonStyle.gray)
    async def back(self,interaction:Interaction,button:Button):
        self.index=(self.index-1)%len(self.snipes)
        await interaction.response.edit_message(embed=self.get_embed())

    @discord.ui.button(label="➡️",style=ButtonStyle.gray)
    async def forward(self,interaction:Interaction,button:Button):
        self.index=(self.index+1)%len(self.snipes)
        await interaction.response.edit_message(embed=self.get_embed())

    def get_embed(self):
        s=self.snipes[self.index]
        if self.type=="deleted":
            e=Embed(title=f"Deleted ({self.index+1}/{len(self.snipes)})",
                    description=f"**Author:** {s['author']}\n**Content:** {s['content']}\n**Channel:** {s['channel']}\n**Time:** {s['timestamp']}")
        else:
            e=Embed(title=f"Edited ({self.index+1}/{len(self.snipes)})",
                    description=f"**Author:** {s['author']}\n**Before:** {s['before']}\n**After:** {s['after']}\n**Channel:** {s['channel']}\n**Time:** {s['timestamp']}")
        return e

@tree.command(name="snipe",description="Show deleted messages")
async def slash_snipe(interaction:Interaction):
    s=data["snipes"]["deleted"]
    if not s: return await interaction.response.send_message("No deleted messages",ephemeral=True)
    await interaction.response.send_message(embed=SnipeView(s,0,"deleted").get_embed(),view=SnipeView(s,0,"deleted"))

@tree.command(name="esnipe",description="Show edited messages")
async def slash_esnipe(interaction:Interaction):
    s=data["snipes"]["edited"]
    if not s: return await interaction.response.send_message("No edited messages",ephemeral=True)
    await interaction.response.send_message(embed=SnipeView(s,0,"edited").get_embed(),view=SnipeView(s,0,"edited"))

# -------------------- Pookie System (Merged) --------------------
# The fully integrated PookieView from previous step goes here
# (Refer to the previous assistant message with PookieView and slash_pookie)

# -------------------- Auto-responder, Blocked Words, Daily Cat, TicTacToe --------------------
# (Include the full implementations I gave previously for these)

# -------------------- On Message --------------------
@bot.event
async def on_message(message):
    if message.author.bot: return
    for w in data["blocked_words"]:
        if w.lower() in message.content.lower():
            await message.delete()
            await message.channel.send("This word is not allowed here.", delete_after=5)
            return
    for t in data["auto_responders"]:
        if t["trigger"].lower() in message.content.lower():
            await message.channel.send(t["response"])
    await bot.process_commands(message)

# -------------------- Run Bot --------------------
bot.run(os.environ["DISCORD_BOT_TOKEN"])
