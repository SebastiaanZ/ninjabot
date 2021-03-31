from __future__ import annotations

import abc
import asyncio
import collections
import datetime
import enum
import itertools
import logging
import random
import typing

import discord
from discord import utils

from ninja_bot import settings
from ninja_bot.bot import NinjaBot

log = logging.getLogger("ninja_bot.ninja_hunt")

COOLDOWN = settings.game.cooldown
MAX_TIME_JITTER = settings.game.max_time_jitter
MAX_POINTS = settings.game.max_points
TIMEOUT = settings.game.reaction_timeout
SECONDS_PER_POINT = TIMEOUT / MAX_POINTS
FALLBACK_EMOJI_ID = settings.guild.emoji_id
P_MULTIPLIER = settings.game.probability_multiplier


class NinjaError(Exception):
    """Base class for all Ninja-related errors."""


class InvalidPhase(NinjaError):
    """Raised when the current phase does not match the expected phase."""


ReactionPoints = collections.namedtuple("ReactionPoints", "member points")


def schedule_task_with_result_handling(
    coroutine: typing.Coroutine, *, name: str
) -> asyncio.Task:
    """Schedule a task and add a result done callback."""
    task = asyncio.create_task(coroutine, name=name)
    task.add_done_callback(task_result_callback)
    return task


def task_result_callback(task: asyncio.Task) -> None:
    """Check if our sleep task ended in an expected way."""
    name = task.get_name()
    if task.cancelled():
        log.debug(f"{name} task callback: the task was cancelled")
        return

    if exc := task.exception():
        log.exception(f"{name} task callback: the task failed!", exc_info=exc)

    log.debug(f"{name} task callback: the task ended normally.")


async def safe_discord_action(coroutine):
    try:
        return await coroutine
    except discord.DiscordException:
        log.exception(f"Failed to execute discord action {coroutine.__name__}")


class NinjaPhase(abc.ABC):
    def __init__(self):
        self._future = asyncio.Future()
        self._cancelled = False
        self._finished = False
        self._timestamp = datetime.datetime.utcnow()

    def __repr__(self) -> str:
        """Return a string representation of this state."""
        cls_name = type(self).__name__
        cancelled = self._cancelled
        finished = self._finished
        return f"{cls_name}({finished=}, {cancelled=})"

    async def run(self):
        try:
            return await self._future
        except asyncio.CancelledError:
            self._cancel()
        finally:
            self._finish()

    @abc.abstractmethod
    async def __aenter__(self) -> NinjaPhase:
        """Prepare the context of this phase."""

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Clean up the context of this phase."""
        if exc_type is not None:
            log.exception(f"something went wrong during phase {self!r}")
        return True

    @abc.abstractmethod
    def _cancel(self) -> None:
        """Cancel the current ongoing NinjaPhase."""

    def _finish(self) -> None:
        """Mark the current NinjaPhase as done."""
        self._finished = True

    @property
    def cancelled(self) -> bool:
        """Check if the current NinjaPhase is done."""
        return self._cancelled

    @property
    def finished(self) -> bool:
        """Check if the current NinjaPhase is done."""
        return self._finished

    @property
    def active(self) -> bool:
        return not self._finished and not self._cancelled


class SleepingPhase(NinjaPhase):
    def __init__(self) -> None:
        super().__init__()
        self._sleep_duration = COOLDOWN + random.randint(1, MAX_TIME_JITTER)
        self._task = schedule_task_with_result_handling(self.sleep(), name="sleep")

    async def __aenter__(self) -> SleepingPhase:
        """Enter the sleeping phase of the game."""
        log.debug("Running SleepingPhase.__aenter__")
        return self

    async def __aexit__(self, *args, **kwargs) -> bool:
        """Clean up the sleeping phase of the game."""
        log.debug("Running SleepingPhase.__aexit__")
        if not self._task.done():
            self._task.cancel()
        return await super().__aexit__(*args, **kwargs)

    async def sleep(self) -> None:
        """Schedule a wake-up for our sleep phase."""
        time = self.time_remaining
        until_dt = datetime.datetime.utcnow() + datetime.timedelta(seconds=time)
        log.info(f"Sleeping {time} seconds until {until_dt:%Y-%m-%d %H:%M:%S}.")
        await asyncio.sleep(time)
        self._future.set_result(None)

    @property
    def time_remaining(self) -> float:
        """Return the time in seconds remaining that we'll sleep."""
        time_passed = (datetime.datetime.utcnow() - self._timestamp).total_seconds()
        return max(self._sleep_duration - time_passed, 0.0)

    def _cancel(self) -> None:
        """Cancel the current sleeping phase."""
        log.info("cancelling the current sleep phase.")
        if self._task:
            self._task.cancel()
        self._cancelled = True


