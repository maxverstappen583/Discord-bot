# main.py
import os
import json
import random
import re
import threading
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List

import aiohttp
import discord
from discord.ext import commands, tasks
from discord import app_commands, ui
from flask import Flask

# ---------------- CONFIG ----------------
OWNER_ID = 1319292111325106296
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", None)
CAT_API_KEY = os.getenv("CAT_API_KEY", "")  # optional
PORT = int(os.getenv("PORT", 8080))        # Render provides this

# default reply used when a trigger word is matched (each trigger stores its own reply)
# Note: triggers will store custom replies per word

# ---------------- FLASK (uptime) ----------------
app = Flask("uptime")

@app.route("/")
def home():
    return "Bot is running"

def _run_flask():
    app.run(host="0.0.0.0", port=PORT)

threading.Thread(target=_run_flask, daemon=True).start()

# ---------------- STORAGE ----------------
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

def _path(name: str) -> str:
    return os.path.join(DATA_DIR, f"{name}.json")

def load_json(name: str, default):
    p = _path(name)
    if not os.path.exists(p):
        with open(p, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2)
        return default
    with open(p, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return default

def save_json(name: str, data):
    p = _path(name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# persistent data
admins = load_json("admins", {})          # {"id": true}
pookies = load_json("pookies", {})        # {"id": true}
blacklist = load_json("blacklist", {})    # {"id": true}
blocked_words = load_json("blocked_words", [])  # ["badword"]
triggers = load_json("triggers", {})      # {"max": "Hello Max!"}
logs = load_json("logs", [])              # list of log entries
cfg = load_json("config", {"log_channel_id": None, "cat_channel_id": None, "last_cat_date_ist": None})

# ---------------- BOT SETUP ----------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.messages = True
intents.guilds = True
intents.reactions = True

bot = commands.Bot(command_prefix="?", intents=intents, help_command=None)
tree = bot.tree

# ---------------- HELPERS ----------------
def is_owner_user(user: discord.abc.User | int) -> bool:
    uid = user.id if hasattr(user, "id") else int(user)
    return int(uid) == int(OWNER_ID)

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
    if not (guild and ch_id):
        return
    try:
        ch = guild.get_channel(int(ch_id))
        if not ch:
            return
        embed = discord.Embed(title=title, description=description, color=discord.Color.blurple(), timestamp=datetime.utcnow())
        await ch.send(embed=embed)
    except Exception:
        # avoid raising errors on logging
        pass

def sanitize_remove_pings(text: str) -> str:
    # Remove @everyone and @here
    t = text.replace("@everyone", "").replace("@here", "")
    # Replace user mentions <@...> and role mentions <@&...> with [mention]
    t = re.sub(r"<@!?\d+>", "[mention]", t)
    t = re.sub(r"<@&\d+>", "[mention]", t)
    return t

# ---------------- SNIPE / ESNIPE ----------------
SNIPE_MAX = 10
snipe_cache: dict = {}   # channel_id (str) -> list of dicts
esnipe_cache: dict = {}

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

# ---------------- TIMEZONE / DAILY CAT ----------------
def now_ist() -> datetime:
    # IST = UTC + 5:30
    return datetime.utcnow() + timedelta(hours=5, minutes=30)

@tasks.loop(minutes=1)
async def daily_cat_job():
    channel_id = cfg.get("cat_channel_id")
    if not channel_id:
        return
    now = now_ist()
    # run at 11:00 IST
    if now.hour == 11 and now.minute == 0:
        last = cfg.get("last_cat_date_ist")
        today = now.strftime("%Y-%m-%d")
        if last == today:
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
        ch = bot.get_channel(int(channel_id))
        if ch:
            try:
                await ch.send(cat_url)
            except Exception:
                pass
        cfg["last_cat_date_ist"] = today
        save_json("config", cfg)

# ---------------- NAV VIEW ----------------
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
        e = discord.Embed(title=f"{self.title} ({self.idx+1}/{len(self.items)})", description=desc, color=discord.Color.dark_teal())
        return e

    @ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: ui.Button):
        if self.idx > 0:
            self.idx -= 1
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: ui.Button):
        if self.idx < len(self.items) - 1:
            self.idx += 1
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

