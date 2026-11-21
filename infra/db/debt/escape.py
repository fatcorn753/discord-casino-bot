"""海外逃亡: 成否の抽選と、失敗時の罰金・成功時の自己破産。"""

import random
from typing import Any, Optional, cast

from domain import teiai
from domain.errors import fail

from .repay import DebtRepo as Base


class DebtRepo(Base):
    def escape_left(self, user_id: int) -> int:
        """海外逃亡を、あと何回試せるか。読み取り専用。"""
        with self._lock:
            v = self._scalar("SELECT escape_tries FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id))
        return max(0, teiai.ESCAPE_MAX_TRIES - (v if v is not None else 0))

    def escape_attempt(self, user_id: int, now: float, rng: Optional[random.Random] = None) -> tuple[Any, ...]:
        """海外逃亡。(成功か, 理由か結果の辞書) を返す。"""
        rng = rng or cast("random.Random", random)
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            if self.status_for(gid, user_id, now) == teiai.BANKRUPT:
                return False, fail("escape_bankrupt_blocked")
            debt = self._account.debt(gid, user_id)
            if debt < teiai.ESCAPE_MIN_DEBT:
                return False, fail("escape_min_debt", min_debt=teiai.ESCAPE_MIN_DEBT)
            row = self._one("SELECT escape_at, escape_tries FROM users WHERE guild_id=? AND user_id=?", (gid, user_id))
            assert row is not None  # 直前に ensure 済み
            if row["escape_tries"] >= teiai.ESCAPE_MAX_TRIES:
                return False, fail("escape_max_tries", max_tries=teiai.ESCAPE_MAX_TRIES)
            ready = (row["escape_at"] or 0) + teiai.ESCAPE_COOLDOWN_HOURS * 3600
            if now < ready:
                return False, fail("escape_cooldown", ready=ready)
            self._conn.execute(
                "UPDATE users SET escape_at=?, escape_tries=escape_tries+1 WHERE guild_id=? AND user_id=?",
                (now, gid, user_id),
            )
            if rng.random() < teiai.ESCAPE_CHANCE:
                ok, result = self.bankrupt_for(gid, user_id, escape=True)
                if not ok:
                    return False, result
                return True, {
                    "escaped": True,
                    "fine": 0,
                    "debt": self._account.debt(gid, user_id),
                    "bankrupt": result,
                }
            fine = max(teiai.ESCAPE_FINE_MIN, int(debt * teiai.ESCAPE_FINE_RATE))
            self._conn.execute("UPDATE users SET debt = debt + ? WHERE guild_id=? AND user_id=?", (fine, gid, user_id))
            self._account.ledger(
                gid,
                user_id,
                0,
                self._account.raw_balance(gid, user_id),
                f"海外逃亡に失敗（罰金 {fine:,}ペリカが借金に加わる）",
            )
            self.after_debt_change(gid, user_id)
            return True, {"escaped": False, "fine": fine, "debt": self._account.debt(gid, user_id), "bankrupt": None}
