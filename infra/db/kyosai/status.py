"""共済の状態の表示用まとめ（kyosai_status）。"""

from domain import kyosai
from domain.ports.contracts import KyosaiStatus

from .base import DAY
from .motions import KyosaiRepo as Base


class KyosaiRepo(Base):
    def kyosai_status(self, user_id: int, now: float) -> KyosaiStatus:
        with self._db.tx():
            gid = self._g()
            self.bank_sync(gid, now)
            holdings = self._holdings_for(gid)
            total = sum(u for _, u in holdings)
            row = self._one("SELECT paid, charged, div_ts FROM kyosai_state WHERE guild_id=?", (gid,))
            assert row is not None  # bank_sync で共済状態の行を作っている
            available, liabilities, theta = self._bank.bank_reserve_for(gid, now)
            motions = []
            for m in self._open_motions_for(gid):
                yes, no = self._tally_for(gid, m["id"])
                motions.append({**m, "yes": yes, "no": no})
            auction = self._open_auction_for(gid)
            return {
                "units": self.units_of_for(gid, user_id),
                "total": total,
                "holders": len([1 for uid, _ in holdings if uid != kyosai.HOUSE]),
                "house": self.units_of_for(gid, kyosai.HOUSE),
                "auction": auction,
                "paid": row["paid"],
                "charged": row["charged"],
                "next_dividend": (row["div_ts"] + DAY) if row["div_ts"] else None,
                "pool": kyosai.dividend_pool(available, liabilities),
                "theta": theta,
                "locked": self._gate_locked_for(gid, now),
                "premium": self._bank.trust_premium_for(gid),
                "circulating": self._circulating_for(gid),
                "motions": motions,
            }
