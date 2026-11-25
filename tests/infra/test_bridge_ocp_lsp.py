"""Bridge パターンにおける OCP・ISP・LSP 準拠を検証する単体テスト。"""

import asyncio
from types import SimpleNamespace

import discord
import pytest

from cogs.shared.bridges import (
    BotBridges,
    DeferredShopTitleBridge,
    IApprovalBridge,
    IEventAdminBridge,
    IHorseAdminBridge,
    IHorseButtonBridge,
    IP2PListingBridge,
    IP2PNavigationBridge,
    IQuizAdminBridge,
    IQuizButtonBridge,
    IShopTitleBridge,
    NoOpApprovalBridge,
    NoOpHorseButtonBridge,
    NoOpLoanButtonBridge,
    NoOpQuizButtonBridge,
    NoOpShopTitleBridge,
)
from domain import teiai
from domain.ports.bridges import (
    AssetSeizureStep,
    compose_asset_collectors,
    compose_asset_seizure_steps,
    compose_bankrupt_prep_hooks,
    compose_grace_enter_hooks,
    compose_liquidation_steps,
    compose_seizure_value_steps,
    compose_valuation_sources,
    noop_asset_seizure_step,
    noop_bankrupt_prep_hook,
    noop_grace_enter_hook,
    noop_liquidation_step,
    noop_seizure_value_step,
)
from infra.db import schema
from infra.db.account import AccountCore
from infra.db.assets import valuation_source_collector
from infra.db.bank import BankRepo
from infra.db.base import EcoDb
from infra.db.bonds import BondRepo
from infra.db.crypto import CryptoRepo
from infra.db.debt import DebtRepo
from infra.db.earnings import EarningsRepo
from infra.db.guild import GuildRepo
from infra.db.lazy_bridges import LazyStatusReader, lazy_asset_seizure_step
from infra.db.loans import LoanRepo
from infra.db.margin import MarginRepo
from infra.db.market import MarketDataRepo
from infra.db.market_events import MarketEventRepo
from infra.db.options import OptionRepo
from tests.fakes import as_bot, must
from tests.support import make_economy_cog, make_test_bot

G = 1
U = 100


def _make_standalone_db() -> EcoDb:
    db = EcoDb(":memory:")
    with db.lock:
        schema.initialize_db(db.conn, db.db_path, None)
    return db


@pytest.mark.real_scope
def test_standalone_repos_run_with_default_noop_bridges_satisfying_lsp():
    """すべての DB リポジトリはオプショナルブリッジ未注入（既定 NoOp*）でも NotImplementedError を出さず LSP を満たす。"""
    db = _make_standalone_db()
    try:
        account = AccountCore(db)
        with db.tx():
            account.ensure(G, U)
            initial = account.raw_balance(G, U)
            assert initial > 0
            assert account.apply(G, U, 500, "test") == 500

        raw_bank = BankRepo(db, account)
        market = MarketDataRepo(db)
        market_events = MarketEventRepo(db)
        earnings = EarningsRepo(db, account, raw_bank)

        bank = raw_bank.for_guild(G)
        debt = DebtRepo(db, account).for_guild(G)
        loans = LoanRepo(db, account).for_guild(G)
        bonds = BondRepo(db, account, raw_bank).for_guild(G)
        crypto = CryptoRepo(db, account, market, raw_bank.charge_gains_tax_for).for_guild(G)
        margin = MarginRepo(db, account, market, raw_bank, raw_bank.charge_gains_tax_for).for_guild(G)
        options = OptionRepo(db, account, market, market_events, earnings, raw_bank).for_guild(G)
        guild_repo = GuildRepo(db).for_guild(G)

        deposit_amt = max(200, teiai.BANK_MIN_DEPOSIT)
        with db.tx():
            account.apply(G, U, deposit_amt, "fund-deposit")
        assert bank.bank_deposit(U, deposit_amt, 1000.0)[0] is True
        bank_totals: dict[int, int] = {}
        bank.collect_values(G, bank_totals)
        assert bank_totals[U] == deposit_amt

        with db.tx():
            account.apply(G, U, -account.raw_balance(G, U), "drain")
            assert debt.owe_for(G, U, 100, "test-debt") == 100
        assert debt.bankrupt(U)[0] is True

        assert loans.loan_due_soon(1000.0) == []
        assert isinstance(loans.loan_credit(U, 1000.0)["status"], str)
        assert bonds.bond_values() == {}
        assert crypto.crypto_values() == {}
        assert crypto.crypto_sell(U, 1, 100.0).ok is False
        assert margin.margin_equity() == {}
        assert margin.margin_ratio_for(G, U) is None
        assert options.option_values() == {}
        assert guild_repo.status_nick_targets() == [U]
    finally:
        db.close()


