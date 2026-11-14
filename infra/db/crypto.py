"""証券の現物保有・売買（Economy に混ぜ込む部品）。"""

from typing import Optional

from domain import market as market_mod
from domain import teiai
from domain.errors import Failure, fail
from domain.market import DEFAULT
from domain.types import CryptoResult
from infra.db.base import EcoBase


class CryptoMixin(EcoBase):
    """現物証券の保有照会・売買・キャピタルゲイン課税。"""

    def crypto_holding(self, user_id: int, symbol: str = DEFAULT) -> tuple:
        """(保有口数, 買うのにかかった総額)。読み取り専用。"""
        with self._lock:
            r = self._conn.execute(
                "SELECT units, cost FROM crypto WHERE guild_id=? AND user_id=? AND symbol=?",
                (self._g(), user_id, symbol),
            ).fetchone()
            return (r["units"], r["cost"]) if r else (0, 0)

    def holdings(self, user_id: int) -> list:
        """持っている現物 [(銘柄, 口数, 買った総額), ...]（全銘柄）。"""
        with self._lock:
            return [
                (r["symbol"], r["units"], r["cost"])
                for r in self._conn.execute(
                    "SELECT symbol, units, cost FROM crypto WHERE guild_id=? AND user_id=? AND units>0 ORDER BY symbol",
                    (self._g(), user_id),
                )
            ]

    def crypto_holders(self, limit: int = 10, symbol: str = DEFAULT) -> list:
        """このサーバーのその銘柄の保有者 [(user_id, 口数, 買った総額), ...]。買った額の大きい順。"""
        with self._lock:
            return [
                (r["user_id"], r["units"], r["cost"])
                for r in self._conn.execute(
                    "SELECT user_id, units, cost FROM crypto WHERE guild_id=? AND symbol=? AND units>0 "
                    "ORDER BY cost DESC, user_id LIMIT ?",
                    (self._g(), symbol, limit),
                )
            ]

    def crypto_buy(self, user_id: int, amount: int, price: float, symbol: str = DEFAULT) -> CryptoResult:
        """amount ペリカの範囲で、price の価格で買えるだけ買う（手数料込み）。"""
        with self._lock, self._conn:
            self._ensure(user_id)
            if amount <= 0 or price <= 0:
                return CryptoResult(False, fail("crypto_invalid_amount"))
            if reason := self._pin_reason(symbol, "buy_spot"):
                return CryptoResult(False, reason, balance=self._balance(user_id))
            if self._debt(user_id) > 0:
                return CryptoResult(
                    False,
                    fail("debt_blocks_buy", debt=self._debt(user_id)),
                    balance=self._balance(user_id),
                )
            if self._balance(user_id) < amount:
                return CryptoResult(
                    False,
                    fail("crypto_insufficient_balance", balance=self._balance(user_id)),
                    balance=self._balance(user_id),
                )
            units, total, fee = market_mod.buy_quote(amount, price)
            if units < 1:
                return CryptoResult(False, fail("crypto_amount_too_small"), balance=self._balance(user_id))
            self._apply(
                user_id,
                -total,
                f"{market_mod.BY_KEY[symbol].name}購入:{market_mod.lots(units)}@{market_mod.fmt(price)}",
            )
            self._conn.execute(
                "INSERT INTO crypto (guild_id, user_id, symbol, units, cost) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id, symbol) DO UPDATE SET units=units+excluded.units, cost=cost+excluded.cost",
                (self._g(), user_id, symbol, units, total),
            )
            return CryptoResult(True, units=units, money=total, fee=fee, balance=self._balance(user_id))

    def crypto_sell(self, user_id: int, units: Optional[int], price: float, symbol: str = DEFAULT) -> CryptoResult:
        """units 口を売る（None なら全部）。受け取る額は手数料を引いたもの。"""
        with self._lock, self._conn:
            if reason := self._pin_reason(symbol, "sell_spot"):
                return CryptoResult(False, reason)
            reason = self._collateral_drop_reason(user_id, symbol, units, price)
            if reason:
                return CryptoResult(False, reason)
            return self._crypto_sell_locked(user_id, units, price, symbol, taxable=True)

    def _collateral_drop_reason(
        self, user_id: int, symbol: str, units: Optional[int], price: float
    ) -> Optional[Failure]:
        """現物を売ると、担保が減って維持率が OPEN_RATIO を下回るなら Failure。信用取引がなければ None。"""
        have, _cost = self.crypto_holding(user_id, symbol)
        if have <= 0 or not self._margin_rows(user_id):
            return None
        sold = have if units is None else min(units, have)
        drop = market_mod.sell_quote(sold, price)[0] * market_mod.COLLATERAL_HAIRCUT["spot"]
        after = self._margin_status_locked(user_id, {**self.latest_prices(), symbol: price}, drop_collateral=drop)
        if after["ratio"] is not None and after["ratio"] < market_mod.OPEN_RATIO:
            return fail("crypto_collateral_locked", ratio=after["ratio"], open_ratio=market_mod.OPEN_RATIO)
        return None

    def _gains_tax(self, user_id: int, profit: int, taxable: bool, what: str) -> int:
        """利益 profit にかかる税を、帝愛の銀行（預金者の利回り）へ納める。納めた額を返す。強制執行・ロスカットでは取らない（taxable=False）。"""
        tax = market_mod.capital_gains_tax(profit) if taxable else 0
        if tax > 0:
            self._ledger(
                user_id, 0, self._balance(user_id), f"帝愛のキャピタルゲイン税（{what} 利益{profit:,} → 税{tax:,}）"
            )
            self._bank_income(tax, teiai.TAX_YIELD_SHARE)  # 預金者に分配する（預金者がいなければ、銀行の手元に入る）
        return tax

    def _crypto_sell_locked(
        self, user_id: int, units: Optional[int], price: float, symbol: str = DEFAULT, taxable: bool = False
    ) -> CryptoResult:
        """crypto_sell の本体（呼び出し側が、ロックとトランザクションの中で使う）。taxable=True なら、利益に税がかかる。"""
        name = market_mod.BY_KEY[symbol].name
        have, cost = self.crypto_holding(user_id, symbol)
        if have <= 0:
            return CryptoResult(False, fail("crypto_not_held", name=name))
        units = have if units is None else units
        if units < 1 or units > have:
            return CryptoResult(False, fail("crypto_sell_range", max_lots=market_mod.lots(have)))
        proceeds, fee = market_mod.sell_quote(units, price)
        if proceeds < 1 and units != have:
            return CryptoResult(False, fail("crypto_proceeds_too_small"))
        removed = (
            cost if units == have else round(cost * units / have)
        )  # 端数の按分は四捨五入（切り捨てだと取得額が少しずつ残る）
        self._ensure(user_id)
        tax = self._gains_tax(user_id, proceeds - removed, taxable, f"{name}売却")
        self._apply(user_id, proceeds - tax, f"{name}売却:{market_mod.lots(units)}@{market_mod.fmt(price)}")
        if units == have:
            self._conn.execute(
                "DELETE FROM crypto WHERE guild_id=? AND user_id=? AND symbol=?", (self._g(), user_id, symbol)
            )
        else:
            self._conn.execute(
                "UPDATE crypto SET units=units-?, cost=cost-? WHERE guild_id=? AND user_id=? AND symbol=?",
                (units, removed, self._g(), user_id, symbol),
            )
        return CryptoResult(
            True,
            units=units,
            money=proceeds - tax,
            fee=fee,
            balance=self._balance(user_id),
            profit=proceeds - removed,
            tax=tax,
        )
