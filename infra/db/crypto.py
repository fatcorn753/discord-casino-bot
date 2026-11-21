"""証券の現物保有・売買。"""


from domain import market as market_mod
from domain.errors import fail
from domain.market import DEFAULT
from domain.ports.bridges import GainsTaxCollector, IMarginCollateralGuard, NoOpMarginCollateralGuard
from domain.ports.contracts import SpotHolding
from domain.ports.repos import ICryptoRepo
from domain.types import CryptoResult
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo
from infra.db.market import MarketDataRepo


class CryptoRepo(ScopedRepo, ICryptoRepo):
    """現物証券の保有照会・売買・キャピタルゲイン課税。"""

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        market: MarketDataRepo,
        tax: GainsTaxCollector,
        collateral_guard: IMarginCollateralGuard | None = None,
        guild_id: int = 0,
        strict: bool = False,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._market = market
        self._tax = tax
        self._collateral_guard: IMarginCollateralGuard = collateral_guard or NoOpMarginCollateralGuard()

    def _crypto_holding_for(self, guild_id: int, user_id: int, symbol: str = DEFAULT) -> SpotHolding:
        r = self._one(
            "SELECT units, cost FROM crypto WHERE guild_id=? AND user_id=? AND symbol=?",
            (guild_id, user_id, symbol),
        )
        return (r["units"], r["cost"]) if r else (0, 0)

    def crypto_holding(self, user_id: int, symbol: str = DEFAULT) -> SpotHolding:
        """(保有口数, 買うのにかかった総額)。読み取り専用。"""
        with self._lock:
            return self._crypto_holding_for(self._g(), user_id, symbol)

    def holdings_for(self, guild_id: int, user_id: int) -> list[tuple[str, int, int]]:
        """持っている現物 [(銘柄, 口数, 買った総額), ...]（全銘柄）。"""
        with self._lock:
            return [
                (r["symbol"], r["units"], r["cost"])
                for r in self._all(
                    "SELECT symbol, units, cost FROM crypto WHERE guild_id=? AND user_id=? AND units>0 ORDER BY symbol",
                    (guild_id, user_id),
                )
            ]

    def holdings(self, user_id: int) -> list[tuple[str, int, int]]:
        return self.holdings_for(self._g(), user_id)

    def crypto_holders(self, limit: int = 10, symbol: str = DEFAULT) -> list[tuple[int, int, int]]:
        """このサーバーのその銘柄の保有者 [(user_id, 口数, 買った総額), ...]。買った額の大きい順。"""
        with self._lock:
            return [
                (r["user_id"], r["units"], r["cost"])
                for r in self._all(
                    "SELECT user_id, units, cost FROM crypto WHERE guild_id=? AND symbol=? AND units>0 "
                    "ORDER BY cost DESC, user_id LIMIT ?",
                    (self._g(), symbol, limit),
                )
            ]

    def crypto_values(self) -> dict[int, int]:
        """このサーバーの {ユーザーID: 現物（全銘柄）の評価額}。売ったときの手取りで、価格がまだない銘柄は数えない。"""
        with self._lock:
            values: dict[int, int] = {}
            self.collect_values(self._g(), values)
            return values

    def collect_values(self, guild_id: int, totals: dict[int, int]) -> None:
        with self._lock:
            prices = self._market.latest_prices()
            for r in self._all("SELECT user_id, symbol, units FROM crypto WHERE guild_id=? AND units>0", (guild_id,)):
                if r["symbol"] in prices:
                    totals[r["user_id"]] = (
                        totals.get(r["user_id"], 0) + market_mod.sell_quote(r["units"], prices[r["symbol"]])[0]
                    )

    def crypto_buy(self, user_id: int, amount: int, price: float, symbol: str = DEFAULT) -> CryptoResult:
        """amount ペリカの範囲で、price の価格で買えるだけ買う（手数料込み）。"""
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            if amount <= 0 or price <= 0:
                return CryptoResult(False, fail("crypto_invalid_amount"))
            if reason := self._market.pin_reason(symbol, "buy_spot"):
                return CryptoResult(False, reason, balance=self._account.raw_balance(gid, user_id))
            debt = self._account.debt(gid, user_id)
            if debt > 0:
                return CryptoResult(
                    False,
                    fail("debt_blocks", debt=debt, verb="買えません"),
                    balance=self._account.raw_balance(gid, user_id),
                )
            bal = self._account.raw_balance(gid, user_id)
            if bal < amount:
                return CryptoResult(
                    False,
                    fail("crypto_insufficient_balance", balance=bal),
                    balance=bal,
                )
            units, total, fee = market_mod.buy_quote(amount, price)
            if units < 1:
                return CryptoResult(False, fail("crypto_amount_too_small"), balance=bal)
            self._account.apply(
                gid,
                user_id,
                -total,
                f"{market_mod.BY_KEY[symbol].name}購入:{market_mod.lots(units)}@{market_mod.fmt(price)}",
            )
            self._conn.execute(
                "INSERT INTO crypto (guild_id, user_id, symbol, units, cost) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id, symbol) DO UPDATE SET units=units+excluded.units, cost=cost+excluded.cost",
                (gid, user_id, symbol, units, total),
            )
            return CryptoResult(
                True, units=units, money=total, fee=fee, balance=self._account.raw_balance(gid, user_id)
            )

    def crypto_sell(self, user_id: int, units: int | None, price: float, symbol: str = DEFAULT) -> CryptoResult:
        """units 口を売る（None なら全部）。受け取る額は手数料を引いたもの。"""
        with self._db.tx():
            gid = self._g()
            if reason := self._market.pin_reason(symbol, "sell_spot"):
                return CryptoResult(False, reason)
            have, _ = self._crypto_holding_for(gid, user_id, symbol)
            if have > 0:
                reason = self._collateral_guard.check_spot_sell(gid, user_id, symbol, units, have, price)
                if reason:
                    return CryptoResult(False, reason)
            return self.crypto_sell_locked_for(gid, user_id, units, price, symbol, taxable=True)

    def crypto_sell_locked_for(
        self,
        guild_id: int,
        user_id: int,
        units: int | None,
        price: float,
        symbol: str = DEFAULT,
        taxable: bool = False,
    ) -> CryptoResult:
        """crypto_sell の本体（呼び出し側が、ロックとトランザクションの中で使う）。taxable=True なら、利益に税がかかる。"""
        name = market_mod.BY_KEY[symbol].name
        have, cost = self._crypto_holding_for(guild_id, user_id, symbol)
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
        self._account.ensure(guild_id, user_id)
        tax = self._tax(guild_id, user_id, proceeds - removed, taxable, f"{name}売却")
        self._account.apply(
            guild_id, user_id, proceeds - tax, f"{name}売却:{market_mod.lots(units)}@{market_mod.fmt(price)}"
        )
        if units == have:
            self._conn.execute(
                "DELETE FROM crypto WHERE guild_id=? AND user_id=? AND symbol=?", (guild_id, user_id, symbol)
            )
        else:
            self._conn.execute(
                "UPDATE crypto SET units=units-?, cost=cost-? WHERE guild_id=? AND user_id=? AND symbol=?",
                (units, removed, guild_id, user_id, symbol),
            )
        return CryptoResult(
            True,
            units=units,
            money=proceeds - tax,
            fee=fee,
            balance=self._account.raw_balance(guild_id, user_id),
            profit=proceeds - removed,
            tax=tax,
        )
