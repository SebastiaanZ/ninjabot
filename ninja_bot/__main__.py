import asyncio
import logging

import async_rediscache
import discord

from .bot import NinjaBot
from .settings import settings

log = logging.getLogger("ninja_bot")


loop = asyncio.get_event_loop()

redis_session = async_rediscache.RedisSession(
    address=("redis", 6379),
    minsize=1,
    maxsize=20,
    use_fakeredis=False,
    global_namespace="ninja_bot",
)

loop.run_until_complete(redis_session.connect())

log.info("Initializing NinjaBot")
bot = NinjaBot(
    command_prefix="$",
    intents=discord.Intents.default(),
    allowed_mentions=discord.AllowedMentions(
        everyone=False,
        replied_user=False,
        roles=False,
        users=False,
    ),
    help_command=None,
    loop=loop,
    redis_session=redis_session,
)

bot.load_extension("ninja_bot.ninja_hunt")

log.info("Starting NinjaBot")
bot.run(settings.NINJABOT_TOKEN)
