"""Bot 本体の型付き基盤クラス。

main.py と各 Cog が共有する依存コンテナ（config, repos, sessions, services, bridges）を
動的な後付けではなく型付き属性として定義する。
"""

from typing import Any

from discord.ext import commands

from cogs.shared.bridges import BotBridges
from domain.ports.repos import IRepositoryBundle
from domain.ports.session import ISessionGuard
from services import ServiceRegistry, build_services


class CasinoBot(commands.Bot):
    """ペリカカジノ Bot 本体。"""

    config: dict[str, Any]
    repos: IRepositoryBundle
    sessions: ISessionGuard
    services: ServiceRegistry
    bridges: BotBridges

    def __init__(
        self,
        *,
        config: dict[str, Any],
        repos: IRepositoryBundle,
        sessions: ISessionGuard,
        services: ServiceRegistry | None = None,
        bridges: BotBridges | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.config = config
        self.repos = repos
        self.sessions = sessions
        self.services = services if services is not None else build_services(config, repos, sessions)
        self.bridges = bridges if bridges is not None else BotBridges()
