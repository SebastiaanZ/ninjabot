from discord.ext import commands

from .cog import NinjaHunt


def setup(bot: commands.Bot):
    """Set up the ninja_hunt extension."""
    bot.add_cog(NinjaHunt(bot))
