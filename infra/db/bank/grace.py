"""破産猶予（借金が一定額を超えた人の猶予期間の管理と時間切れの強制破産）。"""

from typing import Any, Optional

from domain import teiai
from domain.ports.contracts import BankruptResult

from .house import BankRepo as Base


class BankRepo(Base):
    def grace_update_for(self, guild_id: int, user_id: int, now: float) -> None:
        """借金の額に合わせて、破産猶予に入れる／外す。入るときに一言を残す。"""
        row = self._one("SELECT debt, grace_until FROM users WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        if row is None:
            return
        if row["debt"] >= teiai.GRACE_DEBT and row["grace_until"] is None:
            until = now + teiai.GRACE_HOURS * 3600
            self._conn.execute(
                "UPDATE users SET grace_until=? WHERE guild_id=? AND user_id=?", (until, guild_id, user_id)
            )
            self._grace_enter_hook.on_grace_enter(guild_id, user_id, now)
            self._notices.note_for(guild_id, user_id, "grace_entered", grace_debt=teiai.GRACE_DEBT, until=until)
        elif row["debt"] < teiai.GRACE_DEBT and row["grace_until"] is not None:
            self._conn.execute("UPDATE users SET grace_until=NULL WHERE guild_id=? AND user_id=?", (guild_id, user_id))
            self._notices.note_for(guild_id, user_id, "grace_exited")

    def grace_until_for(self, guild_id: int, user_id: int) -> Optional[float]:
        with self._lock:
            return self._scalar("SELECT grace_until FROM users WHERE guild_id=? AND user_id=?", (guild_id, user_id))

    def grace_until(self, user_id: int) -> Optional[float]:
        return self.grace_until_for(self._g(), user_id)

    def grace_list(self) -> list[Any]:
        """破産猶予中の人 [(ユーザーID, 借金, 期限)]（期限の早い順）。"""
        with self._lock:
            return [
                (r["user_id"], r["debt"], r["grace_until"])
                for r in self._all(
                    "SELECT user_id, debt, grace_until FROM users WHERE guild_id=? AND grace_until IS NOT NULL "
                    "ORDER BY grace_until",
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
                "SELECT user_id FROM users WHERE guild_id=? AND grace_until IS NOT NULL AND grace_until<=? ORDER BY grace_until",
                (gid, now),
            ):
                if self._account.debt(gid, r["user_id"]) < teiai.GRACE_DEBT:  # 取りこぼしの保険（外し忘れ）
                    self.grace_update_for(gid, r["user_id"], now)
                    continue
                ok, result = self._bankrupt_executor.bankrupt_for(gid, r["user_id"], forced=True)
                if ok:
                    done.append((r["user_id"], result))
        return done
