# main.py — Full monster Discord bot with Flask keepalive & JSON persistence
# Do NOT put tokens directly in this file. Use environment variables.

import os
import re
import json
import time
import asyncio
import traceback
import platform
import random
import aiohttp
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from threading import Thread
from typing import Optional, List, Dict, Any

import discord
from discord.ext import commands, tasks

# Optional imports
try:
    import psutil
except Exception:
    psutil = None

try:
    from flask import Flask
except Exception:
    Flask = None

# --------------------- ENV helpers ---------------------
def parse_int_env(v: Optional[str], default: int) -> int:
    if not v:
        return default
    s = str(v).strip()
    m = re.search(r"(\d{5,20})", s)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return default
    try:
        return int(s)
    except Exception:
        return default

def parse_list_env(v: Optional[str]) -> List[int]:
    if not v:
        return []
    parts = [p.strip() for p in v.split(",") if p.strip()]
    out = []
    for p in parts:
        try:
            out.append(int(re.search(r"(\d{5,20})", p).group(1)))
        except Exception:
            try:
                out.append(int(p))
            except Exception:
                pass
    return out

# --------------------- ENV VARS ---------------------
DISCORD_BOT_TOKEN = (os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN") or "").strip()
if not DISCORD_BOT_TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN environment variable not set")

OWNER_ID = parse_int_env(os.getenv("OWNER_ID"), 1319292111325106296)
DEFAULT_ADMINS_ENV = os.getenv("DEFAULT_ADMINS", f"{OWNER_ID},1380315427992768633,909468887098216499")
DEFAULT_ADMINS = set(parse_list_env(DEFAULT_ADMINS_ENV)) or {OWNER_ID}
CAT_API_KEY = os.getenv("CAT_API_KEY", "").strip()
RENDER_API_KEY = os.getenv("RENDER_API_KEY", "").strip()
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "") or os.getenv("SERVICE_ID", "")
TZ_NAME = os.getenv("TZ", "Asia/Kolkata").strip() or "Asia/Kolkata"
PORT = int(os.getenv("PORT", os.getenv("RENDER_PORT", "10000") or "10000"))

try:
    LOCAL_TZ = ZoneInfo(TZ_NAME)
except Exception:
    LOCAL_TZ = timezone.utc

# --------------------- Persistence ---------------------
DATA_FILE = "data.json"
DEFAULT_DATA: Dict[str, Any] = {
    "admins": [],          # persisted admins (DEFAULT_ADMINS are implicitly admins)
    "pookies": [],
    "blacklist": [],
    "blocked_words": [],
    "warns": {},
    "logs": [],
    "log_channel": None,
    "triggers": {},
    "daily_cat_channel": None,
    "hourly_cat": {"enabled": False, "channel": None, "interval_hours": 1, "last_sent": 0},
    "snipes": {},
    "esnipes": {},
    "afk": {}
}

def ensure_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_DATA, f, indent=2)
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        try:
            d = json.load(f)
        except Exception:
            d = DEFAULT_DATA.copy()
    changed = False
    for k, v in DEFAULT_DATA.items():
        if k not in d:
            d[k] = v
            changed = True
    if changed:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    return d

DATA: Dict[str, Any] = ensure_data()

def save_data() -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(DATA, f, indent=2)

# --------------------- Bot setup ---------------------
intents = discord.Intents.all()
intents.message_content = True
bot = commands.Bot(command_prefix="?", intents=intents, help_command=None)
bot.start_time = time.time()

def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone(LOCAL_TZ)

def now_local_str() -> str:
    return now_local().strftime("%Y-%m-%d %H:%M:%S %Z")

# --------------------- Flask keep-alive ---------------------
if Flask is not None:
    app = Flask("keepalive")
    @app.route("/")
    def index():
        return "Bot is alive", 200
    def run_flask():
        app.run(host="0.0.0.0", port=PORT)
    Thread(target=run_flask, daemon=True).start()

# --------------------- Utilities ---------------------
def is_owner(user: discord.abc.User) -> bool:
    return getattr(user, "id", None) == OWNER_ID

def is_admin_or_pookie(user: discord.abc.User) -> bool:
    uid = getattr(user, "id", None)
    if uid in DEFAULT_ADMINS:
        return True
    if uid in DATA.get("admins", []):
        return True
    if uid in DATA.get("pookies", []):
        return True
    return False

def user_blacklisted(user: discord.abc.User) -> bool:
    return getattr(user, "id", None) in DATA.get("blacklist", [])

def sanitize_no_pings(text: str) -> str:
    text = text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    text = re.sub(r"<@!?\d+>", "@mention", text)
    return text

def normalize_for_detect(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s).lower()

def contains_blocked_word(message: str) -> Optional[str]:
    if not message:
        return None
    for w in DATA.get("blocked_words", []):
        if re.search(rf"\b{re.escape(w)}\b", message, flags=re.IGNORECASE):
            return w
    norm = normalize_for_detect(message)
    for w in DATA.get("blocked_words", []):
        if normalize_for_detect(w) and normalize_for_detect(w) in norm:
            return w
    return None

def parse_duration(text: str) -> Optional[int]:
    if not isinstance(text, str):
        return None
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*$", text.strip(), re.IGNORECASE)
    if not m:
        return None
    n = int(m.group(1)); su = m.group(2).lower()
    mult = {"s":1,"m":60,"h":3600,"d":86400}[su]
    return n * mult

async def fetch_cat_url() -> Optional[str]:
    url = "https://api.thecatapi.com/v1/images/search"
    headers = {"x-api-key": CAT_API_KEY} if CAT_API_KEY else {}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, headers=headers, timeout=20) as resp:
                if resp.status == 200:
                    j = await resp.json()
                    if isinstance(j, list) and j:
                        return j[0].get("url")
    except Exception:
        pass
    return None

