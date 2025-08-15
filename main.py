# main.py
import os
import json
import random
import asyncio
import threading
from datetime import datetime, timedelta
from typing import Optional, List

import aiohttp
import discord
from discord.ext import commands, tasks
from discord import app_commands, ui
from flask import Flask

# -------------------- CONFIG --------------------
OWNER_ID = 1319292111325106296
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", None)
CAT_API_KEY = os.getenv("CAT_API_KEY", "")
PORT = int(os.getenv("PORT", 8080))  # Render provides PORT

# Default reply message when exact-match reply word is triggered
DEFAULT_REPLY_MESSAGE = "Hello!"  # change to whatever default reply you want

# -------------------- FLASK (uptime) --------------------
app = Flask("uptime")

@app.route("/")
def home():
    return "OK"

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

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

# persistent structures
admins = load_json("admins", {})            # {"user_id": true}
pookies = load_json("pookies", {})          # {"user_id": true}
blacklist = load_json("blacklist", {})      # {"user_id": true}
blocked_words = load_json("blocked_words", [])  # ["badword"]
triggers = load_json("triggers", {})        # {"hello": "hi"}
logs = load_json("logs", [])                # list of log entries
cfg = load_json("config", {"log_channel_id": None, "cat_channel_id": None, "last_cat_date_ist": None})
reply_words = load_json("reply_words", [])  # ["max","hello"]

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
snipe_cache = {}   # str(channel_id) -> list of dicts
esnipe_cache = {}  # str(channel_id) -> list of dicts

def push_snipe(channel_id: int, payload: dict):
    key = str(channel_id)
    arr = snipe_cache.get(key, [])
    arr.insert(0, payload)
    if len(arr) > SNIPE_MAX:
        arr.pop()
    snipe_cache[key] = arr

def push_esnipe(channel_id: int, payload: dict):
    key = str(channel_id)
    arr = esnipe_cache.get(key, [])
    arr.insert(0, payload)
    if len(arr) > SNIPE_MAX:
        arr.pop()
    esnipe_cache[key] = arr

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
    # fire at 11:00 IST
    if now.hour == 11 and now.minute == 0:
        last_date = cfg.get("last_cat_date_ist")
        today = now.strftime("%Y-%m-%d")
        if last_date == today:
            return
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
    def __init__(self, items: List[dict], title: str):
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

# -------------------- EVENTS --------------------
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

    # Exact-word reply system (case-insensitive exact match)
    content_stripped = message.content.strip()
    if content_stripped and any(content_stripped.lower() == rw.lower() for rw in reply_words):
        # Only respond if exact match (case-insensitive)
        try:
            await message.channel.send(DEFAULT_REPLY_MESSAGE)
            log_add("reply_word_trigger", {"user": message.author.id, "word": content_stripped, "channel": message.channel.id})
        except Exception:
            pass

    # Auto-responder triggers (contains)
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

# -------------------- CORE ACTIONS (used by both prefix & slash) --------------------
async def core_set_log_channel(invoker: discord.abc.User, channel: discord.TextChannel):
    if not is_admin_user(invoker):
        return False, "No permission."
    cfg["log_channel_id"] = channel.id
    save_json("config", cfg)
    return True, f"Log channel set to {channel.mention}"

async def core_set_cat_channel(invoker: discord.abc.User, channel: discord.TextChannel):
    if not is_admin_user(invoker):
        return False, "No permission."
    cfg["cat_channel_id"] = channel.id
    save_json("config", cfg)
    return True, f"Daily cat channel set to {channel.mention}"

# -------------------- PREFIX COMMANDS --------------------
# Admin / Pookie management
@bot.command(name="add_admin")
async def cmd_add_admin(ctx: commands.Context, user: discord.User):
    if not is_owner_user(ctx.author):
        return await ctx.send("Only owner.")
    admins[str(user.id)] = True
    save_json("admins", admins)
    log_add("add_admin", {"by": ctx.author.id, "target": user.id})
    await ctx.send(f"{user.mention} added as admin.")

@bot.command(name="remove_admin")
async def cmd_remove_admin(ctx: commands.Context, user: discord.User):
    if not is_owner_user(ctx.author):
        return await ctx.send("Only owner.")
    admins.pop(str(user.id), None)
    save_json("admins", admins)
    log_add("remove_admin", {"by": ctx.author.id, "target": user.id})
    await ctx.send(f"{user.mention} removed from admins.")

@bot.command(name="list_admins")
async def cmd_list_admins(ctx: commands.Context):
    mentions = [f"<@{uid}>" for uid in admins.keys()]
    await ctx.send("Admins: " + (", ".join(mentions) if mentions else "None"))

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
    await ctx.send(f"{user.mention} removed from pookies.")

