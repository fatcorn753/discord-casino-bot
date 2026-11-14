"""Cog 間の疎結合連携用ポート（Protocol）と型付き解決ヘルパー。

文字列による `bot.get_cog("...")` の生呼び出し・他 Cog 具象への直接依存を排し、
各連携ポイントで必要なメソッドだけを切り出した水平インターフェースを定義する。
"""

from typing import Any, Optional, Protocol, TypeVar, cast

import discord

T = TypeVar("T")


class IApprovalCogPort(Protocol):
    async def ensure_requested(self, guild: discord.Guild) -> None: ...
    async def announce_approved(self, guild: discord.Guild) -> None: ...


class IShopCogPort(Protocol):
    async def sync_title_nick(
        self, user_id: int, here: Optional[discord.Guild] = None, member: Optional[discord.Member] = None
    ) -> tuple[str, int]: ...
    async def after_bankruptcy(self, guild: discord.Guild, member: discord.Member) -> None: ...


class IEventCogPort(Protocol):
    async def start_manual(
        self,
        guild: discord.Guild,
        game: str,
        boost: int,
        minutes: int,
        channel: Optional[discord.abc.Messageable] = None,
    ) -> tuple[bool, str]: ...
    async def stop_manual(self, guild: discord.Guild) -> tuple[bool, str]: ...


class IHorseCogPort(Protocol):
    async def start_special(
        self,
        guild: discord.Guild,
        entry_min: int,
        bet_min: int,
        channel: Optional[discord.abc.Messageable] = None,
    ) -> int: ...
    async def post_bet_announcement(self, guild: discord.Guild, eco: Any, race: dict, content: str) -> None: ...


class IRaffleCogPort(Protocol):
    async def host(
        self,
        guild: discord.Guild,
        host_id: int,
        fee: Optional[int] = None,
        minutes: int = ...,
        channel: Optional[discord.abc.Messageable] = None,
    ) -> tuple[bool, str]: ...
    async def repost(
        self, guild: discord.Guild, channel: Optional[discord.abc.Messageable] = None
    ) -> tuple[bool, str]: ...
    async def cancel(self, guild: discord.Guild) -> tuple[bool, str]: ...


class IQuizCogPort(Protocol):
    def add_modal(
        self,
        announce: bool,
        guild: Optional[discord.Guild],
        channel: Optional[discord.abc.Messageable],
    ) -> discord.ui.Modal: ...
    async def broadcast(
        self,
        quiz_id: int,
        guild: Optional[discord.Guild] = None,
        channel: Optional[discord.abc.Messageable] = None,
    ) -> int: ...
    def listing_embed(self) -> discord.Embed: ...
    def remove(self, quiz_id: int) -> bool: ...


class IP2PCogPort(Protocol):
    async def open_detail(self, interaction: discord.Interaction, listing_id: int) -> None: ...
    async def open_sell(self, interaction: discord.Interaction) -> None: ...
    async def open_mine(self, interaction: discord.Interaction) -> None: ...


def get_cog_port(bot: Any, name: str, _port: type[T]) -> Optional[T]:
    """`bot.get_cog(name)` をポート型付きで取得する。未ロード・モック未実装時は None。"""
    getter = getattr(bot, "get_cog", None)
    if getter is None:
        return None
    return cast(Optional[T], getter(name))
