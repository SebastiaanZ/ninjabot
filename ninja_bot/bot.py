import asyncio
import logging
import traceback

import async_rediscache
import discord
from discord.ext import commands

from ninja_bot.settings import settings

log = logging.getLogger(__name__)


class NinjaBot(commands.Bot):
    """No one expects the Ninja Duck!"""

    def __init__(self, redis_session: async_rediscache.RedisSession, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._guild_ready = asyncio.Event()
        self.redis_session = redis_session

    def load_extension(self, extension: str) -> None:
        """Load the extension by its importable path."""
        log.info(f"Loading extension: {extension}")
        super().load_extension(extension)

    async def on_guild_available(self, guild: discord.Guild) -> None:
        if guild.id != settings.guild.guild_id:
            return

        if not guild.roles or not guild.members or not guild.channels:
            return

        self._guild_ready.set()
        self.dispatch("guild_ready", guild)

    async def on_guild_unavailable(self, guild: discord.Guild) -> None:
        """Clear the internal guild available event when constants.Guild.id becomes unavailable."""
        if guild.id != settings.guild.guild_id:
            return

        self._guild_ready.clear()

    async def wait_until_guild_ready(self) -> None:
        """Wait until the guild cache is ready."""
        await self._guild_ready.wait()

    async def on_command_error(
        self, ctx: commands.Context, e: commands.errors.CommandError
    ) -> None:
        """
        Handle error emitted by commands.

        No sophisticated error handlers here, just logging.
        """
        if isinstance(e, commands.errors.CommandNotFound):
            return

        if isinstance(e, commands.errors.CheckFailure):
            return

        log.exception("Oh, no! Something went wrong...", exc_info=e)

    async def on_error(self, event_method, *args, **kwargs):
        """|coro|

        The default error handler provided by the client.

        By default this prints to :data:`sys.stderr` however it could be
        overridden to have a different implementation.
        Check :func:`~discord.on_error` for more details.
        """
        logging.error(f"An error occurred:\n{traceback.format_exc()}")

    async def close(self) -> None:
        await super().close()
        if not self.redis_session.closed:
            await self.redis_session.close()