# ---------------- EVENTS ----------------
@bot.event
async def on_ready():
    print(f"{bot.user} online")
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

    # blacklist
    if is_blacklisted_user(message.author):
        try:
            await message.delete()
        except Exception:
            pass
        return

    # blocked words (mods bypass)
    if not is_admin_user(message.author):
        low = message.content.lower()
        for w in blocked_words:
            if w.lower() in low:
                try:
                    await message.delete()
                except Exception:
                    pass
                try:
                    await message.channel.send("That word is not allowed here.", delete_after=5)
                except Exception:
                    pass
                log_add("blocked_word", {"user": message.author.id, "word": w, "channel": message.channel.id})
                await send_log_embed(message.guild, "Blocked word", f"{message.author.mention} used blocked word `{w}` in {message.channel.mention}")
                return

    # exact-word triggers (case-insensitive exact match)
    content = message.content.strip()
    if content:
        for trigger_word, reply_text in triggers.items():
            if content.lower() == trigger_word.lower():
                # reply with stored reply_text but strip pings (no pings allowed in trigger replies)
                safe = sanitize_remove_pings(reply_text)
                try:
                    await message.channel.send(safe)
                except Exception:
                    pass
                log_add("trigger_fired", {"user": message.author.id, "trigger": trigger_word, "channel": message.channel.id})
                await send_log_embed(message.guild, "Trigger fired", f"{message.author.mention} triggered `{trigger_word}` in {message.channel.mention}")
                return

    # auto triggers (contains) — optional leftover system: if triggers are used only for exact matches, skip this
    # (we keep that other triggers mapping for backward compatibility; currently we already used triggers for exact matches)
    # process commands afterward
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

# ---------------- CORE ACTIONS ----------------
async def set_log_channel(invoker: discord.abc.User, channel: discord.TextChannel):
    if not is_admin_user(invoker):
        return False, "No permission."
    cfg["log_channel_id"] = channel.id
    save_json("config", cfg)
    return True, f"Log channel set to {channel.mention}"

async def set_cat_channel(invoker: discord.abc.User, channel: discord.TextChannel):
    if not is_admin_user(invoker):
        return False, "No permission."
    cfg["cat_channel_id"] = channel.id
    save_json("config", cfg)
    return True, f"Daily cat channel set to {channel.mention}"

# ---------------- PREFIX COMMANDS ----------------
# ADMIN / POOKIE MANAGEMENT
@bot.command(aliases=["addadmin"])
async def add_admin(ctx: commands.Context, user: discord.User):
    if not is_owner_user(ctx.author):
        return await ctx.send("Only owner can add admins.")
    admins[str(user.id)] = True
    save_json("admins", admins)
    log_add("add_admin", {"by": ctx.author.id, "target": user.id})
    await ctx.send(f"{user.mention} added as admin.")

@bot.command(aliases=["removeadmin"])
async def remove_admin(ctx: commands.Context, user: discord.User):
    if not is_owner_user(ctx.author):
        return await ctx.send("Only owner can remove admins.")
    admins.pop(str(user.id), None)
    save_json("admins", admins)
    log_add("remove_admin", {"by": ctx.author.id, "target": user.id})
    await ctx.send(f"{user.mention} removed from admins.")

@bot.command()
async def list_admins(ctx: commands.Context):
    mentions = [f"<@{uid}>" for uid in admins.keys()]
    await ctx.send("Admins: " + (", ".join(mentions) if mentions else "None"))

