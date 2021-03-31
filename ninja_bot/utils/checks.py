from typing import Callable, Container

from discord.ext import commands

from ninja_bot import settings

BYPASS_ROLES = settings.guild.bypass_roles


def in_channel_check(
    ctx: commands.Context, channels: Container[int], staff_bypass: bool
) -> bool:
    """Check if the command was issued in a whitelisted channel."""
    if channels and ctx.channel.id in channels:
        return True

    bypass_check = (r.id in BYPASS_ROLES for r in getattr(ctx.author, "roles", ()))
    if staff_bypass and any(bypass_check):
        return True

    return False


def in_channel(
    *, channels: Container[int] = (), staff_bypass: bool = False
) -> Callable:
    def predicate(ctx: commands.Context) -> bool:
        return in_channel_check(ctx, channels, staff_bypass)

    return commands.check(predicate)


def in_commands_channel(*, staff_bypass: bool = False) -> Callable:
    def predicate(ctx: commands.Context) -> bool:
        return in_channel_check(
            ctx, channels=settings.guild.commands_channels, staff_bypass=staff_bypass
        )

    return commands.check(predicate)
