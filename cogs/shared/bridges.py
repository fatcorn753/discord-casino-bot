"""Cog 間・DynamicItem 間の疎結合ブリッジ契約（名目的基底クラス）とオープンレジストリ。"""

import random
from typing import Any, TypeVar, cast

import discord

T = TypeVar("T")


class IApprovalBridge:
    """サーバー承認フロー（approval_guard / ApprovalButton → ApprovalCog）のブリッジ契約。"""

    async def ensure_requested(self, guild: discord.Guild, reset_denied: bool = False) -> None:
        raise NotImplementedError

    async def announce_approved(self, guild: discord.Guild) -> None:
        raise NotImplementedError

    async def handle_button(self, interaction: discord.Interaction, allow: bool, guild_id: int) -> None:
        raise NotImplementedError


class NoOpApprovalBridge(IApprovalBridge):
    """ApprovalCog 未ロード時に LSP 準拠の無害な動作を提供する既定実装。"""

    async def ensure_requested(self, guild: discord.Guild, reset_denied: bool = False) -> None:
        return None

    async def announce_approved(self, guild: discord.Guild) -> None:
        return None

    async def handle_button(self, interaction: discord.Interaction, allow: bool, guild_id: int) -> None:
        return None


class IShopTitleBridge:
    """ショップの称号・ニックネーム同期（Economy / Weekly → ShopCog）のブリッジ契約。"""

    async def sync_title_nick(
        self,
        user_id: int,
        here: discord.Guild | None = None,
        member: discord.Member | None = None,
    ) -> tuple[str, int]:
        raise NotImplementedError

    async def after_bankruptcy(self, guild: discord.Guild, member: discord.Member) -> None:
        raise NotImplementedError


class NoOpShopTitleBridge(IShopTitleBridge):
    """ShopCog 未ロード時に LSP 準拠の無害な動作を提供する既定実装。"""

    async def sync_title_nick(
        self,
        user_id: int,
        here: discord.Guild | None = None,
        member: discord.Member | None = None,
    ) -> tuple[str, int]:
        return ("", 0)

    async def after_bankruptcy(self, guild: discord.Guild, member: discord.Member) -> None:
        return None


class IEventAdminBridge:
    """運営向けイベント操作（Admin → EventCog）のブリッジ契約。"""

    async def start_manual(
        self,
        guild: discord.Guild,
        game: str,
        boost_pct: int,
        minutes: int,
        channel: discord.abc.Messageable | None = None,
    ) -> tuple[bool, str]:
        raise NotImplementedError

    async def stop_manual(self, guild: discord.Guild) -> tuple[bool, str]:
        raise NotImplementedError


class IHorseAdminBridge:
    """運営向け競馬開催操作（Admin → HorseCog）のブリッジ契約（ISP 分離）。"""

    async def start_special(
        self,
        guild: discord.Guild,
        entry_min: int,
        bet_min: int,
        channel: discord.abc.Messageable | None = None,
    ) -> int:
        raise NotImplementedError


class IArenaAdminBridge:
    """運営向け地下闘技場の開催操作（Admin → ArenaCog）のブリッジ契約。"""

    async def host_now(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable | None,
        advance: bool,
        fmt: str | None = None,
    ) -> str:
        raise NotImplementedError


class IHorseButtonBridge:
    """競馬レースカードボタン（RaceButton → HorseCog）のブリッジ契約（ISP 分離）。"""

    async def handle_button(self, interaction: discord.Interaction, action: str, race_id: int) -> None:
        raise NotImplementedError


class NoOpHorseButtonBridge(IHorseButtonBridge):
    """HorseCog 未ロード時の既定ボタンハンドラ（LSP 準拠）。"""

    async def handle_button(self, interaction: discord.Interaction, action: str, race_id: int) -> None:
        return None


class IRaffleAdminBridge:
    """運営向け抽選操作（Admin → RaffleCog）のブリッジ契約（host/repost/cancel はすべて (成功か, 説明) を返す）。"""

    async def host(
        self,
        guild: discord.Guild,
        host_id: int,
        fee: int | None = None,
        minutes: int = 60,
        channel: discord.abc.Messageable | None = None,
        rng: random.Random | None = None,
    ) -> tuple[bool, str]:
        raise NotImplementedError

    async def repost(self, guild: discord.Guild, channel: discord.abc.Messageable | None = None) -> tuple[bool, str]:
        raise NotImplementedError

    async def cancel(self, guild: discord.Guild) -> tuple[bool, str]:
        raise NotImplementedError


class IQuizAdminBridge:
    """運営向けクイズ操作（Admin → QuizCog）のブリッジ契約（ISP 分離）。"""

    def add_modal(
        self,
        announce: bool,
        guild: discord.Guild | None,
        channel: discord.abc.Messageable | None,
    ) -> discord.ui.Modal:
        raise NotImplementedError

    async def broadcast(
        self,
        quiz_id: int,
        guild: discord.Guild | None = None,
        channel: discord.abc.Messageable | None = None,
    ) -> int:
        raise NotImplementedError

    def listing_embed(self) -> discord.Embed:
        raise NotImplementedError

    def remove(self, quiz_id: int) -> bool:
        raise NotImplementedError


class IQuizButtonBridge:
    """クイズ公開回答ボタン（PublicButton → QuizCog）のブリッジ契約（ISP 分離）。"""

    async def handle_button(self, interaction: discord.Interaction, quiz_id: int, pos: int) -> None:
        raise NotImplementedError


class NoOpQuizButtonBridge(IQuizButtonBridge):
    """QuizCog 未ロード時の既定ボタンハンドラ（LSP 準拠）。"""

    async def handle_button(self, interaction: discord.Interaction, quiz_id: int, pos: int) -> None:
        return None