@bot.command(name="list_pookie")
async def cmd_list_pookie(ctx: commands.Context):
    mentions = [f"<@{uid}>" for uid in pookies.keys()]
    await ctx.send("Pookies: " + (", ".join(mentions) if mentions else "None"))

# Moderation
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
    member = ctx.guild.get_member(user.id)
    if not member:
        return await ctx.send("User not in server.")
    try:
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

# Set channels
@bot.command(name="setlogchannel")
async def cmd_setlogchannel(ctx: commands.Context, channel: discord.TextChannel):
    ok, msg = await core_set_log_channel(ctx.author, channel)
    if not ok:
        return await ctx.send(msg)
    await ctx.send(msg)

@bot.command(name="setcatchannel")
async def cmd_setcatchannel(ctx: commands.Context, channel: discord.TextChannel):
    ok, msg = await core_set_cat_channel(ctx.author, channel)
    if not ok:
        return await ctx.send(msg)
    await ctx.send(msg)

# Purge (prefix) - up to 100 messages
@bot.command(name="purge")
async def cmd_purge(ctx: commands.Context, amount: int):
    if not is_admin_user(ctx.author):
        return await ctx.send("No permission.")
    if amount < 1 or amount > 100:
        return await ctx.send("Amount must be between 1 and 100.")
    try:
        deleted = await ctx.channel.purge(limit=amount+1)  # include command message
        await ctx.send(f"Deleted {len(deleted)-1} messages.", delete_after=5)
        log_add("purge", {"by": ctx.author.id, "channel": ctx.channel.id, "amount": amount})
        await send_log_embed(ctx.guild, "Purge", f"{ctx.author.mention} deleted {amount} messages in {ctx.channel.mention}")
    except Exception as e:
        await ctx.send(f"Failed: {e}")

# Fun / utilities
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

# say commands (prefix)
@bot.command(name="say")
async def cmd_say(ctx: commands.Context, *, text: str):
    # if user is not admin/pookie, remove mass mentions and prevent pinging users
    if not is_admin_user(ctx.author):
        # remove @everyone / @here
        text = text.replace("@everyone","").replace("@here","")
        # remove user/role mentions by stripping <@...> and <@&...>
        import re
        text = re.sub(r"<@!?\d+>", "[mention]", text)
        text = re.sub(r"<@&\d+>", "[mention]", text)
    else:
        # admin can send pings, but still sanitize mass mentions optionally
        text = text
    # blocked words check for non-admins
    if not is_admin_user(ctx.author):
        for w in blocked_words:
            if w.lower() in text.lower():
                return await ctx.send("Message contains blocked word.")
    await ctx.send(text)

@bot.command(name="say_admin")
async def cmd_say_admin(ctx: commands.Context, *, text: str):
    if not is_admin_user(ctx.author):
        return await ctx.send("No permission.")
    await ctx.send(text)

# reply words management (prefix)
@bot.command(name="add_reply_word")
async def cmd_add_reply_word(ctx: commands.Context, word: str):
    if not is_admin_user(ctx.author):
        return await ctx.send("No permission.")
    if word.lower() in [w.lower() for w in reply_words]:
        return await ctx.send("Already present.")
    reply_words.append(word)
    save_json("reply_words", reply_words)
    await ctx.send(f"Added reply word `{word}`")

@bot.command(name="remove_reply_word")
async def cmd_remove_reply_word(ctx: commands.Context, word: str):
    if not is_admin_user(ctx.author):
        return await ctx.send("No permission.")
    for existing in reply_words:
        if existing.lower() == word.lower():
            reply_words.remove(existing)
            save_json("reply_words", reply_words)
            return await ctx.send(f"Removed reply word `{existing}`")
    await ctx.send("Not found.")

@bot.command(name="list_reply_words")
async def cmd_list_reply_words(ctx: commands.Context):
    if not reply_words:
        return await ctx.send("No reply words set.")
    await ctx.send("Reply words: " + ", ".join(reply_words))

# snipe / esnipe prefix
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

# utility prefix commands
@bot.command(name="showcommands")
async def cmd_showcommands(ctx: commands.Context):
    all_cmds = [c.name for c in bot.commands if not c.hidden]
    if not is_admin_user(ctx.author):
        hide = {"ban","kick","blacklist","unblacklist","add_admin","remove_admin","add_pookie","remove_pookie","setlogchannel","setcatchannel","purge"}
        visible = [c for c in all_cmds if c not in hide]
    else:
        visible = all_cmds
    await ctx.send("Commands: " + ", ".join(sorted(visible)))

