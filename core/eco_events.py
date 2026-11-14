"""突発イベントの進み具合と、持ち主が開く特別イベント（Economy に混ぜ込む部品）。

表の定義は core/economy.py の _init_schema にある。定例イベントの中身は core/events.py。
"""

import datetime
from typing import Optional

from core.eco_base import EcoBase


class EventMixin(EcoBase):
    """突発イベントと特別イベントの記録。"""

    def event_phase(self, day: datetime.date) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT phase FROM event_state WHERE guild_id=? AND day=?", (self._g(), day.isoformat())
            ).fetchone()
            return row["phase"] if row else 0

    def set_event_phase(self, day: datetime.date, phase: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO event_state (guild_id, day, phase) VALUES (?, ?, ?) ON CONFLICT(guild_id, day) DO UPDATE SET phase=excluded.phase",
                (self._g(), day.isoformat(), phase),
            )

    # ---- 持ち主が開く特別イベント ---- #
    def manual_event_start(self, game: str, boost_pct: int, starts: float, ends: float, channel_id: int = 0) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO manual_event (guild_id, game, boost, starts, ends, ended, channel_id) VALUES (?, ?, ?, ?, ?, 0, ?)",
                (self._g(), game, boost_pct, starts, ends, channel_id),
            )

    def manual_event_get(self, now: float) -> Optional[dict]:
        """いま開催中の特別イベント。無ければ None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM manual_event WHERE guild_id=? AND ended=0 AND starts<=? AND ends>?",
                (self._g(), now, now),
            ).fetchone()
        return dict(row) if row else None

    def manual_event_stop(self, now: float) -> Optional[dict]:
        """開催中の特別イベントを、すぐ終わらせる。終わらせたイベントを返す（開催中でなければ None）。"""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM manual_event WHERE guild_id=? AND ended=0 AND ends>?", (self._g(), now)
            ).fetchone()
            if row is None:
                return None
            self._conn.execute("UPDATE manual_event SET ended=1, ends=? WHERE guild_id=?", (now, self._g()))
            return dict(row)

    def manual_event_finish_due(self, now: float) -> Optional[dict]:
        """時間が来た特別イベントを終了にして返す（終了の告知を1回だけ出すため）。無ければ None。"""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM manual_event WHERE guild_id=? AND ended=0 AND ends<=?", (self._g(), now)
            ).fetchone()
            if row is None:
                return None
            self._conn.execute("UPDATE manual_event SET ended=1 WHERE guild_id=?", (self._g(),))
            return dict(row)
