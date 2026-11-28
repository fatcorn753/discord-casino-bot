"""テスト用の共有ビルダー（RepositoryBundle・ServiceRegistry・BotBridges・各 Cog の Constructor DI 組み立て）。"""

from types import SimpleNamespace
from typing import Any

from cogs.shared.bridges import (
    BotBridges,
    IApprovalBridge,
    IEventAdminBridge,
    IHorseAdminBridge,
    IHorseButtonBridge,
    ILoanButtonBridge,
    IP2PListingBridge,
    IP2PNavigationBridge,
    IQuizAdminBridge,
    IQuizButtonBridge,
    IRaffleAdminBridge,
    IShopTitleBridge,
)
from domain.ports.session import ISessionGuard
from domain.types import INITIAL_BALANCE
from infra.clock import ConfigClock
from infra.db.repositories import RepositoryBundle, open_repositories
from services import ServiceRegistry, build_services
from services.session import Sessions


def _resolve_guild_id(repos: RepositoryBundle, guild_id: int | None) -> int:
    return repos.wallet.guild_id if guild_id is None else guild_id


def _ensure_account_row(repos: RepositoryBundle, gid: int, user_id: int) -> None:
    repos.db.conn.execute(
        "INSERT OR IGNORE INTO accounts (guild_id, user_id, balance, created_at) VALUES (?, ?, ?, 0.0)",
        (gid, user_id, INITIAL_BALANCE),
    )


def set_test_balance(repos: RepositoryBundle, user_id: int, balance: int, *, guild_id: int | None = None) -> None:
    """テスト用: accounts.balance を直接書き換える（既存 tx の有無を問わず利用可）。"""
    gid = _resolve_guild_id(repos, guild_id)
    repos.db.conn.execute("UPDATE accounts SET balance=? WHERE guild_id=? AND user_id=?", (balance, gid, user_id))


def set_test_debt(
    repos: RepositoryBundle,
    user_id: int,
    debt: int,
    *,
    principal: int | None = None,
    interest_at: float = 0.0,
    guild_id: int | None = None,
) -> None:
    """テスト用: teiai_debts の借金を設定する（0 以下なら行削除）。"""
    gid = _resolve_guild_id(repos, guild_id)
    if debt <= 0:
        repos.db.conn.execute("DELETE FROM teiai_debts WHERE guild_id=? AND user_id=?", (gid, user_id))
        return
    _ensure_account_row(repos, gid, user_id)
    eff_principal = debt if principal is None else principal
    repos.db.conn.execute(
        "INSERT INTO teiai_debts (guild_id, user_id, debt, principal, interest_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
        "debt=excluded.debt, principal=excluded.principal, interest_at=excluded.interest_at",
        (gid, user_id, debt, eff_principal, interest_at),
    )


def set_test_grace(
    repos: RepositoryBundle,
    user_id: int,
    grace_until: float | None,
    *,
    penalty: float = 0.0,
    penalty_at: float = 0.0,
    guild_id: int | None = None,
) -> None:
    """テスト用: teiai_grace の猶予・ペナルティを設定する。"""
    gid = _resolve_guild_id(repos, guild_id)
    _ensure_account_row(repos, gid, user_id)
    repos.db.conn.execute(
        "INSERT INTO teiai_grace (guild_id, user_id, grace_until, penalty, penalty_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
        "grace_until=excluded.grace_until, penalty=excluded.penalty, penalty_at=excluded.penalty_at",
        (gid, user_id, grace_until, penalty, penalty_at),
    )


def set_test_bankrupt(
    repos: RepositoryBundle,
    user_id: int,
    bankrupt_at: float | None,
    blacklist_until: float | None = None,
    *,
    guild_id: int | None = None,
) -> None:
    """テスト用: teiai_bankruptcies の破産・ブラックリスト時刻を設定する（None で削除）。"""
    gid = _resolve_guild_id(repos, guild_id)
    if bankrupt_at is None:
        repos.db.conn.execute("DELETE FROM teiai_bankruptcies WHERE guild_id=? AND user_id=?", (gid, user_id))
        return
    _ensure_account_row(repos, gid, user_id)
    repos.db.conn.execute(
        "INSERT INTO teiai_bankruptcies (guild_id, user_id, bankrupt_at, blacklist_until) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
        "bankrupt_at=excluded.bankrupt_at, blacklist_until=excluded.blacklist_until",
        (gid, user_id, bankrupt_at, blacklist_until),
    )


