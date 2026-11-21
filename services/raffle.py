"""抽選（参加費を預かり、締切で 1 人に全額を渡す）のアプリケーションサービス。"""

from typing import Any, Optional

from domain.errors import Outcome
from domain.ports.clock import IClock
from domain.ports.repos import IRaffleRepo


class RaffleService:
    """抽選の募集・参加・中止・締切の精算。"""

    def __init__(self, raffle: IRaffleRepo, clock: IClock) -> None:
        self._raffle = raffle
        self._clock = clock

    def open(self, guild_id: int, host_id: int, fee: int, minutes: int, channel_id: int) -> Outcome[str]:
        closes = self._clock.now_ts() + minutes * 60
        return self._raffle.for_guild(guild_id).raffle_open(host_id, fee, closes, channel_id)

    def current(self, guild_id: int) -> Optional[dict[str, Any]]:
        """募集中の抽選。無ければ None。"""
        return self._raffle.for_guild(guild_id).raffle_get()

    def set_message(self, guild_id: int, raffle_id: int, message_id: int, channel_id: Optional[int] = None) -> None:
        self._raffle.for_guild(guild_id).raffle_set_message(raffle_id, message_id, channel_id)

    def join(self, guild_id: int, user_id: int) -> Outcome[str]:
        return self._raffle.for_guild(guild_id).raffle_join(user_id, self._clock.now_ts())

    def cancel(self, guild_id: int) -> Optional[dict[str, Any]]:
        """募集を中止して、参加費を返す。中止した抽選を返す（無ければ None）。"""
        return self._raffle.for_guild(guild_id).raffle_close(cancel=True)

    def close_if_due(self, guild_id: int) -> Optional[dict[str, Any]]:
        """締切を過ぎた抽選を締めて、当選者を決める。締めたときだけ結果を返す。"""
        repo = self._raffle.for_guild(guild_id)
        raffle = repo.raffle_get()
        if raffle is None or self._clock.now_ts() < raffle["closes"]:
            return None
        return repo.raffle_close()
