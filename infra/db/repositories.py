"""リポジトリ束と Composition Root（SQLite DB の初期化と依存関係の組み立て）。

28-Mixin の継承束を廃止し、単一責務（SRP）のリポジトリ群を
Bridge パターン / Constructor DI / OCP 戦略ステップで組み立てる。
"""

from dataclasses import dataclass
from typing import Optional

from domain.ports.bridges import (
    CompositeAssetCollector,
    CompositeAssetSeizureStep,
    CompositeBankruptPrepHook,
    CompositeGraceEnterHook,
    CompositeLiquidationStep,
    CompositeSeizureValueStep,
    CompositeValuationSource,
)
from domain.ports.clock import IClock
from domain.ports.repos import IRepositoryBundle
from infra.clock import ConfigClock
from infra.db import schema
from infra.db.account import AccountCore
from infra.db.arena import ArenaRepo
from infra.db.assets import (
    AssetRepo,
    CashAndDebtCollector,
    HorseAssetCollector,
    InventoryAssetCollector,
    LaborAssetCollector,
    LoanAssetCollector,
    P2PAssetCollector,
    ValuationSourceCollector,
)
from infra.db.auction import AuctionRepo
from infra.db.bank import BankRepo
from infra.db.base import EcoDb
from infra.db.bonds import BondRepo
from infra.db.company import CompanyLiquidationStep, CompanyRepo, CompanySeizureStep
from infra.db.crypto import CryptoRepo
from infra.db.debt import (
    BondLiquidationStep,
    BondSeizureStep,
    CryptoLiquidationStep,
    CryptoSeizureStep,
    DebtRepo,
    DepositLiquidationStep,
    DepositSeizureStep,
    KyosaiLiquidationStep,
    MarginLiquidationStep,
    MarginSeizureStep,
    OptionLiquidationStep,
    OptionSeizureStep,
    ShopAndHorseSeizureStep,
)
from infra.db.earnings import EarningsRepo
from infra.db.escrow import EscrowRepo
from infra.db.events import EventRepo
from infra.db.guild import GuildRepo
from infra.db.horse import HorseRepo
from infra.db.kyosai import KyosaiRepo
from infra.db.labor import LaborRepo
from infra.db.lazy_bridges import (
    LazyAssetSeizureStep,
    LazyBankruptExecutor,
    LazyBankruptPrepHook,
    LazyDebtLifecycleHook,
    LazyExcessLossSink,
    LazyGraceEnterHook,
    LazyLiquidationStep,
    LazyMarginCollateralGuard,
    LazyMarginRatioReader,
    LazyOverdueLoanCollector,
    LazySeizureValueStep,
    LazyStatusReader,
    LazyWealthyReader,
)
from infra.db.loans import LoanRepo
from infra.db.margin import MarginRepo
from infra.db.market import MarketDataRepo
from infra.db.market_events import MarketEventRepo
from infra.db.notices import NoticeRepo
from infra.db.options import OptionRepo
from infra.db.p2p import P2PRepo
from infra.db.pachinko import PachinkoRepo
from infra.db.poll import PollRepo
from infra.db.quiz import QuizRepo
from infra.db.race import RaceRepo
from infra.db.raffle import RaffleRepo
from infra.db.records import RecordRepo
from infra.db.relics import RelicRepo
from infra.db.rtp import RtpRepo
from infra.db.shop import ShopRepo
from infra.db.wallet import WalletRepo


