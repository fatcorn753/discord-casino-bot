"""貸し借りの記録。ユーザー同士の貸し借り。

流れ: 提案（offer）→ 借りる人が承諾（active、お金が移る）→ 返済（repaid）／期限に自動回収／足りなければ延滞（overdue）。
延滞の残りは、借りた人に入ってくるお金から、借金の返済のあとで、古い順に自動で回収する。

表の定義は infra/db/schema/tables.py にある。数字と計算は domain/loans.py。
"""

from collections.abc import Mapping
from typing import Any

from domain import loans as loans_mod
from domain.errors import Failure, Outcome, fail
from domain.ports.bridges import GraceReader, IStatusReader, NoOpStatusReader, noop_grace_reader
from domain.ports.contracts import LoanBook, LoanCollectRow, LoanCredit, LoanRow
from domain.ports.repos import ILoanRepo
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo
from infra.db.discord_posts import init_discord_post, set_discord_message

_SELECT_LOAN = (
    "SELECT l.id, l.guild_id, COALESCE(l.lender_id, 0) AS lender_id, l.borrower_id, "
    "l.principal, l.rate_tenths, l.term_days, l.status, l.created, l.offer_expires, "
    "t.accepted_at, t.due, COALESCE(t.paid, 0) AS paid, COALESCE(t.reminded, 0) AS reminded, "
    "COALESCE(p.channel_id, 0) AS channel_id, COALESCE(p.message_id, 0) AS message_id "
    "FROM loans l "
    "LEFT JOIN loan_terms t ON t.loan_id = l.id "
    "LEFT JOIN discord_posts p ON p.entity_type = 'loan' AND p.entity_id = l.id"
)