def test_persistence_composites_and_valuation_collector_support_open_extension():
    """compose_* と valuation_source_collector は既存実装を変更せず新種ステップ・評価源を拡張できる（OCP）。"""

    def custom_source(guild_id: int, totals: dict[int, int]) -> None:
        totals[U] = totals.get(U, 0) + 777

    composed_source = compose_valuation_sources((custom_source,))
    totals: dict[int, int] = {}
    composed_source(G, totals)
    assert totals == {U: 777}

    parts: dict[tuple[int, str], int] = {}

    def add_part(uid: int, key: str, value: int) -> None:
        parts[(uid, key)] = parts.get((uid, key), 0) + value

    collector = compose_asset_collectors((valuation_source_collector("custom_token", composed_source),))
    collector(G, add_part, 0.0)
    assert parts == {(U, "custom_token"): 777}

    calls: list[str] = []

    def hook_a(guild_id: int, user_id: int, now: float) -> None:
        calls.append(f"hook:{guild_id}:{user_id}")

    def prep_a(guild_id: int, user_id: int, now: float) -> None:
        calls.append(f"prep:{guild_id}:{user_id}")

    def seize_a(guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
        return min(need, 120)

    def seize_b(guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
        return min(need, 80)

    def liq_a(guild_id: int, user_id: int, now: float) -> None:
        calls.append(f"liq:{guild_id}:{user_id}")

    def val_a(guild_id: int, user_id: int, now: float) -> int:
        return 10

    def val_b(guild_id: int, user_id: int, now: float) -> int:
        return 20

    compose_grace_enter_hooks((hook_a,))(G, U, 123.0)
    compose_bankrupt_prep_hooks((prep_a,))(G, U, 123.0)
    compose_liquidation_steps((liq_a,))(G, U, 123.0)
    assert calls == [f"hook:{G}:{U}", f"prep:{G}:{U}", f"liq:{G}:{U}"]
    assert compose_asset_seizure_steps((seize_a, seize_b))(G, U, 500, "test", 0.0) == 200
    assert compose_seizure_value_steps((val_a, val_b))(G, U, 0.0) == 30

    # 束縛メソッドもそのままステップとして渡せる（OCP）。遅延解決は lazy_* ファクトリで包む
    class CustomSeize:
        def seize(self, guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
            return min(need, 120)

    resolved_seize = CustomSeize()
    lazy_seize: AssetSeizureStep = lazy_asset_seizure_step(lambda: resolved_seize.seize)
    assert lazy_seize(G, U, 500, "test", 0.0) == 120

    # 既定の noop_* は例外を出さず安全に動く（LSP）
    assert noop_asset_seizure_step(G, U, 500, "test", 0.0) == 0
    assert noop_seizure_value_step(G, U, 0.0) == 0
    noop_grace_enter_hook(G, U, 0.0)
    noop_bankrupt_prep_hook(G, U, 0.0)
    noop_liquidation_step(G, U, 0.0)
    assert compose_grace_enter_hooks((noop_grace_enter_hook, hook_a))(G, U, 1.0) is None
    assert calls[-1] == f"hook:{G}:{U}"

    lazy_db = _make_standalone_db()
    try:
        lazy_status = LazyStatusReader(lambda: DebtRepo(lazy_db, AccountCore(lazy_db)))
        assert isinstance(lazy_status.status_for(G, U, 1000.0), str)
    finally:
        lazy_db.close()


def test_bot_bridges_is_open_for_new_contracts_and_enforces_nominal_types():
    """BotBridges は本体修正なしで新規契約クラスを登録・解決でき（OCP）、契約継承違反は即座に検出する。"""

    class ICustomFeatureBridge:
        def ping(self) -> str:
            raise NotImplementedError

    class CustomFeature(ICustomFeatureBridge):
        def ping(self) -> str:
            return "pong"

    bridges = BotBridges()
    impl = CustomFeature()
    bridges.register(ICustomFeatureBridge, impl)
    assert bridges.has(ICustomFeatureBridge)
    resolved = bridges.resolve(ICustomFeatureBridge)
    assert resolved is impl and must(resolved).ping() == "pong"

    with pytest.raises(TypeError):
        bridges.register(ICustomFeatureBridge, SimpleNamespace(ping=lambda: "bad"))  # type: ignore[arg-type]


def test_deferred_and_dynamic_cog_bridges_are_load_order_independent(tmp_path):
    """ShopCog・P2PCog・EventCog 等のロード順序に依存せず、後から登録されたブリッジを動的解決できる。"""
    from cogs.admin.cog import ApprovalCog
    from cogs.event.cog import EventCog
    from cogs.p2p.cog import P2PCog
    from cogs.shop.cog import ShopCog
    from cogs.weekly.cog import WeeklyCog

    bot = make_test_bot(db_path=str(tmp_path / "order.db"))
    eco = make_economy_cog(bot)
    weekly = WeeklyCog(
        as_bot(bot),
        bot.config,
        bot.services.economy,
        bot.repos.guild,
        clock=bot.services.clock,
    )
    shop_cog = ShopCog(as_bot(bot), bot.config, bot.services.shop, bot.repos.guild, bot.sessions)
    approval = ApprovalCog(
        as_bot(bot),
        config=bot.config,
        guild_repo=bot.repos.guild,
        admin_svc=bot.services.admin,
        quiz_svc=bot.services.quiz,
        labor_svc=bot.services.labor,
    )

    # 未登録時は NoOp へ安全にフォールバックし、後から登録されると実体を解決する
    from cogs.shared.bridges import resolve_bridge

    assert isinstance(eco._shop_bridge, DeferredShopTitleBridge)
    assert asyncio.run(eco._shop_bridge.sync_title_nick(U)) == ("", 0)
    assert asyncio.run(weekly._shop_bridge.sync_title_nick(U)) == ("", 0)
    assert resolve_bridge(shop_cog.bot, IP2PNavigationBridge, shop_cog._p2p_bridge) is None
    assert resolve_bridge(approval.bot, IEventAdminBridge, approval._event_bridge) is None

    sync_calls: list[int] = []

    class StubShop(IShopTitleBridge):
        async def sync_title_nick(self, user_id, here=None, member=None):
            sync_calls.append(user_id)
            return ("", 1)

        async def after_bankruptcy(self, guild, member):
            return None

    p2p_cog = P2PCog(
        as_bot(bot),
        bot.config,
        bot.services.p2p,
        bot.repos.guild,
        bot.sessions,
    )
    event_cog = EventCog(as_bot(bot), bot.config, bot.services.event, bot.repos.guild, bot.services.clock)

    bot.bridges.register(IShopTitleBridge, StubShop())
    bot.bridges.register(IP2PListingBridge, p2p_cog)
    bot.bridges.register(IP2PNavigationBridge, p2p_cog)
    bot.bridges.register(IEventAdminBridge, event_cog)

    assert asyncio.run(eco._shop_bridge.sync_title_nick(U)) == ("", 1)
    assert asyncio.run(weekly._shop_bridge.sync_title_nick(U)) == ("", 1)
    assert sync_calls == [U, U]
    assert resolve_bridge(shop_cog.bot, IP2PNavigationBridge, shop_cog._p2p_bridge) is p2p_cog
    assert resolve_bridge(approval.bot, IEventAdminBridge, approval._event_bridge) is event_cog

    # アンロード時に自身の登録だけを解除できる
    bot.bridges.unregister(IEventAdminBridge, event_cog)
    assert resolve_bridge(approval.bot, IEventAdminBridge, approval._event_bridge) is None


def test_cog_bridge_isp_splits_and_noop_buttons_follow_lsp():
    """ISP 分割後の最小契約だけを実装したスタブが正常に動作し、NoOp* も例外を出さない。"""
    from cogs.horse.cards import RaceButton
    from cogs.loan.cog import LoanButton
    from cogs.quiz.views import PublicButton

    bridges = BotBridges()
    client = SimpleNamespace(bridges=bridges)
    interaction = SimpleNamespace(client=client, user=SimpleNamespace(id=1), guild_id=G)

    # 未登録時の NoOp フォールバックが安全に完了する
    asyncio.run(RaceButton("card", 1).callback(interaction))  # type: ignore[arg-type]
    asyncio.run(PublicButton(1, 0).callback(interaction))  # type: ignore[arg-type]
    asyncio.run(LoanButton("accept", 1).callback(interaction))  # type: ignore[arg-type]

    # ISP: HorseButton と QuizButton は管理メソッドを要求しない
    horse_clicks: list[tuple[str, int]] = []
    quiz_clicks: list[tuple[int, int]] = []

    class OnlyHorseButton(IHorseButtonBridge):
        async def handle_button(self, interaction: discord.Interaction, action: str, race_id: int) -> None:
            horse_clicks.append((action, race_id))

    class OnlyQuizButton(IQuizButtonBridge):
        async def handle_button(self, interaction: discord.Interaction, quiz_id: int, pos: int) -> None:
            quiz_clicks.append((quiz_id, pos))

    bridges.register(IHorseButtonBridge, OnlyHorseButton())
    bridges.register(IQuizButtonBridge, OnlyQuizButton())
    assert not bridges.has(IHorseAdminBridge)
    assert not bridges.has(IQuizAdminBridge)

    asyncio.run(RaceButton("bet", 42).callback(interaction))  # type: ignore[arg-type]
    asyncio.run(PublicButton(7, 2).callback(interaction))  # type: ignore[arg-type]
    assert horse_clicks == [("bet", 42)]
    assert quiz_clicks == [(7, 2)]

    # 全 NoOp クラスの直接呼び出しも LSP 準拠
    guild = SimpleNamespace(id=G)
    member = SimpleNamespace(id=U)
    assert asyncio.run(NoOpApprovalBridge().ensure_requested(guild)) is None  # type: ignore[arg-type]
    assert asyncio.run(NoOpApprovalBridge().announce_approved(guild)) is None  # type: ignore[arg-type]
    assert asyncio.run(NoOpApprovalBridge().handle_button(interaction, True, G)) is None  # type: ignore[arg-type]
    assert asyncio.run(NoOpShopTitleBridge().after_bankruptcy(guild, member)) is None  # type: ignore[arg-type]
    assert asyncio.run(NoOpHorseButtonBridge().handle_button(interaction, "card", 1)) is None  # type: ignore[arg-type]
    assert asyncio.run(NoOpQuizButtonBridge().handle_button(interaction, 1, 0)) is None  # type: ignore[arg-type]
    assert asyncio.run(NoOpLoanButtonBridge().handle_button(interaction, "cancel", 1)) is None  # type: ignore[arg-type]
    assert not bridges.has(IApprovalBridge)
