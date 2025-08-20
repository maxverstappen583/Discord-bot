import discord
from discord.ext import commands
from datetime import datetime

class AFK(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.afk_users = {}  # {user_id: {"reason": str, "since": datetime}}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # If AFK user sends a message -> remove AFK
        if message.author.id in self.afk_users:
            data = self.afk_users.pop(message.author.id)
            since = data["since"].strftime("%H:%M:%S")
            await message.channel.send(
                f"👋 Welcome back {message.author.mention}, I removed your AFK (since {since})."
            )

        # If someone mentions an AFK user
        if message.mentions:
            for user in message.mentions:
                if user.id in self.afk_users:
                    data = self.afk_users[user.id]
                    reason = data['reason']
                    since = data['since'].strftime("%H:%M:%S")
                    await message.channel.send(
                        f"💤 {user.display_name} is AFK (since {since}): {reason}"
                    )

    @commands.hybrid_command(name="afk", description="Set yourself as AFK with an optional reason.")
    async def afk(self, ctx: commands.Context, *, reason: str = "AFK"):
        """Slash + Prefix command to go AFK"""
        self.afk_users[ctx.author.id] = {"reason": reason, "since": datetime.now()}
        await ctx.send(f"✅ {ctx.author.mention}, I set your AFK: {reason}")

async def setup(bot):
    await bot.add_cog(AFK(bot))