class HuntingPhase(NinjaPhase):
    def __init__(self, bot: NinjaBot) -> None:
        super().__init__()
        self._bot = bot
        self._probability = itertools.count(1)

    async def hunt_for_messages(self, message: discord.Message) -> None:
        """Hunt for a message to react to!"""
        if not self.active or self._ignored_message(message):
            return

        # If we fail this check, this message is not the lucky one!
        if random.random() > P_MULTIPLIER * self.ninja_probability:
            return

        log.debug("This message is getting ducked!")
        self._future.set_result(message)

    async def __aenter__(self) -> HuntingPhase:
        """Enter the hunting phase!"""
        self._bot.add_listener(self.hunt_for_messages, name="on_message")
        return self

    async def __aexit__(self, *args, **kwargs) -> bool:
        self._bot.remove_listener(self.hunt_for_messages, name="on_message")
        return await super().__aexit__(*args, **kwargs)

    @property
    def ninja_probability(self) -> float:
        """Calculate the probability for a message to be ninja-ducked."""
        p = next(self._probability)
        return min(p / 100, 1.0)

    def _finish(self) -> None:
        """Mark the phase as finished and schedule the exit callback."""
        self._finished = True

    def _cancel(self) -> None:
        """Cancel the current HuntingPhase without completing it."""
        log.info("cancelling the current hunting phase.")
        self._cancelled = True

    @staticmethod
    def _ignored_message(message: discord.Message) -> bool:
        """Return `True` if the message should be ignored."""
        if getattr(message.guild, "id", None) != settings.guild.guild_id:
            return True

        if message.author.bot:
            return True

        category_id = message.channel.category_id
        channel_id = message.channel.id

        if settings.game.public_only:
            default_role = message.guild.default_role
            default_user = discord.Object(id=1)
            default_user._roles = utils.SnowflakeList([default_role.id])
            permissions = message.channel.permissions_for(default_user)
            if not permissions.read_messages or not permissions.send_messages:
                return True

        if channel_id in settings.permissions.channels.deny:
            return True

        if channel_id in settings.permissions.channels.allow:
            return False

        if category_id in settings.permissions.categories.deny:
            return True

        if category_id in settings.permissions.categories.allow:
            return False

        return True


class ReactionPhase(NinjaPhase):
    """The phase where the magic happens: The Ninja Reacts!"""

    def __init__(
        self, bot: NinjaBot, message: discord.Message, fallback_emoji: discord.Emoji
    ):
        super().__init__()
        self._bot = bot
        self._guild: discord.Guild = message.guild
        self._message = message
        self._fallback_emoji = fallback_emoji
        self._new_emoji: typing.Optional[discord.Emoji] = None
        self._task = schedule_task_with_result_handling(
            self.reaction_wait(), name="wait"
        )
        self._rewarded_users = {}
        self._used_emoji: typing.Optional[discord.Emoji] = None

    async def __aenter__(self) -> ReactionPhase:
        """Prepare the reaction phase."""
        self._bot.add_listener(self._listen_for_reactions, "on_raw_reaction_add")
        self._used_emoji = await self._prepare_ninja_emoji()
        await self._message.add_reaction(self._used_emoji)
        return self

    async def __aexit__(self, *args, **kwargs):
        await safe_discord_action(self._message.clear_reaction(self._used_emoji))
        if self._new_emoji:
            await safe_discord_action(self._new_emoji.delete())

        await asyncio.sleep(2)
        self._bot.remove_listener(self._listen_for_reactions, "on_raw_reaction_add")
        return await super().__aexit__(*args, **kwargs)

    async def _prepare_ninja_emoji(self) -> discord.Emoji:
        self._new_emoji = await safe_discord_action(
            self._guild.create_custom_emoji(
                name=random.choice(settings.ninja_names),
                image=settings.ninja_image,
            )
        )
        if self._new_emoji:
            return self._new_emoji

        if self._fallback_emoji is None:
            raise NinjaError("No fallback emoji available when needed.")

        return self._fallback_emoji

    async def reaction_wait(self) -> None:
        """Schedule the time-out for the reaction phase"""
        await asyncio.sleep(self.time_remaining)
        self._future.set_result(None)

    @property
    def awarded_points(self) -> typing.Dict[int, ReactionPoints]:
        return self._rewarded_users

    @property
    def time_remaining(self) -> float:
        """Return the time in seconds remaining in this reaction event."""
        time_passed = (datetime.datetime.utcnow() - self._timestamp).total_seconds()
        return max(TIMEOUT - time_passed, 0.0)

    def _cancel(self) -> None:
        """Cancel the current sleeping phase."""
        log.info("cancelling the current reaction phase.")
        if self._task:
            self._task.cancel()
        self._cancelled = True

    def _relevant_reaction(self, reaction: discord.RawReactionActionEvent) -> bool:
        """Check if a reaction is relevant for the event."""
        if not self.active or self._used_emoji is None:
            return False

        if reaction.user_id == self._bot.user.id:
            return False

        if reaction.message_id != self._message.id:
            # This isn't the message we added the ninja to
            return False

        if getattr(reaction.emoji, "id", None) is None:
            # This is a unicode emoji and not our ninja
            return False

        if reaction.emoji.id != self._used_emoji.id:
            # This is a custom emoji, but not our current ninja
            return False

        if reaction.user_id in self._rewarded_users:
            # This user has already been rewarded for this ninja
            return False

        # Non of the exclusion criteria triggered, so it must be
        # relevant.
        return True

    def _calculate_win_points(self):
        """Calculate the win points based on the time left."""
        lost_points = int((TIMEOUT - self.time_remaining) / SECONDS_PER_POINT)
        return max(MAX_POINTS - lost_points, 1)

    async def _listen_for_reactions(
        self, raw_reaction: discord.RawReactionActionEvent
    ) -> None:
        """Listen to reactions by users."""
        if not self._relevant_reaction(raw_reaction):
            return

        member = raw_reaction.member
        if member is None:
            member = await self._message.guild.fetch_member(raw_reaction.user_id)

        self._rewarded_users[raw_reaction.user_id] = ReactionPoints(
            member=member, points=self._calculate_win_points()
        )


class GameState(enum.Enum):
    NOT_RUNNING = 0
    SLEEPING = 1
    HUNTING = 2
    ACTIVE_REACTION = 3