class IPollButtonBridge:
    """投票の投稿のボタン（PollButton → PollCog）のブリッジ契約（ISP 分離）。"""

    async def handle_button(self, interaction: discord.Interaction, act: str, poll_id: int) -> None:
        raise NotImplementedError


class NoOpPollButtonBridge(IPollButtonBridge):
    """PollCog 未ロード時の既定ボタンハンドラ（LSP 準拠）。"""

    async def handle_button(self, interaction: discord.Interaction, act: str, poll_id: int) -> None:
        return None


class IStreamButtonBridge:
    """配信パネルのボタン（StreamButton → StreamCog）のブリッジ契約（ISP 分離）。"""

    async def handle_button(self, interaction: discord.Interaction, act: str, stream_id: int) -> None:
        raise NotImplementedError


class NoOpStreamButtonBridge(IStreamButtonBridge):
    """StreamCog 未ロード時の既定ボタンハンドラ（LSP 準拠）。"""

    async def handle_button(self, interaction: discord.Interaction, act: str, stream_id: int) -> None:
        return None


class IKyosaiVoteBridge:
    """共済の動議の投稿のボタン（MotionButton → KyosaiCog）のブリッジ契約（ISP 分離）。"""

    async def handle_vote(self, interaction: discord.Interaction, yes: bool, motion_id: int) -> None:
        raise NotImplementedError


class NoOpKyosaiVoteBridge(IKyosaiVoteBridge):
    """KyosaiCog 未ロード時の既定ボタンハンドラ（LSP 準拠）。"""

    async def handle_vote(self, interaction: discord.Interaction, yes: bool, motion_id: int) -> None:
        return None


class IP2PListingBridge:
    """P2P 出品カードボタン（ListingButton → P2PCog）のブリッジ契約（ISP 分離）。"""

    async def open_detail(self, interaction: discord.Interaction, listing_id: int) -> None:
        raise NotImplementedError


class IP2PNavigationBridge(IP2PListingBridge):
    """P2P 画面遷移（Shop → P2PCog）のブリッジ契約（詳細 + 出品・取引管理）。"""

    async def open_sell(self, interaction: discord.Interaction) -> None:
        raise NotImplementedError

    async def open_mine(self, interaction: discord.Interaction) -> None:
        raise NotImplementedError


class ILoanButtonBridge:
    """個人間貸付ボタン（LoanButton → LoanCog）のブリッジ契約。"""

    async def handle_button(self, interaction: discord.Interaction, action: str, loan_id: int) -> None:
        raise NotImplementedError


class NoOpLoanButtonBridge(ILoanButtonBridge):
    """LoanCog 未ロード時の既定ボタンハンドラ（LSP 準拠）。"""

    async def handle_button(self, interaction: discord.Interaction, action: str, loan_id: int) -> None:
        return None


class BotBridges:
    """Bot 実行時にロードされた各 Cog のブリッジ実装を契約型で登録・解決するオープンレジストリ（OCP）。"""

    def __init__(self) -> None:
        self._entries: dict[type[Any], Any] = {}

    def register(self, contract: type[T], impl: T) -> None:
        if not isinstance(impl, contract):
            raise TypeError(f"{type(impl).__name__} は {contract.__name__} を継承している必要があります。")
        self._entries[contract] = impl

    def unregister(self, contract: type[Any], expected: Any = None) -> None:
        if expected is not None and self._entries.get(contract) is not expected:
            return
        self._entries.pop(contract, None)

    def has(self, contract: type[Any]) -> bool:
        return contract in self._entries

    def resolve(self, contract: type[T]) -> T | None:
        return cast(T | None, self._entries.get(contract))

    def resolve_or(self, contract: type[T], fallback: T) -> T:
        impl = self._entries.get(contract)
        return fallback if impl is None else cast(T, impl)


def resolve_bridge(bot: Any, contract: type[T], override: T | None = None) -> T | None:
    """明示注入されたブリッジ、または BotBridges レジストリから指定契約を解決する。"""
    if override is not None:
        return override
    bridges = getattr(bot, "bridges", None)
    return bridges.resolve(contract) if isinstance(bridges, BotBridges) else None


async def require_bridge(
    interaction: discord.Interaction,
    bot: Any,
    contract: type[T],
    feature_label: str,
    override: T | None = None,
) -> T | None:
    """指定契約のブリッジを解決し、未ロードならエラーを返信して None を返す。"""
    bridge = resolve_bridge(bot, contract, override)
    if bridge is None:
        from cogs.shared import ui

        await ui.reply_error(interaction, "", title=f"{feature_label}が読み込まれていません")
    return bridge


class DeferredShopTitleBridge(IShopTitleBridge):
    """呼び出し時に BotBridges から最新の IShopTitleBridge を解決する遅延プロキシ（ロード順非依存）。"""

    def __init__(self, bot: Any, fallback: IShopTitleBridge | None = None) -> None:
        self._bot = bot
        self._fallback: IShopTitleBridge = fallback if fallback is not None else NoOpShopTitleBridge()

    def _current(self) -> IShopTitleBridge:
        bridges = getattr(self._bot, "bridges", None)
        if isinstance(bridges, BotBridges):
            return bridges.resolve_or(IShopTitleBridge, self._fallback)
        return self._fallback

    async def sync_title_nick(
        self,
        user_id: int,
        here: discord.Guild | None = None,
        member: discord.Member | None = None,
    ) -> tuple[str, int]:
        return await self._current().sync_title_nick(user_id, here=here, member=member)

    async def after_bankruptcy(self, guild: discord.Guild, member: discord.Member) -> None:
        await self._current().after_bankruptcy(guild, member)
