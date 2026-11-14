"""抽選の記録（Economy に混ぜ込む部品）。参加費の預かり・締切の当選／返金。

表の定義は core/economy.py の _init_schema にある。
"""

import random
from typing import Optional

from infra.db.base import EcoBase


class RaffleMixin(EcoBase):
    """抽選の参加・締切。"""

    # ------------------------------------------------------------------ #
    # 抽選（参加費を預かり、締切に1人へ全額渡す）                        #
    # ------------------------------------------------------------------ #
    def _raffle_open_row(self):
        return self._conn.execute(
            "SELECT * FROM raffle WHERE guild_id=? AND status='open' ORDER BY id DESC LIMIT 1", (self._g(),)
        ).fetchone()

    def _raffle_view(self, row) -> Optional[dict]:
        if row is None:
            return None
        d = dict(row)
        d["entries"] = [
            r["user_id"]
            for r in self._conn.execute(
                "SELECT user_id FROM raffle_entries WHERE raffle_id=? ORDER BY rowid", (row["id"],)
            )
        ]
        return d

    def raffle_open(self, host_id: int, fee: int, closes: float, channel_id: int) -> tuple:
        """抽選を始める。(成功か, 理由) を返す。募集中のものがあれば始められない。"""
        if fee < 1:
            return False, "参加費は1ペリカ以上にしてください。"
        with self._lock, self._conn:
            if self._raffle_open_row() is not None:
                return False, "すでに募集中の抽選があります。"
            self._conn.execute(
                "INSERT INTO raffle (guild_id, host_id, fee, closes, channel_id) VALUES (?, ?, ?, ?, ?)",
                (self._g(), host_id, fee, closes, channel_id),
            )
            return True, ""

    def raffle_get(self) -> Optional[dict]:
        """募集中の抽選（参加者つき）。無ければ None。"""
        with self._lock:
            return self._raffle_view(self._raffle_open_row())

    def raffle_set_message(self, raffle_id: int, message_id: int, channel_id: Optional[int] = None) -> None:
        """募集の投稿を記録する。再掲示で別のチャンネルに移したときは channel_id も渡す。"""
        with self._lock, self._conn:
            self._conn.execute("UPDATE raffle SET message_id=? WHERE id=?", (message_id, raffle_id))
            if channel_id is not None:
                self._conn.execute("UPDATE raffle SET channel_id=? WHERE id=?", (channel_id, raffle_id))

    def raffle_join(self, user_id: int, now: float) -> tuple:
        """参加する。参加費を先に引く。(成功か, 理由) を返す。"""
        with self._lock, self._conn:
            row = self._raffle_open_row()
            if row is None or now >= row["closes"]:
                return False, "募集は終わりました。"
            self._ensure(user_id)
            if self._conn.execute(
                "SELECT 1 FROM raffle_entries WHERE raffle_id=? AND user_id=?", (row["id"], user_id)
            ).fetchone():
                return False, "すでに参加しています。"
            if self._debt(user_id) > 0:
                return False, f"借金が {self._debt(user_id):,}ペリカ 残っているので、参加できません。"
            if self._balance(user_id) < row["fee"]:
                return (
                    False,
                    f"残高が足りません（参加費 {row['fee']:,}ペリカ、いまの残高 {self._balance(user_id):,}ペリカ）。",
                )
            self._apply(user_id, -row["fee"], "抽選参加費")
            self._conn.execute("INSERT INTO raffle_entries (raffle_id, user_id) VALUES (?, ?)", (row["id"], user_id))
            return True, ""

    def raffle_close(self, rng=None, cancel: bool = False) -> Optional[dict]:
        """募集中の抽選を締める。参加者が2人以上なら1人を選んで集めた参加費の全額を渡し、足りない・中止なら全員に返金する。

        返すもの: 抽選の記録 + winner（無ければ None）+ prize（渡した額）+ refunded（返金したか）。募集中が無ければ None。
        """
        rng = rng or random
        with self._lock, self._conn:
            raffle = self._raffle_view(self._raffle_open_row())
            if raffle is None:
                return None
            entries = raffle["entries"]
            self._conn.execute(
                "UPDATE raffle SET status=? WHERE id=?", ("cancelled" if cancel else "done", raffle["id"])
            )
            raffle.update(winner=None, prize=0, refunded=False)
            if cancel or len(entries) < 2:
                for uid in entries:
                    self._apply(uid, raffle["fee"], "返金:抽選")
                raffle["refunded"] = True
            else:
                raffle["winner"] = rng.choice(entries)
                raffle["prize"] = raffle["fee"] * len(entries)  # 手数料なし。集めた参加費の全額
                self._apply(raffle["winner"], raffle["prize"], "抽選当選", income=raffle["prize"])
            return raffle
