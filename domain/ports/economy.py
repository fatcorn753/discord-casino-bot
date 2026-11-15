"""経済基盤の水平インターフェース（ISP / DIP）と統合ポート契約。

巨大な単一具象（Economy）へ直接依存するのではなく、利用側の関心事ごとに
小さな Protocol 契約を定義し、永続化層（infra/db/）がこれらを実装する。
"""

from typing import TYPE_CHECKING, Any, Optional, Protocol

from domain.ports.casino import ICasinoPort, IRecordPort, IRtpPort
from domain.ports.community import (
    IAdminPort,
    ILoanPort,
    IP2PPort,
    IRafflePort,
    IShopPort,
    ITeiaiPort,
)
from domain.ports.core import (
    IApprovalAdminPort,
    IEscrowPort,
    IGuildApprovalStore,
    IGuildScopable,
    ILeaderboardStore,
    INoticePort,
    IRankingReader,
    IWalletPort,
    IWeeklyPort,
)
from domain.ports.horse import IHorsePort
from domain.ports.labor import ILaborPort
from domain.ports.market import IMarketPort

if TYPE_CHECKING:
    from domain.earnings import MemoInfo


class IEconomyServicePort(IWeeklyPort, IWalletPort, INoticePort, Protocol):
    """EconomyService が依存する経済・通知・週間・裏情報メモ契約。"""

    def earnings_memo(self, user_id: int, now: Optional[float] = None, rng: Any = None) -> Optional["MemoInfo"]: ...


class IEconomyPanelPort(
    IRecordPort,
    IRtpPort,
    ILoanPort,
    IRankingReader,
    ILeaderboardStore,
    INoticePort,
    Protocol,
):
    """`/ペリカ` Cog（残高・デイリー・ランキング・自己破産）が依存する契約。"""

    @property
    def SLOTS(self) -> tuple[str, ...]: ...

    def inventory(self, user_id: int) -> dict[str, Any]: ...


class IEconomy(
    IAdminPort,
    ICasinoPort,
    IMarketPort,
    IHorsePort,
    ILaborPort,
    IShopPort,
    ITeiaiPort,
    IP2PPort,
    IRafflePort,
    IEconomyServicePort,
    IEconomyPanelPort,
    Protocol,
):
    """Composition Root・CLI・Bot 保持用の統合ポート契約。"""


__all__ = [
    "IApprovalAdminPort",
    "IEconomy",
    "IEconomyPanelPort",
    "IEconomyServicePort",
    "IEscrowPort",
    "IGuildApprovalStore",
    "IGuildScopable",
    "ILeaderboardStore",
    "INoticePort",
    "IRankingReader",
    "IWalletPort",
    "IWeeklyPort",
]
