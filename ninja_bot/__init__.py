import logging.config

from ninja_bot.settings import settings

logging.config.dictConfig(settings.logging.dict(by_alias=True))
