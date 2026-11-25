"""無リスク金利の遷移・帝愛信託の利息支払い・キャピタルゲイン税の徴収。"""

import random
from typing import cast

from domain import market as market_mod
from domain import rates

from .flow import BankRepo as Base


class BankRepo(Base):
    # 帝愛信託の利息（#33。確定した流出として、毎時払う）

    def risk_free_per_day_for(self, guild_id: int, now: float) -> float:
        """無リスク金利 r_f（1日あたり。連続複利）。準備率に連動して毎分動く（#18。bank_rf_tick）。未開始なら基準の金利。"""
        v = self._scalar("SELECT rf FROM teiai_trust WHERE guild_id=?", (guild_id,))
        return v if v is not None and v > 0 else rates.BASE_RISK_FREE_PER_DAY

    def bank_rf_tick(self, now: float, rng: random.Random | None = None) -> float:
        """r_f を、いまの準備率 θ の目標へ向けて進める（CIR 風。式は domain/rates.py）。新しい r_f を返す。"""
        rng = rng if rng is not None else cast("random.Random", random)
        with self._db.tx():
            gid = self._g()
            state = self.bank_sync_for(gid, now)
            row = self._one("SELECT rf, rf_ts FROM teiai_trust WHERE guild_id=?", (gid,))
            rf_cur = row["rf"] if row is not None else 0.0
            rf_ts = row["rf_ts"] if row is not None else 0.0
            r = rf_cur if rf_cur > 0 else rates.BASE_RISK_FREE_PER_DAY
            if rf_ts > 0:
                ratio = self._reserve_for(gid, state["cash"], now)[4]
                r = rates.rf_step(r, ratio, (now - rf_ts) / 86400, rng.gauss(0.0, 1.0))
            self._conn.execute(
                "INSERT INTO teiai_trust (guild_id, rf, rf_ts) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET rf=excluded.rf, rf_ts=excluded.rf_ts",
                (gid, r, now),
            )
            return r

    def bank_reserve_for(self, guild_id: int, now: float) -> tuple[float, int, float | None]:
        """(利用可能現金, 負債 = 信託 + 社債の元本, 準備率 θ)。呼び出し側のトランザクションの中で使う。"""
        state = self.bank_sync_for(guild_id, now)
        total, bonds, _locks, available, ratio = self._reserve_for(guild_id, state["cash"], now)
        return available, total + bonds, ratio

    def bank_trust_accrue(self, now: float) -> int:
        """帝愛信託の利息を、前回からの経過時間ぶん払う（1時間たっていなければ何もしない）。払った額を返す。

        利息は銀行の確定した流出: 手元の現金から払い、足りない分は信託からは取らず（自分で自分に払うことになるため）、
        超過損失として熱浴・共済へ回す。払えた分を、信託の残高に応じて按分し、預けた人の手元へ払う。
        信託の残高へ上乗せすると、現金（資産）を減らしたうえに負債も増やす二重計上になるため、手元へ払う。
        """
        with self._db.tx():
            gid = self._g()
            self.bank_sync_for(gid, now)
            row = self._one("SELECT trust_ts, accrued FROM teiai_trust WHERE guild_id=?", (gid,))
            trust_ts = row["trust_ts"] if row is not None else 0.0
            accrued = row["accrued"] if row is not None else 0.0
            if trust_ts <= 0:
                self._conn.execute(
                    "INSERT INTO teiai_trust (guild_id, trust_ts) VALUES (?, ?) "
                    "ON CONFLICT(guild_id) DO UPDATE SET trust_ts=excluded.trust_ts",
                    (gid, now),
                )
                return 0
            hours = (now - trust_ts) / 3600
            if hours < 1:
                return 0
            deposits = self._deposits_for(gid)
            total = sum(a for _, a in deposits)
            rate = self.trust_rate_per_day_for(gid, now)
            due = total * rates.growth(rate, hours / 24) + accrued
            paid = self._outflow_for(gid, int(due), now, use_trust=False, thermo=True) if total > 0 else 0
            credited = 0
            for uid, amt in deposits:
                gain = paid * amt // total
                if gain:
                    credited += gain
                    self._account.ensure(gid, uid)
                    self._account.apply(gid, uid, gain, "帝愛信託:利息", income=gain, kind="trust_interest")

            # 端数（按分の切り捨て）は手元へ戻し、未払いの小数は次回へ持ち越す
            if paid > credited:
                cash = self.bank_sync_for(gid, now)
                self.bank_set_for(gid, cash["cash"] + paid - credited, cash["loaned"])
            carry = max(0.0, due - int(due)) + (paid - credited if total > 0 else 0)
            self._conn.execute(
                "INSERT INTO teiai_trust (guild_id, trust_ts, accrued, paid) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET trust_ts=excluded.trust_ts, "
                "accrued=excluded.accrued, paid=paid+excluded.paid",
                (gid, now, carry, credited),
            )
            return credited

    def charge_gains_tax_for(self, guild_id: int, user_id: int, profit: int, taxable: bool, what: str) -> int:
        """利益 profit にかかる税を、帝愛の銀行（預金者の利回り）へ納める。納めた額を返す。"""
        tax = market_mod.capital_gains_tax(profit) if taxable else 0
        if tax > 0:
            self._account.ledger(
                guild_id,
                user_id,
                0,
                self._account.raw_balance(guild_id, user_id),
                f"帝愛のキャピタルゲイン税（{what} 利益{profit:,} → 税{tax:,}）",
            )
            self.bank_income_for(guild_id, tax, self._now())
        return tax
