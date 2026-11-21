"""抽選の記録。参加費の預かり・締切の当選／返金。

表の定義は infra/db/schema/tables.py にある。
"""

import random
from typing import Any, Optional, cast

from domain.errors import Outcome, fail
from domain.ports.repos import IRaffleRepo
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo


class RaffleRepo(ScopedRepo, IRaffleRepo):
    """抽選の参加・締切。"""

    def __init__(self, db: EcoDb, account: AccountCore, guild_id: int = 0, strict: bool = False) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account

    def _raffle_open_row(self, guild_id: int) -> Optional[dict[str, Any]]:
        return self._one(
            "SELECT * FROM raffle WHERE guild_id=? AND status='open' ORDER BY id DESC LIMIT 1", (guild_id,)
        )

    def _raffle_view(self, row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        d = row
        d["entries"] = [
            r["user_id"]
            for r in self._all("SELECT user_id FROM raffle_entries WHERE raffle_id=? ORDER BY rowid", (row["id"],))
        ]
        return d

    def raffle_open(self, host_id: int, fee: int, closes: float, channel_id: int) -> Outcome[str]:
        """抽選を始める。(成功か, 理由) を返す。募集中のものがあれば始められない。"""
        if fee < 1:
            return False, fail("raffle_min_fee")
        with self._db.tx():
            gid = self._g()
            if self._raffle_open_row(gid) is not None:
                return False, fail("raffle_already_open")
            self._conn.execute(
                "INSERT INTO raffle (guild_id, host_id, fee, closes, channel_id) VALUES (?, ?, ?, ?, ?)",
                (gid, host_id, fee, closes, channel_id),
            )
            return True, ""

    def raffle_get(self) -> Optional[dict[str, Any]]:
        """募集中の抽選（参加者つき）。無ければ None。"""
        with self._lock:
            return self._raffle_view(self._raffle_open_row(self._g()))

    def raffle_set_message(self, raffle_id: int, message_id: int, channel_id: Optional[int] = None) -> None:
        """募集の投稿を記録する。再掲示で別のチャンネルに移したときは channel_id も渡す。"""
        with self._db.tx():
            self._conn.execute("UPDATE raffle SET message_id=? WHERE id=?", (message_id, raffle_id))
            if channel_id is not None:
                self._conn.execute("UPDATE raffle SET channel_id=? WHERE id=?", (channel_id, raffle_id))

    def raffle_join(self, user_id: int, now: float) -> Outcome[str]:
        """参加する。参加費を先に引く。(成功か, 理由) を返す。"""
        with self._db.tx():
            gid = self._g()
            row = self._raffle_open_row(gid)
            if row is None or now >= row["closes"]:
                return False, fail("raffle_closed")
            self._account.ensure(gid, user_id)
            if self._one("SELECT 1 FROM raffle_entries WHERE raffle_id=? AND user_id=?", (row["id"], user_id)):
                return False, fail("raffle_already_joined")
            debt = self._account.debt(gid, user_id)
            if debt > 0:
                return False, fail("debt_blocks", debt=debt, verb="参加できません")
            bal = self._account.raw_balance(gid, user_id)
            if bal < row["fee"]:
                return False, fail("raffle_insufficient_balance", fee=row["fee"], balance=bal)
            self._account.apply(gid, user_id, -row["fee"], "抽選参加費")
            self._conn.execute("INSERT INTO raffle_entries (raffle_id, user_id) VALUES (?, ?)", (row["id"], user_id))
            return True, ""

    def raffle_close(self, rng: Optional[random.Random] = None, cancel: bool = False) -> Optional[dict[str, Any]]:
        """募集中の抽選を締める。参加者が2人以上なら1人を選んで集めた参加費の全額を渡し、足りない・中止なら全員に返金する。"""
        rng = rng or cast("random.Random", random)
        with self._db.tx():
            gid = self._g()
            raffle = self._raffle_view(self._raffle_open_row(gid))
            if raffle is None:
                return None
            entries = raffle["entries"]
            self._conn.execute(
                "UPDATE raffle SET status=? WHERE id=?", ("cancelled" if cancel else "done", raffle["id"])
            )
            raffle.update(winner=None, prize=0, refunded=False)
            if cancel or len(entries) < 2:
                for uid in entries:
                    self._account.apply(gid, uid, raffle["fee"], "返金:抽選")
                raffle["refunded"] = True
            else:
                raffle["winner"] = rng.choice(entries)
                raffle["prize"] = raffle["fee"] * len(entries)  # 手数料なし。集めた参加費の全額
                self._account.apply(gid, raffle["winner"], raffle["prize"], "抽選当選", income=raffle["prize"])
            return raffle
