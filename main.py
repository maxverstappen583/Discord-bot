# main.py
import os
import json
import random
import re
import threading
from datetime import datetime, timedelta
from typing import Optional, List, Dict

import aiohttp
import discord
from discord.ext import commands, tasks
from discord import app_commands, ui
from flask import Flask

# ---------------- CONFIG ----------------
OWNER_ID = 1319292111325106296  # replace if needed
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CAT_API_KEY = os.getenv("CAT_API_KEY", "")  # use TheCatAPI key here
PORT = int(os.getenv("PORT", 8080))

# ---------------- FLASK UPTIME ----------------
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

admins: Dict[str, bool] = load_json("admins", {})         # global admin list (string ids)
pookies: Dict[str, bool] = load_json("pookies", {})       # pookie users (string ids)
blacklist: Dict[str, bool] = load_json("blacklist", {})   # blacklisted users
blocked_words: List[str] = load_json("blocked_words", [])# substrings disallowed
triggers: Dict[str, str] = load_json("triggers", {})      # exact-word -> reply text
logs: List[dict] = load_json("logs", [])                  # chronological logs
cfg: dict = load_json("config", {"log_channel_id": None, "cat_channel_id": None, "last_cat_date_ist": None})

# ---------------- BOT SETUP ----------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.messages = True

bot = commands.Bot(command_prefix="?", intents=intents, help_command=None)
tree = bot.tree

# ---------------- HELPERS ----------------
def is_owner_user(user) -> bool:
    uid = user.id if hasattr(user, "id") else int(user)
    return int(uid) == int(OWNER_ID)

def is_admin_user(user) -> bool:
    uid = user.id if hasattr(user, "id") else int(user)
    return is_owner_user(uid) or str(uid) in admins or str(uid) in pookies

def is_pookie_user(user) -> bool:
    uid = user.id if hasattr(user, "id") else int(user)
    return str(uid) in pookies

def is_blacklisted_user(user) -> bool:
    uid = user.id if hasattr(user, "id") else int(user)
    return str(uid) in blacklist

def sanitize_remove_pings(text: str) -> str:
    t = text.replace("@everyone", "").replace("@here", "")
    t = re.sub(r"<@!?\d+>", "[mention]", t)
    t = re.sub(r"<@&\d+>", "[mention]", t)
    return t

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
        pass

# ---------------- SNIPE / ESNIPE ----------------
SNIPE_MAX = 15
snipe_cache: Dict[str, List[dict]] = {}
esnipe_cache: Dict[str, List[dict]] = {}

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

# ---------------- TIME helpers ----------------
def now_ist() -> datetime:
    return datetime.utcnow() + timedelta(hours=5, minutes=30)

# ---------------- DAILY CAT ----------------
@tasks.loop(minutes=1)
async def daily_cat_job():
    ch_id = cfg.get("cat_channel_id")
    if not ch_id:
        return
    now = now_ist()
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
                    cat_url = (data[0].get("url") if data else None) or "https://cataas.com/cat"
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
            desc = f"**Author:** {item.get('author')}\n**Before:** {item.get('before')}\n**After:** {item.get('after')}\n**Time:** {item.get('time')}"
        return discord.Embed(title=f"{self.title} ({self.idx+1}/{len(self.items)})", description=desc, color=discord.Color.dark_teal())

    @ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: ui.Button):
        if self.idx > 0:
            self.idx -= 1
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: ui.Button):
        if self.idx < len(self.items)-1:
            self.idx += 1
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

