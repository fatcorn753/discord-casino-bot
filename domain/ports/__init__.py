"""ドメイン境界ポート（インターフェース）群。"""

from domain.ports.casino import ICasinoPort, IEventPort, IRecordPort, IRtpPort
from domain.ports.catalogs import ICatalogs, configure_catalogs, get_catalogs
from domain.ports.clock import IClock
from domain.ports.community import (
    IAdminPort,
    ILoanPort,
    IP2PPort,
    IQuizPort,
    IRafflePort,
    IShopPort,
    ITeiaiPort,
)
from domain.ports.economy import (
    IApprovalAdminPort,
    IEconomy,
    IEconomyPanelPort,
    IEconomyServicePort,
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
from domain.ports.market import IChartPostStore, IMarketDataPort, IMarketPort, IPortfolioPort
from domain.ports.session import ISessionGuard

__all__ = [
    "IAdminPort",
    "IApprovalAdminPort",
    "ICasinoPort",
    "ICatalogs",
    "IChartPostStore",
    "IClock",
    "IEconomy",
    "IEconomyPanelPort",
    "IEconomyServicePort",
    "IEscrowPort",
    "IEventPort",
    "IGuildApprovalStore",
    "IGuildScopable",
    "IHorsePort",
    "ILaborPort",
    "ILeaderboardStore",
    "ILoanPort",
    "IMarketDataPort",
    "IMarketPort",
    "INoticePort",
    "IP2PPort",
    "IPortfolioPort",
    "IQuizPort",
    "IRafflePort",
    "IRankingReader",
    "IRecordPort",
    "IRtpPort",
    "ISessionGuard",
    "IShopPort",
    "ITeiaiPort",
    "IWalletPort",
    "IWeeklyPort",
    "configure_catalogs",
    "get_catalogs",
]