# -------------------- SLASH COMMAND HELPERS --------------------
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

# -------------------- SLASH COMMANDS --------------------
@tree.command(name="say", description="Make the bot say something (public, non-admin pings removed)")
@slash_not_blacklisted()
async def sc_say(inter: discord.Interaction, text: str):
    if not is_admin_user(inter.user):
        # sanitize pings for non-admin
        import re
        t = text.replace("@everyone","").replace("@here","")
        t = re.sub(r"<@!?\d+>", "[mention]", t)
        t = re.sub(r"<@&\d+>", "[mention]", t)
        text = t
        # blocked words check
        for w in blocked_words:
            if w.lower() in text.lower():
                await inter.response.send_message("Message contains blocked word.", ephemeral=True)
                return
    await inter.response.send_message(text)

@tree.command(name="say_admin", description="Admin say (allows mentions)")
@slash_admin_like()
async def sc_say_admin(inter: discord.Interaction, text: str):
    await inter.response.send_message(text)

@tree.command(name="purge", description="Delete up to 100 messages (admin/pookie only)")
@slash_admin_like()
async def sc_purge(inter: discord.Interaction, amount: int):
    if amount < 1 or amount > 100:
        return await inter.response.send_message("Amount must be between 1 and 100.", ephemeral=True)
    if not inter.channel:
        return await inter.response.send_message("This command must be used in a channel.", ephemeral=True)
    # fetch messages and bulk delete
    try:
        deleted = await inter.channel.purge(limit=amount+1)  # include command message if present
        await inter.response.send_message(f"Deleted {len(deleted)-1} messages.", ephemeral=True)
        log_add("purge", {"by": inter.user.id, "channel": inter.channel.id, "amount": amount})
        await send_log_embed(inter.guild, "Purge", f"{inter.user.mention} deleted {amount} messages in {inter.channel.mention}")
    except Exception as e:
        await inter.response.send_message(f"Failed: {e}", ephemeral=True)

# reply words slash management
@tree.command(name="add_reply_word", description="Add a word for exact-match auto-reply (admin/pookie only)")
@slash_admin_like()
async def sc_add_reply_word(inter: discord.Interaction, word: str):
    if word.lower() in [w.lower() for w in reply_words]:
        return await inter.response.send_message("Already present.", ephemeral=True)
    reply_words.append(word)
    save_json("reply_words", reply_words)
    await inter.response.send_message(f"Added reply word `{word}`", ephemeral=True)

@tree.command(name="remove_reply_word", description="Remove a reply word (admin/pookie only)")
@slash_admin_like()
async def sc_remove_reply_word(inter: discord.Interaction, word: str):
    for existing in reply_words:
        if existing.lower() == word.lower():
            reply_words.remove(existing)
            save_json("reply_words", reply_words)
            return await inter.response.send_message(f"Removed reply word `{existing}`", ephemeral=True)
    await inter.response.send_message("Not found.", ephemeral=True)

@tree.command(name="list_reply_words", description="List exact-match reply words")
@slash_not_blacklisted()
async def sc_list_reply_words(inter: discord.Interaction):
    if not reply_words:
        return await inter.response.send_message("No reply words set.", ephemeral=True)
    await inter.response.send_message("Reply words: " + ", ".join(reply_words), ephemeral=True)

# snipe / esnipe slash
@tree.command(name="snipe", description="Show recently deleted messages in this channel")
@slash_not_blacklisted()
async def sc_snipe(inter: discord.Interaction):
    items = snipe_cache.get(str(inter.channel.id), [])
    if not items:
        return await inter.response.send_message("Nothing to snipe.", ephemeral=True)
    view = NavView(items, "Snipe")
    await inter.response.send_message(embed=view.make_embed(), view=view)

@tree.command(name="esnipe", description="Show recently edited messages in this channel")
@slash_not_blacklisted()
async def sc_esnipe(inter: discord.Interaction):
    items = esnipe_cache.get(str(inter.channel.id), [])
    if not items:
        return await inter.response.send_message("Nothing to e-snipe.", ephemeral=True)
    view = NavView(items, "E-Snipe")
    await inter.response.send_message(embed=view.make_embed(), view=view)

# set channels (slash)
@tree.command(name="setlogchannel", description="Set the logs channel (admin/pookie only)")
@slash_admin_like()
async def sc_setlog(inter: discord.Interaction, channel: discord.TextChannel):
    ok, msg = await core_set_log_channel(inter.user, channel)
    if not ok:
        return await inter.response.send_message(msg, ephemeral=True)
    await inter.response.send_message(msg, ephemeral=True)

