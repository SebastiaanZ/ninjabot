import asyncio
import collections
import datetime
import itertools
import logging
import operator
import typing

import async_rediscache
import discord
from discord.ext import commands
from discord.ext.commands import has_any_role

from ninja_bot.bot import NinjaBot
from ninja_bot.ninja_hunt.game import (
    GameState,
    HuntingPhase,
    ReactionPhase,
    ReactionPoints,
    SleepingPhase,
)
from ninja_bot.settings import AllowDenyGroup, settings
from ninja_bot.utils.checks import in_commands_channel
from ninja_bot.utils.numbers import ordinal_number

COOLDOWN = settings.game.cooldown
MAX_TIME_JITTER = settings.game.max_time_jitter
MAX_POINTS = settings.game.max_points
TIMEOUT = settings.game.reaction_timeout
FALLBACK_EMOJI_ID = settings.guild.emoji_id
NINJA_EMOJI = settings.guild.emoji_full
CONFIRM_EMOJI_ID = settings.guild.emoji_confirm
DENY_EMOJI_ID = settings.guild.emoji_deny
CONFIG_ROLES = (settings.guild.admins_id, settings.guild.moderators_id)

ScoresDict = typing.Dict[int, ReactionPoints]
LeaderboardEntry = collections.namedtuple("LeaderboardEntry", ("rank", "score", "tied"))

log = logging.getLogger("ninja_bot.ninja_hunt")