@bot.command()
async def add_pookie(ctx: commands.Context, user: discord.User):
    if not is_owner_user(ctx.author):
        return await ctx.send("Only owner can add pookie.")
    pookies[str(user.id)] = True
    save_json("pookies", pookies)
    log_add("add_pookie", {"by": ctx.author.id, "target": user.id})
    await ctx.send(f"{user.mention} added as pookie.")

@bot.command()
async def remove_pookie(ctx: commands.Context, user: discord.User):
    if not is_owner_user(ctx.author):
        return await ctx.send("Only owner can remove pookie.")
    pookies.pop(str(user.id), None)
    save_json("pookies", pookies)
    log_add("remove_pookie", {"by": ctx.author.id, "target": user.id})
    await ctx.send(f"{user.mention} removed from pookies.")

@bot.command()
async def list_pookie(ctx: commands.Context):
    mentions = [f"<@{uid}>" for uid in pookies.keys()]
    await ctx.send("Pookies: " + (", ".join(mentions) if mentions else "None"))

# MODERATION
@bot.command()
async def ban(ctx: commands.Context, user: discord.User, *, reason: str = "No reason"):
    if not is_admin_user(ctx.author):
        return await ctx.send("No permission.")
    try:
        await ctx.guild.ban(user, reason=reason, delete_message_days=0)
        await ctx.send(f"Banned {user.mention}.")
        log_add("ban", {"by": ctx.author.id, "target": user.id, "reason": reason})
        await send_log_embed(ctx.guild, "Ban", f"{user.mention} banned by {ctx.author.mention}\nReason: {reason}")
    except Exception as e:
        await ctx.send(f"Failed: {e}")

@bot.command()
async def kick(ctx: commands.Context, user: discord.User, *, reason: str = "No reason"):
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

@bot.command()
async def blacklist_user(ctx: commands.Context, user: discord.User):
    if not is_admin_user(ctx.author):
        return await ctx.send("No permission.")
    blacklist[str(user.id)] = True
    save_json("blacklist", blacklist)
    await ctx.send(f"{user.mention} blacklisted.")
    log_add("blacklist", {"by": ctx.author.id, "target": user.id})

@bot.command()
async def unblacklist_user(ctx: commands.Context, user: discord.User):
    if not is_admin_user(ctx.author):
        return await ctx.send("No permission.")
    blacklist.pop(str(user.id), None)
    save_json("blacklist", blacklist)
    await ctx.send(f"{user.mention} removed from blacklist.")
    log_add("unblacklist", {"by": ctx.author.id, "target": user.id})

# Set channels
@bot.command()
async def setlogchannel(ctx: commands.Context, channel: discord.TextChannel):
    ok, msg = await set_log_channel(ctx.author, channel)
    if not ok:
        return await ctx.send(msg)
    await ctx.send(msg)

@bot.command()
async def setcatchannel(ctx: commands.Context, channel: discord.TextChannel):
    ok, msg = await set_cat_channel(ctx.author, channel)
    if not ok:
        return await ctx.send(msg)
    await ctx.send(msg)

# PURGE prefix (max 100)
@bot.command()
async def purge(ctx: commands.Context, amount: int):
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

# FUN / UTILITIES (prefix)
@bot.command()
async def cat(ctx: commands.Context):
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

@bot.command()
async def rps(ctx: commands.Context, choice: str):
    opts = ["rock","paper","scissors"]
    c = choice.lower()
    if c not in opts:
        return await ctx.send("Choose rock, paper, or scissors.")
    bot_choice = random.choice(opts)
    if c == bot_choice:
        res = "Tie!"
    elif (c=="rock" and bot_choice=="scissors") or (c=="paper" and bot_choice=="rock") or (c=="scissors" and bot_choice=="paper"):
        res = "You win!"
    else:
        res = "You lose!"
    await ctx.send(f"You: **{c}** | Me: **{bot_choice}** → {res}")

@bot.command()
async def coinflip(ctx: commands.Context):
    await ctx.send(random.choice(["Heads","Tails"]))

