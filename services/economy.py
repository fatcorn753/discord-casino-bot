"""経済・デイリー・送金・週間ランキング補完のアプリケーションサービス。"""

import datetime
import time
from dataclasses import dataclass
from typing import Any, Optional

from domain.earnings import MemoInfo
from domain.ports import IClock, IEconomyServicePort
from domain.types import DailyResult, TransferResult
from infra.clock import offset_hours


@dataclass(frozen=True)
class DailyClaimOutcome:
    result: DailyResult
    memo: Optional[MemoInfo] = None


class EconomyService:
    """デイリーボーナス・送金・週間集計補完のユースケース調整。"""

    def __init__(self, economy: IEconomyServicePort, clock: IClock, config: dict[str, Any]) -> None:
        self._economy = economy
        self._clock = clock
        self._config = config

    def utc_offset(self) -> float:
        return offset_hours(self._config)

    def claim_daily(self, guild_id: int, user_id: int, now: datetime.datetime, slot: int) -> DailyClaimOutcome:
        """デイリー受取・一時通知の掃き出し・裏情報メモ抽選を 1 ユースケースとして実行する。"""
        eco = self._economy.for_guild(guild_id)
        result = eco.claim_daily(user_id, now.date(), slot)
        if not result.ok:
            return DailyClaimOutcome(result=result)
        eco.take_notices(user_id)
        memo = eco.earnings_memo(user_id, time.time())
        return DailyClaimOutcome(result=result, memo=memo)

    def transfer(
        self,
        guild_id: int,
        sender_id: int,
        receiver_id: int,
        amount: int,
        fee_rate: float,
        daily_limit: int,
        min_games: int,
        now: Optional[datetime.datetime] = None,
    ) -> TransferResult:
        now = self._clock.now() if now is None else now
        day_start = datetime.datetime.combine(now.date(), datetime.time(), tzinfo=now.tzinfo).timestamp()
        return self._economy.for_guild(guild_id).transfer(
            sender_id, receiver_id, amount, fee_rate, daily_limit, day_start, min_games
        )

    def backfill_weekly(self) -> int:
        return self._economy.backfill_weekly(self.utc_offset())
