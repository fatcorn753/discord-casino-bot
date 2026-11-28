"""リポジトリ束と Composition Root（SQLite DB の初期化と依存関係の組み立て）。

28-Mixin の継承束を廃止し、単一責務（SRP）のリポジトリ群を
Bridge パターン / Constructor DI / OCP 戦略ステップで組み立てる。
"""

from dataclasses import dataclass

from domain.ports.bridges import (
    compose_asset_collectors,
    compose_asset_seizure_steps,
    compose_bankrupt_prep_hooks,
    compose_grace_enter_hooks,
    compose_liquidation_steps,
    compose_seizure_value_steps,
    compose_valuation_sources,
)
from domain.ports.clock import IClock
from domain.ports.repos import IRepositoryBundle
from infra.clock import ConfigClock
from infra.db import schema
from infra.db.account import AccountCore
from infra.db.arena import ArenaRepo
from infra.db.assets import (
    AssetRepo,
    cash_and_debt_collector,
    horse_asset_collector,
    inventory_asset_collector,
    labor_asset_collector,
    loan_asset_collector,
    p2p_asset_collector,
    valuation_source_collector,
)
from infra.db.auction import AuctionRepo
from infra.db.bank import BankRepo
from infra.db.base import EcoDb
from infra.db.bonds import BondRepo
from infra.db.company import CompanyRepo, company_liquidation_step, company_seizure_step
from infra.db.crypto import CryptoRepo
from infra.db.debt import (
    DebtRepo,
    bond_liquidation_step,
    bond_seizure_step,
    crypto_liquidation_step,
    crypto_seizure_step,
    deposit_liquidation_step,
    deposit_seizure_step,
    kyosai_liquidation_step,
    margin_liquidation_step,
    margin_seizure_step,
    option_liquidation_step,
    option_seizure_step,
    shop_and_horse_seizure_value_step,
)
from infra.db.earnings import EarningsRepo
from infra.db.escrow import EscrowRepo
from infra.db.events import EventRepo
from infra.db.guild import GuildRepo
from infra.db.horse import HorseRepo
from infra.db.inventory import InventoryCore
from infra.db.kyosai import KyosaiRepo
from infra.db.labor import LaborRepo
from infra.db.lazy_bridges import (
    LazyDebtLifecycleHook,
    LazyMarginCollateralGuard,
    LazyOverdueLoanCollector,
    LazyStatusReader,
    lazy_asset_seizure_step,
    lazy_bankrupt_executor,
    lazy_bankrupt_prep_hook,
    lazy_excess_loss_sink,
    lazy_grace_enter_hook,
    lazy_liquidation_step,
    lazy_margin_ratio_reader,
    lazy_seizure_value_step,
    lazy_wealthy_reader,
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
from infra.db.profile import ProfileCore
from infra.db.quiz import QuizRepo
from infra.db.race import RaceRepo
from infra.db.raffle import RaffleRepo
from infra.db.records import RecordRepo
from infra.db.relics import RelicRepo
from infra.db.rtp import RtpRepo
from infra.db.shop import ShopRepo
from infra.db.stream import StreamRepo
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
    streams: StreamRepo
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
    inventory: InventoryCore
    profile: ProfileCore

    def for_guild(self, guild_id: int) -> "RepositoryBundle":
        """指定サーバー（guild_id）にスコープされた各リポジトリのビュー束を返す（接続は共有）。"""
        return RepositoryBundle(
            db=self.db,
            account=self.account,
            inventory=self.inventory,
            profile=self.profile,
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
            streams=self.streams.for_guild(guild_id),
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
    db_path: str, legacy_guild_id: int | None = None, strict: bool = False, clock: IClock | None = None
) -> RepositoryBundle:
    """DB を開き、マイグレーションとテーブル作成のうえ全リポジトリを組み立てて返す。

    clock は日付の切り替わりに使う時差（utc_offset_hours）の出どころ。省略時は設定の既定値（ConfigClock({})）。
    """
    clock = clock if clock is not None else ConfigClock({})
    db = EcoDb(db_path, clock)
    try:
        with db.lock:
            schema.initialize_db(db.conn, db.db_path, legacy_guild_id)
    except Exception:
        db.close()
        raise

    account = AccountCore(db, debt_hook=LazyDebtLifecycleHook(lambda: debt))
    inventory = InventoryCore(db)
    profile = ProfileCore(db)
    notices = NoticeRepo(db, strict=strict)
    rtp = RtpRepo(db, strict=strict)
    events = EventRepo(db, strict=strict)
    pachinko = PachinkoRepo(db, strict=strict)
    relics = RelicRepo(db, account, strict=strict, inventory=inventory)
    market = MarketDataRepo(db, strict=strict)
    market_events = MarketEventRepo(db, strict=strict)
    records = RecordRepo(db, account, strict=strict, inventory=inventory, profile=profile)
    raffle = RaffleRepo(db, account, strict=strict)
    guild = GuildRepo(
        db, wealthy_reader=lazy_wealthy_reader(lambda: wallet.wealthy_ids_for), strict=strict, profile=profile
    )

    bank = BankRepo(
        db,
        account,
        notices,
        excess_sink=lazy_excess_loss_sink(lambda: kyosai.charge_excess_for),
        status_reader=LazyStatusReader(lambda: debt),
        margin_ratio_reader=lazy_margin_ratio_reader(lambda: margin.margin_ratio_for),
        grace_enter_hook=compose_grace_enter_hooks(
            (lazy_grace_enter_hook(lambda: p2p.on_grace_enter), lazy_grace_enter_hook(lambda: kyosai.on_grace_enter))
        ),
        bankrupt_executor=lazy_bankrupt_executor(lambda: debt.bankrupt_for),
        strict=strict,
    )
    debt = DebtRepo(
        db,
        account,
        notices,
        bank,
        loans=LazyOverdueLoanCollector(lambda: loans),
        seizure_step=lazy_asset_seizure_step(lambda: seizure_step),
        liquidation_step=lazy_liquidation_step(lambda: liquidation_step),
        seizure_value_step=lazy_seizure_value_step(lambda: seizure_value_step),
        bankrupt_prep_hook=lazy_bankrupt_prep_hook(lambda: bankrupt_prep_hook),
        strict=strict,
    )
    polls = PollRepo(db, account, bank, strict=strict)
    streams = StreamRepo(db, account, bank, strict=strict)

    loans = LoanRepo(db, account, debt, bank.grace_until_for, strict=strict)
    shop = ShopRepo(db, account, debt, strict=strict, inventory=inventory, profile=profile)
    auction = AuctionRepo(db, account, debt, bank, strict=strict, inventory=inventory)
    escrow = EscrowRepo(db, account, bank, debt, strict=strict, profile=profile)
    quiz = QuizRepo(db, account, debt, strict=strict)
    horses = HorseRepo(db, account, debt, bank, strict=strict, inventory=inventory)
    races = RaceRepo(db, account, horses, relics, bank, strict=strict)
    labor = LaborRepo(db, account, relics, bank, strict=strict, inventory=inventory)
    arena = ArenaRepo(db, account, bank, strict=strict)
    company = CompanyRepo(db, account, debt, bank, notices, clock.offset_hours(), strict=strict)
    earnings = EarningsRepo(db, account, bank, strict=strict)

    crypto = CryptoRepo(
        db,
        account,
        market,
        tax=bank.charge_gains_tax_for,
        collateral_guard=LazyMarginCollateralGuard(lambda: margin),
        strict=strict,
    )
    margin = MarginRepo(db, account, market, bank, bank.charge_gains_tax_for, notices, crypto, debt, strict=strict)

    bonds = BondRepo(db, account, bank, debt_repay=debt.repay_all_of_for, collateral_guard=margin, strict=strict)
    options = OptionRepo(db, account, market, market_events, earnings, bank, debt, strict=strict)
    p2p = P2PRepo(
        db,
        account,
        horses,
        debt,
        bank,
        bank.grace_until_for,
        notices,
        strict=strict,
        inventory=inventory,
        profile=profile,
    )
    kyosai = KyosaiRepo(db, account, bank, notices, strict=strict)

    seizure_step = compose_asset_seizure_steps(
        (
            deposit_seizure_step(account, bank, notices),
            company_seizure_step(account, company, notices),
            margin_seizure_step(account, market.latest_prices, margin, notices),
            crypto_seizure_step(account, market.latest_prices, crypto, notices),
            option_seizure_step(account, options, notices),
            bond_seizure_step(account, bonds, notices),
        )
    )
    liquidation_step = compose_liquidation_steps(
        (
            margin_liquidation_step(market.latest_prices, margin),
            crypto_liquidation_step(market.latest_prices, crypto),
            bond_liquidation_step(account, bonds),
            option_liquidation_step(options),
            deposit_liquidation_step(account, bank),
            company_liquidation_step(account, company),
            kyosai_liquidation_step(kyosai),
        )
    )
    seizure_value_step = compose_seizure_value_steps(
        (shop_and_horse_seizure_value_step(shop.seize_belongings_for, auction, horses),)
    )
    bankrupt_prep_hook = compose_bankrupt_prep_hooks((p2p.prepare, kyosai.prepare))

    valuation_source = compose_valuation_sources(
        (
            crypto.collect_values,
            margin.collect_values,
            bonds.collect_values,
            bank.collect_values,
            options.collect_values,
            company.collect_values,
        )
    )
    wallet = WalletRepo(
        db,
        account,
        bank,
        debt,
        debt,
        relics,
        valuation_source,
        strict=strict,
        inventory=inventory,
        profile=profile,
    )

    asset_collector = compose_asset_collectors(
        (
            cash_and_debt_collector(db),
            valuation_source_collector("deposit", compose_valuation_sources((bank.collect_values,))),
            valuation_source_collector(
                "crypto", compose_valuation_sources((crypto.collect_values, margin.collect_values))
            ),
            valuation_source_collector("bonds", bonds.collect_values),
            valuation_source_collector("options", options.collect_values),
            valuation_source_collector("company", company.collect_values),
            inventory_asset_collector(db),
            labor_asset_collector(db),
            horse_asset_collector(db, horses.horse_worth),
            p2p_asset_collector(db, horses.horse_worth),
            loan_asset_collector(db, loans),
        )
    )
    assets = AssetRepo(db, asset_collector, horse_worth_fn=horses.horse_worth, strict=strict)

    with db.tx():
        auction.purge_unlistable_auctions()

    return RepositoryBundle(
        db=db,
        account=account,
        inventory=inventory,
        profile=profile,
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
        streams=streams,
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
