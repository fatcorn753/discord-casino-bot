"""信用取引（空売り・信用買い）・クロスマージン・金利・ロスカット。"""

import logging
import math
from collections.abc import Mapping
from typing import Any

from domain import market as market_mod
from domain.errors import Failure, fail
from domain.market import DEFAULT
from domain.ports.bridges import (
    GainsTaxCollector,
    IBankCashFlow,
    IDebtOps,
    INoticeSink,
    ISpotCollateralReader,
    NoOpDebtOps,
    NoOpNoticeSink,
    NoOpSpotCollateralReader,
)
from domain.ports.contracts import MarginPosition, MarginStatus
from domain.ports.repos import IMarginRepo
from domain.types import CryptoResult
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo, validate_guild_id
from infra.db.market import MarketDataRepo

_log = logging.getLogger(__name__)


class MarginRepo(ScopedRepo, IMarginRepo):
    """空売り・信用買い・維持率管理・信用金利・連鎖ロスカット。"""

    SHORT_LEVERAGES = market_mod.SHORT_LEVERAGES
    MARGIN_LEVERAGES = SHORT_LEVERAGES
    SIDE_NAME = market_mod.SIDE_NAME

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        market: MarketDataRepo,
        bank: IBankCashFlow,
        tax: GainsTaxCollector,
        notices: INoticeSink | None = None,
        crypto: ISpotCollateralReader | None = None,
        debt: IDebtOps | None = None,
        guild_id: int = 0,
        strict: bool = False,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._market = market
        self._bank = bank
        self._tax = tax
        self._notices: INoticeSink = notices or NoOpNoticeSink()
        self._crypto: ISpotCollateralReader = crypto or NoOpSpotCollateralReader()
        self._debt: IDebtOps = debt or NoOpDebtOps()

    def _margin_position_for(self, guild_id: int, user_id: int, symbol: str, side: str) -> MarginPosition:
        r = self._one(
            "SELECT units, entry_value, margin FROM margin_positions "
            "WHERE guild_id=? AND user_id=? AND symbol=? AND side=?",
            (guild_id, user_id, symbol, side),
        )
        return (r["units"], r["entry_value"], r["margin"]) if r else (0, 0.0, 0)

    def margin_position(self, user_id: int, symbol: str, side: str) -> MarginPosition:
        """(口数, 建てた総額, 担保)。建てていなければ (0, 0.0, 0)。"""
        with self._lock:
            return self._margin_position_for(self._g(), user_id, symbol, side)

    def short_position(self, user_id: int, symbol: str = DEFAULT) -> MarginPosition:
        return self.margin_position(user_id, symbol, "short")

    def long_position(self, user_id: int, symbol: str = DEFAULT) -> MarginPosition:
        return self.margin_position(user_id, symbol, "long")

    def margin_positions_for(self, guild_id: int, user_id: int) -> list[tuple[str, str, int, float, int]]:
        """建てている信用取引すべて [(side, 銘柄, 口数, 建てた総額, 担保), ...]（空売り・信用買いの両方）。"""
        return [
            (r["side"], r["symbol"], r["units"], r["entry_value"], r["margin"])
            for r in self._all(
                "SELECT side, symbol, units, entry_value, margin FROM margin_positions "
                "WHERE guild_id=? AND user_id=? AND units>0 "
                "ORDER BY CASE side WHEN 'short' THEN 0 ELSE 1 END, symbol",
                (guild_id, user_id),
            )
        ]

    def margin_positions(self, user_id: int) -> list[tuple[str, str, int, float, int]]:
        with self._lock:
            return self.margin_positions_for(self._g(), user_id)

    def short_positions(self, user_id: int) -> list[tuple[str, int, float, int]]:
        """建てている空売り [(銘柄, 口数, 建てた総額, 担保), ...]（全銘柄）。"""
        return [row[1:] for row in self.margin_positions(user_id) if row[0] == "short"]

    def long_positions(self, user_id: int) -> list[tuple[str, int, float, int]]:
        """建てている信用買い [(銘柄, 口数, 建てた総額, 担保), ...]（全銘柄）。"""
        return [row[1:] for row in self.margin_positions(user_id) if row[0] == "long"]

    def short_holders(self, limit: int = 10, symbol: str = DEFAULT) -> list[tuple[int, int, int]]:
        """[(ユーザーID, 口数, 建てた総額)] 建てている口数が多い順。チャートの表示用。"""
        with self._lock:
            return [
                (r["user_id"], r["units"], r["entry_value"])
                for r in self._all(
                    "SELECT user_id, units, entry_value FROM margin_positions "
                    "WHERE guild_id=? AND symbol=? AND side='short' AND units>0 "
                    "ORDER BY units DESC, user_id LIMIT ?",
                    (self._g(), symbol, limit),
                )
            ]

    def margin_equity(self) -> dict[int, int]:
        """このサーバーの {ユーザーID: 信用取引の評価額（担保 + 含み損益。建玉ごとに、0未満にはしない。全銘柄・両方向の合計）}。総資産に足す。"""
        with self._lock:
            out: dict[int, int] = {}
            self.collect_values(self._g(), out)
            return out

    def collect_values(self, guild_id: int, totals: dict[int, int]) -> None:
        with self._lock:
            prices = self._market.latest_prices()
            rows = self._all(
                "SELECT user_id, symbol, side, units, entry_value, margin FROM margin_positions WHERE guild_id=?",
                (guild_id,),
            )
            for r in rows:
                if r["symbol"] not in prices:
                    continue
                pnl = market_mod.margin_pnl(r["side"], r["units"], r["entry_value"], prices[r["symbol"]])
                totals[r["user_id"]] = totals.get(r["user_id"], 0) + max(0, r["margin"] + pnl)

    def _collateral_value_for(self, guild_id: int, user_id: int, prices: Mapping[str, float]) -> float:
        """代用有価証券の評価（現物は売った手取り×掛目、債券は元本×掛目）。信用取引の担保には数えない。"""
        hair = market_mod.COLLATERAL_HAIRCUT
        total = 0.0
        for symbol, units, _cost in self._crypto.holdings_for(guild_id, user_id):
            if symbol in prices:
                total += market_mod.sell_quote(units, prices[symbol])[0] * hair["spot"]
        for r in self._all(
            "SELECT kind, SUM(principal) AS p FROM bonds WHERE guild_id=? AND user_id=? AND status='active' GROUP BY kind",
            (guild_id, user_id),
        ):
            total += r["p"] * hair.get(r["kind"], 0.0)
        return total

    def _margin_status_locked_for(
        self,
        guild_id: int,
        user_id: int,
        prices: Mapping[str, float],
        add: tuple[str, str, float, float] | None = None,
        drop_collateral: float = 0.0,
    ) -> MarginStatus:
        """口座の維持率。add=(side, 銘柄, 建玉額, 預ける担保) を足した場合、drop_collateral を担保から引いた場合の仮の値も出せる。"""
        exposure: dict[str, float] = {}
        equity = 0.0
        for side, symbol, units, entry, margin in self.margin_positions_for(guild_id, user_id):
            if symbol not in prices:
                continue
            now_val = units * prices[symbol] / market_mod.UNIT
            exposure[symbol] = exposure.get(symbol, 0.0) + (now_val if side == "long" else -now_val)
            equity += margin + market_mod.margin_pnl(side, units, entry, prices[symbol])
        if add is not None:
            side, symbol, notional, margin = add
            exposure[symbol] = exposure.get(symbol, 0.0) + (notional if side == "long" else -notional)
            equity += margin
        collateral = max(0.0, self._collateral_value_for(guild_id, user_id, prices) - drop_collateral)
        equity += collateral
        required = market_mod.required_margin(exposure)
        return {
            "equity": equity,
            "required": required,
            "collateral": collateral,
            "ratio": equity / required if required > 0 else None,
        }

    def margin_status(self, user_id: int, prices: Mapping[str, float] | None = None) -> MarginStatus:
        """口座の維持率 {equity, required, collateral, ratio}。建玉がなければ ratio は None。読み取り専用。"""
        with self._lock:
            return self._margin_status_locked_for(self._g(), user_id, prices or self._market.latest_prices())

    def margin_ratio_for(self, guild_id: int, user_id: int) -> float | None:
        with self._lock:
            return self._margin_status_locked_for(guild_id, user_id, self._market.latest_prices())["ratio"]

    # --- IMarginCollateralGuard --- #
    def check_spot_sell(
        self, guild_id: int, user_id: int, symbol: str, units: int | None, have: int, price: float
    ) -> Failure | None:
        if have <= 0 or not self.margin_positions_for(guild_id, user_id):
            return None
        sold = have if units is None else min(units, have)
        drop = market_mod.sell_quote(sold, price)[0] * market_mod.COLLATERAL_HAIRCUT["spot"]
        after = self._margin_status_locked_for(
            guild_id, user_id, {**self._market.latest_prices(), symbol: price}, drop_collateral=drop
        )
        if after["ratio"] is not None and after["ratio"] < market_mod.OPEN_RATIO:
            return fail("crypto_collateral_locked", ratio=after["ratio"], open_ratio=market_mod.OPEN_RATIO)
        return None

    def check_bond_cancel(self, guild_id: int, user_id: int, kind: str, principal: int) -> Failure | None:
        if not self.margin_positions_for(guild_id, user_id):
            return None
        drop = principal * market_mod.COLLATERAL_HAIRCUT.get(kind, 0.0)
        after = self._margin_status_locked_for(guild_id, user_id, self._market.latest_prices(), drop_collateral=drop)
        if after["ratio"] is not None and after["ratio"] < market_mod.OPEN_RATIO:
            return fail("crypto_collateral_locked", ratio=after["ratio"], open_ratio=market_mod.OPEN_RATIO)
        return None

    def _margin_open(
        self, user_id: int, side: str, units: int, price: float, leverage: int, symbol: str
    ) -> CryptoResult:
        """units 口を建てる。担保 = 総額 ÷ レバレッジ（切り上げ）を、残高から預かる。口座の維持率が OPEN_RATIO を下回る建玉は受け付けない。"""
        label = self.SIDE_NAME[side]
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            if leverage not in self.MARGIN_LEVERAGES or units < 1 or price <= 0:
                return CryptoResult(False, fail("invalid_spec"))
            if side == "long" and (reason := self._market.pin_reason(symbol, "open_long")):
                return CryptoResult(False, reason, balance=self._account.raw_balance(gid, user_id))
            debt = self._account.debt(gid, user_id)
            if debt > 0:
                return CryptoResult(
                    False,
                    fail("margin_debt_blocks", debt=debt, label=label),
                    balance=self._account.raw_balance(gid, user_id),
                )
            other = "long" if side == "short" else "short"
            if self._margin_position_for(gid, user_id, symbol, other)[0] > 0:
                return CryptoResult(
                    False,
                    fail(
                        "margin_opposite_open",
                        name=market_mod.BY_KEY[symbol].name,
                        other_label=self.SIDE_NAME[other],
                    ),
                    balance=self._account.raw_balance(gid, user_id),
                )
            notional = units * price / market_mod.UNIT
            margin = math.ceil(round(notional / leverage, 6))
            bal = self._account.raw_balance(gid, user_id)
            if margin < market_mod.SHORT_MIN_MARGIN:
                return CryptoResult(
                    False,
                    fail(
                        "margin_min_collateral",
                        min_margin=market_mod.SHORT_MIN_MARGIN,
                        leverage=leverage,
                        min_total=(market_mod.SHORT_MIN_MARGIN - 1) * leverage + 1,
                        balance=bal,
                    ),
                    balance=bal,
                )
            if bal < margin:
                return CryptoResult(
                    False,
                    fail("margin_insufficient_collateral", need=margin, balance=bal),
                    balance=bal,
                )
            prices = {**self._market.latest_prices(), symbol: price}
            after = self._margin_status_locked_for(gid, user_id, prices, add=(side, symbol, notional, margin))
            if after["ratio"] is not None and after["ratio"] < market_mod.OPEN_RATIO:
                return CryptoResult(
                    False,
                    fail("margin_ratio_too_low", ratio=after["ratio"], open_ratio=market_mod.OPEN_RATIO),
                    balance=bal,
                )
            name = market_mod.BY_KEY[symbol].name
            self._account.apply(
                gid,
                user_id,
                -margin,
                f"{name}{label}担保:{market_mod.lots(units)}@{market_mod.fmt(price)}（{leverage}倍）",
            )
            self._conn.execute(
                "INSERT INTO margin_positions (guild_id, user_id, symbol, side, units, entry_value, margin) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(guild_id, user_id, symbol, side) DO UPDATE SET "
                "units=units+excluded.units, entry_value=entry_value+excluded.entry_value, margin=margin+excluded.margin",
                (gid, user_id, symbol, side, units, notional, margin),
            )
            return CryptoResult(True, units=units, money=margin, balance=self._account.raw_balance(gid, user_id))

    def short_open(self, user_id: int, units: int, price: float, leverage: int, symbol: str = DEFAULT) -> CryptoResult:
        """units 口を売り建てる。"""
        return self._margin_open(user_id, "short", units, price, leverage, symbol)

    def long_open(self, user_id: int, units: int, price: float, leverage: int, symbol: str = DEFAULT) -> CryptoResult:
        """units 口を信用で買い建てる。"""
        return self._margin_open(user_id, "long", units, price, leverage, symbol)

    def margin_close_locked_for(
        self,
        guild_id: int,
        user_id: int,
        side: str,
        units: int | None,
        price: float,
        label: str,
        symbol: str = DEFAULT,
        owe: bool = False,
        taxable: bool = False,
    ) -> CryptoResult:
        """建玉を units 口、決済する（None なら全部）。担保 + 損益が戻る。owe=True なら、損失が担保を超えた分は、強制執行と借金で払わせる。"""
        have, entry_value, margin = self._margin_position_for(guild_id, user_id, symbol, side)
        if have <= 0:
            return CryptoResult(False, fail("margin_not_open", label=self.SIDE_NAME[side]))
        units = have if units is None else units
        if units < 1 or units > have:
            return CryptoResult(False, fail("margin_close_range", max_lots=market_mod.lots(have)))
        whole = units == have
        part_margin = margin if whole else round(margin * units / have)
        part_entry = entry_value if whole else entry_value * units / have
        pnl = market_mod.margin_pnl(side, units, part_entry, price)
        payout = max(0, part_margin + pnl)
        tax = self._tax(guild_id, user_id, pnl, taxable, self.SIDE_NAME[side] + "決済")
        payout -= tax
        if payout:
            self._account.apply(guild_id, user_id, payout, f"{label}:{market_mod.lots(units)}@{market_mod.fmt(price)}")
        if whole:
            self._conn.execute(
                "DELETE FROM margin_positions WHERE guild_id=? AND user_id=? AND symbol=? AND side=?",
                (guild_id, user_id, symbol, side),
            )
        else:
            self._conn.execute(
                "UPDATE margin_positions SET units=units-?, entry_value=entry_value-?, margin=margin-? "
                "WHERE guild_id=? AND user_id=? AND symbol=? AND side=?",
                (units, part_entry, part_margin, guild_id, user_id, symbol, side),
            )
        shortfall = -(part_margin + pnl)
        if owe and shortfall > 0:  # 担保を超える損失は、手元の現金や現物から取り立てて、足りなければ借金になる
            self._debt.owe_for(guild_id, user_id, shortfall, f"{self.SIDE_NAME[side]}の損失")
        return CryptoResult(
            True,
            units=units,
            money=payout,
            balance=self._account.raw_balance(guild_id, user_id),
            profit=pnl if owe else payout + tax - part_margin,
            tax=tax,
        )

    def margin_close(
        self, user_id: int, side: str, units: int | None, price: float, symbol: str = DEFAULT
    ) -> CryptoResult:
        """units 口を決済する（None なら全部）。担保 + 損益が戻る。担保を超えた損失は、借金になりうる。"""
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            if reason := self._market.pin_reason(symbol, "close_long" if side == "long" else "cover_short"):
                return CryptoResult(False, reason, balance=self._account.raw_balance(gid, user_id))
            return self.margin_close_locked_for(
                gid, user_id, side, units, price, f"{self.SIDE_NAME[side]}決済", symbol, owe=True, taxable=True
            )

    def short_close(self, user_id: int, units: int | None, price: float, symbol: str = DEFAULT) -> CryptoResult:
        return self.margin_close(user_id, "short", units, price, symbol)

    def long_close(self, user_id: int, units: int | None, price: float, symbol: str = DEFAULT) -> CryptoResult:
        return self.margin_close(user_id, "long", units, price, symbol)

    def _losscut_user_locked_for(self, guild_id: int, user_id: int, prices: Mapping[str, float]) -> list[Any]:
        """維持率が LOSSCUT_RATIO を下回る間、含み損の大きい建玉から順に、全部決済する（連鎖ロスカット）。[(銘柄, 向き, 損失)]。"""
        cut = []
        while True:
            status = self._margin_status_locked_for(guild_id, user_id, prices)
            if status["ratio"] is None or status["ratio"] >= market_mod.LOSSCUT_RATIO:
                break
            rows = [r for r in self.margin_positions_for(guild_id, user_id) if r[1] in prices]
            if not rows:
                break
            side, symbol, units, entry, _margin = min(
                rows, key=lambda r: (market_mod.margin_pnl(r[0], r[2], r[3], prices[r[1]]), r[1], r[0])
            )
            res = self.margin_close_locked_for(
                guild_id, user_id, side, None, prices[symbol], f"{self.SIDE_NAME[side]}ロスカット", symbol, owe=True
            )
            if not res.ok:
                break
            self._notices.note_for(
                guild_id,
                user_id,
                "margin_losscut",
                losscut_ratio=market_mod.LOSSCUT_RATIO,
                symbol_name=market_mod.BY_KEY[symbol].name,
                side_label=self.SIDE_NAME[side],
                profit=res.profit,
            )
            cut.append((symbol, side, -res.profit))
        return cut

    def margin_cut_map(self, prices: Mapping[str, float] | None = None) -> list[tuple[str, str, float, int]]:
        """このサーバーの、信用建玉のロスカット水準の地図 [(銘柄, 向き, ロスカットされる価格, 建玉額), ...]。"""
        out = []
        with self._lock:
            gid = self._g()
            prices = prices or self._market.latest_prices()
            uids = {
                r["user_id"]
                for r in self._all("SELECT DISTINCT user_id FROM margin_positions WHERE guild_id=? AND units>0", (gid,))
            }
            positions = [
                (uid, side, symbol, units)
                for uid in uids
                for side, symbol, units, _entry, _margin in self.margin_positions_for(gid, uid)
                if symbol in prices
            ]
            for uid, side, symbol, units in positions:
                spot = prices[symbol]
                notional = units * spot / market_mod.UNIT
                sign = -1 if side == "long" else 1

                def broken(
                    move: float, uid: int = uid, symbol: str = symbol, spot: float = spot, sign: int = sign
                ) -> bool:
                    status = self._margin_status_locked_for(gid, uid, {**prices, symbol: spot * (1 + sign * move)})
                    return status["ratio"] is not None and status["ratio"] < market_mod.LOSSCUT_RATIO

                if not broken(0.5):
                    continue
                lo, hi = 0.0, 0.5
                for _ in range(18):
                    mid = (lo + hi) / 2
                    lo, hi = (lo, mid) if broken(mid) else (mid, hi)
                out.append((symbol, side, spot * (1 + sign * hi), notional))
        return out

    def margin_interest(self, now: float) -> list[tuple[int, int, str, str, int]]:
        """全サーバーの建玉に、信用金利（信用買いは借入金利、空売りは貸株料）を数えて、貯まった分を担保から引く。[(サーバー, ユーザー, 銘柄, 向き, 引いた額)]。"""
        done: list[tuple[int, int, str, str, int]] = []
        with self._db.tx():
            rows = self._all(
                "SELECT guild_id, user_id, symbol, side, entry_value, margin, interest_at, accrued FROM margin_positions"
            )
            for r in rows:
                side = r["side"]
                rate = market_mod.BORROW_RATE_PER_DAY[side]
                key = (r["guild_id"], r["user_id"], r["symbol"], side)
                if r["interest_at"] <= 0:
                    self._conn.execute(
                        "UPDATE margin_positions SET interest_at=? WHERE guild_id=? AND user_id=? AND symbol=? AND side=?",
                        (now, *key),
                    )
                    continue
                base = market_mod.margin_interest_base(side, r["entry_value"], r["margin"])
                accrued = r["accrued"] + base * rate * max(0.0, now - r["interest_at"]) / 86400.0
                charge = 0
                if accrued >= market_mod.INTEREST_MIN_CHARGE:
                    charge = min(int(accrued), r["margin"])
                    accrued -= int(accrued)
                self._conn.execute(
                    "UPDATE margin_positions SET margin=margin-?, accrued=?, interest_at=? "
                    "WHERE guild_id=? AND user_id=? AND symbol=? AND side=?",
                    (charge, accrued, now, *key),
                )
                if charge <= 0:
                    continue
                try:
                    gid = validate_guild_id(r["guild_id"])
                except ValueError:
                    continue
                name = market_mod.BY_KEY[r["symbol"]].name
                self._account.ledger(
                    gid,
                    r["user_id"],
                    0,
                    self._account.raw_balance(gid, r["user_id"]),
                    f"帝愛の信用金利（{self.SIDE_NAME[side]} {name}）担保から-{charge:,}",
                )
                self._bank.bank_income_for(gid, charge, now)
                done.append((gid, r["user_id"], r["symbol"], side, charge))
        return done

    def margin_losscut(self, prices: Mapping[str, float]) -> list[tuple[int, int, str, str, int]]:
        """全サーバーの口座を調べ、維持率が LOSSCUT_RATIO を下回る口座を、連鎖ロスカットする。"""
        done = []
        with self._db.tx():
            accounts = sorted(
                {
                    (r["guild_id"], r["user_id"])
                    for r in self._all("SELECT DISTINCT guild_id, user_id FROM margin_positions")
                }
            )
            for guild_id, user_id in accounts:
                try:
                    gid = validate_guild_id(guild_id)
                except ValueError:
                    _log.warning("出どころ不明の建玉は、ロスカットできません: guild=%s user=%s", guild_id, user_id)
                    continue
                for symbol, side, loss in self._losscut_user_locked_for(gid, user_id, prices):
                    done.append((gid, user_id, symbol, side, loss))
        return done