# ---------------- EVENTS ----------------
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

    if is_blacklisted_user(message.author):
        try:
            await message.delete()
        except Exception:
            pass
        return

    # blocked-words (mods bypass)
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

    # triggers: exact-word match (case-insensitive)
    content = message.content.strip()
    if content:
        for trg, reply_text in triggers.items():
            if content.lower() == trg.lower():
                safe_reply = sanitize_remove_pings(reply_text)
                try:
                    await message.channel.send(safe_reply)
                except Exception:
                    pass
                log_add("trigger_fired", {"user": message.author.id, "trigger": trg, "channel": message.channel.id})
                await send_log_embed(message.guild, "Trigger fired", f"{message.author.mention} triggered `{trg}` in {message.channel.mention}")
                return

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
async def core_set_log_channel(invoker, channel: discord.TextChannel):
    if not is_admin_user(invoker):
        return False, "No permission."
    cfg["log_channel_id"] = channel.id
    save_json("config", cfg)
    return True, f"Log channel set to {channel.mention}"

async def core_remove_log_channel(invoker):
    if not is_admin_user(invoker):
        return False, "No permission."
    cfg["log_channel_id"] = None
    save_json("config", cfg)
    return True, "Log channel removed."

async def core_set_cat_channel(invoker, channel: discord.TextChannel):
    if not is_admin_user(invoker):
        return False, "No permission."
    cfg["cat_channel_id"] = channel.id
    save_json("config", cfg)
    return True, f"Daily cat channel set to {channel.mention}"

# ---------------- PREFIX COMMANDS ----------------
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

# Set/remove channels
@bot.command(name="setlogchannel")
async def cmd_setlogchannel(ctx: commands.Context, channel: discord.TextChannel):
    ok, msg = await core_set_log_channel(ctx.author, channel)
    if not ok:
        return await ctx.send(msg)
    await ctx.send(msg)

@bot.command(name="remove_log_channel")
async def cmd_remove_log_channel(ctx: commands.Context):
    ok, msg = await core_remove_log_channel(ctx.author)
    if not ok:
        return await ctx.send(msg)
    await ctx.send(msg)

@bot.command(name="setcatchannel")
async def cmd_setcatchannel(ctx: commands.Context, channel: discord.TextChannel):
    ok, msg = await core_set_cat_channel(ctx.author, channel)
    if not ok:
        return await ctx.send(msg)
    await ctx.send(msg)

# Purge (prefix) up to 100
@bot.command(name="purge")
async def cmd_purge(ctx: commands.Context, amount: int):
    if not is_admin_user(ctx.author):
        return await ctx.send("No permission.")
    if amount < 1 or amount > 100:
        return await ctx.send("Amount must be between 1 and 100.")
    try:
        deleted = await ctx.channel.purge(limit=amount+1)
        await ctx.send(f"Deleted {len(deleted)-1} messages.", delete_after=5)
        log_add("purge", {"by": ctx.author.id, "channel": ctx.channel.id, "amount": amount})
        await send_log_embed(ctx.guild, "Purge", f"{ctx.author.mention} deleted {amount} messages in {ctx.channel.mention}")
    except Exception as e:
        await ctx.send(f"Failed: {e}")

# Fun / utilities (prefix)
@bot.command(name="cat")
async def cmd_cat(ctx: commands.Context):
    url = "https://api.thecatapi.com/v1/images/search"
    headers = {"x-api-key": CAT_API_KEY} if CAT_API_KEY else {}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, headers=headers) as resp:
                data = await resp.json()
                cat_url = (data[0].get("url") if data else None) or "https://cataas.com/cat"
    except Exception:
        cat_url = "https://cataas.com/cat"
    await ctx.send(cat_url)

@bot.command(name="rps")
async def cmd_rps(ctx: commands.Context, choice: str):
    opts = ["rock", "paper", "scissors"]
    c = choice.lower()
    if c not in opts:
        return await ctx.send("Choose rock, paper, or scissors.")
    bot_choice = random.choice(opts)
    if c == bot_choice:
        result = "Tie!"
    elif (c == "rock" and bot_choice == "scissors") or (c == "paper" and bot_choice == "rock") or (c == "scissors" and bot_choice == "paper"):
        result = "You win!"
    else:
        result = "You lose!"
    await ctx.send(f"You: **{c}** | Me: **{bot_choice}** → {result}")

