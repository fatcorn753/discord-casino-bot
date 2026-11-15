"""Bot 本体の型付き基盤クラス。

main.py と各 Cog が共有する属性（config, economy, sessions, services）を
動的な後付けではなく型付き属性として定義する。
"""

from typing import Any, Optional

from discord.ext import commands

from domain.ports import IEconomy, ISessionGuard
from services import ServiceRegistry, build_services


class CasinoBot(commands.Bot):
    """ペリカカジノ Bot 本体。"""

    config: dict[str, Any]
    economy: IEconomy
    sessions: ISessionGuard
    services: ServiceRegistry

    def __init__(
        self,
        *,
        config: dict[str, Any],
        economy: IEconomy,
        sessions: ISessionGuard,
        services: Optional[ServiceRegistry] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.config = config
        self.economy = economy
        self.sessions = sessions
        self.services = services if services is not None else build_services(config, economy, sessions)
