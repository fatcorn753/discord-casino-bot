"""突発イベント（日替わりの自動開催）と、持ち主が開く特別イベントのアプリケーションサービス。"""

import datetime

from domain.ports.clock import IClock
from domain.ports.contracts import ManualEventRow
from domain.ports.repos import IEventRepo


class EventService:
    """イベントの告知状態の記録と、特別イベントの開始・終了。"""

    def __init__(self, events: IEventRepo, clock: IClock) -> None:
        self._events = events
        self._clock = clock

    def phase(self, guild_id: int, day: datetime.date) -> int:
        """その日の突発イベントの告知状態（0: まだ・1: 開始を告知・2: 終了を告知）。"""
        return self._events.for_guild(guild_id).event_phase(day)

    def set_phase(self, guild_id: int, day: datetime.date, phase: int) -> None:
        self._events.for_guild(guild_id).set_event_phase(day, phase)

    def start_manual(
        self, guild_id: int, game: str, boost_pct: int, minutes: int, channel_id: int
    ) -> tuple[bool, ManualEventRow | None]:
        """特別イベントを開く（開催中のものは置き換える）。(置き換えたか, 開いたイベント) を返す。"""
        repo = self._events.for_guild(guild_id)
        now = self._clock.now_ts()
        replaced = repo.manual_event_get(now) is not None
        repo.manual_event_start(game, boost_pct, now, now + minutes * 60, channel_id)
        return replaced, repo.manual_event_get(now)

    def stop_manual(self, guild_id: int) -> ManualEventRow | None:
        return self._events.for_guild(guild_id).manual_event_stop(self._clock.now_ts())

    def finish_due(self, guild_id: int, now: float) -> ManualEventRow | None:
        """時間が来た特別イベントを終える（終了の告知は1回だけ）。"""
        return self._events.for_guild(guild_id).manual_event_finish_due(now)