@bot.command(name="eightball")
async def cmd_8ball(ctx: commands.Context, *, question: str):
    answers = ["Yes","No","Maybe","Definitely","Ask again later","It is certain","Very doubtful"]
    await ctx.send(f"🎱 {random.choice(answers)}")

@bot.command(name="joke")
async def cmd_joke(ctx: commands.Context):
    jokes = ["Why did the dev go broke? Because they used all their cache.", "I would tell you a UDP joke but you might not get it."]
    await ctx.send(random.choice(jokes))

@bot.command(name="dadjoke")
async def cmd_dadjoke(ctx: commands.Context):
    dads = ["I'm reading a book on anti-gravity. It's impossible to put down!","Why don't scientists trust atoms? Because they make up everything."]
    await ctx.send(random.choice(dads))

@bot.command(name="coinflip")
async def cmd_coinflip(ctx: commands.Context):
    await ctx.send(random.choice(["Heads", "Tails"]))

@bot.command(name="rolldice")
async def cmd_rolldice(ctx: commands.Context, sides: int = 6):
    if sides < 2 or sides > 1000:
        return await ctx.send("Choose sides between 2 and 1000.")
    await ctx.send(f"🎲 {random.randint(1, sides)}")

# Say prefix commands
@bot.command(name="say")
async def cmd_say(ctx: commands.Context, *, text: str):
    # NEVER allow pings in normal say
    safe = sanitize_remove_pings(text)
    for w in blocked_words:
        if w.lower() in safe.lower():
            return await ctx.send("Message contains blocked word.")
    await ctx.send(safe)

@bot.command(name="say_admin")
async def cmd_say_admin(ctx: commands.Context, *, text: str):
    if not is_admin_user(ctx.author):
        return await ctx.send("No permission.")
    await ctx.send(text)

# Trigger management (prefix): ?trigger add/remove/list
@bot.command(name="trigger")
async def cmd_trigger(ctx: commands.Context, action: str, word: Optional[str] = None, *, reply: Optional[str] = None):
    act = action.lower()
    if act == "list":
        if not is_admin_user(ctx.author):
            return await ctx.send("No permission.")
        if not triggers:
            return await ctx.send("No triggers set.")
        lines = [f"`{k}` -> {v}" for k, v in triggers.items()]
        return await ctx.send("\n".join(lines))
    if act == "add":
        if not is_admin_user(ctx.author):
            return await ctx.send("No permission.")
        if not word or not reply:
            return await ctx.send("Usage: ?trigger add <word> <reply>")
        triggers[word] = reply
        save_json("triggers", triggers)
        log_add("trigger_add", {"by": ctx.author.id, "word": word, "reply": reply})
        return await ctx.send(f"Added trigger `{word}`")
    if act == "remove":
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

# Snipe / Esnipe prefix
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

# showcommands prefix - only commands user can run
@bot.command(name="showcommands")
async def cmd_showcommands(ctx: commands.Context):
    all_cmds = sorted({c.name for c in bot.commands if not c.hidden})
    admin_only = {
        "ban","kick","blacklist","unblacklist","add_admin","remove_admin","add_pookie","remove_pookie",
        "purge","setlogchannel","remove_log_channel","setcatchannel","trigger","logs"
    }
    visible = [c for c in all_cmds if (is_admin_user(ctx.author) or c not in admin_only)]
    await ctx.send("Commands: " + ", ".join(visible))

# Logs prefix command: ?logs [amount]
@bot.command(name="logs")
async def cmd_logs(ctx: commands.Context, amount: int = 10):
    if not is_admin_user(ctx.author):
        return await ctx.send("No permission.")
    amt = max(1, min(200, amount))
    last = logs[-amt:]
    if not last:
        return await ctx.send("No logs.")
    embed = discord.Embed(title=f"Last {len(last)} logs", color=discord.Color.gold(), timestamp=datetime.utcnow())
    for i, entry in enumerate(reversed(last), 1):
        t = entry.get("time", "")
        kind = entry.get("kind", "")
        detail = entry.get("detail", {})
        embed.add_field(name=f"{i}. {kind} @ {t}", value=str(detail), inline=False)
    await ctx.send(embed=embed)

