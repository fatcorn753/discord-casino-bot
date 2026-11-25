"""自己破産: 全財産の換金・差し押さえと、帝愛 → 貸付の順の配当。"""

from domain import teiai
from domain.errors import Outcome, fail
from domain.ports.contracts import BankruptResult

from .base import _take_cash
from .escape import DebtRepo as Base


class DebtRepo(Base):
    def _liquidate_everything_for(self, guild_id: int, user_id: int, now: float) -> int:
        self._liquidation_step(guild_id, user_id, now)
        return _take_cash(
            self._account,
            guild_id,
            user_id,
            self._account.raw_balance(guild_id, user_id),
            "破産:現金の差し押さえ",
        )

    def _seize_valuables_for(self, guild_id: int, user_id: int, now: float) -> int:
        return self._seizure_value_step(guild_id, user_id, now)

    def bankrupt_for(
        self, guild_id: int, user_id: int, forced: bool = False, escape: bool = False
    ) -> Outcome[BankruptResult]:
        """自己破産。全財産とショップの全品を差し押さえて換金し、帝愛 → 貸付の順に配当する。"""
        with self._db.tx():
            self._account.ensure(guild_id, user_id)
            if self.status_for(guild_id, user_id, self._now()) == teiai.BANKRUPT:
                return False, fail("bankrupt_already")
            loans = self._loans.active_loans_for(guild_id, user_id)
            if self._account.debt(guild_id, user_id) <= 0 and not loans:
                return False, fail("bankrupt_no_debt")
            debt_before = self._account.debt(guild_id, user_id)
            now = self._now()
            self._bankrupt_prep_hook(guild_id, user_id, now)
            in_grace = self._bank.grace_until_for(guild_id, user_id) is not None
            pool = self._liquidate_everything_for(guild_id, user_id, now)
            shop_value = self._seize_valuables_for(guild_id, user_id, now)
            pool += shop_value
            total_pool = pool
            to_senior = min(pool, self._account.debt(guild_id, user_id))
            if to_senior > 0:
                self._account.reduce_debt(guild_id, user_id, to_senior)
            pool -= to_senior
            junior_paid, forgiven = 0, 0
            for loan in loans:
                pay = min(pool, self._loans.loan_remaining(loan, now))
                if pay > 0:
                    self._account.ensure(guild_id, loan["lender_id"])
                    self._account.apply(
                        guild_id,
                        loan["lender_id"],
                        pay,
                        f"貸付の配当（借り手の自己破産）←{user_id}（#{loan['id']}）",
                    )
                    loan["paid"] += pay
                    self._loans.record_bankrupt_payment_for(loan["id"], loan["paid"])
                    pool -= pay
                    junior_paid += pay
                left = self._loans.loan_remaining(loan, now)
                forgiven += left
                self._loans.finalize_bankrupt_loan_for(loan["id"], left <= 0)
            if pool > 0:
                self._account.apply(guild_id, user_id, pool, "破産の整理のあまり")
            debt_now = self._account.debt(guild_id, user_id)
            compressed = max(0, debt_now - (0 if escape else teiai.BANKRUPT_FLOOR))
            final_debt = max(0, debt_now - compressed)
            self._account.set_debt(guild_id, user_id, final_debt, final_debt, 0.0)
            self._conn.execute(
                "INSERT INTO teiai_bankruptcies (guild_id, user_id, bankrupt_at, blacklist_until) "
                "VALUES (?, ?, ?, NULL) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET bankrupt_at=excluded.bankrupt_at, blacklist_until=NULL",
                (guild_id, user_id, now),
            )
            self.after_debt_change(guild_id, user_id)
            seed = 0  # 破産・逃亡直後の自動種銭付与は行わない（B-2）
            self._bank.grace_clear_for(guild_id, user_id)
            payable = forced or in_grace
            cds_paid = self._bank.cds_settle_default_for(guild_id, user_id, debt_before, now) if payable else 0
            if not payable:
                self._bank.cds_void_for(guild_id, user_id)
            result: BankruptResult = {
                "forced": forced,
                "cds_paid": cds_paid,
                "seized": total_pool,
                "shop_value": shop_value,
                "senior_paid": to_senior,
                "junior_paid": junior_paid,
                "forgiven": forgiven,
                "compressed": compressed,
                "debt": self._account.debt(guild_id, user_id),
                "seed": seed,
            }
            return True, result

    def bankrupt(self, user_id: int, forced: bool = False, escape: bool = False) -> Outcome[BankruptResult]:
        return self.bankrupt_for(self._g(), user_id, forced=forced, escape=escape)