class NinjaHunt(commands.Cog):
    """A class representing our message-ambushing Ninja."""

    scoreboard = async_rediscache.RedisCache()
    config = async_rediscache.RedisCache()
    stats = async_rediscache.RedisCache()
    blocked_users = async_rediscache.RedisCache()

    def __init__(self, bot: NinjaBot) -> None:
        self._bot = bot
        self._auto_start = settings.game.auto_start
        self._running = False
        self._game = self._bot.loop.create_task(self.game())
        self._state = GameState.NOT_RUNNING
        self._fallback_emoji: typing.Optional[discord.Emoji] = None
        self._emoji_confirm: typing.Optional[discord.Emoji] = None
        self._emoji_deny: typing.Optional[discord.Emoji] = None
        self._summary_channel: typing.Optional[discord.TextChannel] = None

    def cog_unload(self) -> None:
        """Tear down the game by ensuring the current phase is cleaned up."""
        self._state = GameState.NOT_RUNNING
        if not self._game.done():
            self._game.cancel()

    @commands.Cog.listener()
    async def on_guild_ready(self, guild: discord.Guild):
        log.info("The guild cache is ready!")
        self._summary_channel = guild.get_channel(settings.guild.summary_channel)
        self._fallback_emoji = await guild.fetch_emoji(FALLBACK_EMOJI_ID)
        self._emoji_confirm = await guild.fetch_emoji(CONFIRM_EMOJI_ID)
        self._emoji_deny = await guild.fetch_emoji(DENY_EMOJI_ID)

    @property
    def state(self) -> GameState:
        """Return the current state of the Ninja game."""
        return self._state

    @state.setter
    def state(self, new_state: GameState) -> None:
        """Set a new game state."""
        log.info(f"the game is now entering {new_state}")
        self._state = new_state

    async def game(self) -> None:
        """Start the game and restart the loop on failure."""
        await self._bot.wait_until_guild_ready()

        self._running = await self.config.get("running", self._auto_start)

        while self._running:
            # noinspection PyBroadException
            try:
                await self._game_loop()
            except Exception:
                log.exception("Something went wrong!")

        log.info("The game is stopped.")

    async def _sync_set(
        self, group: AllowDenyGroup, group_name: str, set_type: str
    ) -> None:
        """Sync this specific allowdeny set."""
        cache_key = f"{group_name}_{set_type}"
        cached_set = await self.config.get(cache_key)

        if cached_set is not None:
            setattr(group, set_type, [int(e) for e in cached_set.split(",") if e])
            return

        config_set = getattr(group, set_type)
        await self.config.set(cache_key, ",".join(str(e) for e in config_set))

    async def _sync_allowdeny(self) -> None:
        """Sync the settings with those recorded in Redis."""
        for group_name in ("categories", "channels"):
            group = getattr(settings.permissions, group_name)
            for set_type in ("allow", "deny"):
                await self._sync_set(group, group_name, set_type)

    @staticmethod
    def _undetected_appearance(channel: discord.TextChannel) -> discord.Embed:
        undetected = (
            f"No one noticed {NINJA_EMOJI} when " f"it appeared in {channel.mention}."
        )
        embed = discord.Embed(
            title="Ninja Duck sneaked by undetected!",
            description=undetected,
            colour=discord.Colour.from_rgb(45, 45, 45),
            timestamp=datetime.datetime.utcnow(),
        )
        return embed

    @staticmethod
    def _detected_appearance(
        scores: ScoresDict, channel: discord.TextChannel
    ) -> discord.Embed:
        pronoun, noun = ("This", "member") if len(scores) == 1 else ("These", "members")
        rewarded_users = ", ".join(f"{u.mention} (+{p})" for u, p in scores.values())

        if len(rewarded_users) >= 1800:
            rewarded_users = rewarded_users[:1800] + "..."

        description = (
            f"Ninja Duck appeared in {channel.mention}.\n\n"
            f"{pronoun} {noun} earned points: {rewarded_users}"
        )
        embed = discord.Embed(
            title=f"Ninja Duck was detected by {len(scores)} {noun}!",
            description=description,
            colour=discord.Colour.from_rgb(214, 40, 24),
            timestamp=datetime.datetime.utcnow(),
        )
        return embed

    async def _process_scores(self, scores: ScoresDict) -> None:
        """Process the scores of this round by updating the score board."""
        for member_id, score in scores.items():
            await self.scoreboard.increment(member_id, score.points)

        log.info(f"Updated scores for {len(scores)} members.")

    async def _send_embed(
        self,
        scores: ScoresDict,
        target_channel: discord.TextChannel,
    ) -> None:
        """Send an embed with the results of this round."""
        if not self._summary_channel:
            log.warning(f"No summary channel!")
            return
        if not scores:
            embed = self._undetected_appearance(channel=target_channel)
        else:
            embed = self._detected_appearance(scores=scores, channel=target_channel)

        embed.set_thumbnail(
            url="https://cdn.discordapp.com/emojis/637923502535606293.png"
        )
        await self._summary_channel.send(embed=embed)

    async def _filter_blocked_users(self, scores: ScoresDict) -> ScoresDict:
        """Filter blocked users out of the scores for this round."""
        blocked_users = await self.blocked_users.to_dict()
        return {
            user: score for user, score in scores.items() if user not in blocked_users
        }

    async def _process_results(
        self,
        scores: ScoresDict,
        target_channel: discord.TextChannel,
    ):
        """Process teh results of this round!"""
        scores = await self._filter_blocked_users(scores)
        await self._process_scores(scores)
        await self._send_embed(scores, target_channel)

    async def _game_loop(self) -> None:
        """Loop through the various phases of the game."""
        self.state = GameState.SLEEPING
        async with SleepingPhase() as sleeping_phase:
            await sleeping_phase.run()

        if not self._running:
            return

        await self._sync_allowdeny()

        self.state = GameState.HUNTING
        async with HuntingPhase(bot=self._bot) as hunting_phase:
            target_message = await hunting_phase.run()

        if not self._running:
            return

        self.state = GameState.ACTIVE_REACTION
        async with ReactionPhase(
            bot=self._bot, message=target_message, fallback_emoji=self._fallback_emoji
        ) as reaction_phase:
            await reaction_phase.run()

        await self._process_results(
            scores=reaction_phase.awarded_points,
            target_channel=target_message.channel,
        )

        log.info("Finished current game loop.")

    async def _get_sorted_leaderboard(self) -> typing.Dict[int, LeaderboardEntry]:
        scores = await self.scoreboard.to_dict()
        if not scores:
            return {}

        get_first_item = operator.itemgetter(1)
        sorted_scores = sorted(scores.items(), key=get_first_item, reverse=True)
        grouped_scores = itertools.groupby(sorted_scores, key=get_first_item)

        leaderboard = {}
        rank = 1
        for _, group in grouped_scores:
            group = list(group)
            for member in group:
                member_id, score = member
                tied = len(group) > 1
                leaderboard[member_id] = LeaderboardEntry(
                    rank=rank, score=score, tied=tied
                )
            rank += len(group)
        return leaderboard

    @commands.command("help")
    @in_commands_channel(staff_bypass=True)
    async def help(self, ctx: commands.Context, *_args, **_kwargs) -> None:
        """Show custom event help."""
        await ctx.invoke(self.ninja_group)

    @commands.group(
        name="ninja",
        aliases=("ninja_hunt", "ninja_bot", "ninjahunt", "ninjabot", "n"),
        invoke_without_command=True,
    )
    @in_commands_channel(staff_bypass=True)
    async def ninja_group(self, ctx: commands.Context) -> None:
        """Give information about the ninja event."""
        description = (
            "All day, ninja duck will sneak up on our messages. Those "
            f"who are observant may earn points by clicking on the "
            f"{NINJA_EMOJI} reaction as it appears.\n\n"
            "**How it works**\n"
            f"The bot will automatically react with {NINJA_EMOJI}. "
            "If you click that reaction before the timer runs out, "
            "you'll earn points. The quicker you react, the more "
            "points you get. \n\n"
            "*Spamming messages will not make the ninja appear sooner, "
            "so please be mindful of others.*\n\n"
            "**Commands**\n"
            "• `$ninja score` — get your personal ninja score\n"
            "• `$ninja leaderboard` — get the current top 10\n"
        )
        embed = discord.Embed(
            title=f"Spot Ninja Duck!",
            description=description,
            colour=discord.Colour.from_rgb(214, 40, 24),
        )
        embed.set_thumbnail(
            url="https://cdn.discordapp.com/emojis/637923502535606293.png"
        )
        await ctx.send(embed=embed)

    @ninja_group.command(name="score", aliases=("s",))
    @in_commands_channel(staff_bypass=True)
    async def personal_score(self, ctx: commands.Context) -> None:
        """Get your personal score from the bot."""
        leaderboard = await self._get_sorted_leaderboard()
        entry = leaderboard.get(ctx.author.id)
        if entry is None:
            description = "You have not scored any points yet."
        else:
            ordinal_rank = ordinal_number(entry.rank)
            if entry.tied:
                position = f"You're currently tied for {ordinal_rank} place."
            else:
                position = f"You're currently in {ordinal_rank} place."

            description = f"Your score is {entry.score}. {position}"

        embed = discord.Embed(
            title=f"Your ninja duck score",
            description=description,
            colour=discord.Colour.from_rgb(214, 40, 24),
        )
        await ctx.send(embed=embed)

    @ninja_group.command(name="leaderboard", aliases=("lb",))
    @in_commands_channel(staff_bypass=True)
    async def leaderboard(self, ctx: commands.Context) -> None:
        """Get the current top 10."""
        leaderboard = await self._get_sorted_leaderboard()
        top_ten = list(leaderboard.items())[:11]

        lines = []
        for member_id, entry in top_ten:
            rank = format(ordinal_number(entry.rank), " >4")
            score = format(entry.score, " >4")
            mention = f"<@{member_id}>"
            lines.append(f"`{rank} |  {score} |` {mention}")

        board_formatted = "\n".join(lines)
        description = f"`Rank | Score |` Member\n{board_formatted}"
        embed = discord.Embed(
            title="Top 10",
            description=description,
            colour=discord.Colour.from_rgb(214, 40, 24),
            timestamp=datetime.datetime.utcnow(),
        )
        embed.set_thumbnail(
            url="https://cdn.discordapp.com/emojis/637923502535606293.png"
        )
        await ctx.send(embed=embed)

    @commands.group(
        name="admin",
        aliases=("a",),
        invoke_without_command=True,
    )
    @has_any_role(*CONFIG_ROLES)
    async def admin_group(self, ctx: commands.Context) -> None:
        """Show an overview of the game's admin commands."""
        description = "\n".join(
            (
                "**Moderation Commands**",
                "`$admin block <user>` — block a user and REMOVE their score",
                "`$admin unblock <user>` — unblock a user",
                "",
                "**Admin Commands**",
                "`$admin game [status|start|stop|clear]`",
                "`$admin permissions`",
                "`$admin permissions add <list_type> <snowflake>`",
                "`$admin permissions remove <list_type> <snowflake>`",
                "`$admin permissions list <list_type> <snowflake>`",
            )
        )
        embed = discord.Embed(
            title="Admin & Moderation Commands",
            description=description,
            colour=discord.Colour.from_rgb(214, 40, 24),
        )
        embed.set_thumbnail(
            url="https://cdn.discordapp.com/emojis/637923502535606293.png"
        )
        await ctx.send(embed=embed)

    @admin_group.command("block")
    @has_any_role(*CONFIG_ROLES)
    async def block(
        self, ctx: commands.context, *, user: commands.UserConverter
    ) -> None:
        """Block a user and remove their score."""
        user: discord.User
        await self.blocked_users.set(user.id, "")
        await self.scoreboard.delete(user.id)
        await ctx.send(f"Successfully blocked {user.mention} and removed their score.")
        log.info(
            f"User {user} ({user.id}) was blocked by {ctx.author} ({ctx.author.id})"
        )

    @admin_group.command("unblock")
    @has_any_role(*CONFIG_ROLES)
    async def unblock(
        self, ctx: commands.context, *, user: commands.UserConverter
    ) -> None:
        """Unblock a user."""
        user: discord.User
        await self.blocked_users.delete(user.id)
        await ctx.send(f"Successfully unblocked {user.mention}.")
        log.info(
            f"User {user} ({user.id}) was unblocked by {ctx.author} ({ctx.author.id})"
        )

    @admin_group.command("blocked")
    @has_any_role(*CONFIG_ROLES)
    async def blocked(self, ctx: commands.context) -> None:
        """Unblock a user."""
        user: discord.User
        blocked_users = await self.blocked_users.to_dict()
        formatted_users = ", ".join(f"<@{u}>" for u in blocked_users)
        formatted_users = formatted_users or "(no blocked users)"
        await ctx.send(f"Currently blocked users: {formatted_users}")

    @admin_group.group(
        "game",
        invoke_without_command=True,
    )
    @has_any_role(settings.guild.admins_id)
    async def game_group(self, ctx: commands.context) -> None:
        """Unblock a user."""
        await ctx.invoke(self.game_status)

    @game_group.command("status")
    @has_any_role(settings.guild.admins_id)
    async def game_status(self, ctx: commands.context) -> None:
        """Unblock a user."""
        is_running = self._running and not self._game.done()
        qualifier = "" if is_running else "NOT "
        await ctx.send(f"The game is currently {qualifier}running.")

    @game_group.command("stop")
    @has_any_role(settings.guild.admins_id)
    async def game_stop(self, ctx: commands.context) -> None:
        """Unblock a user."""
        if not self._running and self._game.done():
            await ctx.send("The game is not running.")
            return

        self._running = False
        self._game.cancel()
        await self.config.set("running", False)
        self._state = GameState.NOT_RUNNING
        await ctx.send("Stopped the game.")

    @game_group.command("start")
    @has_any_role(settings.guild.admins_id)
    async def game_start(self, ctx: commands.context) -> None:
        """Unblock a user."""
        if self._running and not self._game.done():
            await ctx.send("The game is already running.")
            return

        self._running = True
        self._game = asyncio.create_task(self.game())
        await self.config.set("running", True)
        await ctx.send("Started the game.")

    async def _add_confirm_interface(self, msg: discord.Message) -> None:
        await msg.add_reaction(self._emoji_confirm)
        await msg.add_reaction(self._emoji_deny)

    @game_group.command("clear")
    @has_any_role(settings.guild.admins_id)
    async def game_clear(self, ctx: commands.context) -> None:
        """Clear the current scoreboard."""
        msg: discord.Message = await ctx.send(
            "THIS WILL IRREVOCABLY CLEAR THE LEADERBOARD. ARE YOU SURE?"
        )

        asyncio.create_task(self._add_confirm_interface(msg))

        def check(r, u):
            check_user = u == ctx.author
            check_emoji = r.custom_emoji and r.emoji.id in (
                self._emoji_confirm.id,
                self._emoji_deny.id,
            )
            check_message = r.message.id == msg.id
            return check_user and check_emoji and check_message

        try:
            reaction, _ = await self._bot.wait_for(
                "reaction_add", timeout=20.0, check=check
            )
        except asyncio.TimeoutError:
            await msg.clear_reactions()
            await ctx.send("Timed out. Please try again.")
            return

        if reaction.emoji == self._emoji_deny:
            await ctx.send("Scoreboard NOT cleared.")
        elif reaction.emoji == self._emoji_confirm:
            await self.scoreboard.clear()
            await ctx.send("Scoreboard cleared.")
            log.info(f"The leaderboard was cleared by {ctx.author} ({ctx.author.id})")
        else:
            await ctx.send("Wait, what? How did you do that?")

    @admin_group.group(
        "permissions",
        aliases=("perms", "perm", "p"),
        invoke_without_command=True,
    )
    @has_any_role(settings.guild.admins_id)
    async def permissions_group(self, ctx: commands.Context) -> None:
        """List the permissions options."""
        description = (
            "Usage:\n`$admin permissions [list|add|delete] <list_type> <id>`\n\n"
            "The following lists are available:\n"
            "• `categories_allow`\n"
            "• `categories_deny`\n"
            "• `channels_allow`\n"
            "• `channels_deny`\n\n"
            "**Note:** Only raw IDs are supported, without validation!"
        )

        embed = discord.Embed(
            title="Admin — Permissions Management",
            description=description,
            colour=discord.Colour.from_rgb(214, 40, 24),
            timestamp=datetime.datetime.utcnow(),
        )

        await ctx.send(embed=embed)

    @staticmethod
    async def _valid_add_remove_params(
        ctx: commands.context, list_type: str, snowflake: str
    ) -> bool:
        if list_type not in (
            "categories_allow",
            "categories_deny",
            "channels_allow",
            "channels_deny",
        ):
            await ctx.send(f"Invalid list type: {list_type!r}")
            return False

        try:
            int(snowflake)
        except ValueError:
            await ctx.send(f"Invalid snowflake id: {snowflake!r}")
            return False

        return True

    @permissions_group.command("add")
    @has_any_role(settings.guild.admins_id)
    async def add_permission(
        self, ctx: commands.context, list_type: str, snowflake: str
    ) -> None:
        """Add a permission to the specified list_type."""
        if not self._valid_add_remove_params(ctx, list_type, snowflake):
            await ctx.invoke(self.permissions_group)
            return

        await self._sync_allowdeny()
        cached_permissions = await self.config.get(list_type)
        new_permissions = f"{cached_permissions},{snowflake}"
        await self.config.set(list_type, new_permissions)
        await self._sync_allowdeny()
        await ctx.send(f"Added {snowflake} to {list_type}")

    @permissions_group.command("remove")
    @has_any_role(settings.guild.admins_id)
    async def remove_permission(
        self, ctx: commands.context, list_type: str, snowflake: str
    ) -> None:
        """Remove a permission to the specified list_type."""
        if not self._valid_add_remove_params(ctx, list_type, snowflake):
            await ctx.invoke(self.permissions_group)
            return

        await self._sync_allowdeny()
        cached_permissions = await self.config.get(list_type)
        new_permissions = [
            e for e in cached_permissions.split(",") if e and e != snowflake
        ]
        await self.config.set(list_type, ",".join(new_permissions))
        await self._sync_allowdeny()
        await ctx.send(f"Removed {snowflake} from {list_type}")

    @permissions_group.command("list")
    @has_any_role(settings.guild.admins_id)
    async def remove_permission(self, ctx: commands.context, list_type: str) -> None:
        """Remove a permission to the specified list_type."""
        if not self._valid_add_remove_params(ctx, list_type, "1"):
            await ctx.invoke(self.permissions_group)
            return

        await self._sync_allowdeny()
        cached_permissions = await self.config.get(list_type)
        await ctx.send(f"Current {list_type}: {cached_permissions}")