def set_test_games_played(
    repos: RepositoryBundle, user_id: int, games_played: int, *, guild_id: int | None = None
) -> None:
    """テスト用: user_play_stats.games_played を直接設定する。"""
    gid = _resolve_guild_id(repos, guild_id)
    _ensure_account_row(repos, gid, user_id)
    repos.db.conn.execute(
        "INSERT INTO user_play_stats (guild_id, user_id, games_played) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET games_played=excluded.games_played",
        (gid, user_id, games_played),
    )


def give_test_kyosai_units(
    repos: RepositoryBundle, user_id: int, units: int, *, guild_id: int | None = None, now: float = 0.0
) -> None:
    """テスト用: 帝愛持ち口（kyosai_state.house_units）から user_id へ共済の口を移す。"""
    gid = _resolve_guild_id(repos, guild_id)
    repos.kyosai.bank_sync(gid, now)
    repos.db.conn.execute(
        "UPDATE kyosai_state SET house_units=house_units-? WHERE guild_id=?", (units, gid)
    )
    repos.db.conn.execute(
        "INSERT INTO kyosai_units (guild_id, user_id, units) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET units=units+excluded.units",
        (gid, user_id, units),
    )


def insert_test_loan(
    repos: RepositoryBundle,
    lender_id: int | None,
    borrower_id: int,
    principal: int,
    *,
    status: str = "active",
    guild_id: int | None = None,
    rate_tenths: int = 0,
    term_days: int = 5,
    created: float = 0.0,
    offer_expires: float = 0.0,
    accepted_at: float | None = None,
    due: float | None = None,
    paid: int = 0,
    reminded: int = 0,
) -> int:
    """テスト用: 正規化された loans + loan_terms 行を直接挿入する。"""
    gid = _resolve_guild_id(repos, guild_id)
    cur = repos.db.conn.execute(
        "INSERT INTO loans (guild_id, lender_id, borrower_id, principal, rate_tenths, term_days, status, created, offer_expires) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (gid, lender_id, borrower_id, principal, rate_tenths, term_days, status, created, offer_expires),
    )
    loan_id = cur.lastrowid
    assert loan_id is not None
    if accepted_at is not None or due is not None:
        eff_acc = created if accepted_at is None else accepted_at
        eff_due = (eff_acc + term_days * 86400.0) if due is None else due
        repos.db.conn.execute(
            "INSERT INTO loan_terms (loan_id, accepted_at, due, paid, reminded) VALUES (?, ?, ?, ?, ?)",
            (loan_id, eff_acc, eff_due, paid, reminded),
        )
    return loan_id


def make_test_bot(
    db_path: str = ":memory:",
    *,
    legacy_guild_id: int | None = None,
    strict: bool = False,
    config: dict[str, Any] | None = None,
    sessions: ISessionGuard | None = None,
    repos: RepositoryBundle | None = None,
) -> SimpleNamespace:
    """テスト用の軽量 Bot コンテナ（repos / services / sessions / bridges / config を保持）を組み立てる。"""
    cfg = {} if config is None else config
    bundle = (
        repos
        if repos is not None
        else open_repositories(db_path, legacy_guild_id=legacy_guild_id, strict=strict, clock=ConfigClock(cfg))
    )
    sess = sessions if sessions is not None else Sessions(cooldown=0)
    # リポジトリの内部処理と Service・Cog が同じ時計を使う（freeze_time で両方の時刻が動く）
    services: ServiceRegistry = build_services(cfg, bundle, sess, bundle.db.clock)
    cogs: dict[str, Any] = {}
    return SimpleNamespace(
        config=cfg,
        repos=bundle,
        sessions=sess,
        services=services,
        bridges=BotBridges(),
        guilds=[],
        get_guild=lambda _gid: None,
        get_cog=lambda name: cogs.get(name),
        _cogs=cogs,
    )


def make_economy_cog(bot: Any):
    from cogs.economy.cog import EconomyCog

    return EconomyCog(
        bot,
        config=bot.config,
        economy_svc=bot.services.economy,
        teiai_svc=bot.services.teiai,
        ranking_svc=bot.services.ranking,
        guild_repo=bot.repos.guild,
        sessions=bot.sessions,
        clock=bot.services.clock,
    )


