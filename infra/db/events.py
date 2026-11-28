"""突発イベントの進み具合と、持ち主が開く特別イベント。

表の定義は infra/db/schema/tables.py にある。定例イベントの中身は domain/events.py。
"""

import datetime

from domain.ports.contracts import ManualEventRow
from domain.ports.repos import IEventRepo
from infra.db.base import EcoDb, ScopedRepo
from infra.db.discord_posts import init_discord_post

_SELECT_EVENT = (
    "SELECT e.guild_id, e.game, e.boost, e.starts, e.ends, e.ended, COALESCE(p.channel_id, 0) AS channel_id "
    "FROM manual_event e "
    "LEFT JOIN discord_posts p ON p.entity_type = 'manual_event' AND p.entity_id = e.guild_id"
)


class EventRepo(ScopedRepo, IEventRepo):
    """突発イベントと特別イベントの記録。"""

    def __init__(self, db: EcoDb, guild_id: int = 0, strict: bool = False) -> None:
        super().__init__(db, guild_id, strict)

    def event_phase(self, day: datetime.date) -> int:
        with self._lock:
            v = self._scalar("SELECT phase FROM event_state WHERE guild_id=? AND day=?", (self._g(), day.isoformat()))
            return v if v is not None else 0

    def set_event_phase(self, day: datetime.date, phase: int) -> None:
        with self._db.tx():
            self._conn.execute(
                "INSERT INTO event_state (guild_id, day, phase) VALUES (?, ?, ?) ON CONFLICT(guild_id, day) DO UPDATE SET phase=excluded.phase",
                (self._g(), day.isoformat(), phase),
            )

    def manual_event_start(self, game: str, boost_pct: int, starts: float, ends: float, channel_id: int = 0) -> None:
        with self._db.tx():
            gid = self._g()
            self._conn.execute(
                "INSERT OR REPLACE INTO manual_event (guild_id, game, boost, starts, ends, ended) VALUES (?, ?, ?, ?, ?, 0)",
                (gid, game, boost_pct, starts, ends),
            )
            init_discord_post(self._conn, "manual_event", gid, channel_id)

    def manual_event_get(self, now: float) -> ManualEventRow | None:
        """いま開催中の特別イベント。無ければ None。"""
        with self._lock:
            return self._one(
                f"{_SELECT_EVENT} WHERE e.guild_id=? AND e.ended=0 AND e.starts<=? AND e.ends>?",
                (self._g(), now, now),
            )

    def manual_event_stop(self, now: float) -> ManualEventRow | None:
        """開催中の特別イベントを、すぐ終わらせる。終わらせたイベントを返す（開催中でなければ None）。"""
        with self._db.tx():
            gid = self._g()
            row = self._one(f"{_SELECT_EVENT} WHERE e.guild_id=? AND e.ended=0 AND e.ends>?", (gid, now))
            if row is None:
                return None
            self._conn.execute("UPDATE manual_event SET ended=1, ends=? WHERE guild_id=?", (now, gid))
            return row

    def manual_event_finish_due(self, now: float) -> ManualEventRow | None:
        """時間が来た特別イベントを終了にして返す（終了の告知を1回だけ出すため）。無ければ None。"""
        with self._db.tx():
            gid = self._g()
            row = self._one(f"{_SELECT_EVENT} WHERE e.guild_id=? AND e.ended=0 AND e.ends<=?", (gid, now))
            if row is None:
                return None
            self._conn.execute("UPDATE manual_event SET ended=1 WHERE guild_id=?", (gid,))
            return row