def log_command(user: discord.abc.User, command_name: str, channel: Optional[discord.abc.Messageable]) -> None:
    entry = {
        "time": now_local_str(),
        "user": f"{getattr(user,'name',str(user))} ({getattr(user,'id','')})",
        "command": command_name,
        "channel": getattr(channel, "name", str(channel) if channel else "DM")
    }
    DATA.setdefault("logs", []).append(entry)
    DATA["logs"] = DATA["logs"][-1000:]
    save_data()
    ch_id = DATA.get("log_channel")
    if ch_id:
        try:
            ch = bot.get_channel(int(ch_id))
            if ch and isinstance(ch, discord.TextChannel):
                embed = discord.Embed(title="Command Log", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
                embed.add_field(name="User", value=entry["user"], inline=False)
                embed.add_field(name="Command", value=entry["command"], inline=False)
                embed.add_field(name="Channel", value=entry["channel"], inline=False)
                asyncio.create_task(ch.send(embed=embed))
        except Exception:
            pass

# --------------------- Events: ready, message, snipes, edits ---------------------
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} application commands")
    except Exception as e:
        print("Slash sync failed:", e)
    print(f"✅ Logged in as {bot.user} (id: {bot.user.id})")
    # start loops
    try:
        daily_cat_loop.start()
    except RuntimeError:
        pass
    try:
        hourly_cat_loop.start()
    except RuntimeError:
        pass