@bot.command()
async def rolldice(ctx: commands.Context, sides: int = 6):
    if sides < 2 or sides > 1000:
        return await ctx.send("Choose sides between 2 and 1000.")
    await ctx.send(f"🎲 {random.randint(1, sides)}")

# SAY prefix commands
@bot.command()
async def say(ctx: commands.Context, *, text: str):
    if not is_admin_user(ctx.author):
        # remove mentions and mass pings
        safe = sanitize_remove_pings(text)
        # blocked words check
        for w in blocked_words:
            if w.lower() in safe.lower():
                return await ctx.send("Message contains blocked word.")
        await ctx.send(safe)
    else:
        await ctx.send(text)

@bot.command()
async def say_admin(ctx: commands.Context, *, text: str):
    if not is_admin_user(ctx.author):
        return await ctx.send("No permission.")
    await ctx.send(text)

# TRIGGER prefix management (admin-only)
@bot.command()
async def trigger(ctx: commands.Context, action: str, word: str = None, *, reply: str = None):
    """
    prefix usage:
    ?trigger add <word> <reply>
    ?trigger remove <word>
    ?trigger list
    """
    action = action.lower()
    if action == "list":
        if not is_admin_user(ctx.author) and not is_pookie_user(ctx.author):
            return await ctx.send("No permission.")
        if not triggers:
            return await ctx.send("No triggers set.")
        lines = [f"`{k}` -> {v}" for k, v in triggers.items()]
        return await ctx.send("\n".join(lines))
    if action == "add":
        if not is_admin_user(ctx.author):
            return await ctx.send("No permission.")
        if not word or not reply:
            return await ctx.send("Usage: ?trigger add <word> <reply>")
        triggers[word] = reply
        save_json("triggers", triggers)
        log_add("trigger_add", {"by": ctx.author.id, "word": word, "reply": reply})
        return await ctx.send(f"Added trigger `{word}`")
    if action == "remove":
        if not is_admin_user(ctx.author):
            return await ctx.send("No permission.")
        if not word:
            return await ctx.send("Usage: ?trigger remove <word>")
        if word in triggers:
            triggers.pop(word, None)
            save_json("triggers", triggers)
            log_add("trigger_remove", {"by": ctx.author.id, "word": word})
            return await ctx.send(f"Removed trigger `{word}`")
        return await ctx.send("Not found.")
    return await ctx.send("Unknown action. Use add/remove/list.")

# SNIPES prefix
@bot.command()
async def snipe(ctx: commands.Context):
    items = snipe_cache.get(str(ctx.channel.id), [])
    if not items:
        return await ctx.send("Nothing to snipe.")
    view = NavView(items, "Snipe")
    await ctx.send(embed=view.make_embed(), view=view)

@bot.command()
async def esnipe(ctx: commands.Context):
    items = esnipe_cache.get(str(ctx.channel.id), [])
    if not items:
        return await ctx.send("Nothing to e-snipe.")
    view = NavView(items, "E-Snipe")
    await ctx.send(embed=view.make_embed(), view=view)

# SHOW COMMANDS
@bot.command()
async def showcommands(ctx: commands.Context):
    all_cmds = [c.name for c in bot.commands if not c.hidden]
    if not is_admin_user(ctx.author):
        hide = {"ban","kick","blacklist_user","unblacklist_user","add_admin","remove_admin","add_pookie","remove_pookie","setlogchannel","setcatchannel","purge"}
        visible = [c for c in all_cmds if c not in hide]
    else:
        visible = all_cmds
    await ctx.send("Commands: " + ", ".join(sorted(visible)))