@tree.command(name="setcatchannel", description="Set daily cat channel (11:00 IST)")
@slash_admin_like()
async def sc_setcat(inter: discord.Interaction, channel: discord.TextChannel):
    ok, msg = await core_set_cat_channel(inter.user, channel)
    if not ok:
        return await inter.response.send_message(msg, ephemeral=True)
    await inter.response.send_message(msg, ephemeral=True)

# moderation slash commands (blacklist/ban/kick)
@tree.command(name="blacklist_user", description="Blacklist a user (admin/pookie only)")
@slash_admin_like()
async def sc_blacklist(inter: discord.Interaction, user: discord.User):
    blacklist[str(user.id)] = True
    save_json("blacklist", blacklist)
    await inter.response.send_message(f"Blacklisted {user.mention}", ephemeral=True)

@tree.command(name="unblacklist_user", description="Unblacklist a user (admin/pookie only)")
@slash_admin_like()
async def sc_unblacklist(inter: discord.Interaction, user: discord.User):
    blacklist.pop(str(user.id), None)
    save_json("blacklist", blacklist)
    await inter.response.send_message(f"Unblacklisted {user.mention}", ephemeral=True)

@tree.command(name="ban", description="Ban a user (admin/pookie only)")
@slash_admin_like()
async def sc_ban(inter: discord.Interaction, user: discord.User, reason: Optional[str] = "No reason"):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    try:
        await inter.guild.ban(user, reason=reason, delete_message_days=0)
        await inter.response.send_message(f"Banned {user.mention}", ephemeral=True)
        await send_log_embed(inter.guild, "Ban", f"{user.mention} banned by {inter.user.mention}\nReason: {reason}")
    except Exception as e:
        await inter.response.send_message(f"Failed: {e}", ephemeral=True)

@tree.command(name="kick", description="Kick a user (admin/pookie only)")
@slash_admin_like()
async def sc_kick(inter: discord.Interaction, user: discord.User, reason: Optional[str] = "No reason"):
    if not inter.guild:
        return await inter.response.send_message("Guild only.", ephemeral=True)
    member = inter.guild.get_member(user.id)
    if not member:
        return await inter.response.send_message("User not in server.", ephemeral=True)
    try:
        await member.kick(reason=reason)
        await inter.response.send_message(f"Kicked {user.mention}", ephemeral=True)
        await send_log_embed(inter.guild, "Kick", f"{user.mention} kicked by {inter.user.mention}\nReason: {reason}")
    except Exception as e:
        await inter.response.send_message(f"Failed: {e}", ephemeral=True)

# admin/pookie management slash
@tree.command(name="add_admin", description="Owner: add an admin")
@slash_admin_like()
async def sc_add_admin(inter: discord.Interaction, user: discord.User):
    if not is_owner_user(inter.user):
        return await inter.response.send_message("Owner only.", ephemeral=True)
    admins[str(user.id)] = True
    save_json("admins", admins)
    await inter.response.send_message(f"Added {user.mention} as admin", ephemeral=True)

@tree.command(name="remove_admin", description="Owner: remove an admin")
@slash_admin_like()
async def sc_remove_admin(inter: discord.Interaction, user: discord.User):
    if not is_owner_user(inter.user):
        return await inter.response.send_message("Owner only.", ephemeral=True)
    admins.pop(str(user.id), None)
    save_json("admins", admins)
    await inter.response.send_message(f"Removed {user.mention} from admins", ephemeral=True)

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
    await inter.response.send_message(f"Added {user.mention} as pookie", ephemeral=True)

@tree.command(name="remove_pookie", description="Owner: remove pookie")
@slash_admin_like()
async def sc_remove_pookie(inter: discord.Interaction, user: discord.User):
    if not is_owner_user(inter.user):
        return await inter.response.send_message("Owner only.", ephemeral=True)
    pookies.pop(str(user.id), None)
    save_json("pookies", pookies)
    await inter.response.send_message(f"Removed {user.mention} from pookie", ephemeral=True)

@tree.command(name="list_pookie", description="List pookies")
@slash_not_blacklisted()
async def sc_list_pookie(inter: discord.Interaction):
    mentions = [f"<@{uid}>" for uid in pookies.keys()]
    await inter.response.send_message("Pookies: " + (", ".join(mentions) if mentions else "None"), ephemeral=True)

# -------------------- RUN --------------------
if not BOT_TOKEN:
    print("ERROR: DISCORD_BOT_TOKEN env var is not set. Set it in Render.")
else:
    bot.run(BOT_TOKEN)
