"""自分からの借金返済。"""

from typing import Optional

from domain.errors import Outcome, fail

from .ops import DebtRepo as Base


class DebtRepo(Base):
    def repay_debt(self, user_id: int, amount: Optional[int] = None) -> Outcome[int]:
        """手元の現金から、帝愛の借金を返す（amount が None なら、返せる限り全額）。(成功か, 理由か返した額)。"""
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            debt, balance = self._account.debt(gid, user_id), self._account.raw_balance(gid, user_id)
            if debt <= 0:
                return False, fail("repay_no_debt")
            pay = min(debt, balance) if amount is None else amount
            if pay <= 0:
                return False, fail("repay_no_cash", balance=balance)
            if pay > debt:
                return False, fail("repay_exceeds_debt", debt=debt)
            if pay > balance:
                return False, fail("insufficient_balance", balance=balance)
            self._conn.execute(
                "UPDATE users SET balance=balance-?, debt=debt-? WHERE guild_id=? AND user_id=?",
                (pay, pay, gid, user_id),
            )
            self._account.ledger(gid, user_id, -pay, self._account.raw_balance(gid, user_id), "帝愛への返済")
            self.after_debt_change(gid, user_id)
            return True, pay