# ---------------- SLASH UTIL HELPERS ----------------
def slash_not_blacklisted():
    async def predicate(inter: discord.Interaction):
        if is_blacklisted_user(inter.user):
            await inter.response.send_message("You are blacklisted.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

def slash_admin_like():
    async def predicate(inter: discord.Interaction):
        if not is_admin_user(inter.user):
            await inter.response.send_message("No permission.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

# ---------------- SLASH COMMANDS ----------------
@tree.command(name="say", description="Make the bot say something (non-admin pings removed)")
@slash_not_blacklisted()
async def sc_say(inter: discord.Interaction, text: str):
    if not is_admin_user(inter.user):
        safe = sanitize_remove_pings(text)
        for w in blocked_words:
            if w.lower() in safe.lower():
                return await inter.response.send_message("Message contains blocked word.", ephemeral=True)
        await inter.response.send_message(safe)
    else:
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
    try:
        deleted = await inter.channel.purge(limit=amount+1)
        await inter.response.send_message(f"Deleted {len(deleted)-1} messages.", ephemeral=True)
        log_add("purge", {"by": inter.user.id, "channel": inter.channel.id, "amount": amount})
        await send_log_embed(inter.guild, "Purge", f"{inter.user.mention} deleted {amount} messages in {inter.channel.mention}")
    except Exception as e:
        await inter.response.send_message(f"Failed: {e}", ephemeral=True)

# TRIGGER slash group: /trigger add/remove/list
@tree.command(name="trigger", description="Manage exact-word triggers (admin only)")
@slash_admin_like()
async def sc_trigger(inter: discord.Interaction, action: str, word: Optional[str] = None, reply: Optional[str] = None):
    """
    Usage:
    /trigger action:add word:<word> reply:<reply>
    /trigger action:remove word:<word>
    /trigger action:list
    """
    act = action.lower()
    if act == "list":
        if not triggers:
            return await inter.response.send_message("No triggers set.", ephemeral=True)
        lines = [f"`{k}` -> {v}" for k, v in triggers.items()]
        return await inter.response.send_message("\n".join(lines), ephemeral=True)
    if act == "add":
        if not word or not reply:
            return await inter.response.send_message("Usage: /trigger add <word> <reply>", ephemeral=True)
        triggers[word] = reply
        save_json("triggers", triggers)
        log_add("trigger_add", {"by": inter.user.id, "word": word, "reply": reply})
        return await inter.response.send_message(f"Added trigger `{word}`", ephemeral=True)
    if act == "remove":
        if not word:
            return await inter.response.send_message("Usage: /trigger remove <word>", ephemeral=True)
        if word in triggers:
            triggers.pop(word, None)
            save_json("triggers", triggers)
            log_add("trigger_remove", {"by": inter.user.id, "word": word})
            return await inter.response.send_message(f"Removed trigger `{word}`", ephemeral=True)
        return await inter.response.send_message("Not found.", ephemeral=True)
    return await inter.response.send_message("Unknown action. Use add/remove/list.", ephemeral=True)

# SNIPE / ESNIPE slash
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

# Set channels (slash)
@tree.command(name="setlogchannel", description="Set the logs channel (admin/pookie only)")
@slash_admin_like()
async def sc_setlog(inter: discord.Interaction, channel: discord.TextChannel):
    ok, msg = await set_log_channel(inter.user, channel)
    if not ok:
        return await inter.response.send_message(msg, ephemeral=True)
    await inter.response.send_message(msg, ephemeral=True)

@tree.command(name="setcatchannel", description="Set daily cat channel (11:00 IST)")
@slash_admin_like()
async def sc_setcat(inter: discord.Interaction, channel: discord.TextChannel):
    ok, msg = await set_cat_channel(inter.user, channel)
    if not ok:
        return await inter.response.send_message(msg, ephemeral=True)
    await inter.response.send_message(msg, ephemeral=True)

# Moderation (slash)
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

# Admin/pookie (slash)
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

# ---------------- RUN ----------------
if not BOT_TOKEN:
    print("ERROR: DISCORD_BOT_TOKEN is not set. Set it in Render environment variables.")
else:
    bot.run(BOT_TOKEN)
