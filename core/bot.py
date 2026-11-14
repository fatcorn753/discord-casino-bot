"""Bot 本体の型付き基盤クラス。

main.py と各 Cog / play モジュールが共有する属性（config, economy, sessions）を
動的な後付けではなく型付き属性として定義する。
"""

from typing import Any

from discord.ext import commands

from core.economy import Economy
from core.session import Sessions


class CasinoBot(commands.Bot):
    """ペリカカジノ Bot 本体。"""

    config: dict[str, Any]
    economy: Economy
    sessions: Sessions

    def __init__(
        self,
        *,
        config: dict[str, Any],
        economy: Economy,
        sessions: Sessions,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.config = config
        self.economy = economy
        self.sessions = sessions
