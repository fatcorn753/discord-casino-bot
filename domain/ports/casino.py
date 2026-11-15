"""カジノ・記録・RTP・イベント領域の水平ポート契約。"""

import datetime
from typing import Any, Iterable, Optional, Protocol

from domain.ports.core import (
    IEscrowPort,
    IGuildApprovalStore,
    IGuildScopable,
    INoticePort,
    IWalletPort,
)


class IRecordPort(Protocol):
    """プレイ記録・ミッション・パチンコ統計の契約。"""

    def record_play(
        self, user_id: int, game: str, bet: int, net: int, day: datetime.date, tags: Iterable[Any] = ()
    ) -> dict[str, Any]: ...

    def mission_status(self, user_id: int, day: datetime.date) -> list[Any]: ...

    def play_summary(self, user_id: int) -> dict[str, Any]: ...

    def win_streak(self, user_id: int) -> tuple[Any, ...]: ...

    def peek_stats(self, user_id: int) -> Optional[dict[str, Any]]: ...

    def pachinko_record_set(
        self, user_id: int, bet: int, balls: int, result: Any, now: Optional[float] = None
    ) -> None: ...

    def pachinko_stats(self, guild_id: Optional[int] = None, since: Optional[float] = None) -> dict[str, Any]: ...


class IRtpPort(Protocol):
    """RTP レベル・持ち物プロファイル参照の契約。"""

    def get_profile(self, user_id: int, raw: bool = False) -> dict[str, Any]: ...

    def rtp_level_get(self, game: str) -> int: ...

    def rtp_levels(self) -> dict[str, Any]: ...

    def rtp_level_set(self, game: Optional[str], level: int) -> list[Any]: ...


class IEventPort(IGuildScopable, IGuildApprovalStore, Protocol):
    """突発イベント（自動フェーズ・手動開催）の契約。"""

    def event_phase(self, day: datetime.date) -> int: ...

    def set_event_phase(self, day: datetime.date, phase: int) -> None: ...

    def manual_event_start(
        self, game: str, boost_pct: int, starts: float, ends: float, channel_id: int = 0
    ) -> None: ...

    def manual_event_get(self, now: float) -> Optional[dict[str, Any]]: ...

    def manual_event_stop(self, now: float) -> Optional[dict[str, Any]]: ...

    def manual_event_finish_due(self, now: float) -> Optional[dict[str, Any]]: ...


class ICasinoPort(
    IRecordPort,
    IRtpPort,
    IEscrowPort,
    IEventPort,
    IWalletPort,
    INoticePort,
    Protocol,
):
    """カジノサービス・カジノ Cog が依存する統合契約。"""