def make_market_cog(bot: Any):
    from cogs.market.cog import MarketCog

    return MarketCog(
        bot,
        config=bot.config,
        market_svc=bot.services.market,
        economy_svc=bot.services.economy,
        teiai_svc=bot.services.teiai,
        guild_repo=bot.repos.guild,
        sessions=bot.sessions,
    )


def make_bond_cog(bot: Any):
    from cogs.market.bond_cog import BondCog

    return BondCog(
        bot,
        config=bot.config,
        guild_repo=bot.repos.guild,
        market_svc=bot.services.market,
        teiai_svc=bot.services.teiai,
    )


def make_p2p_cog(bot: Any):
    from cogs.p2p.cog import P2PCog

    cog = P2PCog(
        bot,
        bot.config,
        bot.services.p2p,
        bot.repos.guild,
        bot.sessions,
    )
    bot.bridges.register(IP2PListingBridge, cog)
    bot.bridges.register(IP2PNavigationBridge, cog)
    return cog


def make_shop_cog(bot: Any):
    from cogs.shop.cog import ShopCog

    cog = ShopCog(bot, bot.config, bot.services.shop, bot.repos.guild, bot.sessions)
    bot.bridges.register(IShopTitleBridge, cog)
    return cog


def make_horse_cog(bot: Any):
    from cogs.horse.cog import HorseCog

    cog = HorseCog(
        bot,
        bot.config,
        bot.services.horse,
        bot.repos.guild,
    )
    bot.bridges.register(IHorseAdminBridge, cog)
    bot.bridges.register(IHorseButtonBridge, cog)
    return cog


def make_quiz_cog(bot: Any):
    from cogs.quiz.cog import QuizCog

    cog = QuizCog(bot, bot.config, bot.services.quiz, bot.repos.guild)
    bot.bridges.register(IQuizAdminBridge, cog)
    bot.bridges.register(IQuizButtonBridge, cog)
    return cog


def make_raffle_cog(bot: Any):
    from cogs.raffle.cog import RaffleCog

    cog = RaffleCog(bot, bot.config, bot.services.raffle, bot.repos.guild)
    bot.bridges.register(IRaffleAdminBridge, cog)
    return cog


def make_event_cog(bot: Any):
    from cogs.event.cog import EventCog

    cog = EventCog(bot, bot.config, bot.services.event, bot.repos.guild, bot.services.clock)
    bot.bridges.register(IEventAdminBridge, cog)
    return cog


def make_approval_cog(bot: Any):
    from cogs.admin.cog import ApprovalCog

    cog = ApprovalCog(
        bot,
        config=bot.config,
        guild_repo=bot.repos.guild,
        admin_svc=bot.services.admin,
        quiz_svc=bot.services.quiz,
        labor_svc=bot.services.labor,
    )
    bot.bridges.register(IApprovalBridge, cog)
    return cog


def make_ranking_repos(repos: RepositoryBundle):
    from services.ranking import RankingService

    return RankingService(wallet=repos.wallet, assets=repos.assets)


def make_casino_deps(bot: Any):
    from cogs.casino.common import CasinoDeps

    return CasinoDeps(
        casino=bot.services.casino,
        teiai=bot.services.teiai,
        guild_repo=bot.repos.guild,
        config=bot.config,
    )


def make_loan_cog(bot: Any):
    from cogs.loan.cog import LoanCog

    cog = LoanCog(bot, bot.config, bot.services.loan, bot.services.economy, bot.repos.guild)
    bot.bridges.register(ILoanButtonBridge, cog)
    return cog


def make_labor_cog(bot: Any):
    from cogs.labor.cog import LaborCog

    return LaborCog(bot, bot.services.labor)


def make_arena_cog(bot: Any):
    from cogs.arena.cog import ArenaCog

    return ArenaCog(bot, bot.config, bot.services.arena, bot.repos.guild)


def make_weekly_cog(bot: Any):
    from cogs.weekly.cog import WeeklyCog

    return WeeklyCog(
        bot,
        bot.config,
        bot.services.economy,
        bot.repos.guild,
        clock=bot.services.clock,
    )
