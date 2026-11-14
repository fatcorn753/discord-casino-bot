"""市場イベント（ボラティリティ予告）の記録と、予告の抽選（Economy に混ぜ込む部品）。

市場は全サーバー共通なので、イベントもサーバーに属さない（表は core/schema.py の market_events）。数字は core/market_events.py。
"""

import random
import time
from typing import Optional

from domain import market_events as ev_mod
from infra.db.base import EcoBase


class MarketEventMixin(EcoBase):
    """ボラティリティ予告の抽選・取り出し。"""

    def market_events(self, now: Optional[float] = None) -> list:
        """終わっていないイベントと、直近に終わったもの（古い順）。"""
        now = time.time() if now is None else now
        with self._lock:
            return [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM market_events WHERE end_ts>=? ORDER BY start_ts", (now - ev_mod.COOLDOWN_MIN * 60,)
                )
            ]

    def vol_multipliers(self, now: Optional[float] = None) -> dict:
        """いま発生中のイベントによる、銘柄ごとの値動きの倍率 {銘柄: 倍率}（なければ空）。"""
        now = time.time() if now is None else now
        events = self.market_events(now)
        out = {}
        for ev in events:
            m = ev_mod.multiplier(events, ev["symbol"], now)
            if m > 1.0:
                out[ev["symbol"]] = m
        return out

    def event_step(self, now: float, rng=None, force: Optional[dict] = None) -> Optional[dict]:
        """1分ごとに呼ぶ。終わっていないイベントも、終わって COOLDOWN_MIN 分以内のものもなければ、確率で新しい予告を出す。出したらその行を返す。

        force を渡すと、抽選なしで、そのイベントを登録する（テスト・持ち主の手動用）。古い記録は消す。
        """
        rng = rng or random.SystemRandom()
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM market_events WHERE end_ts<?", (now - ev_mod.KEEP_DAYS * 86400,))
            recent = self._conn.execute(
                "SELECT 1 FROM market_events WHERE end_ts>=? LIMIT 1", (now - ev_mod.COOLDOWN_MIN * 60,)
            ).fetchone()
            if force is None and (recent or rng.random() >= ev_mod.ANNOUNCE_PROB):
                return None
            plan = force or ev_mod.plan_event(now, rng)
            cur = self._conn.execute(
                "INSERT INTO market_events (symbol, announce_ts, start_ts, end_ts, factor) VALUES (?, ?, ?, ?, ?)",
                (plan["symbol"], plan["announce_ts"], plan["start_ts"], plan["end_ts"], plan["factor"]),
            )
            return {**plan, "id": cur.lastrowid}
