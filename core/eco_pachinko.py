"""パチンコの1回転ごとの記録（Economy に混ぜ込む部品）。設計した当たり率と、実際の結果を比べて、数字を調整するため。

表の定義は core/economy.py の _init_schema にある。記録は統計のためだけで、ゲームの結果には影響しない。
"""

import time
from typing import Optional


class PachinkoMixin:
    """パチンコの回転の記録と、集計。"""

    def pachinko_record(self, user_id: int, omen: str, tier: str, multiplier: int, bet: int, now: Optional[float] = None) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO pachinko_log (guild_id, user_id, ts, omen, tier, multiplier, bet) VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (self._g(), user_id, now if now is not None else time.time(), omen, tier, multiplier, bet))

    def pachinko_stats(self, guild_id: Optional[int] = None, since: Optional[float] = None) -> dict:
        """予告の色・リーチの格ごとの（回転数, 当たり数）と、全体の（回転数, 当たり数, 賭け金の合計, 払い戻しの合計）。

        guild_id が None なら全サーバーの合計。since（epoch秒）を渡すと、それ以降だけ。
        """
        where, args = "WHERE (? IS NULL OR guild_id=?) AND ts>=?", (guild_id, guild_id, since or 0)
        with self._lock:
            def group(col):
                return {r[0]: (r[1], r[2]) for r in self._conn.execute(
                    f"SELECT {col}, COUNT(*), SUM(multiplier>0) FROM pachinko_log {where} GROUP BY {col}", args)}
            total = self._conn.execute(f"SELECT COUNT(*), COALESCE(SUM(multiplier>0), 0), COALESCE(SUM(bet), 0), COALESCE(SUM(bet*multiplier), 0) FROM pachinko_log {where}", args).fetchone()
            return {"omen": group("omen"), "tier": group("tier"), "total": tuple(total)}
