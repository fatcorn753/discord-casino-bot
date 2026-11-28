"""毎日の配当と休眠口座の回収。"""

from domain import kyosai
from domain.ports.contracts import KyosaiDaily

from .base import DAY
from .excess import KyosaiRepo as Base


class KyosaiRepo(Base):
    def kyosai_daily(self, now: float) -> KyosaiDaily | None:
        """1日に1回: 休眠の人の口を回収し、余剰から配当を配る。配ったときだけ {pool, paid, recovered} を返す。"""
        with self._db.tx():
            gid = self._g()
            self.bank_sync(gid, now)
            last = self._scalar("SELECT div_ts FROM kyosai_state WHERE guild_id=?", (gid,))
            if not last:
                self._conn.execute(
                    "INSERT INTO kyosai_state (guild_id, div_ts) VALUES (?, ?) "
                    "ON CONFLICT(guild_id) DO UPDATE SET div_ts=excluded.div_ts",
                    (gid, now),
                )
                return None
            if now - last < DAY:
                return None
            self._conn.execute("UPDATE kyosai_state SET div_ts=? WHERE guild_id=?", (now, gid))
            recovered = self._recover_dormant_for(gid, now)
            available, liabilities, _ = self._bank.bank_reserve_for(gid, now)
            holdings = self._holdings_for(gid)
            pool = kyosai.dividend_pool(available, liabilities)
            shares = kyosai.split(pool, holdings)
            paid = 0
            to_users = sum(s for uid, s in shares.items() if uid != kyosai.HOUSE)  # 帝愛の持ち口の分は、銀行に残す
            if to_users > 0:
                # 配当は余剰の分配で損失ではないため、サーモスタットに通さない（L < 0 のときに熱浴の現金が湧かないように）
                self._bank.bank_pay_for(gid, to_users, now, thermo=False)
                paid = 0
                payouts = [(uid, s) for uid, s in shares.items() if uid != kyosai.HOUSE and s > 0]
                for user_id, share in payouts:  # 帝愛の持ち口の分は、銀行に残す
                    self._account.ensure(gid, user_id)
                    self._account.apply(gid, user_id, share, "共済:配当", income=share, kind="kyosai_dividend")
                    paid += share
                self._conn.execute("UPDATE kyosai_state SET paid=paid+? WHERE guild_id=?", (paid, gid))
            result: KyosaiDaily = {"pool": pool, "paid": paid, "recovered": recovered}
            return result

    def _recover_dormant_for(self, guild_id: int, now: float) -> int:
        """DORMANT_DAYS 日、口座が動いていない人の口を回収する。

        本人が何もしなくても起きる動き（共済の配当・超過損失の負担、信託の利息）は数えない。
        口の売買や競りの入札のような、本人の操作は数える。
        """
        since = now - kyosai.DORMANT_DAYS * DAY
        recovered = 0
        for user_id, _units in self._holdings_for(guild_id, with_house=False):
            last = self._scalar(
                "SELECT MAX(ts) FROM ledger WHERE guild_id=? AND user_id=? "
                "AND kind NOT IN ('kyosai_dividend', 'trust_interest', 'kyosai_excess_charge')",
                (guild_id, user_id),
            )
            if last is None:
                row = self._one("SELECT created_at FROM accounts WHERE guild_id=? AND user_id=?", (guild_id, user_id))
                last = row["created_at"] if row and row["created_at"] else None
            if last is not None and last < since:
                recovered += self.recover_units_for(guild_id, user_id, now)
        return recovered
