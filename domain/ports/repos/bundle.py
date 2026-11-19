"""単一責務リポジトリポート群を束ねる Composition Root 用バンドル契約。"""

from dataclasses import dataclass

from domain.ports.repos.base import IGuildScopedRepo
from domain.ports.repos.economy import IAssetRepo, IGuildRepo, INoticeRepo, IRecordRepo, IWalletRepo
from domain.ports.repos.games import (
    IArenaRepo,
    IEventRepo,
    IHorseRepo,
    IPachinkoRepo,
    IRaceRepo,
    IRelicRepo,
    IRtpRepo,
)
from domain.ports.repos.market import (
    IBondRepo,
    ICryptoRepo,
    IEarningsRepo,
    IMarginRepo,
    IMarketDataRepo,
    IMarketEventRepo,
    IOptionRepo,
)
from domain.ports.repos.social import (
    IAuctionRepo,
    ICompanyRepo,
    IEscrowRepo,
    ILaborRepo,
    IP2PRepo,
    IQuizRepo,
    IRaffleRepo,
    IShopRepo,
)
from domain.ports.repos.teiai import IBankRepo, IDebtRepo, IKyosaiRepo, ILoanRepo


@dataclass(frozen=True)
class IRepositoryBundle(IGuildScopedRepo):
    """単一責務（SRP）の抽象リポジトリ群を束ねる Composition Root 用契約。"""

    notices: INoticeRepo
    rtp: IRtpRepo
    events: IEventRepo
    pachinko: IPachinkoRepo
    relics: IRelicRepo
    market: IMarketDataRepo
    market_events: IMarketEventRepo
    records: IRecordRepo
    raffle: IRaffleRepo
    guild: IGuildRepo
    bank: IBankRepo
    debt: IDebtRepo
    loans: ILoanRepo
    shop: IShopRepo
    auction: IAuctionRepo
    escrow: IEscrowRepo
    quiz: IQuizRepo
    horses: IHorseRepo
    races: IRaceRepo
    labor: ILaborRepo
    arena: IArenaRepo
    company: ICompanyRepo
    earnings: IEarningsRepo
    crypto: ICryptoRepo
    margin: IMarginRepo
    bonds: IBondRepo
    options: IOptionRepo
    p2p: IP2PRepo
    kyosai: IKyosaiRepo
    wallet: IWalletRepo
    assets: IAssetRepo

    def close(self) -> None:
        raise NotImplementedError