@dataclass(frozen=True)
class RepositoryBundle(IRepositoryBundle):
    """1 つの SQLite DB に対する単一責務リポジトリ群の束。"""

    notices: NoticeRepo
    rtp: RtpRepo
    events: EventRepo
    pachinko: PachinkoRepo
    relics: RelicRepo
    market: MarketDataRepo
    market_events: MarketEventRepo
    records: RecordRepo
    raffle: RaffleRepo
    polls: PollRepo
    guild: GuildRepo
    bank: BankRepo
    debt: DebtRepo
    loans: LoanRepo
    shop: ShopRepo
    auction: AuctionRepo
    escrow: EscrowRepo
    quiz: QuizRepo
    horses: HorseRepo
    races: RaceRepo
    labor: LaborRepo
    arena: ArenaRepo
    company: CompanyRepo
    earnings: EarningsRepo
    crypto: CryptoRepo
    margin: MarginRepo
    bonds: BondRepo
    options: OptionRepo
    p2p: P2PRepo
    kyosai: KyosaiRepo
    wallet: WalletRepo
    assets: AssetRepo
    db: EcoDb
    account: AccountCore

    def for_guild(self, guild_id: int) -> "RepositoryBundle":
        """指定サーバー（guild_id）にスコープされた各リポジトリのビュー束を返す（接続は共有）。"""
        return RepositoryBundle(
            db=self.db,
            account=self.account,
            notices=self.notices.for_guild(guild_id),
            rtp=self.rtp.for_guild(guild_id),
            events=self.events.for_guild(guild_id),
            pachinko=self.pachinko.for_guild(guild_id),
            relics=self.relics.for_guild(guild_id),
            market=self.market.for_guild(guild_id),
            market_events=self.market_events.for_guild(guild_id),
            records=self.records.for_guild(guild_id),
            raffle=self.raffle.for_guild(guild_id),
            polls=self.polls.for_guild(guild_id),
            guild=self.guild.for_guild(guild_id),
            bank=self.bank.for_guild(guild_id),
            debt=self.debt.for_guild(guild_id),
            loans=self.loans.for_guild(guild_id),
            shop=self.shop.for_guild(guild_id),
            auction=self.auction.for_guild(guild_id),
            escrow=self.escrow.for_guild(guild_id),
            quiz=self.quiz.for_guild(guild_id),
            horses=self.horses.for_guild(guild_id),
            races=self.races.for_guild(guild_id),
            labor=self.labor.for_guild(guild_id),
            arena=self.arena.for_guild(guild_id),
            company=self.company.for_guild(guild_id),
            earnings=self.earnings.for_guild(guild_id),
            crypto=self.crypto.for_guild(guild_id),
            margin=self.margin.for_guild(guild_id),
            bonds=self.bonds.for_guild(guild_id),
            options=self.options.for_guild(guild_id),
            p2p=self.p2p.for_guild(guild_id),
            kyosai=self.kyosai.for_guild(guild_id),
            wallet=self.wallet.for_guild(guild_id),
            assets=self.assets.for_guild(guild_id),
        )

    def close(self) -> None:
        self.db.close()


