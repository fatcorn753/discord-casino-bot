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
from infra.clock import ConfigClock
from infra.db.repositories import RepositoryBundle, open_repositories
from services import ServiceRegistry, build_services
from services.session import Sessions


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

    return RankingService(
        wallet=repos.wallet,
        crypto=repos.crypto,
        bonds=repos.bonds,
        assets=repos.assets,
    )


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