# ---------------- SLASH HELPERS ----------------
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

# ---------------- SLASH COMMANDS ----------------
@tree.command(name="say", description="Make the bot say something (no pings allowed)")
@slash_not_blacklisted()
async def sc_say(inter: discord.Interaction, text: str):
    safe = sanitize_remove_pings(text)  # no pings for anyone
    for w in blocked_words:
        if w.lower() in safe.lower():
            return await inter.response.send_message("Message contains blocked word.", ephemeral=True)
    await inter.response.send_message(safe)

@tree.command(name="say_admin", description="Admin say (allows mentions)")
@slash_admin_like()
async def sc_say_admin(inter: discord.Interaction, text: str):
    await inter.response.send_message(text)

@tree.command(name="purge", description="Delete up to 100 messages (admin only)")
@slash_admin_like()
async def sc_purge(inter: discord.Interaction, amount: int):
    if amount < 1 or amount > 100:
        return await inter.response.send_message("Amount must be between 1 and 100.", ephemeral=True)
    if not inter.channel:
        return await inter.response.send_message("Use this in a channel.", ephemeral=True)
    try:
        deleted = await inter.channel.purge(limit=amount+1)
        await inter.response.send_message(f"Deleted {len(deleted)-1} messages.", ephemeral=True)
        log_add("purge", {"by": inter.user.id, "channel": inter.channel.id, "amount": amount})
        await send_log_embed(inter.guild, "Purge", f"{inter.user.mention} deleted {amount} messages in {inter.channel.mention}")
    except Exception as e:
        await inter.response.send_message(f"Failed: {e}", ephemeral=True)

@tree.command(name="cat", description="Show a random cat image")
@slash_not_blacklisted()
async def sc_cat(inter: discord.Interaction):
    url = "https://api.thecatapi.com/v1/images/search"
    headers = {"x-api-key": CAT_API_KEY} if CAT_API_KEY else {}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, headers=headers) as resp:
                data = await resp.json()
                cat_url = (data[0].get("url") if data else None) or "https://cataas.com/cat"
    except Exception:
        cat_url = "https://cataas.com/cat"
    await inter.response.send_message(cat_url)

@tree.command(name="rps", description="Rock Paper Scissors")
@slash_not_blacklisted()
@app_commands.describe(choice="rock/paper/scissors")
@app_commands.choices(choice=[
    app_commands.Choice(name="rock", value="rock"),
    app_commands.Choice(name="paper", value="paper"),
    app_commands.Choice(name="scissors", value="scissors"),
])
async def sc_rps(inter: discord.Interaction, choice: app_commands.Choice[str]):
    c = choice.value.lower()
    opts = ["rock", "paper", "scissors"]
    bot_choice = random.choice(opts)
    if c == bot_choice:
        res = "Tie!"
    elif (c == "rock" and bot_choice == "scissors") or (c == "paper" and bot_choice == "rock") or (c == "scissors" and bot_choice == "paper"):
        res = "You win!"
    else:
        res = "You lose!"
    await inter.response.send_message(f"You: **{c}** | Me: **{bot_choice}** → {res}")

# Trigger slash (action:add/remove/list) - admin only
@tree.command(name="trigger", description="Manage exact-word triggers (admin only). Usage: /trigger action:add/remove/list word: reply:")
@slash_admin_like()
async def sc_trigger(inter: discord.Interaction, action: str, word: Optional[str] = None, reply: Optional[str] = None):
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

