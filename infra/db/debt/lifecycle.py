"""借金のライフサイクル: 利息・後始末・収入からの天引き・非収入の全額充当。"""

from domain import teiai

from .base import DebtRepo as Base


class DebtRepo(Base):
    # 利息・後始末・天引き（IDebtLifecycleHook / IDebtRepayHook）

    def accrue_debt_interest_for(self, guild_id: int, user_id: int, now: float) -> int:
        """帝愛の利息（トイチ）を、借金に加える。加えた額を返す。呼び出し側のトランザクションの中で使う。"""
        row = self._one("SELECT debt, debt_interest_at FROM users WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        if row is None:
            return 0
        debt, since = row["debt"], row["debt_interest_at"]
        if debt <= 0:
            if since:
                self._conn.execute(
                    "UPDATE users SET debt_interest_at=0 WHERE guild_id=? AND user_id=?", (guild_id, user_id)
                )
            return 0
        if not since:
            self._conn.execute(
                "UPDATE users SET debt_interest_at=? WHERE guild_id=? AND user_id=?", (now, guild_id, user_id)
            )
            return 0
        days = int(max(0.0, now - since) // 86400)
        if days <= 0:
            return 0
        counted = min(days, teiai.DEBT_INTEREST_MAX_DAYS)
        add = max(0, min(int(debt * teiai.DEBT_RATE_PER_DAY * counted), teiai.DEBT_INTEREST_CEILING - debt))
        new_since = now if days > counted else since + days * 86400.0
        self._conn.execute(
            "UPDATE users SET debt=debt+?, debt_interest_at=? WHERE guild_id=? AND user_id=?",
            (add, new_since, guild_id, user_id),
        )
        if add > 0:
            balance = self._account.raw_balance(guild_id, user_id)
            self._account.ledger(
                guild_id,
                user_id,
                0,
                balance,
                f"帝愛の利息（トイチ・日利{teiai.DEBT_RATE_PER_DAY:.0%}・{counted}日分）+{add:,}",
            )
            self._notices.note_for(guild_id, user_id, "debt_interest", add=add, rate=teiai.DEBT_RATE_PER_DAY)
        return add

    def accrue_interest(self, guild_id: int, user_id: int, now: float | None = None) -> int:
        return self.accrue_debt_interest_for(guild_id, user_id, self._now() if now is None else now)

    def after_debt_change(self, guild_id: int, user_id: int) -> None:
        """借金が増減したあとの後始末。破産猶予の出入りを直し、破産者が完済したら、そこから BLACKLIST_DAYS 日が始まる。"""
        self._bank.grace_update_for(guild_id, user_id, self._now())
        if self._account.debt(guild_id, user_id) > 0:
            return
        self._conn.execute(
            "UPDATE users SET blacklist_until=? WHERE guild_id=? AND user_id=? AND bankrupt_at IS NOT NULL "
            "AND blacklist_until IS NULL",
            (self._now() + teiai.BLACKLIST_DAYS * 86400, guild_id, user_id),
        )

    def withhold_income(self, guild_id: int, user_id: int, income: int) -> int:
        """収入 income の REPAY_RATE を、借金の返済へ天引きする。先に帝愛、残りを延滞中の貸付へ。天引きした合計を返す。"""
        amount = teiai.withhold(income)
        if amount <= 0:
            return 0
        pay = min(self._account.debt(guild_id, user_id), amount, self._account.raw_balance(guild_id, user_id))
        if pay > 0:
            self._account.pay_debt_from_balance(guild_id, user_id, pay)
            self._account.ledger(
                guild_id, user_id, -pay, self._account.raw_balance(guild_id, user_id), "帝愛への返済（収入から天引き）"
            )
            self.after_debt_change(guild_id, user_id)
        return pay + self._loans.collect_overdue_for(guild_id, user_id, amount - pay)

    def repay_all_of_for(self, guild_id: int, user_id: int, amount: int, label: str) -> int:
        """入ってきた amount（元本など、収入ではないお金）を、借金があれば全額、返済に充てる。返済した合計を返す。"""
        pay = min(self._account.debt(guild_id, user_id), amount, self._account.raw_balance(guild_id, user_id))
        if pay > 0:
            self._account.pay_debt_from_balance(guild_id, user_id, pay)
            self._account.ledger(guild_id, user_id, -pay, self._account.raw_balance(guild_id, user_id), label)
            self.after_debt_change(guild_id, user_id)
        return pay + self._loans.collect_overdue_for(
            guild_id, user_id, min(amount - pay, self._account.raw_balance(guild_id, user_id))
        )
