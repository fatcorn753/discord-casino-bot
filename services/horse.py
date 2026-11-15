"""競馬のレース進行・オッズ計算・スケジュール管理のアプリケーションサービス。"""

import time
from typing import Any, Optional

from domain import horse as horse_mod
from domain.ports import IHorsePort
from infra.clock import offset_hours


class HorseService:
    """定時レースのスケジュール作成・締切オッズ確定・発走精算の調整役。"""

    def __init__(self, economy: IHorsePort, config: dict[str, Any]) -> None:
        self._economy = economy
        self._config = config

    def tz_hours(self) -> float:
        return offset_hours(self._config)

    def next_slots(self, guild_id: int, now: Optional[float] = None) -> list[float]:
        now = time.time() if now is None else now
        return self._economy.for_guild(guild_id).race_slots(now, self.tz_hours())

    def ensure_scheduled(self, guild_id: int, now: float, channel_id: int) -> Optional[int]:
        eco = self._economy.for_guild(guild_id)
        return eco.race_ensure_scheduled(now, self.tz_hours(), channel_id)

    def close_entries_and_set_odds(self, guild_id: int, race_id: int) -> Optional[dict]:
        """出走申込を締め切ってオッズを計算・保存し、最新の race 辞書を返す。"""
        eco = self._economy.for_guild(guild_id)
        field = eco.race_close_entries(race_id)
        if field is None:
            return None
        eco.race_set_odds(race_id, horse_mod.make_all_odds(field["runners"]))
        return eco.race_get(race_id)

    def run_race(self, guild_id: int, race_id: int) -> Optional[dict]:
        """レースを実行して賞金・馬券を精算し、演出結果辞書を返す。"""
        eco = self._economy.for_guild(guild_id)
        return eco.race_run(race_id, tz_hours=self.tz_hours())
