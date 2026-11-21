"""承認済みギルドに対する定期バックグラウンド処理の共通ヘルパー。"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import discord

from domain import approval
from domain.ports.repos import IGuildRepo

_LOG = logging.getLogger(__name__)


def allowed_guilds(bot: discord.Client, guild_repo: IGuildRepo, config: dict[str, Any]) -> list[discord.Guild]:
    """利用が許可されている参加中ギルドの一覧を返す。"""
    return [g for g in list(bot.guilds) if approval.is_allowed(guild_repo, config, g.id, 0)]


async def run_for_allowed_guilds(
    bot: discord.Client,
    guild_repo: IGuildRepo,
    config: dict[str, Any],
    handler: Callable[[discord.Guild], Awaitable[None]],
    *,
    error_label: str,
    busy: set[int] | None = None,
    logger: logging.Logger | None = None,
) -> None:
    """承認済みギルドごとに非同期処理を順次実行し、1 ギルドの失敗で他を止めない。"""
    log = logger or _LOG
    for guild in allowed_guilds(bot, guild_repo, config):
        if busy is not None and guild.id in busy:
            continue
        if busy is not None:
            busy.add(guild.id)
        try:
            await handler(guild)
        except Exception as exc:
            log.warning("%s: guild=%s: %r", error_label, guild.id, exc)
        finally:
            if busy is not None:
                busy.discard(guild.id)


def run_sync_for_allowed_guilds(
    bot: discord.Client,
    guild_repo: IGuildRepo,
    config: dict[str, Any],
    handler: Callable[[discord.Guild], None],
    *,
    error_label: str,
    logger: logging.Logger | None = None,
) -> None:
    """承認済みギルドごとに同期処理を順次実行し、1 ギルドの失敗で他を止めない。"""
    log = logger or _LOG
    for guild in allowed_guilds(bot, guild_repo, config):
        try:
            handler(guild)
        except Exception as exc:
            log.warning("%s: guild=%s: %r", error_label, guild.id, exc)
