"""債券の記録。購入・満期の決着・倒産抽選・売却。

表の定義は infra/db/schema/tables.py にある。利率などの数字は domain/bonds.py。
帝愛社債（#35）: 発行体は帝愛銀行。購入の元本は銀行の手元の現金に入り、満期・売却・換金で銀行から払う。
元本は銀行の負債なので、出し入れをサーモスタットに通さない（thermo=False）。銀行が得をした・損をした分
（ジャンク債の倒産で残った元本、売却の値引き・上乗せ、利息）だけを、サーモスタットに通す。
満期の利息は、帝愛の手元現金から払う（新しく作らない。現金が足りなければ利息が減る。元本は必ず全額返す）。
"""

import random
from typing import Any, Optional

from domain import bonds as bonds_mod
from domain import rates
from domain.errors import fail
from domain.ports.bridges import (
    IBankCashFlow,
    IBondHoldings,
    IDebtRepayHook,
    IMarginCollateralGuard,
    NoOpDebtRepayHook,
    NoOpMarginCollateralGuard,
)
from domain.ports.contracts import ActiveBond, BondQuote, HoldingBook
from domain.ports.repos import IBondRepo
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo


class BondRepo(ScopedRepo, IBondRepo, IBondHoldings):
    """債券（元本を預かり、満期に利息つきで返す。ジャンク債は倒産することがある）。購入・満期の決着・解約。"""

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        bank: IBankCashFlow,
        debt_repay: Optional[IDebtRepayHook] = None,
        collateral_guard: Optional[IMarginCollateralGuard] = None,
        guild_id: int = 0,
        strict: bool = False,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._bank = bank
        self._debt_repay: IDebtRepayHook = debt_repay or NoOpDebtRepayHook()
        self._collateral_guard: IMarginCollateralGuard = collateral_guard or NoOpMarginCollateralGuard()

    def bond_buy(self, user_id: int, kind_key: str, amount: int, now: float) -> tuple[Any, ...]:
        """債券を買う。(成功か, Failure | "") を返す。"""
        kind = bonds_mod.KINDS.get(kind_key)
        if kind is None:
            return False, fail("bond_invalid_kind")
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            if amount < bonds_mod.MIN_AMOUNT:
                return False, fail("bond_min_amount", min_amount=bonds_mod.MIN_AMOUNT)
            debt = self._account.debt(gid, user_id)
            if debt > 0:
                return False, fail("debt_blocks", debt=debt, verb="買えません")
            bal = self._account.raw_balance(gid, user_id)
            if bal < amount:
                return False, fail("insufficient_balance", balance=bal)
            held = self._bond_held(gid, user_id, kind_key)
            if held + amount > kind.max_total:
                return False, fail(
                    "bond_max_total",
                    name=kind.name,
                    max_total=kind.max_total,
                    held=held,
                    room=kind.max_total - held,
                )
            rate = rates.bond_rate_pct(
                kind, self._bank.risk_free_per_day_for(gid, now)
            )  # 買ったときの金利で固定（#18）
            self._account.apply(gid, user_id, -amount, f"債券購入:{kind.name}")
            self._bank.bank_income_for(gid, amount, now, thermo=False)
            self._conn.execute(
                "INSERT INTO bonds (guild_id, user_id, kind, principal, due, ts, rate) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (gid, user_id, kind_key, amount, now + kind.hours * 3600, now, rate),
            )
            return True, ""

    def _bond_held(self, guild_id: int, user_id: int, kind_key: str) -> int:
        return self._conn.execute(
            "SELECT COALESCE(SUM(principal), 0) FROM bonds WHERE guild_id=? AND user_id=? AND kind=? AND status='active'",
            (guild_id, user_id, kind_key),
        ).fetchone()[0]

    def bond_values(self) -> dict[int, int]:
        """{ユーザーID: 預けている元本の合計}（このサーバーの、保有中の債券。ランキングの総資産に含める）。"""
        with self._lock:
            gid = self._g()
            return {
                r["user_id"]: r["p"]
                for r in self._conn.execute(
                    "SELECT user_id, SUM(principal) AS p FROM bonds WHERE guild_id=? AND status='active' GROUP BY user_id",
                    (gid,),
                )
            }

    def collect_values(self, guild_id: int, totals: dict[int, int]) -> None:
        with self._lock:
            for r in self._conn.execute(
                "SELECT user_id, SUM(principal) AS p FROM bonds WHERE guild_id=? AND status='active' GROUP BY user_id",
                (guild_id,),
            ):
                totals[r["user_id"]] = totals.get(r["user_id"], 0) + r["p"]

    def bond_list(self, user_id: int, recent: int = 5) -> HoldingBook:
        """保有中（満期が近い順）と、直近に決着したもの（新しい順）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM bonds WHERE guild_id=? AND user_id=? ORDER BY id", (self._g(), user_id)
            ).fetchall()
        active = sorted((dict(r) for r in rows if r["status"] == "active"), key=lambda r: r["due"])
        done = [dict(r) for r in reversed(rows) if r["status"] != "active"][:recent]
        return {"active": active, "done": done}

    def bond_settle(self, now: float, rng: Any = None) -> list[Any]:
        """ジャンク債の倒産抽選と、満期を過ぎた債券の決着（このサーバー全員分）。決着した行（status と paid つき）を返す。"""
        rng = rng or random
        out = []
        with self._db.tx():
            gid = self._g()
            for row in self._conn.execute(
                "SELECT * FROM bonds WHERE guild_id=? AND status='active' ORDER BY id", (gid,)
            ).fetchall():
                kind = bonds_mod.KINDS[row["kind"]]
                until = min(now, row["due"])
                since = row["checked"] if row["checked"] is not None else row["ts"]
                if kind.default_pct and until > since:
                    if rng.random() < bonds_mod.default_probability(kind, (until - since) / 3600):
                        self._conn.execute("UPDATE bonds SET status='defaulted', paid=0 WHERE id=?", (row["id"],))
                        # 返さなくてよくなった元本は、銀行の儲け（流入）になる
                        self._bank.bank_thermo_batch_for(gid, row["principal"], now)
                        out.append({**dict(row), "status": "defaulted", "paid": 0})
                        continue
                    self._conn.execute("UPDATE bonds SET checked=? WHERE id=?", (until, row["id"]))
                if row["due"] > now:
                    continue
                interest = max(0, bonds_mod.interest(kind, row["principal"], row["rate"]))
                # 元本を先に返す（社債はシニア。現金が足りなければ信託 → 熱浴・共済の順で負担する）
                self._bank.bank_pay_for(gid, row["principal"], now, thermo=False)
                got_interest = 0
                if interest:
                    state = self._bank.bank_sync_for(gid, now)
                    got_interest = int(min(interest, max(state["cash"], 0.0)))
                    if got_interest:
                        self._bank.bank_pay_for(gid, got_interest, now)
                paid = row["principal"] + got_interest
                self._account.ensure(gid, row["user_id"])
                self._account.apply(gid, row["user_id"], paid, f"債券償還:{kind.name}", income=got_interest)
                self._debt_repay.repay_all_of_for(
                    gid, row["user_id"], row["principal"], f"帝愛への返済（債券の元本:{kind.name}）"
                )
                self._conn.execute("UPDATE bonds SET status='paid', paid=? WHERE id=?", (paid, row["id"]))
                out.append({**dict(row), "status": "paid", "paid": paid})
        return out

    def bond_price(self, row: dict[str, Any], now: float) -> int:
        """その債券を、いま売ったときの手取り（売れない債券は 0）。表示用。"""
        kind = bonds_mod.KINDS[row["kind"]]
        return self._sale_value_for(self._g(), row, now) if kind.sellable else 0

    def bond_quote(self, kind_key: str, amount: int, now: float) -> BondQuote:
        """いま買うときの利率・満期の受取額・すぐ売ったときの手取り（表示用。#18）。"""
        kind = bonds_mod.KINDS[kind_key]
        with self._lock:
            gid = self._g()
            rf = self._bank.risk_free_per_day_for(gid, now)
        rate = rates.bond_rate_pct(kind, rf)
        due = now + kind.hours * 3600
        sale = (
            bonds_mod.sale_value(kind, amount, now, due, rate, rates.sale_discount_per_hour(rf)) if kind.sellable else 0
        )
        return BondQuote(rate=rate, payout=bonds_mod.maturity_payout(kind, amount, rate), sale=sale)

    def _sale_value_for(self, guild_id: int, row: Any, now: float) -> int:
        """DCF の売値。割引率は、いまの r_f + 売却スプレッド（金利が上がると下がる。#18）。"""
        kind = bonds_mod.KINDS[row["kind"]]
        discount = rates.sale_discount_per_hour(self._bank.risk_free_per_day_for(guild_id, now))
        return bonds_mod.sale_value(kind, row["principal"], now, row["due"], row["rate"], discount)

    def bond_cancel(self, user_id: int, bond_id: int, now: Optional[float] = None) -> tuple[Any, ...]:
        """満期前に、DCF の値段で売る（通常債だけ）。(成功か, 理由か受け取った額) を返す。"""
        with self._db.tx():
            gid = self._g()
            row = self._conn.execute(
                "SELECT kind, principal FROM bonds WHERE id=? AND guild_id=? AND user_id=? AND status='active'",
                (bond_id, gid, user_id),
            ).fetchone()
            if row is not None:
                reason = self._collateral_guard.check_bond_cancel(gid, user_id, row["kind"], row["principal"])
                if reason is not None:
                    return False, reason
            return self.bond_cancel_locked_for(gid, user_id, bond_id, now)

    def active_bonds_for(self, guild_id: int, user_id: int) -> list[ActiveBond]:
        """持っている（status='active' の）債券（古い順）。呼び出し側のトランザクションの中で使う。"""
        return [
            ActiveBond(id=r["id"], kind=r["kind"], principal=r["principal"])
            for r in self._conn.execute(
                "SELECT id, kind, principal FROM bonds WHERE guild_id=? AND user_id=? AND status='active' ORDER BY id",
                (guild_id, user_id),
            ).fetchall()
        ]

    def bond_write_off_for(self, guild_id: int, user_id: int, bond_id: int, payout: int, now: float) -> None:
        row = self._conn.execute(
            "SELECT principal FROM bonds WHERE id=? AND guild_id=? AND user_id=? AND status='active'",
            (bond_id, guild_id, user_id),
        ).fetchone()
        if row is None:
            return
        self._settle_early_for(guild_id, user_id, row["principal"], payout, now, "破産:債券の差し押さえ")
        self._conn.execute("UPDATE bonds SET status='cancelled', paid=? WHERE id=?", (payout, bond_id))

    def _settle_early_for(
        self, guild_id: int, user_id: int, principal: int, payout: int, now: float, reason: str
    ) -> None:
        """満期の前に payout で払い戻す（売却・破産の換金）。

        払い戻しは thermo=False で銀行から払い、元本との差（値引きなら流入、上乗せなら流出）だけを
        サーモスタットに通す。流出なら支払いの前、流入なら支払いのあと（bank_thermo_batch_for の約束）。
        """
        net = principal - payout
        if net < 0:
            self._bank.bank_thermo_batch_for(guild_id, net, now)
        self._bank.bank_pay_for(guild_id, payout, now, thermo=False)
        if net > 0:
            self._bank.bank_thermo_batch_for(guild_id, net, now)
        self._account.apply(guild_id, user_id, payout, reason)

    def bond_cancel_locked_for(
        self, guild_id: int, user_id: int, bond_id: int, now: Optional[float] = None
    ) -> tuple[Any, ...]:
        """bond_cancel の本体（呼び出し側が、ロックとトランザクションの中で使う）。"""
        row = self._conn.execute(
            "SELECT * FROM bonds WHERE id=? AND guild_id=? AND user_id=? AND status='active'",
            (bond_id, guild_id, user_id),
        ).fetchone()
        if row is None:
            return False, fail("bond_not_found")
        kind = bonds_mod.KINDS[row["kind"]]
        if not kind.sellable:
            return False, fail("bond_not_sellable", name=kind.name)
        at = self._now() if now is None else now
        refund = self._sale_value_for(guild_id, row, at)
        # もともと自分のお金なので、借金の徴収はしない
        self._settle_early_for(guild_id, user_id, row["principal"], refund, at, "返金:債券売却")
        self._conn.execute("UPDATE bonds SET status='cancelled', paid=? WHERE id=?", (refund, bond_id))
        return True, refund
