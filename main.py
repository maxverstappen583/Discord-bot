import os
import discord
from discord.ext import commands

# Get token from environment variable (set in Render)
TOKEN = os.getenv("TOKEN")

# Setup intents
intents = discord.Intents.default()
intents.message_content = True

# Setup bot
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

@bot.command()
async def hello(ctx):
    await ctx.send("Hello! I'm alive 👋")

# Run bot
bot.run(TOKEN)
