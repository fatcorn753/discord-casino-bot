"""地下作業のアプリケーションサービス（日付キー解決・クリック不正検知レポート）。"""

import time
from typing import Any

from domain import labor as labor_mod
from domain.ports import IClock, ILaborPort
from infra.clock import offset_hours


class LaborService:
    """地下作業の日付キー・クリック分析レポート取得を担う。"""

    def __init__(self, economy: ILaborPort, clock: IClock, config: dict[str, Any]) -> None:
        self._economy = economy
        self._clock = clock
        self._config = config

    def today_key(self) -> str:
        return self._clock.today().isoformat()

    def utc_offset(self) -> float:
        return offset_hours(self._config)

    def click_report(self, guild_id: int, user_id: int, hours: int) -> dict[str, Any]:
        eco = self._economy.for_guild(guild_id)
        rows = eco.labor_click_hours(user_id, time.time() - hours * 3600)
        return labor_mod.click_report(rows)
