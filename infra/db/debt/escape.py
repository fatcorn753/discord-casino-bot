"""海外逃亡: 成否の抽選と、失敗時の罰金・成功時の自己破産。"""

import random
from typing import cast

from domain import teiai
from domain.errors import Failure, Outcome, fail
from domain.ports.contracts import EscapeResult

from .repay import DebtRepo as Base


class DebtRepo(Base):
    def escape_left(self, user_id: int) -> int:
        """海外逃亡を、あと何回試せるか。読み取り専用。"""
        with self._lock:
            v = self._scalar("SELECT tries FROM teiai_escapes WHERE guild_id=? AND user_id=?", (self._g(), user_id))
        return max(0, teiai.ESCAPE_MAX_TRIES - (v if v is not None else 0))

    def escape_attempt(self, user_id: int, now: float, rng: random.Random | None = None) -> Outcome[EscapeResult]:
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
            row = self._one(
                "SELECT last_attempt_at, tries FROM teiai_escapes WHERE guild_id=? AND user_id=?", (gid, user_id)
            )
            tries = int(row["tries"]) if row is not None else 0
            last_attempt_at = row["last_attempt_at"] if row is not None else None
            if tries >= teiai.ESCAPE_MAX_TRIES:
                return False, fail("escape_max_tries", max_tries=teiai.ESCAPE_MAX_TRIES)
            ready = (last_attempt_at or 0) + teiai.ESCAPE_COOLDOWN_HOURS * 3600
            if now < ready:
                return False, fail("escape_cooldown", ready=ready)
            self._conn.execute(
                "INSERT INTO teiai_escapes (guild_id, user_id, last_attempt_at, tries) VALUES (?, ?, ?, 1) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                "last_attempt_at=excluded.last_attempt_at, tries=teiai_escapes.tries+1",
                (gid, user_id, now),
            )
            if rng.random() < teiai.ESCAPE_CHANCE:
                ok, result = self.bankrupt_for(gid, user_id, escape=True)
                if not ok:
                    assert isinstance(result, Failure)
                    return False, result
                assert not isinstance(result, Failure)
                escaped: EscapeResult = {
                    "escaped": True,
                    "fine": 0,
                    "debt": self._account.debt(gid, user_id),
                    "bankrupt": result,
                }
                return True, escaped
            fine = max(teiai.ESCAPE_FINE_MIN, int(debt * teiai.ESCAPE_FINE_RATE))
            self._account.add_debt_principal(gid, user_id, fine)
            self._account.ledger(
                gid,
                user_id,
                0,
                self._account.raw_balance(gid, user_id),
                f"海外逃亡に失敗（罰金 {fine:,}ペリカが借金に加わる）",
            )
            self.after_debt_change(gid, user_id)
            failed: EscapeResult = {
                "escaped": False,
                "fine": fine,
                "debt": self._account.debt(gid, user_id),
                "bankrupt": None,
            }
            return True, failed
