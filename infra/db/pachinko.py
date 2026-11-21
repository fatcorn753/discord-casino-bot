"""パチンコの記録。設計した数字と、実際の結果を比べて、数字を調整するため。

- pachinko_spins … 1回転ごと（予告の色・リーチの格・当たったか）
- pachinko_sets  … 1セットごと（賭け金・打った玉・スタート入賞の数・大当たりの数・連チャンの数・払い戻しの倍数）
表の定義は infra/db/schema/tables.py にある。記録は統計のためだけで、ゲームの結果には影響しない。
"""


from domain.games.pachinko import SetResult
from domain.ports.contracts import PachinkoStats
from domain.ports.repos import IPachinkoRepo
from infra.db.base import EcoDb, ScopedRepo


class PachinkoRepo(ScopedRepo, IPachinkoRepo):
    """パチンコの1セットの記録と、集計。"""

    def __init__(self, db: EcoDb, guild_id: int = 0, strict: bool = False) -> None:
        super().__init__(db, guild_id, strict)

    def pachinko_record_set(
        self, user_id: int, bet: int, balls: int, result: SetResult, now: float | None = None
    ) -> None:
        """1セット（SetResult）を記録する。bet はそのセットの賭け金。"""
        ts = now if now is not None else self._now()
        with self._db.tx():
            gid = self._g()
            self._conn.execute(
                "INSERT INTO pachinko_sets (guild_id, user_id, ts, bet, balls, starts, hits, chains, chain_max, mult) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    gid,
                    user_id,
                    ts,
                    bet,
                    balls,
                    result.starts,
                    result.hits,
                    result.chains,
                    result.max_chain,
                    result.mult,
                ),
            )
            self._conn.executemany(
                "INSERT INTO pachinko_spins (guild_id, user_id, ts, omen, tier, hit) VALUES (?, ?, ?, ?, ?, ?)",
                [(gid, user_id, ts, s.omen, s.tier, int(s.hit)) for s in result.spins],
            )

    def pachinko_stats(self, guild_id: int | None = None, since: float | None = None) -> PachinkoStats:
        """予告の色・リーチの格ごとの（回転数, 当たり数）と、セット全体の集計。"""
        args = (guild_id, guild_id, since or 0)
        where = "WHERE (? IS NULL OR guild_id=?) AND ts>=?"
        with self._lock:

            def group(col: str) -> dict[str, tuple[int, int]]:
                return {
                    r["k"]: (r["n"], r["h"])
                    for r in self._all(
                        f"SELECT {col} AS k, COUNT(*) AS n, SUM(hit) AS h FROM pachinko_spins {where} GROUP BY {col}",
                        args,
                    )
                }

            sets = self._conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM(mult>0), 0), COALESCE(SUM(bet), 0), COALESCE(SUM(bet*mult), 0), COALESCE(SUM(balls), 0), COALESCE(SUM(starts), 0), COALESCE(MAX(chain_max), 0) "
                f"FROM pachinko_sets {where}",
                args,
            ).fetchone()
            spins = self._conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM(hit), 0) FROM pachinko_spins {where}", args
            ).fetchone()
            return {"omen": group("omen"), "tier": group("tier"), "spins": tuple(spins), "sets": tuple(sets)}
