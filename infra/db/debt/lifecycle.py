"""借金のライフサイクル: 利息・後始末・収入からの天引き・非収入の全額充当。"""

from domain import teiai

from .base import DebtRepo as Base


class DebtRepo(Base):
    # 利息・後始末・天引き（IDebtLifecycleHook / IDebtRepayHook）

    def _auto_seize_on_interest_for(
        self, guild_id: int, user_id: int, new_debt: int, new_since: float, now: float
    ) -> None:
        """利息加算で破産猶予ラインに達したとき、猶予入り前に流動資産を差し押さえて返済する（A-1）。"""
        seized = self._seize_assets_for(guild_id, user_id, new_debt, "強制回収:帝愛の利息超過", now=now)
        if seized > 0:
            cur = self._one(
                "SELECT debt, principal FROM teiai_debts WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            )
            if cur is not None:
                remaining = max(0, cur["debt"] - seized)
                new_principal = min(cur["principal"], remaining)
                self._account.set_debt(
                    guild_id, user_id, remaining, new_principal, 0.0 if remaining == 0 else new_since
                )
                self._account.ledger(
                    guild_id,
                    user_id,
                    0,
                    self._account.raw_balance(guild_id, user_id),
                    f"強制回収:帝愛の利息超過（{seized:,}ペリカ）",
                )
                self._notices.note_for(guild_id, user_id, "debt_auto_seized", seized=seized, debt=remaining)
        self.after_debt_change(guild_id, user_id, now=now)

    def accrue_debt_interest_for(self, guild_id: int, user_id: int, now: float) -> int:
        """帝愛の利息（トイチ）を、借金に加える。加えた額を返す。呼び出し側のトランザクションの中で使う。"""
        row = self._one(
            "SELECT d.debt, d.principal, d.interest_at, g.grace_until "
            "FROM teiai_debts d LEFT JOIN teiai_grace g ON g.guild_id=d.guild_id AND g.user_id=d.user_id "
            "WHERE d.guild_id=? AND d.user_id=?",
            (guild_id, user_id),
        )
        if row is None:
            return 0
        debt, since = row["debt"], row["interest_at"]
        if debt <= 0:
            self._account.set_debt(guild_id, user_id, 0)
            return 0
        principal = min(row["principal"], debt) if row["principal"] > 0 else debt
        if not since:
            self._account.set_debt(guild_id, user_id, debt, principal, now)
            return 0
        days = int(max(0.0, now - since) // 86400)
        if days <= 0:
            return 0
        cap = teiai.debt_interest_cap(principal)
        add = max(0, min(int(debt * teiai.DEBT_RATE_PER_DAY * days), cap - debt))
        new_since = since + days * 86400.0
        new_debt = debt + add
        # 差し押さえフック内で debt() が再帰的に呼ばれても days == 0 に収束するよう先に更新する
        self._account.set_debt(guild_id, user_id, new_debt, principal, new_since)
        if add <= 0:
            return 0
        balance = self._account.raw_balance(guild_id, user_id)
        self._account.ledger(
            guild_id,
            user_id,
            0,
            balance,
            f"帝愛の利息（トイチ・日利{teiai.DEBT_RATE_PER_DAY:.0%}・{days}日分）+{add:,}",
        )
        self._notices.note_for(guild_id, user_id, "debt_interest", add=add, rate=teiai.DEBT_RATE_PER_DAY)
        if new_debt >= teiai.GRACE_DEBT and row["grace_until"] is None:
            self._auto_seize_on_interest_for(guild_id, user_id, new_debt, new_since, now)
        return add

    def accrue_all_debt_interest(self, now: float) -> int:
        """このサーバーのすべての債務者に帝愛の利息（トイチ）を反映する。加えた利息の合計を返す。"""
        total = 0
        with self._db.tx():
            gid = self._g()
            uids = [
                r["user_id"]
                for r in self._all("SELECT user_id FROM teiai_debts WHERE guild_id=? AND debt>0 ORDER BY user_id", (gid,))
            ]
            for uid in uids:
                total += self.accrue_debt_interest_for(gid, uid, now)
        return total

    def accrue_interest(self, guild_id: int, user_id: int, now: float | None = None) -> int:
        return self.accrue_debt_interest_for(guild_id, user_id, self._now() if now is None else now)

    def after_debt_change(self, guild_id: int, user_id: int, now: float | None = None) -> None:
        """借金が増減したあとの後始末。破産猶予の出入りを直し、破産者が完済したら、そこから BLACKLIST_DAYS 日が始まる。"""
        ts = self._now() if now is None else now
        self._bank.grace_update_for(guild_id, user_id, ts)
        debt = self._scalar("SELECT debt FROM teiai_debts WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        if debt is not None and debt > 0:
            return
        self._conn.execute(
            "UPDATE teiai_bankruptcies SET blacklist_until=? WHERE guild_id=? AND user_id=? AND blacklist_until IS NULL",
            (ts + teiai.BLACKLIST_DAYS * 86400, guild_id, user_id),
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
