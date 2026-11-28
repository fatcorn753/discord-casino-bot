"""破産猶予（借金が一定額を超えた人の猶予期間の管理と時間切れの強制破産）。"""

from typing import Any

from domain import teiai
from domain.ports.contracts import BankruptResult

from .house import BankRepo as Base


class BankRepo(Base):
    def grace_update_for(self, guild_id: int, user_id: int, now: float) -> None:
        """借金の額に合わせて、破産猶予に入れる／外す。入るときは累積・時間減衰ペナルティに応じて猶予時間を決め、一言を残す。"""
        if self._one("SELECT 1 FROM accounts WHERE guild_id=? AND user_id=?", (guild_id, user_id)) is None:
            return
        debt_val = self._scalar("SELECT debt FROM teiai_debts WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        debt = int(debt_val) if debt_val is not None else 0
        row = self._one(
            "SELECT grace_until, penalty, penalty_at FROM teiai_grace WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        grace_until = row["grace_until"] if row is not None else None
        penalty = float(row["penalty"]) if row is not None else 0.0
        penalty_at = float(row["penalty_at"]) if row is not None else 0.0
        if debt >= teiai.GRACE_DEBT and grace_until is None:
            elapsed_days = max(0.0, now - penalty_at) / 86400.0 if penalty_at > 0 else 0.0
            decayed = teiai.decay_grace_penalty(penalty, elapsed_days)
            hours = teiai.effective_grace_hours(decayed)
            until = now + hours * 3600
            self._conn.execute(
                "INSERT INTO teiai_grace (guild_id, user_id, grace_until, penalty, penalty_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                "grace_until=excluded.grace_until, penalty=excluded.penalty, penalty_at=excluded.penalty_at",
                (guild_id, user_id, until, decayed + 1.0, now),
            )
            self._grace_enter_hook(guild_id, user_id, now)
            self._notices.note_for(guild_id, user_id, "grace_entered", grace_debt=teiai.GRACE_DEBT, until=until)
        elif debt < teiai.GRACE_DEBT and grace_until is not None:
            self._conn.execute(
                "UPDATE teiai_grace SET grace_until=NULL WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            )
            self._notices.note_for(guild_id, user_id, "grace_exited")

    def grace_clear_for(self, guild_id: int, user_id: int) -> None:
        """猶予を解除する（呼び出し側のトランザクション内で使う）。"""
        self._conn.execute(
            "UPDATE teiai_grace SET grace_until=NULL WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )

    def grace_until_for(self, guild_id: int, user_id: int) -> float | None:
        with self._lock:
            return self._scalar("SELECT grace_until FROM teiai_grace WHERE guild_id=? AND user_id=?", (guild_id, user_id))

    def grace_until(self, user_id: int) -> float | None:
        return self.grace_until_for(self._g(), user_id)

    def grace_list(self) -> list[Any]:
        """破産猶予中の人 [(ユーザーID, 借金, 期限)]（期限の早い順）。"""
        with self._lock:
            return [
                (r["user_id"], r["debt"], r["grace_until"])
                for r in self._all(
                    "SELECT g.user_id, COALESCE(d.debt, 0) AS debt, g.grace_until "
                    "FROM teiai_grace g LEFT JOIN teiai_debts d ON d.guild_id=g.guild_id AND d.user_id=g.user_id "
                    "WHERE g.guild_id=? AND g.grace_until IS NOT NULL ORDER BY g.grace_until",
                    (self._g(),),
                )
            ]

    def grace_expire(self, now: float) -> list[tuple[int, BankruptResult]]:
        """猶予の時間切れになった人を、強制破産させる。CDS の失効もここで済ます。[(ユーザーID, 破産の結果)] を返す。"""
        done = []
        with self._db.tx():
            gid = self._g()
            self._conn.execute(
                "UPDATE cds SET status='expired' WHERE guild_id=? AND status='active' AND expires<=?", (gid, now)
            )
            for r in self._all(
                "SELECT user_id FROM teiai_grace WHERE guild_id=? AND grace_until IS NOT NULL AND grace_until<=? "
                "ORDER BY grace_until",
                (gid, now),
            ):
                if self._account.debt(gid, r["user_id"]) < teiai.GRACE_DEBT:  # 取りこぼしの保険（外し忘れ）
                    self.grace_update_for(gid, r["user_id"], now)
                    continue
                ok, result = self._bankrupt_executor(gid, r["user_id"], True, False)
                if ok:
                    done.append((r["user_id"], result))
        return done
