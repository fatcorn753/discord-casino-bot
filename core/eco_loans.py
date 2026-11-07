"""貸し借りの記録（Economy に混ぜ込む部品）。ユーザー同士の貸し借り。

流れ: 提案（offer）→ 借りる人が承諾（active、お金が移る）→ 返済（repaid）／期限に自動回収／足りなければ延滞（overdue）。
延滞の残りは、借りた人に入ってくるお金から、借金の返済のあとで、古い順に自動で回収する。

表の定義は core/economy.py の _init_schema にある。数字と計算は core/loans.py。
"""

from typing import Optional

from core import loans as loans_mod


class LoanMixin:
    """貸し借りの提案・承諾・返済・回収。"""

    # ---- 内部 ---- #
    @staticmethod
    def _loan_row(row) -> Optional[dict]:
        return dict(row) if row is not None else None

    def _loan_owed(self, loan: dict, now: float) -> int:
        """いまの返済総額（利息つき）。承諾前は 0。"""
        if not loan["accepted_at"]:
            return 0
        return loans_mod.total_owed(loan["principal"], loan["rate_tenths"], loan["term_days"], loan["accepted_at"], now)

    def loan_remaining(self, loan: dict, now: float) -> int:
        return max(0, self._loan_owed(loan, now) - loan["paid"])

    def _loan_pay(self, loan: dict, amount: int) -> None:
        """借りた人 → 貸した人へ amount を動かす（借りた人の残高は、呼び出し側が確かめておく）。"""
        self._loan_guard = True  # 回収の連鎖（受け取った人の延滞の回収）を止める
        try:
            self._apply(loan["borrower_id"], -amount, f"貸付の返済→{loan['lender_id']}（#{loan['id']}）")
            self._ensure(loan["lender_id"])
            self._apply(loan["lender_id"], amount, f"貸付の回収←{loan['borrower_id']}（#{loan['id']}）")
        finally:
            self._loan_guard = False
        loan["paid"] += amount
        self._conn.execute("UPDATE loans SET paid=? WHERE id=?", (loan["paid"], loan["id"]))

    def _collect_overdue_for(self, user_id: int) -> None:
        """延滞中の貸付があれば、この人の残高から古い順に回収する（_apply から、入金のあとに呼ばれる）。"""
        rows = self._conn.execute(
            "SELECT * FROM loans WHERE guild_id=? AND borrower_id=? AND status='overdue' ORDER BY id",
            (self._g(), user_id),
        ).fetchall()
        for row in rows:
            loan = dict(row)
            take = min(self._balance(user_id), self.loan_remaining(loan, loan["due"]))
            if take > 0:
                self._loan_pay(loan, take)
            if self.loan_remaining(loan, loan["due"]) <= 0:
                self._conn.execute("UPDATE loans SET status='repaid' WHERE id=?", (loan["id"],))

    # ---- 提案・承諾・辞退・取り下げ ---- #
    def loan_offer(
        self,
        lender_id: int,
        borrower_id: int,
        principal: int,
        rate_tenths: int,
        term_days: int,
        channel_id: int,
        now: float,
    ) -> tuple:
        """貸す提案を作る。(成功か, 理由か貸付の番号) を返す。"""
        low = loans_mod.USER_MIN_PRINCIPAL
        if lender_id == borrower_id:
            return False, "自分には貸せません。"
        if principal < low:
            return False, f"金額は {low:,}ペリカ 以上にしてください。"
        if rate_tenths < 0:
            return False, "日利は 0 以上にしてください。"
        if not loans_mod.MIN_TERM_DAYS <= term_days <= loans_mod.MAX_TERM_DAYS:
            return False, f"期間は {loans_mod.MIN_TERM_DAYS}〜{loans_mod.MAX_TERM_DAYS}日 にしてください。"
        with self._lock, self._conn:
            self._ensure(lender_id)
            if self._balance(lender_id) < principal:
                return False, f"残高が足りません（いまの残高: {self._balance(lender_id):,}ペリカ）。"
            opened = self._conn.execute(
                "SELECT COUNT(*) FROM loans WHERE guild_id=? AND lender_id=? AND status='offer'", (self._g(), lender_id)
            ).fetchone()[0]
            active = self._conn.execute(
                "SELECT COUNT(*) FROM loans WHERE guild_id=? AND lender_id=? AND status IN ('active','overdue')",
                (self._g(), lender_id),
            ).fetchone()[0]
            if opened >= loans_mod.MAX_LENDER_OPEN:
                return False, f"承諾待ちの提案は {loans_mod.MAX_LENDER_OPEN} 件までです。"
            if active >= loans_mod.MAX_LENDER_ACTIVE:
                return False, f"同時に貸せるのは {loans_mod.MAX_LENDER_ACTIVE} 件までです。"
            cur = self._conn.execute(
                "INSERT INTO loans (guild_id, lender_id, borrower_id, principal, rate_tenths, term_days, status, created, offer_expires, channel_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 'offer', ?, ?, ?)",
                (
                    self._g(),
                    lender_id,
                    borrower_id,
                    principal,
                    rate_tenths,
                    term_days,
                    now,
                    now + loans_mod.OFFER_HOURS * 3600,
                    channel_id,
                ),
            )
            return True, cur.lastrowid

    def loan_set_message(self, loan_id: int, message_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE loans SET message_id=? WHERE id=?", (message_id, loan_id))

    def loan_get(self, loan_id: int) -> Optional[dict]:
        with self._lock:
            return self._loan_row(
                self._conn.execute("SELECT * FROM loans WHERE id=? AND guild_id=?", (loan_id, self._g())).fetchone()
            )

    def loan_accept(self, loan_id: int, user_id: int, now: float) -> tuple:
        """借りる人が承諾して、お金を受け取る。(成功か, 理由) を返す。"""
        with self._lock, self._conn:
            loan = self._loan_row(
                self._conn.execute("SELECT * FROM loans WHERE id=? AND guild_id=?", (loan_id, self._g())).fetchone()
            )
            if loan is None or loan["status"] != "offer":
                return False, "この提案はもう終わっています。"
            if user_id != loan["borrower_id"]:
                return False, "この提案を承諾できるのは、借りる本人だけです。"
            if now >= loan["offer_expires"]:
                return False, "承諾の期限が過ぎました。"
            self._ensure(user_id)
            if self._debt(user_id) > 0:
                return False, f"借金が {self._debt(user_id):,}ペリカ 残っているので、借りられません。"
            row = self._conn.execute(
                "SELECT COALESCE(SUM(status='overdue'), 0) AS late FROM loans WHERE guild_id=? AND borrower_id=? AND status IN ('active','overdue')",
                (self._g(), user_id),
            ).fetchone()
            if row["late"]:
                return False, "延滞中の貸付があるので、借りられません。先に返してください。"
            self._ensure(loan["lender_id"])
            if self._balance(loan["lender_id"]) < loan["principal"]:
                return False, "貸す側の残高が足りなくなったので、成立しませんでした。"
            self._apply(loan["lender_id"], -loan["principal"], f"貸付→{user_id}（#{loan_id}）")
            self._apply(user_id, loan["principal"], f"借入←{loan['lender_id']}（#{loan_id}）")
            self._conn.execute(
                "UPDATE loans SET status='active', accepted_at=?, due=? WHERE id=?",
                (now, now + loan["term_days"] * 86400, loan_id),
            )
            return True, ""

    def loan_decline(self, loan_id: int, user_id: int) -> tuple:
        """借りる人が断る。"""
        with self._lock, self._conn:
            loan = self._loan_row(
                self._conn.execute("SELECT * FROM loans WHERE id=? AND guild_id=?", (loan_id, self._g())).fetchone()
            )
            if loan is None or loan["status"] != "offer":
                return False, "この提案はもう終わっています。"
            if user_id != loan["borrower_id"]:
                return False, "断れるのは、借りる本人だけです。"
            self._conn.execute("UPDATE loans SET status='declined' WHERE id=?", (loan_id,))
            return True, ""

    def loan_cancel(self, loan_id: int, user_id: int) -> tuple:
        """貸す側が提案を取り下げる。"""
        with self._lock, self._conn:
            loan = self._loan_row(
                self._conn.execute("SELECT * FROM loans WHERE id=? AND guild_id=?", (loan_id, self._g())).fetchone()
            )
            if loan is None or loan["status"] != "offer":
                return False, "この提案はもう終わっています。"
            if user_id != loan["lender_id"]:
                return False, "取り下げられるのは、貸す側だけです。"
            self._conn.execute("UPDATE loans SET status='cancelled' WHERE id=?", (loan_id,))
            return True, ""

    # ---- 返済 ----
    def loan_repay(self, loan_id: int, user_id: int, amount: Optional[int], now: float) -> tuple:
        """借りた人が返す。amount が None なら全額（残高の範囲で）。(成功か, 理由か返した額) を返す。"""
        with self._lock, self._conn:
            loan = self._loan_row(
                self._conn.execute("SELECT * FROM loans WHERE id=? AND guild_id=?", (loan_id, self._g())).fetchone()
            )
            if loan is None or loan["status"] not in ("active", "overdue"):
                return False, "返す貸付が見つかりません。"
            if loan["borrower_id"] != user_id:
                return False, "返せるのは、借りた本人だけです。"
            at = (
                loan["due"] if loan["status"] == "overdue" else now
            )  # 延滞中は、期限の時点の金額（延滞の利息は付かない）
            remaining = self.loan_remaining(loan, at)
            self._ensure(user_id)
            pay = min(remaining, self._balance(user_id)) if amount is None else amount
            if pay <= 0:
                return False, f"残高がありません（いまの残高: {self._balance(user_id):,}ペリカ）。"
            if pay > remaining:
                return False, f"返す額が残りを超えています（残り {remaining:,}ペリカ）。"
            if pay > self._balance(user_id):
                return False, f"残高が足りません（いまの残高: {self._balance(user_id):,}ペリカ）。"
            self._loan_pay(loan, pay)
            if self.loan_remaining(loan, at) <= 0:
                self._conn.execute("UPDATE loans SET status='repaid' WHERE id=?", (loan_id,))
            return True, pay

    # ---- 期限・通知 ----
    def loan_collect_due(self, now: float) -> list:
        """期限が来た貸付を、借りた人の残高から自動で回収する。足りなければ延滞にする。結果の一覧を返す。"""
        out = []
        with self._lock, self._conn:
            for row in self._conn.execute(
                "SELECT * FROM loans WHERE guild_id=? AND status='active' AND due<=? ORDER BY id", (self._g(), now)
            ).fetchall():
                loan = dict(row)
                remaining = self.loan_remaining(loan, loan["due"])
                take = min(self._balance(loan["borrower_id"]), remaining) if remaining else 0
                if take > 0:
                    self._loan_pay(loan, take)
                left = self.loan_remaining(loan, loan["due"])
                status = "repaid" if left <= 0 else "overdue"
                self._conn.execute("UPDATE loans SET status=? WHERE id=?", (status, loan["id"]))
                out.append({**loan, "status": status, "collected": take, "left": left})
        return out

    def loan_due_soon(self, now: float) -> list:
        """期限まで REMIND_HOURS 時間を切った、まだ知らせていない貸付。知らせたことを記録して返す。"""
        with self._lock, self._conn:
            rows = [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM loans WHERE guild_id=? AND status='active' AND reminded=0 AND due>? AND due<=?",
                    (self._g(), now, now + loans_mod.REMIND_HOURS * 3600),
                )
            ]
            for r in rows:
                self._conn.execute("UPDATE loans SET reminded=1 WHERE id=?", (r["id"],))
            return rows

    def loan_expire_offers(self, now: float) -> list:
        """承諾の期限が過ぎた提案を終了にして返す（投稿を書き換えるため）。"""
        with self._lock, self._conn:
            rows = [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM loans WHERE guild_id=? AND status='offer' AND offer_expires<=?", (self._g(), now)
                )
            ]
            for r in rows:
                self._conn.execute("UPDATE loans SET status='expired' WHERE id=?", (r["id"],))
            return rows

    def loan_list(self, user_id: int) -> dict:
        """自分が借りている／貸している、終了前の貸付（承諾待ちを含む）。"""
        with self._lock:
            rows = [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM loans WHERE guild_id=? AND (borrower_id=? OR lender_id=?) AND status IN ('offer','active','overdue') ORDER BY id",
                    (self._g(), user_id, user_id),
                )
            ]
        return {
            "borrowed": [r for r in rows if r["borrower_id"] == user_id],
            "lent": [r for r in rows if r["lender_id"] == user_id],
        }
