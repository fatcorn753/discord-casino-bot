"""パチンコの記録（Economy に混ぜ込む部品）。設計した数字と、実際の結果を比べて、数字を調整するため。

- pachinko_spins … 1回転ごと（予告の色・リーチの格・当たったか）
- pachinko_sets  … 1セットごと（賭け金・打った玉・スタート入賞の数・大当たりの数・連チャンの数・払い戻しの倍数）
表の定義は core/economy.py の _init_schema にある。記録は統計のためだけで、ゲームの結果には影響しない。
"""

import time
from typing import Optional


class PachinkoMixin:
    """パチンコの1セットの記録と、集計。"""

    def pachinko_record_set(self, user_id: int, bet: int, balls: int, result, now: Optional[float] = None) -> None:
        """1セット（SetResult）を記録する。bet はそのセットの賭け金。"""
        ts = now if now is not None else time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO pachinko_sets (guild_id, user_id, ts, bet, balls, starts, hits, chains, chain_max, mult) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self._g(), user_id, ts, bet, balls, result.starts, result.hits, result.chains, result.max_chain, result.mult))
            self._conn.executemany("INSERT INTO pachinko_spins (guild_id, user_id, ts, omen, tier, hit) VALUES (?, ?, ?, ?, ?, ?)",
                                   [(self._g(), user_id, ts, s.omen, s.tier, int(s.hit)) for s in result.spins])

    def pachinko_stats(self, guild_id: Optional[int] = None, since: Optional[float] = None) -> dict:
        """予告の色・リーチの格ごとの（回転数, 当たり数）と、セット全体の集計。

        "sets" は (セット数, 当たったセット数, 賭け金の合計, 払い戻しの合計, 打った玉の合計, スタート入賞の合計, 連チャンの最大)。
        guild_id が None なら全サーバーの合計。since（epoch秒）を渡すと、それ以降だけ。
        """
        args = (guild_id, guild_id, since or 0)
        where = "WHERE (? IS NULL OR guild_id=?) AND ts>=?"
        with self._lock:
            def group(col):
                return {r[0]: (r[1], r[2]) for r in self._conn.execute(f"SELECT {col}, COUNT(*), SUM(hit) FROM pachinko_spins {where} GROUP BY {col}", args)}
            sets = self._conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM(mult>0), 0), COALESCE(SUM(bet), 0), COALESCE(SUM(bet*mult), 0), COALESCE(SUM(balls), 0), COALESCE(SUM(starts), 0), COALESCE(MAX(chain_max), 0) "
                f"FROM pachinko_sets {where}", args).fetchone()
            spins = self._conn.execute(f"SELECT COUNT(*), COALESCE(SUM(hit), 0) FROM pachinko_spins {where}", args).fetchone()
            return {"omen": group("omen"), "tier": group("tier"), "spins": tuple(spins), "sets": tuple(sets)}
