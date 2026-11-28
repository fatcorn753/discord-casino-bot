"""CDS（破綻保証）: オッズの提示・購入・自己肩代わり・破産時の配当と失効。"""

from domain import teiai
from domain.errors import Outcome, fail
from domain.ports.contracts import CdsHoldings, CdsQuote, CdsRow

from .grace import BankRepo as Base


class BankRepo(Base):
    def cds_quote(self, target_id: int, now: float) -> CdsQuote:
        """対象の、いまの CDS のオッズと状況。読み取り専用。"""
        gid = self._g()
        status = self._status_reader.status_for(gid, target_id, now)
        debt = self._account.debt_readonly(gid, target_id)
        ratio = self._margin_ratio_reader(gid, target_id)
        return {
            "odds": teiai.cds_odds(status, debt, ratio),
            "status": status,
            "debt": debt,
            "grace": self.grace_until_for(gid, target_id) is not None,
        }

    def cds_buy(self, buyer_id: int, target_id: int, stake: int, now: float) -> Outcome[float]:
        """CDS を買う（自分にも、他人にも）。(成功か, 理由かオッズ) を返す。"""
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, buyer_id)
            if stake < teiai.CDS_MIN_STAKE:
                return False, fail("cds_min_stake", min_stake=teiai.CDS_MIN_STAKE)
            if self._one("SELECT 1 FROM accounts WHERE guild_id=? AND user_id=?", (gid, target_id)) is None:
                return False, fail("user_not_registered")
            bal = self._account.raw_balance(gid, buyer_id)
            if bal < stake:
                return False, fail("insufficient_balance", balance=bal)
            if self.grace_until_for(gid, target_id) is not None:
                return False, fail("cds_target_in_grace")
            if self._status_reader.status_for(gid, target_id, now) == teiai.BANKRUPT:
                return False, fail("cds_target_bankrupt")
            odds = self.cds_quote(target_id, now)["odds"]
            if buyer_id == target_id:  # 自分への保証は、積める額に上限がある
                have = self._scalar(
                    "SELECT COALESCE(SUM(stake * odds), 0) FROM cds WHERE guild_id=? AND buyer_id=? AND target_id=? "
                    "AND status='active' AND expires>?",
                    (gid, buyer_id, buyer_id, now),
                )
                if have + stake * odds > teiai.CDS_SELF_CAP:
                    room = max(0, int((teiai.CDS_SELF_CAP - have) / odds))
                    return False, fail(
                        "cds_self_cap",
                        cap=teiai.CDS_SELF_CAP,
                        have=int(have),
                        room=room,
                    )
            self._account.apply(gid, buyer_id, -stake, f"CDS の掛け金（対象 {target_id}・{odds}倍）")
            self.bank_income_for(gid, stake, now)
            self._conn.execute(
                "INSERT INTO cds (guild_id, buyer_id, target_id, stake, odds, ts, expires) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (gid, buyer_id, target_id, stake, odds, now, now + teiai.CDS_TERM_DAYS * 86400),
            )
            return True, odds

    def cds_list(self, user_id: int) -> CdsHoldings:
        """自分が買っている CDS（有効なもの）と、自分に掛けられている件数。"""
        with self._lock:
            gid = self._g()
            mine: list[CdsRow] = self._all(
                "SELECT * FROM cds WHERE guild_id=? AND buyer_id=? AND status='active' ORDER BY id",
                (gid, user_id),
            )
            on_me = self._scalar(
                "SELECT COUNT(*) FROM cds WHERE guild_id=? AND target_id=? AND buyer_id!=target_id AND status='active'",
                (gid, user_id),
            )
            return {"bought": mine, "on_me": on_me}

    def cds_total_claims(self) -> int:
        """このサーバーの、有効な CDS の配当見込みの合計（表示用）。"""
        with self._lock:
            return int(
                self._scalar(
                    "SELECT COALESCE(SUM(stake * odds), 0) FROM cds WHERE guild_id=? AND status='active'", (self._g(),)
                )
            )

    def cds_cover_self_for(self, guild_id: int, user_id: int, shortfall: int, now: float) -> int:
        """払えなかった shortfall を、自分への CDS が肩代わりする（古い順に、1件ずつ使い切り）。肩代わりした額を返す。現金は渡らない。"""
        covered = 0
        for r in self._all(
            "SELECT id, stake, odds FROM cds WHERE guild_id=? AND buyer_id=? AND target_id=? AND status='active' "
            "AND expires>? ORDER BY id",
            (guild_id, user_id, user_id, now),
        ):
            if covered >= shortfall:
                break
            pay = self.bank_pay_for(guild_id, min(shortfall - covered, int(r["stake"] * r["odds"])), now)
            self._conn.execute("UPDATE cds SET status='paid', paid=? WHERE id=?", (pay, r["id"]))
            covered += pay
            self._notices.note_for(guild_id, user_id, "cds_self_covered", pay=pay)
        return covered

    def cds_settle_default_for(self, guild_id: int, target_id: int, debt_before: int, now: float) -> int:
        """対象が強制破産したとき、他人からの CDS に配当を払う。合計は、対象の借金の CDS_PAYOUT_CAP_RATE まで（超える分は、一律に減らす）。払った合計を返す。"""
        rows = self._all(
            "SELECT id, buyer_id, stake, odds FROM cds WHERE guild_id=? AND target_id=? AND buyer_id!=target_id "
            "AND status='active' AND expires>? ORDER BY id",
            (guild_id, target_id, now),
        )
        claims = [(r["id"], r["buyer_id"], int(r["stake"] * r["odds"])) for r in rows]
        total_claim = sum(c for _, _, c in claims)
        cap = int(teiai.CDS_PAYOUT_CAP_RATE * debt_before)
        scale = min(1.0, cap / total_claim) if total_claim > 0 else 0.0
        total_paid = 0
        for cid, buyer, claim in claims:
            pay = self.bank_pay_for(guild_id, int(claim * scale), now)
            if pay > 0:
                self._account.ensure(guild_id, buyer)
                self._account.apply(guild_id, buyer, pay, f"CDS の配当（対象 {target_id} が強制破産）", income=pay)
                self._notices.note_for(guild_id, buyer, "cds_default_payout", target_id=target_id, pay=pay)
            self._conn.execute("UPDATE cds SET status='paid', paid=? WHERE id=?", (pay, cid))
            total_paid += pay
        self.cds_void_for(guild_id, target_id)
        return total_paid

    def cds_void_for(self, guild_id: int, target_id: int) -> None:
        """対象が破産したあと、まだ有効な CDS（配当されなかったもの）を無効にする。"""
        self._conn.execute(
            "UPDATE cds SET status='void' WHERE guild_id=? AND target_id=? AND status='active'",
            (guild_id, target_id),
        )
