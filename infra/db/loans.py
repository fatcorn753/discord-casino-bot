"""貸し借りの記録（Economy に混ぜ込む部品）。ユーザー同士の貸し借り。

流れ: 提案（offer）→ 借りる人が承諾（active、お金が移る）→ 返済（repaid）／期限に自動回収／足りなければ延滞（overdue）。
延滞の残りは、借りた人に入ってくるお金から、借金の返済のあとで、古い順に自動で回収する。

表の定義は core/economy.py の _init_schema にある。数字と計算は core/loans.py。
"""

import time
from typing import Optional

from domain import loans as loans_mod
from infra.db.base import EcoBase


def _loan_row(row) -> Optional[dict]:
    return dict(row) if row is not None else None


class LoanMixin(EcoBase):
    """貸し借りの提案・承諾・返済・回収。"""

    # ---- 内部 ---- #

    def _loan_owed(self, loan: dict, now: float) -> int:
        """いまの返済総額（利息つき）。承諾前は 0。"""
        if not loan["accepted_at"]:
            return 0
        return loans_mod.total_owed(loan["principal"], loan["rate_tenths"], loan["term_days"], loan["accepted_at"], now)

    def loan_remaining(self, loan: dict, now: float) -> int:
        return max(0, self._loan_owed(loan, now) - loan["paid"])

    def _loan_pay(self, loan: dict, amount: int) -> None:
        """借りた人 → 貸した人へ amount を動かす（借りた人の残高は、呼び出し側が確かめておく）。"""
        self._apply(loan["borrower_id"], -amount, f"貸付の返済→{loan['lender_id']}（#{loan['id']}）")
        self._ensure(loan["lender_id"])
        self._apply(loan["lender_id"], amount, f"貸付の回収←{loan['borrower_id']}（#{loan['id']}）")
        loan["paid"] += amount
        self._conn.execute("UPDATE loans SET paid=? WHERE id=?", (loan["paid"], loan["id"]))

    def _collect_overdue_for(self, user_id: int, budget: int) -> int:
        """延滞中の貸付（ジュニア債権）を、この人の残高から、古い順に最大 budget まで回収する。回収した合計を返す。

        帝愛への返済のあとの、天引きの残りで呼ばれる（core/eco_debt.py の _withhold）。
        """
        total = 0
        now = time.time()
        rows = self._conn.execute(
            "SELECT * FROM loans WHERE guild_id=? AND borrower_id=? AND status='overdue' ORDER BY id",
            (self._g(), user_id),
        ).fetchall()
        for row in rows:
            loan = dict(row)
            take = min(self._balance(user_id), self.loan_remaining(loan, now), budget - total)
            if take > 0:
                self._loan_pay(loan, take)
                total += take
            if self.loan_remaining(loan, now) <= 0:
                self._conn.execute("UPDATE loans SET status='repaid' WHERE id=?", (loan["id"],))
        return total

    # ---- 信用情報（貸す人が、借りる人を見極めるための記録） ---- #
    def loan_credit(self, user_id: int, now: Optional[float] = None) -> dict:
        """借りる人の信用情報。資産の額（残高・証券・預金）は含めない（見極めの駆け引きを残すため）。"""
        now = time.time() if now is None else now
        with self._lock:
            counts = {
                r["status"]: r["n"]
                for r in self._conn.execute(
                    "SELECT status, COUNT(*) AS n FROM loans WHERE guild_id=? AND borrower_id=? GROUP BY status",
                    (self._g(), user_id),
                )
            }
            live = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(principal - MIN(paid, principal)), 0) AS owed FROM loans "
                "WHERE guild_id=? AND borrower_id=? AND status IN ('active','overdue')",
                (self._g(), user_id),
            ).fetchone()
            row = self._conn.execute(
                "SELECT bankrupt_at FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
            return {
                "status": self.status(user_id, now),
                "debt": self.debt(user_id),
                "grace_until": self.grace_until(user_id),
                "repaid": counts.get("repaid", 0),
                "overdue": counts.get("overdue", 0),
                "discharged": counts.get("discharged", 0),  # 借り手の破産で、貸した人が回収しきれなかった件数
                "active": live["n"],
                "owed": live["owed"],  # いま借りている元本の残り（利息を除く）
                "bankrupt_at": row["bankrupt_at"] if row else None,
            }

    # ---- 募集（借りたい人が出す。最初に貸すと言った人が貸し手になる） ---- #
    def _loan_borrow_block(self, user_id: int) -> Optional[str]:
        """いま、この人が貸付を借りられない理由（借りられるなら None）。承諾・募集・貸すときに、同じ条件で確かめる。"""
        if self.loan_blacklisted(user_id):
            return "破産のブラックリストの間は、貸付を借りられません（完済して7日たつと解除されます）。"
        late = self._conn.execute(
            "SELECT COUNT(*) FROM loans WHERE guild_id=? AND borrower_id=? AND status='overdue'", (self._g(), user_id)
        ).fetchone()[0]
        if late:
            return "延滞中の貸付があるので、借りられません。先に返してください。"
        return None

    def loan_request(
        self, borrower_id: int, principal: int, rate_tenths: int, term_days: int, channel_id: int, now: float
    ) -> tuple:
        """借りたい人の募集を出す（貸す人はまだ決まっていない）。(成功か, 理由か貸付の番号) を返す。"""
        low = loans_mod.USER_MIN_PRINCIPAL
        if principal < low:
            return False, f"金額は {low:,}ペリカ 以上にしてください。"
        if rate_tenths < 0:
            return False, "日利は 0 以上にしてください。"
        if not loans_mod.MIN_TERM_DAYS <= term_days <= loans_mod.MAX_TERM_DAYS:
            return False, f"期間は {loans_mod.MIN_TERM_DAYS}〜{loans_mod.MAX_TERM_DAYS}日 にしてください。"
        with self._lock, self._conn:
            self._ensure(borrower_id)
            reason = self._loan_borrow_block(borrower_id)
            if reason:
                return False, reason
            opened = self._conn.execute(
                "SELECT COUNT(*) FROM loans WHERE guild_id=? AND borrower_id=? AND status='request'",
                (self._g(), borrower_id),
            ).fetchone()[0]
            if opened >= loans_mod.MAX_BORROWER_OPEN:
                return (
                    False,
                    f"出せる募集は、同時に {loans_mod.MAX_BORROWER_OPEN} 件までです。先の募集を取り下げてください。",
                )
            cur = self._conn.execute(
                "INSERT INTO loans (guild_id, lender_id, borrower_id, principal, rate_tenths, term_days, status, created, offer_expires, channel_id) "
                "VALUES (?, 0, ?, ?, ?, ?, 'request', ?, ?, ?)",
                (
                    self._g(),
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

    def loan_lend(self, loan_id: int, lender_id: int, now: float) -> tuple:
        """募集に応じて貸す（早い者勝ち）。貸す人の残高から借りる人へ、その場でお金が移る。(成功か, 理由) を返す。"""
        with self._lock, self._conn:
            loan = _loan_row(
                self._conn.execute("SELECT * FROM loans WHERE id=? AND guild_id=?", (loan_id, self._g())).fetchone()
            )
            if loan is None or loan["status"] != "request":
                return False, "この募集はもう終わっています（ほかの人が貸したか、取り下げられました）。"
            if lender_id == loan["borrower_id"]:
                return False, "自分の募集には、貸せません。"
            if now >= loan["offer_expires"]:
                return False, "募集の期限が過ぎました。"
            self._ensure(loan["borrower_id"])
            reason = self._loan_borrow_block(
                loan["borrower_id"]
            )  # 募集を出したあとで、借りられない状態になっていないか
            if reason:
                return False, "借りる人が、いまは借りられない状態です。" + reason
            self._ensure(lender_id)
            if self._balance(lender_id) < loan["principal"]:
                return False, f"残高が足りません（いまの残高: {self._balance(lender_id):,}ペリカ）。"
            active = self._conn.execute(
                "SELECT COUNT(*) FROM loans WHERE guild_id=? AND lender_id=? AND status IN ('active','overdue')",
                (self._g(), lender_id),
            ).fetchone()[0]
            if active >= loans_mod.MAX_LENDER_ACTIVE:
                return False, f"同時に貸せるのは {loans_mod.MAX_LENDER_ACTIVE} 件までです。"
            self._apply(lender_id, -loan["principal"], f"貸付→{loan['borrower_id']}（#{loan_id}）")
            self._apply(loan["borrower_id"], loan["principal"], f"借入←{lender_id}（#{loan_id}）")
            self._conn.execute(
                "UPDATE loans SET status='active', lender_id=?, accepted_at=?, due=? WHERE id=?",
                (lender_id, now, now + loan["term_days"] * 86400, loan_id),
            )
            return True, ""

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
            return _loan_row(
                self._conn.execute("SELECT * FROM loans WHERE id=? AND guild_id=?", (loan_id, self._g())).fetchone()
            )

    def loan_accept(self, loan_id: int, user_id: int, now: float) -> tuple:
        """借りる人が承諾して、お金を受け取る。(成功か, 理由) を返す。"""
        with self._lock, self._conn:
            loan = _loan_row(
                self._conn.execute("SELECT * FROM loans WHERE id=? AND guild_id=?", (loan_id, self._g())).fetchone()
            )
            if loan is None or loan["status"] != "offer":
                return False, "この提案はもう終わっています。"
            if user_id != loan["borrower_id"]:
                return False, "この提案を承諾できるのは、借りる本人だけです。"
            if now >= loan["offer_expires"]:
                return False, "承諾の期限が過ぎました。"
            self._ensure(user_id)
            reason = self._loan_borrow_block(
                user_id
            )  # 借金があっても借りられる（借り換え）。破産者・延滞中は借りられない
            if reason:
                return False, reason
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
            loan = _loan_row(
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
            loan = _loan_row(
                self._conn.execute("SELECT * FROM loans WHERE id=? AND guild_id=?", (loan_id, self._g())).fetchone()
            )
            if loan is None or loan["status"] not in ("offer", "request"):
                return False, "この提案はもう終わっています。"
            owner = loan["borrower_id"] if loan["status"] == "request" else loan["lender_id"]
            if user_id != owner:
                return False, "取り下げられるのは、" + (
                    "募集を出した人だけです。" if loan["status"] == "request" else "貸す側だけです。"
                )
            self._conn.execute("UPDATE loans SET status='cancelled' WHERE id=?", (loan_id,))
            return True, ""

    # ---- 返済 ----
    def loan_repay(self, loan_id: int, user_id: int, amount: Optional[int], now: float) -> tuple:
        """借りた人が返す。amount が None なら全額（残高の範囲で）。(成功か, 理由か返した額) を返す。"""
        with self._lock, self._conn:
            loan = _loan_row(
                self._conn.execute("SELECT * FROM loans WHERE id=? AND guild_id=?", (loan_id, self._g())).fetchone()
            )
            if loan is None or loan["status"] not in ("active", "overdue"):
                return False, "返す貸付が見つかりません。"
            if loan["borrower_id"] != user_id:
                return False, "返せるのは、借りた本人だけです。"
            at = now  # 延滞中は、いまの時刻まで遅延損害金が付く
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
                    "SELECT * FROM loans WHERE guild_id=? AND status IN ('offer','request') AND offer_expires<=?",
                    (self._g(), now),
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
                    "SELECT * FROM loans WHERE guild_id=? AND (borrower_id=? OR lender_id=?) AND status IN ('offer','request','active','overdue') ORDER BY id",
                    (self._g(), user_id, user_id),
                )
            ]
        return {
            "borrowed": [r for r in rows if r["borrower_id"] == user_id],
            "lent": [r for r in rows if r["lender_id"] == user_id],
        }