def open_repositories(
    db_path: str, legacy_guild_id: Optional[int] = None, strict: bool = False, clock: Optional[IClock] = None
) -> RepositoryBundle:
    """DB を開き、マイグレーションとテーブル作成のうえ全リポジトリを組み立てて返す。

    clock は日付の切り替わりに使う時差（utc_offset_hours）の出どころ。省略時は設定の既定値（ConfigClock({})）。
    """
    clock = clock if clock is not None else ConfigClock({})
    db = EcoDb(db_path, clock)
    schema.migrate_to_guild_scope(db.conn, db.db_path, legacy_guild_id)

    account = AccountCore(db, debt_hook=LazyDebtLifecycleHook(lambda: debt))
    notices = NoticeRepo(db, strict=strict)
    rtp = RtpRepo(db, strict=strict)
    events = EventRepo(db, strict=strict)
    pachinko = PachinkoRepo(db, strict=strict)
    relics = RelicRepo(db, account, strict=strict)
    market = MarketDataRepo(db, strict=strict)
    market_events = MarketEventRepo(db, strict=strict)
    records = RecordRepo(db, account, strict=strict)
    raffle = RaffleRepo(db, account, strict=strict)
    guild = GuildRepo(db, wealthy_reader=LazyWealthyReader(lambda: wallet), strict=strict)

    bank = BankRepo(
        db,
        account,
        notices,
        excess_sink=LazyExcessLossSink(lambda: kyosai),
        status_reader=LazyStatusReader(lambda: debt),
        margin_ratio_reader=LazyMarginRatioReader(lambda: margin),
        grace_enter_hook=CompositeGraceEnterHook((LazyGraceEnterHook(lambda: p2p), LazyGraceEnterHook(lambda: kyosai))),
        bankrupt_executor=LazyBankruptExecutor(lambda: debt),
        strict=strict,
    )
    debt = DebtRepo(
        db,
        account,
        notices,
        bank,
        loans=LazyOverdueLoanCollector(lambda: loans),
        seizure_step=LazyAssetSeizureStep(lambda: seizure_step),
        liquidation_step=LazyLiquidationStep(lambda: liquidation_step),
        seizure_value_step=LazySeizureValueStep(lambda: seizure_value_step),
        bankrupt_prep_hook=LazyBankruptPrepHook(lambda: bankrupt_prep_hook),
        strict=strict,
    )
    polls = PollRepo(db, account, bank, strict=strict)

    loans = LoanRepo(db, account, debt, bank, strict=strict)
    shop = ShopRepo(db, account, debt, strict=strict)
    auction = AuctionRepo(db, account, debt, bank, strict=strict)
    escrow = EscrowRepo(db, account, bank, debt, strict=strict)
    quiz = QuizRepo(db, account, debt, strict=strict)
    horses = HorseRepo(db, account, debt, bank, strict=strict)
    races = RaceRepo(db, account, horses, relics, bank, strict=strict)
    labor = LaborRepo(db, account, relics, bank, strict=strict)
    arena = ArenaRepo(db, account, bank, strict=strict)
    company = CompanyRepo(db, account, debt, bank, bank, notices, clock.offset_hours(), strict=strict)
    earnings = EarningsRepo(db, account, bank, strict=strict)

    crypto = CryptoRepo(
        db,
        account,
        market,
        bank,
        collateral_guard=LazyMarginCollateralGuard(lambda: margin),
        strict=strict,
    )
    margin = MarginRepo(db, account, market, bank, bank, notices, crypto, debt, strict=strict)

    bonds = BondRepo(db, account, bank, debt_repay=debt, collateral_guard=margin, strict=strict)
    options = OptionRepo(db, account, market, market_events, earnings, bank, debt, strict=strict)
    p2p = P2PRepo(db, account, horses, debt, bank, bank, notices, strict=strict)
    kyosai = KyosaiRepo(db, account, bank, notices, strict=strict)

    seizure_step = CompositeAssetSeizureStep(
        (
            DepositSeizureStep(account, bank, notices),
            CompanySeizureStep(account, company, notices),
            MarginSeizureStep(account, market, margin, notices),
            CryptoSeizureStep(account, market, crypto, notices),
            OptionSeizureStep(account, options, notices),
            BondSeizureStep(account, bonds, notices),
        )
    )
    liquidation_step = CompositeLiquidationStep(
        (
            MarginLiquidationStep(market, margin),
            CryptoLiquidationStep(market, crypto),
            BondLiquidationStep(account, bonds),
            OptionLiquidationStep(options),
            DepositLiquidationStep(account, bank),
            CompanyLiquidationStep(account, company),
            KyosaiLiquidationStep(kyosai),
        )
    )
    seizure_value_step = CompositeSeizureValueStep((ShopAndHorseSeizureStep(shop, auction, horses),))
    bankrupt_prep_hook = CompositeBankruptPrepHook((p2p, kyosai))

    valuation_source = CompositeValuationSource((crypto, margin, bonds, bank, options, company))
    wallet = WalletRepo(db, account, bank, debt, debt, relics, valuation_source, strict=strict)

    asset_collector = CompositeAssetCollector(
        (
            CashAndDebtCollector(db),
            ValuationSourceCollector("deposit", CompositeValuationSource((bank,))),
            ValuationSourceCollector("crypto", CompositeValuationSource((crypto, margin))),
            ValuationSourceCollector("bonds", bonds),
            ValuationSourceCollector("options", options),
            ValuationSourceCollector("company", company),
            InventoryAssetCollector(db),
            LaborAssetCollector(db),
            HorseAssetCollector(db, horses.horse_worth),
            P2PAssetCollector(db, horses.horse_worth),
            LoanAssetCollector(db, loans),
        )
    )
    assets = AssetRepo(db, asset_collector, horse_worth_fn=horses.horse_worth, strict=strict)

    with db.tx():
        schema.create_tables(db.conn)
        schema.add_missing_columns(db.conn)
        auction.purge_unlistable_auctions()
    with db.tx():
        schema.retire_autoplay(db.conn)

    return RepositoryBundle(
        db=db,
        account=account,
        notices=notices,
        rtp=rtp,
        events=events,
        pachinko=pachinko,
        relics=relics,
        market=market,
        market_events=market_events,
        records=records,
        raffle=raffle,
        polls=polls,
        guild=guild,
        bank=bank,
        debt=debt,
        loans=loans,
        shop=shop,
        auction=auction,
        escrow=escrow,
        quiz=quiz,
        horses=horses,
        races=races,
        labor=labor,
        arena=arena,
        company=company,
        earnings=earnings,
        crypto=crypto,
        margin=margin,
        bonds=bonds,
        options=options,
        p2p=p2p,
        kyosai=kyosai,
        wallet=wallet,
        assets=assets,
    )
