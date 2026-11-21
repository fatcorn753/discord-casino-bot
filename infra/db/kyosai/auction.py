"""帝愛の持ち口の競り（#93。値段は入札で決まる）。"""

from typing import Any, Optional

from domain import kyosai, p2p
from domain.errors import fail

from .daily import KyosaiRepo as Base


class KyosaiRepo(Base):
    def _open_auction_for(self, guild_id: int) -> Optional[dict[str, Any]]:
        return self._one(
            "SELECT * FROM kyosai_auctions WHERE guild_id=? AND status='open' ORDER BY id LIMIT 1", (guild_id,)
        )

    def kyosai_auction_tick(self, now: float) -> list[dict[str, Any]]:
        """締切を過ぎた競りを締め（落札なら口を渡し、代金を銀行の収入にする）、帝愛の持ち口があれば次の競りを出す。

        流動性ゲート（準備率が低い・破産猶予中の人がいる）の間も、競りは止めない（落札の代金で銀行に現金が入り、
        流動性を助けるため。止めるのは、プレイヤーどうしの譲渡だけ）。起きたこと [{event: sold/unsold/listed, ...}] を返す。
        """
        events: list[dict[str, Any]] = []
        with self._db.tx():
            gid = self._g()
            self.bank_sync(gid, now)
            row = self._open_auction_for(gid)
            if row is not None and row["ends"] <= now:
                if row["top_bidder"] is not None and row["top_bid"] > 0:
                    self._account.ensure(gid, row["top_bidder"])
                    self._move_units_for(gid, kyosai.HOUSE, row["top_bidder"], row["units"], now)
                    self._bank.bank_income_for(gid, row["top_bid"], now)
                    self._conn.execute("UPDATE kyosai_auctions SET status='sold' WHERE id=?", (row["id"],))
                    events.append({**dict(row), "event": "sold"})
                else:
                    self._conn.execute("UPDATE kyosai_auctions SET status='unsold' WHERE id=?", (row["id"],))
                    events.append({**dict(row), "event": "unsold"})
                row = None
            house = self.units_of_for(gid, kyosai.HOUSE)
            if row is None and house > 0:
                units = min(house, kyosai.AUCTION_LOT)
                ends = now + kyosai.AUCTION_HOURS * 3600
                cur = self._conn.execute(
                    "INSERT INTO kyosai_auctions (guild_id, units, ends) VALUES (?, ?, ?)", (gid, units, ends)
                )
                listed = {"id": cur.lastrowid, "units": units, "ends": ends, "top_bid": 0, "top_bidder": None}
                # 売れ残りの出し直しは告知しない（同じ口の出し直しで、告知チャンネルを埋めないように）
                if not any(e["event"] == "unsold" for e in events):
                    events.append({**listed, "event": "listed"})
        return events

    def kyosai_bid(self, user_id: int, amount: int, now: float) -> tuple[Any, ...]:
        """出ている競りに入札する（入札額は手元から預かり、前の最高値の人へ返す）。(成功か, Failure か競りの行)。"""
        with self._db.tx():
            gid = self._g()
            self.bank_sync(gid, now)
            row = self._open_auction_for(gid)
            if row is None or row["ends"] <= now:
                return False, fail("kyosai_no_auction")
            self._account.ensure(gid, user_id)
            debt = self._account.debt(gid, user_id)
            if debt > 0:
                return False, fail("debt_blocks", debt=debt, verb="入札できません")
            need = p2p.min_next_bid(kyosai.AUCTION_MIN_BID, row["top_bid"])
            if amount < need:
                return False, fail("kyosai_bid_low", need=need)
            own = row["top_bid"] if row["top_bidder"] == user_id else 0  # 自分の最高値を上げるときは、差額だけ要る
            bal = self._account.raw_balance(gid, user_id)
            if bal + own < amount:
                return False, fail("insufficient_balance_need", need=amount, balance=bal)
            if row["top_bidder"] is not None and row["top_bid"] > 0:
                self._account.ensure(gid, row["top_bidder"])
                self._account.apply(gid, row["top_bidder"], row["top_bid"], "共済:競りの入札の返金")
                if row["top_bidder"] != user_id:
                    self._note.note_for(gid, row["top_bidder"], "kyosai_outbid", units=row["units"], amount=amount)
            self._account.apply(gid, user_id, -amount, "共済:競りの入札（預かり）")
            ends = p2p.end_after_bid(row["ends"], now)
            self._conn.execute(
                "UPDATE kyosai_auctions SET top_bid=?, top_bidder=?, ends=? WHERE id=?",
                (amount, user_id, ends, row["id"]),
            )
            return True, {**dict(row), "top_bid": amount, "top_bidder": user_id, "ends": ends}
