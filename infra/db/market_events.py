"""市場イベント（ボラティリティ予告）の記録と、予告の抽選。

市場は全サーバー共通なので、イベントもサーバーに属さない（表は infra/db/schema/tables.py の market_events）。数字は domain/market_events.py。
"""

import random
from typing import cast

from domain import market_events as ev_mod
from domain.ports.contracts import MarketEventPlan, MarketEventRow
from domain.ports.repos import IMarketEventRepo
from infra.db.base import EcoDb, ScopedRepo


class MarketEventRepo(ScopedRepo, IMarketEventRepo):
    """ボラティリティ予告の抽選・取り出し。"""

    def __init__(self, db: EcoDb, guild_id: int = 0, strict: bool = False) -> None:
        super().__init__(db, guild_id, strict)

    def market_events(self, now: float) -> list[MarketEventRow]:
        """終わっていないイベントと、直近に終わったもの（古い順）。"""
        with self._lock:
            return self._all(
                "SELECT * FROM market_events WHERE end_ts>=? ORDER BY start_ts", (now - ev_mod.COOLDOWN_MIN * 60,)
            )

    def vol_multipliers(self, now: float) -> dict[str, float]:
        """いま発生中のイベントによる、銘柄ごとの値動きの倍率 {銘柄: 倍率}（なければ空）。"""
        events = self.market_events(now)
        out = {}
        for ev in events:
            m = ev_mod.multiplier(events, ev["symbol"], now)
            if m > 1.0:
                out[ev["symbol"]] = m
        return out

    def event_step(
        self, now: float, rng: random.Random | None = None, force: MarketEventPlan | None = None
    ) -> MarketEventRow | None:
        """1分ごとに呼ぶ。終わっていないイベントも、終わって COOLDOWN_MIN 分以内のものもなければ、確率で新しい予告を出す。出したらその行を返す。"""
        rng = rng or random.SystemRandom()
        with self._db.tx():
            self._conn.execute("DELETE FROM market_events WHERE end_ts<?", (now - ev_mod.KEEP_DAYS * 86400,))
            recent = self._one("SELECT 1 FROM market_events WHERE end_ts>=? LIMIT 1", (now - ev_mod.COOLDOWN_MIN * 60,))
            if force is None and (recent or rng.random() >= ev_mod.ANNOUNCE_PROB):
                return None
            plan = force or ev_mod.plan_event(now, rng)
            cur = self._conn.execute(
                "INSERT INTO market_events (symbol, announce_ts, start_ts, end_ts, factor) VALUES (?, ?, ?, ?, ?)",
                (plan["symbol"], plan["announce_ts"], plan["start_ts"], plan["end_ts"], plan["factor"]),
            )
            return cast(MarketEventRow, {**plan, "id": cur.lastrowid})