@bot.event
async def on_message_delete(message: discord.Message):
    if not message.guild or message.author.bot:
        return
    ch = str(message.channel.id)
    DATA.setdefault("snipes", {}).setdefault(ch, [])
    DATA["snipes"][ch].insert(0, {
        "author_id": str(message.author.id),
        "author_name": str(message.author),
        "content": message.content,
        "attachments": [a.url for a in message.attachments],
        "created_at": message.created_at.isoformat() if message.created_at else "",
        "deleted_at": now_local_str()
    })
    DATA["snipes"][ch] = DATA["snipes"][ch][:50]
    save_data()
    log_command(message.author, "message_delete", message.channel)

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if not before.guild or before.author.bot:
        return
    if before.content == after.content:
        return
    ch = str(before.channel.id)
    DATA.setdefault("esnipes", {}).setdefault(ch, [])
    DATA["esnipes"][ch].insert(0, {
        "author_id": str(before.author.id),
        "author_name": str(before.author),
        "before": before.content,
        "after": after.content,
        "edited_at": now_local_str()
    })
    DATA["esnipes"][ch] = DATA["esnipes"][ch][:50]
    save_data()
    log_command(before.author, "message_edit", before.channel)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # remove AFK when user speaks
    uid = str(message.author.id)
    if uid in DATA.get("afk", {}):
        DATA["afk"].pop(uid, None)
        save_data()
        try:
            await message.channel.send(f"✅ Welcome back {message.author.mention}. I removed your AFK.")
        except Exception:
            pass

    # blocked words enforcement (skip admins/pookies/owner)
    if message.guild and not is_admin_or_pookie(message.author):
        bad = contains_blocked_word(message.content)
        if bad:
            try:
                await message.delete()
                await message.channel.send(f"{message.author.mention} that word is not allowed here.", delete_after=6)
                log_command(message.author, "blocked_word_deleted", message.channel)
            except Exception:
                pass
            return

    # AFK mention handling and reply-to detection
    if message.mentions:
        for m in message.mentions:
            info = DATA.get("afk", {}).get(str(m.id))
            if info:
                since = datetime.fromtimestamp(info.get("since", 0), timezone.utc).astimezone(LOCAL_TZ)
                embed = discord.Embed(title="User is AFK", color=discord.Color.orange())
                embed.add_field(name="User", value=f"{m.mention}", inline=False)
                embed.add_field(name="Reason", value=info.get("reason", "AFK"), inline=False)
                embed.set_footer(text=f"Since {since.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                try:
                    await message.channel.send(embed=embed)
                except Exception:
                    pass

    if message.reference and getattr(message.reference, "resolved", None):
        ref = message.reference.resolved
        if ref and getattr(ref, "author", None):
            info = DATA.get("afk", {}).get(str(ref.author.id))
            if info:
                since = datetime.fromtimestamp(info.get("since", 0), timezone.utc).astimezone(LOCAL_TZ)
                embed = discord.Embed(title="User is AFK", color=discord.Color.orange())
                embed.add_field(name="User", value=f"{ref.author.mention}", inline=False)
                embed.add_field(name="Reason", value=info.get("reason", "AFK"), inline=False)
                embed.set_footer(text=f"Since {since.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                try:
                    await message.channel.send(embed=embed)
                except Exception:
                    pass

    # Triggers (exact-word)
    for trig_word, reply in (DATA.get("triggers") or {}).items():
        if re.search(rf"\b{re.escape(trig_word)}\b", message.content, flags=re.IGNORECASE):
            out = reply.replace("{user}", message.author.mention)
            try:
                await message.channel.send(out)
            except Exception:
                pass
            break

    await bot.process_commands(message)

# --------------------- NavView for snipe ---------------------
class NavView(discord.ui.View):
    def __init__(self, items: List[Dict[str, Any]]):
        super().__init__(timeout=120)
        self.items = items
        self.idx = 0

    def embed_for(self) -> discord.Embed:
        it = self.items[self.idx]
        embed = discord.Embed(color=discord.Color.orange())
        if "content" in it:
            embed.title = f"🗑 Deleted ({self.idx+1}/{len(self.items)})"
            embed.add_field(name="Author", value=f"<@{it['author_id']}> ({it['author_name']})", inline=False)
            embed.add_field(name="Content", value=it.get("content") or "(no text)", inline=False)
            if it.get("attachments"):
                embed.add_field(name="Attachments", value="\n".join(it.get("attachments", [])[:5]), inline=False)
            embed.set_footer(text=f"Deleted at {it.get('deleted_at','')}")
        else:
            embed.title = f"✏️ Edited ({self.idx+1}/{len(self.items)})"
            embed.add_field(name="Author", value=f"<@{it['author_id']}> ({it['author_name']})", inline=False)
            embed.add_field(name="Before", value=it.get("before","(empty)")[:1024], inline=False)
            embed.add_field(name="After", value=it.get("after","(empty)")[:1024], inline=False)
            embed.set_footer(text=f"Edited at {it.get('edited_at','')}")
        return embed

    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.secondary)
    async def left(self, inter: discord.Interaction, btn: discord.ui.Button):
        self.idx = (self.idx - 1) % len(self.items)
        await inter.response.edit_message(embed=self.embed_for(), view=self)

    @discord.ui.button(label="➡️", style=discord.ButtonStyle.secondary)
    async def right(self, inter: discord.Interaction, btn: discord.ui.Button):
        self.idx = (self.idx + 1) % len(self.items)
        await inter.response.edit_message(embed=self.embed_for(), view=self)

# --------------------- Trigger Add Modal & View ---------------------
class TriggerAddModal(discord.ui.Modal, title="Add Trigger"):
    word = discord.ui.TextInput(label="Trigger word (exact)", placeholder="hello", max_length=100)
    reply = discord.ui.TextInput(label="Reply (use {user} to mention)", style=discord.TextStyle.long, placeholder="Hi {user}!", max_length=2000)

    def __init__(self, invoker_id: int):
        super().__init__()
        self.invoker_id = invoker_id

    async def on_submit(self, interaction: discord.Interaction):
        if not is_admin_or_pookie(interaction.user) and not is_owner(interaction.user):
            await interaction.response.send_message("❌ No permission to add triggers.", ephemeral=True)
            return
        word = self.word.value.strip().lower()
        reply = self.reply.value.strip()
        if not word or not reply:
            await interaction.response.send_message("Invalid input.", ephemeral=True)
            return
        DATA.setdefault("triggers", {})[word] = reply
        save_data()
        await interaction.response.send_message(f"✅ Trigger `{word}` added.", ephemeral=True)

class TriggerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Add trigger", style=discord.ButtonStyle.primary)
    async def add_trigger(self, inter: discord.Interaction, btn: discord.ui.Button):
        if not is_admin_or_pookie(interaction := inter.user) and not is_owner(interaction):
            await inter.response.send_message("❌ No permission.", ephemeral=True)
            return
        modal = TriggerAddModal(invoker_id=inter.user.id)
        await inter.response.send_modal(modal)

# --------------------- Commands (hybrid) ---------------------

# -- AFK --
@bot.hybrid_command(name="afk", description="Set your AFK with optional reason")
async def cmd_afk(ctx: commands.Context, *, reason: str = "AFK"):
    uid = str(ctx.author.id)
    DATA.setdefault("afk", {})[uid] = {"reason": reason, "since": time.time()}
    save_data()
    log_command(ctx.author, "afk", ctx.channel)
    await ctx.reply(f"✅ {ctx.author.mention} is now AFK: **{reason}**")

@bot.hybrid_command(name="show_afk", description="Show AFK for a user")
async def cmd_show_afk(ctx: commands.Context, user: Optional[discord.Member] = None):
    target = user or ctx.author
    info = DATA.get("afk", {}).get(str(target.id))
    if not info:
        return await ctx.reply(f"{target.mention} is not AFK.")
    since = datetime.fromtimestamp(info["since"], timezone.utc).astimezone(LOCAL_TZ)
    embed = discord.Embed(title=f"AFK: {target}", color=discord.Color.orange())
    embed.add_field(name="Reason", value=info.get("reason", "AFK"))
    embed.add_field(name="Since", value=since.strftime("%Y-%m-%d %H:%M:%S %Z"))
    await ctx.reply(embed=embed)

@bot.hybrid_command(name="remove_afk", description="Remove your AFK")
async def cmd_remove_afk(ctx: commands.Context):
    uid = str(ctx.author.id)
    if uid in DATA.get("afk", {}):
        DATA["afk"].pop(uid, None)
        save_data()
        await ctx.reply("✅ Your AFK was removed.")
    else:
        await ctx.reply("You are not AFK.")

# -- Avatar & Userinfo --
@bot.hybrid_command(name="avatar", description="Show a user's avatar")
async def cmd_avatar(ctx: commands.Context, user: Optional[discord.User] = None):
    user = user or ctx.author
    embed = discord.Embed(title=f"{user.name}'s avatar", color=discord.Color.blue())
    embed.set_image(url=user.display_avatar.url)
    await ctx.reply(embed=embed)

@bot.hybrid_command(name="userinfo", description="Show info about a user")
async def cmd_userinfo(ctx: commands.Context, member: Optional[discord.Member] = None):
    member = member or ctx.author
    embed = discord.Embed(title=f"User Info - {member}", color=discord.Color.blue())
    embed.set_thumbnail(url=member.display_avatar.url if member.display_avatar else discord.Embed.Empty)
    embed.add_field(name="Username", value=member.name, inline=True)
    embed.add_field(name="Discriminator", value=f"#{member.discriminator}", inline=True)
    embed.add_field(name="ID", value=str(member.id), inline=True)
    embed.add_field(name="Created At", value=member.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"), inline=False)
    embed.add_field(name="Joined At", value=member.joined_at.strftime("%Y-%m-%d %H:%M:%S UTC") if member.joined_at else "N/A", inline=False)
    embed.add_field(name="Top Role", value=member.top_role.mention if member.top_role else "N/A", inline=True)
    embed.add_field(name="Bot?", value="Yes 🤖" if member.bot else "No", inline=True)
    await ctx.reply(embed=embed)

# -- Say / Say_admin --
@bot.hybrid_command(name="say", description="Repeat text (pings blocked)")
async def cmd_say(ctx: commands.Context, *, message: str):
    if user_blacklisted(ctx.author):
        return await ctx.reply("🚫 You are blacklisted.")
    if contains_blocked_word(message):
        return await ctx.reply("Your message contains a blocked word.")
    out = sanitize_no_pings(message)
    log_command(ctx.author, "say", ctx.channel)
    await ctx.reply(out)

@bot.hybrid_command(name="say_admin", description="Admin/pookie/owner only: repeat text (pings allowed)")
async def cmd_say_admin(ctx: commands.Context, *, message: str):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    log_command(ctx.author, "say_admin", ctx.channel)
    await ctx.reply(message)

# -- Refresh / Sync --
@bot.hybrid_command(name="refresh", description="Sync slash commands (Admin/Pookie/Owner)")
async def cmd_refresh(ctx: commands.Context):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    try:
        synced = await bot.tree.sync()
        log_command(ctx.author, "refresh", ctx.channel)
        await ctx.reply(f"✅ Synced {len(synced)} commands.")
    except Exception as e:
        await ctx.reply(f"Sync error: {e}")

# -- Restart via Render and fallback exit --
@bot.hybrid_command(name="restart", description="Owner only: restart (Render deploy if configured)")
async def cmd_restart(ctx: commands.Context):
    if not is_owner(ctx.author):
        return await ctx.reply("❌ Owner only.")
    log_command(ctx.author, "restart", ctx.channel)
    if RENDER_API_KEY and RENDER_SERVICE_ID:
        url = f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/deploys"
        headers = {"Authorization": f"Bearer {RENDER_API_KEY}", "Content-Type": "application/json"}
        async with aiohttp.ClientSession() as sess:
            try:
                async with sess.post(url, headers=headers, json={"clearCache": True}, timeout=30) as resp:
                    txt = await resp.text()
                    await ctx.reply(f"Render response {resp.status}.")
            except Exception as e:
                await ctx.reply(f"Render API error: {e}")
    else:
        await ctx.reply("No Render API configured — exiting process.")
        await asyncio.sleep(1)
        os._exit(1)

# -- Eval (owner only) --
@bot.hybrid_command(name="eval", description="Owner-only: evaluate python (dangerous)")
async def cmd_eval(ctx: commands.Context, *, code: str):
    if not is_owner(ctx.author):
        return await ctx.reply("❌ Owner only.")
    env = {"bot": bot, "discord": discord, "asyncio": asyncio, "ctx": ctx, "DATA": DATA}
    try:
        if code.startswith("```") and code.endswith("```"):
            code = "\n".join(code.splitlines()[1:-1])
        result = eval(code, env)
        if asyncio.iscoroutine(result):
            result = await result
        out = str(result)
    except Exception:
        out = traceback.format_exc()
    if len(out) > 1900:
        out = out[:1900] + "…"
    await ctx.reply(f"```py\n{out}\n```")

# -- Debug --
@bot.hybrid_command(name="debug", description="Admin/Pookie/Owner: debug info")
async def cmd_debug(ctx: commands.Context):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    uptime_s = int(time.time() - bot.start_time)
    uptime = str(timedelta(seconds=uptime_s))
    guild_count = len(bot.guilds)
    member_count = sum((g.member_count for g in bot.guilds), 0)
    try:
        cmd_count = len(await bot.tree.fetch_commands())
    except Exception:
        cmd_count = len(bot.commands)
    mem = None
    cpu = None
    if psutil:
        try:
            mem = psutil.Process(os.getpid()).memory_info().rss / 1024**2
            cpu = psutil.cpu_percent(interval=0.1)
        except Exception:
            pass
    embed = discord.Embed(title="🛠 Debug Info", color=discord.Color.green())
    embed.add_field(name="Uptime", value=uptime, inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency*1000)} ms", inline=True)
    embed.add_field(name="Guilds", value=str(guild_count), inline=True)
    embed.add_field(name="Members", value=str(member_count), inline=True)
    embed.add_field(name="Commands (approx)", value=str(cmd_count), inline=True)
    if mem is not None:
        embed.add_field(name="Memory (MB)", value=f"{mem:.1f}", inline=True)
    if cpu is not None:
        embed.add_field(name="CPU %", value=f"{cpu}", inline=True)
    embed.add_field(name="Timezone", value=TZ_NAME, inline=True)
    embed.add_field(name="Owner ID", value=str(OWNER_ID), inline=True)
    embed.set_footer(text=f"Python {platform.python_version()} | discord.py {discord.__version__}")
    await ctx.reply(embed=embed, ephemeral=True)

# -- Showcommands (filtered) --
COMMAND_CATEGORIES = {
    "Public": ["say","avatar","userinfo","afk","show_afk","cat","coin","roll","rps","joke","dadjoke","8ball","snipe","esnipe","serverinfo","showcommands","trigger_list"],
    "Admin": ["say_admin","setlogchannel","disable_log_channel","add_pookie","remove_pookie","listpookie","warn","show_warns","remove_warn","clear_warns","giverole","removerole","temprole","lock","unlock","mute","unmute","setdailycatchannel","sethourlycatchannel","hourlycat_on","hourlycat_off","trigger_add","trigger_remove","trigger_list","refresh","logs","blacklist","unblacklist","add_admin","remove_admin","listadmin"],
    "Owner": ["restart","eval"]
}

@bot.hybrid_command(name="showcommands", description="Show commands you can use (filtered)")
async def cmd_showcommands(ctx: commands.Context):
    lines = []
    for cat, cmds in COMMAND_CATEGORIES.items():
        visible = []
        for name in cmds:
            # owner-only commands
            if cat == "Owner" and not is_owner(ctx.author):
                continue
            if cat == "Admin" and not (is_admin_or_pookie(ctx.author) or is_owner(ctx.author)):
                continue
            visible.append(name)
        if visible:
            lines.append(f"**{cat}**: " + ", ".join(visible))
    if not lines:
        return await ctx.reply("No commands available to you.", ephemeral=True)
    await ctx.reply("\n".join(lines), ephemeral=True)

# -- Logging channel & logs --
@bot.hybrid_command(name="setlogchannel", description="Set channel to receive logs (Admin/Pookie)")
async def cmd_setlogchannel(ctx: commands.Context, channel: discord.TextChannel):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    DATA["log_channel"] = int(channel.id)
    save_data()
    await ctx.reply(f"✅ Log channel set to {channel.mention}")

@bot.hybrid_command(name="disable_log_channel", description="Disable the log channel")
async def cmd_disable_log_channel(ctx: commands.Context):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    DATA["log_channel"] = None
    save_data()
    await ctx.reply("✅ Log channel disabled")

@bot.hybrid_command(name="logs", description="Show recent logs (default 10)")
async def cmd_logs(ctx: commands.Context, amount: int = 10):
    amount = max(1, min(100, amount))
    items = DATA.get("logs", [])[-amount:]
    if not items:
        return await ctx.reply("No logs.")
    lines = [f"{i+1}. [{e['time']}] {e['command']} — {e['user']} in {e['channel']}" for i,e in enumerate(items)]
    msg = "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[-1900:]
    await ctx.reply(f"```\n{msg}\n```", ephemeral=True)

# -- Triggers (add/remove/list + UI) --
@bot.hybrid_command(name="trigger_add", description="Add exact-word trigger (Admin/Pookie). Use {user} to mention.")
async def cmd_trigger_add(ctx: commands.Context, word: str, *, reply: str):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    DATA.setdefault("triggers", {})[word.lower()] = reply
    save_data()
    await ctx.reply(f"✅ Trigger added: `{word}` -> {reply}")

@bot.hybrid_command(name="trigger_remove", description="Remove a trigger")
async def cmd_trigger_remove(ctx: commands.Context, word: str):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    if (DATA.get("triggers") or {}).pop(word.lower(), None) is None:
        return await ctx.reply("Trigger not found.")
    save_data()
    await ctx.reply(f"✅ Removed trigger `{word}`")

@bot.hybrid_command(name="trigger_list", description="List triggers")
async def cmd_trigger_list(ctx: commands.Context):
    t = DATA.get("triggers", {}) or {}
    if not t:
        return await ctx.reply("No triggers set.", ephemeral=True)
    lines = [f"`{k}` -> {v}" for k,v in t.items()]
    # If invoked as a slash we can show a button to add
    view = TriggerView()
    await ctx.reply("\n".join(lines), view=view, ephemeral=True)

# -- Cat & scheduling --
@bot.hybrid_command(name="cat", description="Get a random cat image")
async def cmd_cat(ctx: commands.Context):
    await ctx.defer()
    url = await fetch_cat_url()
    if not url:
        await ctx.followup.send("Could not fetch a cat right now.")
    else:
        await ctx.followup.send(url)
    log_command(ctx.author, "cat", ctx.channel)

@bot.hybrid_command(name="setdailycatchannel", description="Set daily cat channel at 11:00 local TZ (Admin/Pookie)")
async def cmd_setdailycatchannel(ctx: commands.Context, channel: discord.TextChannel):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    DATA["daily_cat_channel"] = int(channel.id)
    save_data()
    await ctx.reply(f"Daily cat channel set to {channel.mention}")

@bot.hybrid_command(name="sethourlycatchannel", description="Set hourly cat channel and interval hours (Admin/Pookie)")
async def cmd_sethourlycatchannel(ctx: commands.Context, channel: discord.TextChannel, interval_hours: int = 1):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    h = DATA.setdefault("hourly_cat", {})
    h["channel"] = int(channel.id)
    h["interval_hours"] = max(1, min(24, int(interval_hours)))
    h["enabled"] = True
    save_data()
    await ctx.reply(f"Hourly cats enabled in {channel.mention} every {h['interval_hours']} hours")

@bot.hybrid_command(name="hourlycat_on", description="Enable hourly cats")
async def cmd_hourlycat_on(ctx: commands.Context):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    DATA.setdefault("hourly_cat", {})["enabled"] = True
    save_data()
    await ctx.reply("Hourly cat posting enabled.")

@bot.hybrid_command(name="hourlycat_off", description="Disable hourly cats")
async def cmd_hourlycat_off(ctx: commands.Context):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    DATA.setdefault("hourly_cat", {})["enabled"] = False
    save_data()
    await ctx.reply("Hourly cat posting disabled.")

@tasks.loop(minutes=1)
async def daily_cat_loop():
    try:
        now = datetime.now(timezone.utc).astimezone(LOCAL_TZ)
        if now.hour == 11 and now.minute == 0:
            ch_id = DATA.get("daily_cat_channel")
            if not ch_id:
                return
            ch = bot.get_channel(int(ch_id))
            if ch:
                url = await fetch_cat_url()
                if url:
                    try:
                        await ch.send(f"🐱 Daily Cat — {now.strftime('%Y-%m-%d %H:%M %Z')}\n{url}")
                        log_command(bot.user, "daily_cat", ch)
                    except Exception:
                        pass
    except Exception:
        pass

@tasks.loop(minutes=5)
async def hourly_cat_loop():
    try:
        h = DATA.get("hourly_cat", {})
        if not h.get("enabled"):
            return
        ch_id = h.get("channel")
        if not ch_id:
            return
        interval = max(1, int(h.get("interval_hours", 1))) * 3600
        last = float(h.get("last_sent", 0))
        now_ts = time.time()
        if now_ts - last >= interval:
            ch = bot.get_channel(int(ch_id))
            if ch:
                url = await fetch_cat_url()
                if url:
                    try:
                        await ch.send(f"⏰ Hourly Cat — {datetime.now(timezone.utc).astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M %Z')}\n{url}")
                        h["last_sent"] = now_ts
                        DATA["hourly_cat"] = h
                        save_data()
                        log_command(bot.user, "hourly_cat", ch)
                    except Exception:
                        pass
    except Exception:
        pass

# -- Snipe / Esnipe --
@bot.hybrid_command(name="snipe", description="Show recently deleted messages in this channel")
async def cmd_snipe(ctx: commands.Context):
    ch_id = str(ctx.channel.id)
    items = DATA.get("snipes", {}).get(ch_id, [])
    if not items:
        return await ctx.reply("Nothing to snipe.", ephemeral=True)
    view = NavView(items)
    await ctx.reply(embed=view.embed_for(), view=view, ephemeral=True)

@bot.hybrid_command(name="esnipe", description="Show recently edited messages in this channel")
async def cmd_esnipe(ctx: commands.Context):
    ch_id = str(ctx.channel.id)
    items = DATA.get("esnipes", {}).get(ch_id, [])
    if not items:
        return await ctx.reply("Nothing to e-snipe.", ephemeral=True)
    view = NavView(items)
    await ctx.reply(embed=view.embed_for(), view=view, ephemeral=True)

# -- Moderation: kick / ban / purge --
@bot.hybrid_command(name="kick", description="Kick a member (Admin/Pookie/Owner)")
async def cmd_kick(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason"):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    try:
        await member.kick(reason=reason)
        log_command(ctx.author, f"kick {member}", ctx.channel)
        await ctx.reply(f"✅ Kicked {member.mention}")
    except Exception as e:
        await ctx.reply(f"Error: {e}")

@bot.hybrid_command(name="ban", description="Ban a member (Admin/Pookie/Owner)")
async def cmd_ban(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason"):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    try:
        await member.ban(reason=reason, delete_message_days=0)
        log_command(ctx.author, f"ban {member}", ctx.channel)
        await ctx.reply(f"✅ Banned {member.mention}")
    except Exception as e:
        await ctx.reply(f"Error: {e}")

@bot.hybrid_command(name="purge", description="Purge messages (<=100) (Admin/Pookie)")
async def cmd_purge(ctx: commands.Context, amount: int = 10):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    amount = max(1, min(100, amount))
    try:
        deleted = await ctx.channel.purge(limit=amount)
        log_command(ctx.author, f"purge {len(deleted)}", ctx.channel)
        await ctx.reply(f"Deleted {len(deleted)} messages.", delete_after=6)
    except Exception as e:
        await ctx.reply(f"Error: {e}")

# -- Role management & temp role --
@bot.hybrid_command(name="giverole", description="Give role to a member (Admin/Pookie)")
async def cmd_giverole(ctx: commands.Context, member: discord.Member, role: discord.Role):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    try:
        await member.add_roles(role, reason=f"by {ctx.author}")
        log_command(ctx.author, f"giverole {role.id} to {member.id}", ctx.channel)
        await ctx.reply(f"✅ Gave {role.mention} to {member.mention}")
    except Exception as e:
        await ctx.reply(f"Error: {e}")

@bot.hybrid_command(name="removerole", description="Remove role from a member (Admin/Pookie)")
async def cmd_removerole(ctx: commands.Context, member: discord.Member, role: discord.Role):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    try:
        await member.remove_roles(role, reason=f"by {ctx.author}")
        log_command(ctx.author, f"removerole {role.id} from {member.id}", ctx.channel)
        await ctx.reply(f"✅ Removed {role.mention} from {member.mention}")
    except Exception as e:
        await ctx.reply(f"Error: {e}")

@bot.hybrid_command(name="temprole", description="Give a role temporarily, e.g., 10m 12h 4d (Admin/Pookie)")
async def cmd_temprole(ctx: commands.Context, member: discord.Member, role: discord.Role, duration: str):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    seconds = parse_duration(duration)
    if not seconds:
        return await ctx.reply("Invalid duration. Use `10m`, `12h`, `4d`.")
    try:
        await member.add_roles(role, reason=f"temp by {ctx.author} for {duration}")
        log_command(ctx.author, f"temprole {role.id} to {member.id} for {duration}", ctx.channel)
        await ctx.reply(f"✅ Gave {role.mention} to {member.mention} for {duration}")
        async def _rem():
            await asyncio.sleep(seconds)
            try:
                await member.remove_roles(role, reason="temp role expired")
            except Exception:
                pass
        bot.loop.create_task(_rem())
    except Exception as e:
        await ctx.reply(f"Error: {e}")

# -- Mute / Unmute helpers & commands --
async def ensure_muted_role(guild: discord.Guild) -> discord.Role:
    role = discord.utils.get(guild.roles, name="Muted")
    if role:
        return role
    role = await guild.create_role(name="Muted", reason="Create mute role")
    for ch in guild.channels:
        try:
            if isinstance(ch, discord.TextChannel):
                await ch.set_permissions(role, send_messages=False, add_reactions=False)
            elif isinstance(ch, discord.VoiceChannel):
                await ch.set_permissions(role, speak=False, connect=False)
        except Exception:
            pass
    return role

@bot.hybrid_command(name="mute", description="Mute a user for duration, e.g., 10m (Admin/Pookie)")
async def cmd_mute(ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = "Muted"):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    seconds = parse_duration(duration)
    if not seconds:
        return await ctx.reply("Invalid duration.")
    try:
        role = await ensure_muted_role(ctx.guild)
        await member.add_roles(role, reason=f"{reason} by {ctx.author}")
        log_command(ctx.author, f"mute {member.id} for {duration}", ctx.channel)
        await ctx.reply(f"🔇 Muted {member.mention} for {duration}")
        async def _unmute():
            await asyncio.sleep(seconds)
            try:
                await member.remove_roles(role, reason="Mute expired")
            except Exception:
                pass
        bot.loop.create_task(_unmute())
    except Exception as e:
        await ctx.reply(f"Error: {e}")

@bot.hybrid_command(name="unmute", description="Unmute a member (Admin/Pookie)")
async def cmd_unmute(ctx: commands.Context, member: discord.Member):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    role = discord.utils.get(ctx.guild.roles, name="Muted")
    if not role:
        return await ctx.reply("Muted role not found.")
    try:
        await member.remove_roles(role, reason=f"Unmuted by {ctx.author}")
        log_command(ctx.author, f"unmute {member.id}", ctx.channel)
        await ctx.reply(f"🔈 Unmuted {member.mention}")
    except Exception as e:
        await ctx.reply(f"Error: {e}")

# -- Lock / Unlock channel --
@bot.hybrid_command(name="lock", description="Lock a channel for @everyone (Admin/Pookie)")
async def cmd_lock(ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    channel = channel or ctx.channel
    try:
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        log_command(ctx.author, f"lock {channel.id}", ctx.channel)
        await ctx.reply(f"🔒 Locked {channel.mention}")
    except Exception as e:
        await ctx.reply(f"Error: {e}")

@bot.hybrid_command(name="unlock", description="Unlock a channel for @everyone (Admin/Pookie)")
async def cmd_unlock(ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    channel = channel or ctx.channel
    try:
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = True
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        log_command(ctx.author, f"unlock {channel.id}", ctx.channel)
        await ctx.reply(f"🔓 Unlocked {channel.mention}")
    except Exception as e:
        await ctx.reply(f"Error: {e}")

# -- Warns & Blacklist --
@bot.hybrid_command(name="warn", description="Warn a user (Admin/Pookie)")
async def cmd_warn(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason"):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    uid = str(member.id)
    w = {"by": f"{ctx.author} ({ctx.author.id})", "reason": reason, "time": now_local_str()}
    DATA.setdefault("warns", {}).setdefault(uid, []).append(w)
    save_data()
    log_command(ctx.author, f"warn {member.id}", ctx.channel)
    await ctx.reply(f"⚠️ Warned {member.mention}: {reason}")

@bot.hybrid_command(name="show_warns", description="Show warns for a user")
async def cmd_show_warns(ctx: commands.Context, member: discord.Member):
    uid = str(member.id)
    lst = DATA.get("warns", {}).get(uid, [])
    if not lst:
        return await ctx.reply(f"{member.mention} has no warns.")
    embed = discord.Embed(title=f"Warns for {member}", color=discord.Color.orange())
    for i,w in enumerate(lst, start=1):
        embed.add_field(name=f"#{i}", value=f"By: {w['by']}\nAt: {w['time']}\nReason: {w['reason']}", inline=False)
    await ctx.reply(embed=embed)

@bot.hybrid_command(name="remove_warn", description="Remove a warn by index (Admin/Pookie)")
async def cmd_remove_warn(ctx: commands.Context, member: discord.Member, index: int):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    uid = str(member.id)
    lst = DATA.get("warns", {}).get(uid, [])
    if 1 <= index <= len(lst):
        removed = lst.pop(index-1)
        DATA["warns"][uid] = lst
        save_data()
        log_command(ctx.author, f"remove_warn {member.id} #{index}", ctx.channel)
        await ctx.reply(f"Removed warn #{index} for {member.mention}: {removed['reason']}")
    else:
        await ctx.reply("Invalid warn index.")

@bot.hybrid_command(name="clear_warns", description="Clear all warns for a user (Admin/Pookie)")
async def cmd_clear_warns(ctx: commands.Context, member: discord.Member):
    if not is_admin_or_pookie(ctx.author):
        return await ctx.reply("❌ No permission.")
    DATA.setdefault("warns", {})[str(member.id)] = []
    save_data()
    log_command(ctx.author, f"clear_warns {member.id}", ctx.channel)
    await ctx.reply(f"Cleared all warns for {member.mention}")

@bot.hybrid_command(name="blacklist", description="Blacklist a user (Admin/Pookie/Owner)")
async def cmd_blacklist(ctx: commands.Context, user: discord.User):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    uid = int(user.id)
    if uid not in DATA.get("blacklist", []):
        DATA.setdefault("blacklist", []).append(uid)
        save_data()
    await ctx.reply(f"Blacklisted {user.mention}")

@bot.hybrid_command(name="unblacklist", description="Remove user from blacklist (Admin/Pookie/Owner)")
async def cmd_unblacklist(ctx: commands.Context, user: discord.User):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    uid = int(user.id)
    if uid in DATA.get("blacklist", []):
        DATA["blacklist"].remove(uid)
        save_data()
    await ctx.reply(f"Removed {user.mention} from blacklist")

# -- Admin & Pookie management --
@bot.hybrid_command(name="add_admin", description="Owner: add admin")
async def cmd_add_admin(ctx: commands.Context, user: discord.User):
    if not is_owner(ctx.author):
        return await ctx.reply("❌ Owner only.")
    uid = int(user.id)
    if uid not in DATA.get("admins", []):
        DATA.setdefault("admins", []).append(uid)
        save_data()
    await ctx.reply(f"Added admin: {user.mention}")

@bot.hybrid_command(name="remove_admin", description="Owner: remove admin")
async def cmd_remove_admin(ctx: commands.Context, user: discord.User):
    if not is_owner(ctx.author):
        return await ctx.reply("❌ Owner only.")
    uid = int(user.id)
    if uid in DATA.get("admins", []):
        DATA["admins"].remove(uid)
        save_data()
    await ctx.reply(f"Removed admin: {user.mention}")

@bot.hybrid_command(name="listadmin", description="List admins (persistent + defaults)")
async def cmd_list_admin(ctx: commands.Context):
    admins_list = set(DATA.get("admins", []) or []) | DEFAULT_ADMINS
    await ctx.reply(", ".join(f"<@{a}>" for a in admins_list))

@bot.hybrid_command(name="add_pookie", description="Add pookie (Admin/Owner)")
async def cmd_add_pookie(ctx: commands.Context, user: discord.User):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    uid = int(user.id)
    if uid not in DATA.get("pookies", []):
        DATA.setdefault("pookies", []).append(uid)
        save_data()
    await ctx.reply(f"Added pookie: {user.mention}")

@bot.hybrid_command(name="remove_pookie", description="Remove pookie (Admin/Owner)")
async def cmd_remove_pookie(ctx: commands.Context, user: discord.User):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    uid = int(user.id)
    if uid in DATA.get("pookies", []):
        DATA["pookies"].remove(uid)
        save_data()
    await ctx.reply(f"Removed pookie: {user.mention}")

@bot.hybrid_command(name="listpookie", description="List pookies")
async def cmd_list_pookie(ctx: commands.Context):
    pks = DATA.get("pookies", []) or []
    if not pks:
        return await ctx.reply("No pookies set.")
    await ctx.reply(", ".join(f"<@{p}>" for p in pks))

# -- Server info & servers --
@bot.hybrid_command(name="serverinfo", description="Detailed server info")
async def cmd_serverinfo(ctx: commands.Context):
    g = ctx.guild
    if not g:
        return await ctx.reply("Use this inside a server.")
    owner = g.owner or await g.fetch_owner()
    embed = discord.Embed(title=f"{g.name}", color=discord.Color.blurple())
    embed.set_thumbnail(url=g.icon.url if g.icon else discord.Embed.Empty)
    embed.add_field(name="ID", value=str(g.id))
    embed.add_field(name="Owner", value=f"{owner} ({owner.id})")
    embed.add_field(name="Created", value=g.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"))
    embed.add_field(name="Members", value=str(g.member_count))
    embed.add_field(name="Text/Voice", value=f"{len(g.text_channels)}/{len(g.voice_channels)}")
    embed.add_field(name="Roles", value=str(len(g.roles)))
    embed.add_field(name="Boosts", value=str(g.premium_subscription_count))
    await ctx.reply(embed=embed)

@bot.hybrid_command(name="servers", description="List bot servers (Admin/Pookie/Owner)")
async def cmd_servers(ctx: commands.Context):
    if not is_admin_or_pookie(ctx.author) and not is_owner(ctx.author):
        return await ctx.reply("❌ No permission.")
    lines = [f"**{g.name}** (`{g.id}`) — Members: {g.member_count} — Owner: {g.owner or g.owner_id}" for g in bot.guilds]
    out = "\n".join(lines)
    if len(out) > 1900:
        out = out[:1900] + "…"
    await ctx.reply(out, ephemeral=True)

# --------------------- Fun commands (8ball, joke, dadjoke, coin, roll, rps) ---------------------
EIGHT_BALL = [
    "It is certain.", "It is decidedly so.", "Without a doubt.", "Yes — definitely.",
    "You may rely on it.", "As I see it, yes.", "Most likely.", "Outlook good.",
    "Yes.", "Signs point to yes.", "Reply hazy, try again.", "Ask again later.",
    "Better not tell you now.", "Cannot predict now.", "Concentrate and ask again.",
    "Don't count on it.", "My reply is no.", "My sources say no.", "Outlook not so good.", "Very doubtful."
]

@bot.hybrid_command(name="8ball", description="Ask the magic 8-ball a question")
async def cmd_8ball(ctx: commands.Context, *, question: str):
    if not question:
        return await ctx.reply("Ask a full question.")
    answer = random.choice(EIGHT_BALL)
    await ctx.reply(f"🎱 {answer}")
    log_command(ctx.author, "8ball", ctx.channel)

@bot.hybrid_command(name="joke", description="Get a random joke")
async def cmd_joke(ctx: commands.Context):
    await ctx.defer()
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get("https://official-joke-api.appspot.com/jokes/random", timeout=10) as resp:
                j = await resp.json()
                if isinstance(j, dict):
                    out = f"{j.get('setup')}\n{j.get('punchline')}"
                else:
                    out = "Couldn't fetch a joke."
    except Exception:
        out = "Couldn't fetch a joke."
    await ctx.followup.send(out)
    log_command(ctx.author, "joke", ctx.channel)

@bot.hybrid_command(name="dadjoke", description="Get a dad joke")
async def cmd_dadjoke(ctx: commands.Context):
    await ctx.defer()
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get("https://icanhazdadjoke.com/", headers={"Accept":"application/json"}, timeout=10) as resp:
                j = await resp.json()
                out = j.get("joke", "No dad joke right now.")
    except Exception:
        out = "Couldn't fetch a dad joke."
    await ctx.followup.send(out)
    log_command(ctx.author, "dadjoke", ctx.channel)

@bot.hybrid_command(name="coin", description="Flip a coin")
async def cmd_coin(ctx: commands.Context):
    await ctx.reply(random.choice(["🪙 Heads", "🪙 Tails"]))
    log_command(ctx.author, "coin", ctx.channel)

@bot.hybrid_command(name="roll", description="Roll dice: e.g., roll 1d6 or roll 20")
async def cmd_roll(ctx: commands.Context, spec: str = "1d6"):
    # support formats: NdM or a single max int
    try:
        if "d" in spec:
            n, m = spec.lower().split("d")
            n = max(1, min(100, int(n)))
            m = max(1, min(1000, int(m)))
            rolls = [random.randint(1, m) for _ in range(n)]
            await ctx.reply(f"🎲 Rolled {spec}: {rolls} → total {sum(rolls)}")
        else:
            m = int(spec)
            m = max(1, min(1000, m))
            r = random.randint(1, m)
            await ctx.reply(f"🎲 Rolled 1d{m}: {r}")
    except Exception:
        await ctx.reply("Invalid format. Use `NdM` or a number like `20`.")
    log_command(ctx.author, "roll", ctx.channel)

@bot.hybrid_command(name="rps", description="Play Rock Paper Scissors")
async def cmd_rps(ctx: commands.Context, choice: str):
    choice = choice.lower()
    if choice not in ("rock","paper","scissors","r","p","s"):
        return await ctx.reply("Choose: rock/paper/scissors")
    mapping = {"r":"rock","p":"paper","s":"scissors"}
    user = mapping.get(choice, choice) if choice in mapping else choice
    bot_choice = random.choice(["rock","paper","scissors"])
    result = "It's a tie!"
    if user == bot_choice:
        result = "It's a tie!"
    elif (user=="rock" and bot_choice=="scissors") or (user=="scissors" and bot_choice=="paper") or (user=="paper" and bot_choice=="rock"):
        result = "You win!"
    else:
        result = "You lose!"
    await ctx.reply(f"You: {user} — Bot: {bot_choice} — {result}")
    log_command(ctx.author, "rps", ctx.channel)

# --------------------- Snipe navigation done earlier ---------------------

# --------------------- Startup ---------------------
if __name__ == "__main__":
    print(f"Starting bot — owner={OWNER_ID} TZ={TZ_NAME} defaults={DEFAULT_ADMINS}")
    try:
        daily_cat_loop.start()
    except RuntimeError:
        pass
    try:
        hourly_cat_loop.start()
    except RuntimeError:
        pass
    bot.run(DISCORD_BOT_TOKEN)