class LoanRepo(ScopedRepo, ILoanRepo):
    """貸し借りの提案・承諾・返済・回収。"""

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        status_reader: IStatusReader | None = None,
        bank: GraceReader | None = None,
        guild_id: int = 0,
        strict: bool = False,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._status_reader: IStatusReader = status_reader or NoOpStatusReader()
        self._bank: GraceReader = bank or noop_grace_reader

    def _loan_owed(self, loan: Mapping[str, Any], now: float) -> int:
        """いまの返済総額（利息つき）。承諾前は 0。"""
        if not loan["accepted_at"]:
            return 0
        return loans_mod.total_owed(loan["principal"], loan["rate_tenths"], loan["term_days"], loan["accepted_at"], now)

    def loan_remaining(self, loan: Mapping[str, Any], now: float) -> int:
        return max(0, self._loan_owed(loan, now) - loan["paid"])

    def has_overdue_for(self, guild_id: int, user_id: int) -> bool:
        return (
            self._one(
                "SELECT 1 FROM loans WHERE guild_id=? AND borrower_id=? AND status='overdue' LIMIT 1",
                (guild_id, user_id),
            )
            is not None
        )

    def active_loans_for(self, guild_id: int, user_id: int) -> list[LoanRow]:
        return self._all(
            f"{_SELECT_LOAN} WHERE l.guild_id=? AND l.borrower_id=? AND l.status IN ('active','overdue') ORDER BY l.id",
            (guild_id, user_id),
        )

    def record_bankrupt_payment_for(self, loan_id: int, paid: int) -> None:
        self._conn.execute("UPDATE loan_terms SET paid=? WHERE loan_id=?", (paid, loan_id))

    def finalize_bankrupt_loan_for(self, loan_id: int, repaid: bool) -> None:
        self._conn.execute("UPDATE loans SET status=? WHERE id=?", ("repaid" if repaid else "discharged", loan_id))

    def _loan_pay_for(self, guild_id: int, loan: LoanRow, amount: int) -> None:
        """借りた人 → 貸した人へ amount を動かす（借りた人の残高は、呼び出し側が確かめておく）。"""
        self._account.apply(
            guild_id,
            loan["borrower_id"],
            -amount,
            f"貸付の返済→{loan['lender_id']}（#{loan['id']}）",
            kind="loan_repay",
            counterparty_id=loan["lender_id"],
            ref_id=loan["id"],
        )
        self._account.ensure(guild_id, loan["lender_id"])
        self._account.apply(
            guild_id,
            loan["lender_id"],
            amount,
            f"貸付の回収←{loan['borrower_id']}（#{loan['id']}）",
            kind="loan_collect",
            counterparty_id=loan["borrower_id"],
            ref_id=loan["id"],
        )
        loan["paid"] += amount
        self._conn.execute("UPDATE loan_terms SET paid=? WHERE loan_id=?", (loan["paid"], loan["id"]))

    def collect_overdue_for(self, guild_id: int, user_id: int, budget: int) -> int:
        """延滞中の貸付（ジュニア債権）を、この人の残高から、古い順に最大 budget まで回収する。回収した合計を返す。

        帝愛への返済のあとの、天引きの残りで呼ばれる（infra/db/debt/ の withhold_income）。
        """
        total = 0
        now = self._now()
        rows: list[LoanRow] = self._all(
            f"{_SELECT_LOAN} WHERE l.guild_id=? AND l.borrower_id=? AND l.status='overdue' ORDER BY l.id",
            (guild_id, user_id),
        )
        for row in rows:
            loan = row
            take = min(self._account.raw_balance(guild_id, user_id), self.loan_remaining(loan, now), budget - total)
            if take > 0:
                self._loan_pay_for(guild_id, loan, take)
                total += take
            if self.loan_remaining(loan, now) <= 0:
                self._conn.execute("UPDATE loans SET status='repaid' WHERE id=?", (loan["id"],))
        return total

    def loan_credit(self, user_id: int, now: float) -> LoanCredit:
        """借りる人の信用情報。資産の額（残高・証券・預金）は含めない（見極めの駆け引きを残すため）。"""
        with self._lock:
            gid = self._g()
            counts = {
                r["status"]: r["n"]
                for r in self._all(
                    "SELECT status, COUNT(*) AS n FROM loans WHERE guild_id=? AND borrower_id=? GROUP BY status",
                    (gid, user_id),
                )
            }
            live = self._one(
                "SELECT COUNT(*) AS n, COALESCE(SUM(l.principal - MIN(COALESCE(t.paid, 0), l.principal)), 0) AS owed "
                "FROM loans l LEFT JOIN loan_terms t ON t.loan_id = l.id "
                "WHERE l.guild_id=? AND l.borrower_id=? AND l.status IN ('active','overdue')",
                (gid, user_id),
            )
            assert live is not None  # 集計なので必ず1行返る
            row = self._one("SELECT bankrupt_at FROM teiai_bankruptcies WHERE guild_id=? AND user_id=?", (gid, user_id))
            return {
                "status": self._status_reader.status_for(gid, user_id, now),
                "debt": self._account.debt_readonly(gid, user_id),
                "grace_until": self._bank(gid, user_id),
                "repaid": counts.get("repaid", 0),
                "overdue": counts.get("overdue", 0),
                "discharged": counts.get("discharged", 0),  # 借り手の破産で、貸した人が回収しきれなかった件数
                "active": live["n"],
                "owed": live["owed"],  # いま借りている元本の残り（利息を除く）
                "bankrupt_at": row["bankrupt_at"] if row else None,
            }

    def _loan_borrow_block(self, guild_id: int, user_id: int) -> Failure | None:
        """いま、この人が貸付を借りられない理由（借りられるなら None）。承諾・募集・貸すときに、同じ条件で確かめる。"""
        if self._status_reader.loan_blacklisted_for(guild_id, user_id, self._now()):
            return fail("loan_blacklisted")
        late = self._scalar(
            "SELECT COUNT(*) FROM loans WHERE guild_id=? AND borrower_id=? AND status='overdue'", (guild_id, user_id)
        )
        if late:
            return fail("loan_has_overdue")
        return None

    def loan_request(
        self, borrower_id: int, principal: int, rate_tenths: int, term_days: int, channel_id: int, now: float
    ) -> Outcome[int]:
        """借りたい人の募集を出す（貸す人はまだ決まっていない）。(成功か, Failure か貸付の番号) を返す。"""
        low = loans_mod.USER_MIN_PRINCIPAL
        if principal < low:
            return False, fail("loan_min_principal", min_principal=low)
        if rate_tenths < 0:
            return False, fail("loan_min_rate")
        if not loans_mod.MIN_TERM_DAYS <= term_days <= loans_mod.MAX_TERM_DAYS:
            return False, fail("loan_term_range", min_days=loans_mod.MIN_TERM_DAYS, max_days=loans_mod.MAX_TERM_DAYS)
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, borrower_id)
            reason = self._loan_borrow_block(gid, borrower_id)
            if reason:
                return False, reason
            opened = self._scalar(
                "SELECT COUNT(*) FROM loans WHERE guild_id=? AND borrower_id=? AND status='request'",
                (gid, borrower_id),
            )
            if opened >= loans_mod.MAX_BORROWER_OPEN:
                return False, fail("loan_max_borrower_open", max_open=loans_mod.MAX_BORROWER_OPEN)
            cur = self._conn.execute(
                "INSERT INTO loans (guild_id, lender_id, borrower_id, principal, rate_tenths, term_days, status, created, offer_expires) "
                "VALUES (?, NULL, ?, ?, ?, ?, 'request', ?, ?)",
                (gid, borrower_id, principal, rate_tenths, term_days, now, now + loans_mod.OFFER_HOURS * 3600),
            )
            lastrowid = cur.lastrowid
            assert lastrowid is not None
            init_discord_post(self._conn, "loan", lastrowid, channel_id)
            return True, lastrowid

    def loan_lend(self, loan_id: int, lender_id: int, now: float) -> Outcome[str]:
        """募集に応じて貸す（早い者勝ち）。貸す人の残高から借りる人へ、その場でお金が移る。(成功か, 理由) を返す。"""
        with self._db.tx():
            gid = self._g()
            loan: LoanRow | None = self._one(f"{_SELECT_LOAN} WHERE l.id=? AND l.guild_id=?", (loan_id, gid))
            if loan is None or loan["status"] != "request":
                return False, fail("loan_request_ended")
            if lender_id == loan["borrower_id"]:
                return False, fail("loan_self_lend")
            if now >= loan["offer_expires"]:
                return False, fail("loan_request_expired")
            self._account.ensure(gid, loan["borrower_id"])
            reason = self._loan_borrow_block(
                gid, loan["borrower_id"]
            )  # 募集を出したあとで、借りられない状態になっていないか
            if reason:
                return False, fail("loan_borrower_blocked", inner=reason)
            self._account.ensure(gid, lender_id)
            bal = self._account.raw_balance(gid, lender_id)
            if bal < loan["principal"]:
                return False, fail("insufficient_balance", balance=bal)
            active = self._scalar(
                "SELECT COUNT(*) FROM loans WHERE guild_id=? AND lender_id=? AND status IN ('active','overdue')",
                (gid, lender_id),
            )
            if active >= loans_mod.MAX_LENDER_ACTIVE:
                return False, fail("loan_max_lender_active", max_active=loans_mod.MAX_LENDER_ACTIVE)
            self._account.apply(
                gid,
                lender_id,
                -loan["principal"],
                f"貸付→{loan['borrower_id']}（#{loan_id}）",
                kind="loan_lend",
                counterparty_id=loan["borrower_id"],
                ref_id=loan_id,
            )
            self._account.apply(
                gid,
                loan["borrower_id"],
                loan["principal"],
                f"借入←{lender_id}（#{loan_id}）",
                kind="loan_borrow",
                counterparty_id=lender_id,
                ref_id=loan_id,
            )
            self._conn.execute("UPDATE loans SET status='active', lender_id=? WHERE id=?", (lender_id, loan_id))
            self._conn.execute(
                "INSERT INTO loan_terms (loan_id, accepted_at, due, paid, reminded) VALUES (?, ?, ?, 0, 0)",
                (loan_id, now, now + loan["term_days"] * 86400),
            )
            return True, ""

    def loan_offer(
        self,
        lender_id: int,
        borrower_id: int,
        principal: int,
        rate_tenths: int,
        term_days: int,
        channel_id: int,
        now: float,
    ) -> Outcome[int]:
        """貸す提案を作る。(成功か, 理由か貸付の番号) を返す。"""
        low = loans_mod.USER_MIN_PRINCIPAL
        if lender_id == borrower_id:
            return False, fail("loan_self_offer")
        if principal < low:
            return False, fail("loan_min_principal", min_principal=low)
        if rate_tenths < 0:
            return False, fail("loan_min_rate")
        if not loans_mod.MIN_TERM_DAYS <= term_days <= loans_mod.MAX_TERM_DAYS:
            return False, fail("loan_term_range", min_days=loans_mod.MIN_TERM_DAYS, max_days=loans_mod.MAX_TERM_DAYS)
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, lender_id)
            bal = self._account.raw_balance(gid, lender_id)
            if bal < principal:
                return False, fail("insufficient_balance", balance=bal)
            opened = self._scalar(
                "SELECT COUNT(*) FROM loans WHERE guild_id=? AND lender_id=? AND status='offer'", (gid, lender_id)
            )
            active = self._scalar(
                "SELECT COUNT(*) FROM loans WHERE guild_id=? AND lender_id=? AND status IN ('active','overdue')",
                (gid, lender_id),
            )
            if opened >= loans_mod.MAX_LENDER_OPEN:
                return False, fail("loan_max_lender_open", max_open=loans_mod.MAX_LENDER_OPEN)
            if active >= loans_mod.MAX_LENDER_ACTIVE:
                return False, fail("loan_max_lender_active", max_active=loans_mod.MAX_LENDER_ACTIVE)
            cur = self._conn.execute(
                "INSERT INTO loans (guild_id, lender_id, borrower_id, principal, rate_tenths, term_days, status, created, offer_expires) "
                "VALUES (?, ?, ?, ?, ?, ?, 'offer', ?, ?)",
                (gid, lender_id, borrower_id, principal, rate_tenths, term_days, now, now + loans_mod.OFFER_HOURS * 3600),
            )
            lastrowid = cur.lastrowid
            assert lastrowid is not None
            init_discord_post(self._conn, "loan", lastrowid, channel_id)
            return True, lastrowid

    def loan_set_message(self, loan_id: int, message_id: int) -> None:
        with self._db.tx():
            set_discord_message(self._conn, "loan", loan_id, message_id)

    def loan_get(self, loan_id: int) -> LoanRow | None:
        with self._lock:
            return self._one(f"{_SELECT_LOAN} WHERE l.id=? AND l.guild_id=?", (loan_id, self._g()))

    def loan_accept(self, loan_id: int, user_id: int, now: float) -> Outcome[str]:
        """借りる人が承諾して、お金を受け取る。(成功か, 理由) を返す。"""
        with self._db.tx():
            gid = self._g()
            loan: LoanRow | None = self._one(f"{_SELECT_LOAN} WHERE l.id=? AND l.guild_id=?", (loan_id, gid))
            if loan is None or loan["status"] != "offer":
                return False, fail("loan_offer_ended")
            if user_id != loan["borrower_id"]:
                return False, fail("loan_accept_borrower_only")
            if now >= loan["offer_expires"]:
                return False, fail("loan_offer_expired")
            self._account.ensure(gid, user_id)
            reason = self._loan_borrow_block(
                gid, user_id
            )  # 借金があっても借りられる（借り換え）。破産者・延滞中は借りられない
            if reason:
                return False, reason
            self._account.ensure(gid, loan["lender_id"])
            if self._account.raw_balance(gid, loan["lender_id"]) < loan["principal"]:
                return False, fail("loan_lender_insufficient_balance")
            self._account.apply(
                gid,
                loan["lender_id"],
                -loan["principal"],
                f"貸付→{user_id}（#{loan_id}）",
                kind="loan_lend",
                counterparty_id=user_id,
                ref_id=loan_id,
            )
            self._account.apply(
                gid,
                user_id,
                loan["principal"],
                f"借入←{loan['lender_id']}（#{loan_id}）",
                kind="loan_borrow",
                counterparty_id=loan["lender_id"],
                ref_id=loan_id,
            )
            self._conn.execute("UPDATE loans SET status='active' WHERE id=?", (loan_id,))
            self._conn.execute(
                "INSERT INTO loan_terms (loan_id, accepted_at, due, paid, reminded) VALUES (?, ?, ?, 0, 0)",
                (loan_id, now, now + loan["term_days"] * 86400),
            )
            return True, ""

    def loan_decline(self, loan_id: int, user_id: int) -> Outcome[str]:
        """借りる人が断る。"""
        with self._db.tx():
            loan: LoanRow | None = self._one(f"{_SELECT_LOAN} WHERE l.id=? AND l.guild_id=?", (loan_id, self._g()))
            if loan is None or loan["status"] != "offer":
                return False, fail("loan_offer_ended")
            if user_id != loan["borrower_id"]:
                return False, fail("loan_decline_borrower_only")
            self._conn.execute("UPDATE loans SET status='declined' WHERE id=?", (loan_id,))
            return True, ""

    def loan_cancel(self, loan_id: int, user_id: int) -> Outcome[str]:
        """貸す側が提案を取り下げる。"""
        with self._db.tx():
            loan: LoanRow | None = self._one(f"{_SELECT_LOAN} WHERE l.id=? AND l.guild_id=?", (loan_id, self._g()))
            if loan is None or loan["status"] not in ("offer", "request"):
                return False, fail("loan_offer_ended")
            owner = loan["borrower_id"] if loan["status"] == "request" else loan["lender_id"]
            if user_id != owner:
                return False, fail(
                    "loan_cancel_requester_only" if loan["status"] == "request" else "loan_cancel_lender_only"
                )
            self._conn.execute("UPDATE loans SET status='cancelled' WHERE id=?", (loan_id,))
            return True, ""

    def loan_repay(self, loan_id: int, user_id: int, amount: int | None, now: float) -> Outcome[int]:
        """借りた人が返す。amount が None なら全額（残高の範囲で）。(成功か, Failure か返した額) を返す。"""
        with self._db.tx():
            gid = self._g()
            loan: LoanRow | None = self._one(f"{_SELECT_LOAN} WHERE l.id=? AND l.guild_id=?", (loan_id, gid))
            if loan is None or loan["status"] not in ("active", "overdue"):
                return False, fail("loan_repay_not_found")
            if loan["borrower_id"] != user_id:
                return False, fail("loan_repay_borrower_only")
            at = now  # 延滞中は、いまの時刻まで遅延損害金が付く
            remaining = self.loan_remaining(loan, at)
            self._account.ensure(gid, user_id)
            bal = self._account.raw_balance(gid, user_id)
            pay = min(remaining, bal) if amount is None else amount
            if pay <= 0:
                return False, fail("repay_no_cash", balance=bal)
            if pay > remaining:
                return False, fail("loan_repay_exceeds_remaining", remaining=remaining)
            if pay > bal:
                return False, fail("insufficient_balance", balance=bal)
            self._loan_pay_for(gid, loan, pay)
            if self.loan_remaining(loan, at) <= 0:
                self._conn.execute("UPDATE loans SET status='repaid' WHERE id=?", (loan_id,))
            return True, pay

    def loan_collect_due(self, now: float) -> list[LoanCollectRow]:
        """期限が来た貸付を、借りた人の残高から自動で回収する。足りなければ延滞にする。結果の一覧を返す。"""
        out: list[LoanCollectRow] = []
        with self._db.tx():
            gid = self._g()
            collect_rows: list[LoanRow] = self._all(
                f"{_SELECT_LOAN} WHERE l.guild_id=? AND l.status='active' AND t.due<=? ORDER BY l.id", (gid, now)
            )
            for row in collect_rows:
                loan: LoanRow = row
                due = loan["due"]
                assert due is not None
                remaining = self.loan_remaining(loan, due)
                take = min(self._account.raw_balance(gid, loan["borrower_id"]), remaining) if remaining else 0
                if take > 0:
                    self._loan_pay_for(gid, loan, take)
                left = self.loan_remaining(loan, due)
                status = "repaid" if left <= 0 else "overdue"
                self._conn.execute("UPDATE loans SET status=? WHERE id=?", (status, loan["id"]))
                out.append({**loan, "status": status, "collected": take, "left": left})
        return out

    def loan_due_soon(self, now: float) -> list[LoanRow]:
        """期限まで REMIND_HOURS 時間を切った、まだ知らせていない貸付。知らせたことを記録して返す。"""
        with self._db.tx():
            rows: list[LoanRow] = self._all(
                f"{_SELECT_LOAN} WHERE l.guild_id=? AND l.status='active' AND COALESCE(t.reminded, 0)=0 AND t.due>? AND t.due<=?",
                (self._g(), now, now + loans_mod.REMIND_HOURS * 3600),
            )
            for r in rows:
                self._conn.execute("UPDATE loan_terms SET reminded=1 WHERE loan_id=?", (r["id"],))
            return rows

    def loan_expire_offers(self, now: float) -> list[LoanRow]:
        """承諾の期限が過ぎた提案を終了にして返す（投稿を書き換えるため）。"""
        with self._db.tx():
            rows: list[LoanRow] = self._all(
                f"{_SELECT_LOAN} WHERE l.guild_id=? AND l.status IN ('offer','request') AND l.offer_expires<=?",
                (self._g(), now),
            )
            for r in rows:
                self._conn.execute("UPDATE loans SET status='expired' WHERE id=?", (r["id"],))
            return rows

    def loan_list(self, user_id: int) -> LoanBook[LoanRow]:
        """自分が借りている／貸している、終了前の貸付（承諾待ちを含む）。"""
        with self._lock:
            rows: list[LoanRow] = self._all(
                f"{_SELECT_LOAN} WHERE l.guild_id=? AND (l.borrower_id=? OR l.lender_id=?) AND l.status IN ('offer','request','active','overdue') ORDER BY l.id",
                (self._g(), user_id, user_id),
            )
        return {
            "borrowed": [r for r in rows if r["borrower_id"] == user_id],
            "lent": [r for r in rows if r["lender_id"] == user_id],
        }