# Logs (slash): view last N
@tree.command(name="logs", description="Show last N logs (admin only)")
@slash_admin_like()
async def sc_logs(inter: discord.Interaction, amount: Optional[int] = 10):
    amt = max(1, min(200, amount or 10))
    last = logs[-amt:]
    if not last:
        return await inter.response.send_message("No logs.", ephemeral=True)
    embed = discord.Embed(title=f"Last {len(last)} logs", color=discord.Color.gold(), timestamp=datetime.utcnow())
    for i, entry in enumerate(reversed(last), 1):
        t = entry.get("time", "")
        kind = entry.get("kind", "")
        detail = entry.get("detail", {})
        embed.add_field(name=f"{i}. {kind} @ {t}", value=str(detail), inline=False)
    await inter.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="remove_log_channel", description="Remove configured log channel (admin only)")
@slash_admin_like()
async def sc_remove_log_channel(inter: discord.Interaction):
    ok, msg = await core_remove_log_channel(inter.user)
    if not ok:
        return await inter.response.send_message(msg, ephemeral=True)
    await inter.response.send_message(msg, ephemeral=True)

@tree.command(name="setlogchannel", description="Set log channel (admin only)")
@slash_admin_like()
async def sc_setlog(inter: discord.Interaction, channel: discord.TextChannel):
    ok, msg = await core_set_log_channel(inter.user, channel)
    if not ok:
        return await inter.response.send_message(msg, ephemeral=True)
    await inter.response.send_message(msg, ephemeral=True)

@tree.command(name="setcatchannel", description="Set daily cat channel (admin only)")
@slash_admin_like()
async def sc_setcat(inter: discord.Interaction, channel: discord.TextChannel):
    ok, msg = await core_set_cat_channel(inter.user, channel)
    if not ok:
        return await inter.response.send_message(msg, ephemeral=True)
    await inter.response.send_message(msg, ephemeral=True)

# showcommands (slash) - show only commands user can use
@tree.command(name="showcommands", description="Show commands you can use")
@slash_not_blacklisted()
async def sc_showcommands(inter: discord.Interaction):
    admin_only = {
        "ban","kick","blacklist","unblacklist","add_admin","remove_admin","add_pookie","remove_pookie",
        "purge","setlogchannel","remove_log_channel","setcatchannel","trigger","logs"
    }
    visible = set()
    # slash commands
    for cmd in tree.get_commands():
        if is_admin_user(inter.user) or cmd.name not in admin_only:
            visible.add(cmd.name)
    # prefix commands
    for c in bot.commands:
        if is_admin_user(inter.user) or c.name not in admin_only:
            visible.add(c.name)
    names = sorted(visible)
    await inter.response.send_message("Available: " + ", ".join(names), ephemeral=True)

# moderation slash commands (blacklist/ban/kick)
@tree.command(name="blacklist_user", description="Blacklist a user (admin only)")
@slash_admin_like()
async def sc_blacklist(inter: discord.Interaction, user: discord.User):
    blacklist[str(user.id)] = True
    save_json("blacklist", blacklist)
    await inter.response.send_message(f"Blacklisted {user.mention}", ephemeral=True)

@tree.command(name="unblacklist_user", description="Unblacklist a user (admin only)")
@slash_admin_like()
async def sc_unblacklist(inter: discord.Interaction, user: discord.User):
    blacklist.pop(str(user.id), None)
    save_json("blacklist", blacklist)
    await inter.response.send_message(f"Unblacklisted {user.mention}", ephemeral=True)

@tree.command(name="ban", description="Ban a user (admin only)")
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

@tree.command(name="kick", description="Kick a user (admin only)")
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

# admin/pookie management (slash)
@tree.command(name="add_admin", description="Owner: add admin")
@slash_admin_like()
async def sc_add_admin(inter: discord.Interaction, user: discord.User):
    if not is_owner_user(inter.user):
        return await inter.response.send_message("Owner only.", ephemeral=True)
    admins[str(user.id)] = True
    save_json("admins", admins)
    await inter.response.send_message(f"Added {user.mention} as admin", ephemeral=True)

@tree.command(name="remove_admin", description="Owner: remove admin")
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
    print("ERROR: DISCORD_BOT_TOKEN env var not set. Set in Render environment variables.")
else:
    bot.run(BOT_TOKEN)
