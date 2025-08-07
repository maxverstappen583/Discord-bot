import discord
from discord.ext import commands
from discord import app_commands
import os

# Create the bot with default intents
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = app_commands.CommandTree(bot)

# Slash command
@tree.command(name="hello", description="Say hello")
async def hello(interaction: discord.Interaction):
    await interaction.response.send_message("Hello!")

# Sync commands and confirm bot is ready
@bot.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")

# Run the bot using token from environment variable
bot.run(os.environ["TOKEN"])
