"""信用取引（空売り・信用買い）・クロスマージン・金利・ロスカット（Economy に混ぜ込む部品）。"""

import math
from typing import Optional

from domain import market as market_mod
from domain import teiai
from domain.errors import fail
from domain.market import DEFAULT
from domain.types import CryptoResult
from infra.db.base import EcoBase


class MarginMixin(EcoBase):
    """空売り・信用買い・維持率管理・信用金利・連鎖ロスカット。"""

    SHORT_LEVERAGES = (1, 2, 3, 5)
    MARGIN_LEVERAGES = SHORT_LEVERAGES
    SHORT_MIN_MARGIN = 10
    _MARGIN_TABLE = {"short": "crypto_short", "long": "crypto_long"}  # 表の名前は固定の文字列だけ（SQL に埋め込むため）
    SIDE_NAME = {"short": "空売り", "long": "信用買い"}

    def margin_position(self, user_id: int, symbol: str, side: str) -> tuple:
        """(口数, 建てた総額, 担保)。建てていなければ (0, 0.0, 0)。"""
        with self._lock:
            r = self._conn.execute(
                f"SELECT units, entry_value, margin FROM {self._MARGIN_TABLE[side]} "
                "WHERE guild_id=? AND user_id=? AND symbol=?",
                (self._g(), user_id, symbol),
            ).fetchone()
            return (r["units"], r["entry_value"], r["margin"]) if r else (0, 0.0, 0)

    def short_position(self, user_id: int, symbol: str = DEFAULT) -> tuple:
        return self.margin_position(user_id, symbol, "short")

    def long_position(self, user_id: int, symbol: str = DEFAULT) -> tuple:
        return self.margin_position(user_id, symbol, "long")

    def _margin_rows(self, user_id: int) -> list:
        """建てている信用取引すべて [(side, 銘柄, 口数, 建てた総額, 担保), ...]（空売り・信用買いの両方）。"""
        out = []
        for side, table in self._MARGIN_TABLE.items():
            for r in self._conn.execute(
                f"SELECT symbol, units, entry_value, margin FROM {table} WHERE guild_id=? AND user_id=? AND units>0 ORDER BY symbol",
                (self._g(), user_id),
            ):
                out.append((side, r["symbol"], r["units"], r["entry_value"], r["margin"]))
        return out

    def margin_positions(self, user_id: int) -> list:
        with self._lock:
            return self._margin_rows(user_id)

    def short_positions(self, user_id: int) -> list:
        """建てている空売り [(銘柄, 口数, 建てた総額, 担保), ...]（全銘柄）。"""
        return [row[1:] for row in self.margin_positions(user_id) if row[0] == "short"]

    def long_positions(self, user_id: int) -> list:
        """建てている信用買い [(銘柄, 口数, 建てた総額, 担保), ...]（全銘柄）。"""
        return [row[1:] for row in self.margin_positions(user_id) if row[0] == "long"]

    def short_holders(self, limit: int = 10, symbol: str = DEFAULT) -> list:
        """[(ユーザーID, 口数, 建てた総額)] 建てている口数が多い順。チャートの表示用。"""
        with self._lock:
            return [
                (r["user_id"], r["units"], r["entry_value"])
                for r in self._conn.execute(
                    "SELECT user_id, units, entry_value FROM crypto_short WHERE guild_id=? AND symbol=? AND units>0 "
                    "ORDER BY units DESC, user_id LIMIT ?",
                    (self._g(), symbol, limit),
                )
            ]

    def margin_equity(self) -> dict:
        """このサーバーの {ユーザーID: 信用取引の評価額（担保 + 含み損益。建玉ごとに、0未満にはしない。全銘柄・両方向の合計）}。総資産に足す。"""
        with self._lock:
            prices = self.latest_prices()
            out = {}
            for side, table in self._MARGIN_TABLE.items():
                for r in self._conn.execute(
                    f"SELECT user_id, symbol, units, entry_value, margin FROM {table} WHERE guild_id=?", (self._g(),)
                ):
                    if r["symbol"] in prices:
                        pnl = market_mod.margin_pnl(side, r["units"], r["entry_value"], prices[r["symbol"]])
                        out[r["user_id"]] = out.get(r["user_id"], 0) + max(0, r["margin"] + pnl)
            return out

    # --- クロスマージン: 口座ぜんたいの維持率 ---
    def _collateral_value(self, user_id: int, prices: dict) -> float:
        """代用有価証券の評価（現物は売った手取り×掛目、債券は元本×掛目）。信用取引の担保には数えない。"""
        hair = market_mod.COLLATERAL_HAIRCUT
        total = 0.0
        for symbol, units, _cost in self.holdings(user_id):
            if symbol in prices:
                total += market_mod.sell_quote(units, prices[symbol])[0] * hair["spot"]
        for r in self._conn.execute(
            "SELECT kind, SUM(principal) AS p FROM bonds WHERE guild_id=? AND user_id=? AND status='active' GROUP BY kind",
            (self._g(), user_id),
        ):
            total += r["p"] * hair.get(r["kind"], 0.0)
        return total

    def _margin_status_locked(
        self, user_id: int, prices: dict, add: Optional[tuple] = None, drop_collateral: float = 0.0
    ) -> dict:
        """口座の維持率。add=(side, 銘柄, 建玉額, 預ける担保) を足した場合、drop_collateral を担保から引いた場合の仮の値も出せる。

        有効証拠金 = Σ(担保 + 含み損益) + 代用有価証券の評価。必要証拠金 = market.required_margin(建玉額)。維持率 = 有効 ÷ 必要。
        建玉がなければ必要額は 0 で、維持率は None。
        """
        exposure, equity = {}, 0.0
        for side, symbol, units, entry, margin in self._margin_rows(user_id):
            if symbol not in prices:
                continue
            now = units * prices[symbol] / market_mod.UNIT
            exposure[symbol] = exposure.get(symbol, 0.0) + (now if side == "long" else -now)
            equity += margin + market_mod.margin_pnl(side, units, entry, prices[symbol])
        if add is not None:
            side, symbol, notional, margin = add
            exposure[symbol] = exposure.get(symbol, 0.0) + (notional if side == "long" else -notional)
            equity += margin
        collateral = max(0.0, self._collateral_value(user_id, prices) - drop_collateral)
        equity += collateral
        required = market_mod.required_margin(exposure)
        return {
            "equity": equity,
            "required": required,
            "collateral": collateral,
            "ratio": equity / required if required > 0 else None,
        }

    def margin_status(self, user_id: int, prices: Optional[dict] = None) -> dict:
        """口座の維持率 {equity, required, collateral, ratio}。建玉がなければ ratio は None。読み取り専用。"""
        with self._lock:
            return self._margin_status_locked(user_id, prices or self.latest_prices())

    def _margin_open(
        self, user_id: int, side: str, units: int, price: float, leverage: int, symbol: str
    ) -> CryptoResult:
        """units 口を建てる。担保 = 総額 ÷ レバレッジ（切り上げ）を、残高から預かる。口座の維持率が OPEN_RATIO を下回る建玉は受け付けない。"""
        label = self.SIDE_NAME[side]
        with self._lock, self._conn:
            self._ensure(user_id)
            if leverage not in self.MARGIN_LEVERAGES or units < 1 or price <= 0:
                return CryptoResult(False, fail("invalid_spec"))
            if side == "long" and (reason := self._pin_reason(symbol, "open_long")):
                return CryptoResult(False, reason, balance=self._balance(user_id))
            if self._debt(user_id) > 0:
                return CryptoResult(
                    False,
                    fail("margin_debt_blocks", debt=self._debt(user_id), label=label),
                    balance=self._balance(user_id),
                )
            other = "long" if side == "short" else "short"
            if self.margin_position(user_id, symbol, other)[0] > 0:
                return CryptoResult(
                    False,
                    fail(
                        "margin_opposite_open",
                        name=market_mod.BY_KEY[symbol].name,
                        other_label=self.SIDE_NAME[other],
                    ),
                    balance=self._balance(user_id),
                )
            notional = units * price / market_mod.UNIT
            margin = math.ceil(round(notional / leverage, 6))
            if margin < self.SHORT_MIN_MARGIN:
                return CryptoResult(
                    False,
                    fail("margin_min_collateral", min_margin=self.SHORT_MIN_MARGIN),
                    balance=self._balance(user_id),
                )
            if self._balance(user_id) < margin:
                return CryptoResult(
                    False,
                    fail("margin_insufficient_collateral", need=margin, balance=self._balance(user_id)),
                    balance=self._balance(user_id),
                )
            prices = {**self.latest_prices(), symbol: price}
            after = self._margin_status_locked(user_id, prices, add=(side, symbol, notional, margin))
            if after["ratio"] is not None and after["ratio"] < market_mod.OPEN_RATIO:
                return CryptoResult(
                    False,
                    fail("margin_ratio_too_low", ratio=after["ratio"], open_ratio=market_mod.OPEN_RATIO),
                    balance=self._balance(user_id),
                )
            name = market_mod.BY_KEY[symbol].name
            self._apply(
                user_id, -margin, f"{name}{label}担保:{market_mod.lots(units)}@{market_mod.fmt(price)}（{leverage}倍）"
            )
            self._conn.execute(
                f"INSERT INTO {self._MARGIN_TABLE[side]} (guild_id, user_id, symbol, units, entry_value, margin) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(guild_id, user_id, symbol) DO UPDATE SET units=units+excluded.units, "
                "entry_value=entry_value+excluded.entry_value, margin=margin+excluded.margin",
                (self._g(), user_id, symbol, units, notional, margin),
            )
            return CryptoResult(True, units=units, money=margin, balance=self._balance(user_id))

    def short_open(self, user_id: int, units: int, price: float, leverage: int, symbol: str = DEFAULT) -> CryptoResult:
        """units 口を売り建てる。"""
        return self._margin_open(user_id, "short", units, price, leverage, symbol)

    def long_open(self, user_id: int, units: int, price: float, leverage: int, symbol: str = DEFAULT) -> CryptoResult:
        """units 口を信用で買い建てる。"""
        return self._margin_open(user_id, "long", units, price, leverage, symbol)

    def _margin_close_locked(
        self,
        user_id: int,
        side: str,
        units: Optional[int],
        price: float,
        label: str,
        symbol: str = DEFAULT,
        owe: bool = False,
        taxable: bool = False,
    ) -> CryptoResult:
        """建玉を units 口、決済する（None なら全部）。担保 + 損益が戻る。owe=True なら、損失が担保を超えた分は、強制執行と借金で払わせる。

        taxable=True なら、利益（pnl > 0）に税がかかる（戻る額から引く）。
        """
        have, entry_value, margin = self.margin_position(user_id, symbol, side)
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
        tax = self._gains_tax(user_id, pnl, taxable, self.SIDE_NAME[side] + "決済")
        payout -= tax
        table = self._MARGIN_TABLE[side]
        if payout:
            self._apply(user_id, payout, f"{label}:{market_mod.lots(units)}@{market_mod.fmt(price)}")
        if whole:
            self._conn.execute(
                f"DELETE FROM {table} WHERE guild_id=? AND user_id=? AND symbol=?", (self._g(), user_id, symbol)
            )
        else:
            self._conn.execute(
                f"UPDATE {table} SET units=units-?, entry_value=entry_value-?, margin=margin-? "
                "WHERE guild_id=? AND user_id=? AND symbol=?",
                (units, part_entry, part_margin, self._g(), user_id, symbol),
            )
        shortfall = -(part_margin + pnl)
        if owe and shortfall > 0:  # 担保を超える損失は、手元の現金や現物から取り立て、足りなければ借金になる
            self._owe(user_id, shortfall, f"{self.SIDE_NAME[side]}の損失")
        return CryptoResult(
            True,
            units=units,
            money=payout,
            balance=self._balance(user_id),
            profit=pnl if owe else payout + tax - part_margin,
            tax=tax,
        )

    def _short_close_locked(
        self, user_id: int, units: Optional[int], price: float, label: str, symbol: str = DEFAULT
    ) -> CryptoResult:
        return self._margin_close_locked(user_id, "short", units, price, label, symbol)

    def margin_close(
        self, user_id: int, side: str, units: Optional[int], price: float, symbol: str = DEFAULT
    ) -> CryptoResult:
        """units 口を決済する（None なら全部）。担保 + 損益が戻る。担保を超えた損失は、借金になりうる。"""
        with self._lock, self._conn:
            self._ensure(user_id)
            if reason := self._pin_reason(symbol, "close_long" if side == "long" else "cover_short"):
                return CryptoResult(False, reason, balance=self._balance(user_id))
            return self._margin_close_locked(
                user_id, side, units, price, f"{self.SIDE_NAME[side]}決済", symbol, owe=True, taxable=True
            )

    def short_close(self, user_id: int, units: Optional[int], price: float, symbol: str = DEFAULT) -> CryptoResult:
        return self.margin_close(user_id, "short", units, price, symbol)

    def long_close(self, user_id: int, units: Optional[int], price: float, symbol: str = DEFAULT) -> CryptoResult:
        return self.margin_close(user_id, "long", units, price, symbol)

    def _losscut_user_locked(self, user_id: int, prices: dict) -> list:
        """維持率が LOSSCUT_RATIO を下回る間、含み損の大きい建玉から順に、全部決済する（連鎖ロスカット）。[(銘柄, 向き, 損失)]。"""
        cut = []
        while True:
            status = self._margin_status_locked(user_id, prices)
            if status["ratio"] is None or status["ratio"] >= market_mod.LOSSCUT_RATIO:
                break
            rows = [r for r in self._margin_rows(user_id) if r[1] in prices]
            if not rows:
                break
            side, symbol, units, entry, _margin = min(
                rows, key=lambda r: (market_mod.margin_pnl(r[0], r[2], r[3], prices[r[1]]), r[1], r[0])
            )
            res = self._margin_close_locked(
                user_id, side, None, prices[symbol], f"{self.SIDE_NAME[side]}ロスカット", symbol, owe=True
            )
            if not res.ok:
                break
            self._note(
                user_id,
                "margin_losscut",
                losscut_ratio=market_mod.LOSSCUT_RATIO,
                symbol_name=market_mod.BY_KEY[symbol].name,
                side_label=self.SIDE_NAME[side],
                profit=res.profit,
            )
            cut.append((symbol, side, -res.profit))
        return cut

    def margin_cut_map(self, prices: Optional[dict] = None) -> list:
        """このサーバーの、信用建玉のロスカット水準の地図 [(銘柄, 向き, ロスカットされる価格, 建玉額), ...]。"""
        out = []
        with self._lock:
            prices = prices or self.latest_prices()
            for uid in {
                r["user_id"]
                for table in self._MARGIN_TABLE.values()
                for r in self._conn.execute(f"SELECT user_id FROM {table} WHERE guild_id=? AND units>0", (self._g(),))
            }:
                for side, symbol, units, _entry, _margin in self._margin_rows(uid):
                    if symbol not in prices:
                        continue
                    spot = prices[symbol]
                    notional = units * spot / market_mod.UNIT
                    sign = -1 if side == "long" else 1  # 買い建ては下へ、空売りは上へ動くと損

                    def broken(move: float) -> bool:
                        status = self._margin_status_locked(uid, {**prices, symbol: spot * (1 + sign * move)})
                        return status["ratio"] is not None and status["ratio"] < market_mod.LOSSCUT_RATIO

                    if not broken(0.5):
                        continue
                    lo, hi = 0.0, 0.5
                    for _ in range(18):
                        mid = (lo + hi) / 2
                        lo, hi = (lo, mid) if broken(mid) else (mid, hi)
                    out.append((symbol, side, spot * (1 + sign * hi), notional))
        return out

    def margin_interest(self, now: float) -> list:
        """全サーバーの建玉に、信用金利（信用買いは借入金利、空売りは貸株料）を数えて、貯まった分を担保から引く。[(サーバー, ユーザー, 銘柄, 向き, 引いた額)]。"""
        done = []
        with self._lock, self._conn:
            for side, table in self._MARGIN_TABLE.items():
                rate = market_mod.BORROW_RATE_PER_DAY[side]
                for r in self._conn.execute(
                    f"SELECT guild_id, user_id, symbol, entry_value, margin, interest_at, accrued FROM {table}"
                ).fetchall():
                    key = (r["guild_id"], r["user_id"], r["symbol"])
                    if r["interest_at"] <= 0:  # 数え始め（金利の列がなかった頃の建玉は、ここから数える）
                        self._conn.execute(
                            f"UPDATE {table} SET interest_at=? WHERE guild_id=? AND user_id=? AND symbol=?", (now, *key)
                        )
                        continue
                    base = market_mod.margin_interest_base(side, r["entry_value"], r["margin"])
                    accrued = r["accrued"] + base * rate * max(0.0, now - r["interest_at"]) / 86400.0
                    charge = 0
                    if accrued >= market_mod.INTEREST_MIN_CHARGE:
                        charge = min(int(accrued), r["margin"])
                        accrued -= int(accrued)
                    self._conn.execute(
                        f"UPDATE {table} SET margin=margin-?, accrued=?, interest_at=? "
                        "WHERE guild_id=? AND user_id=? AND symbol=?",
                        (charge, accrued, now, *key),
                    )
                    if charge > 0:
                        try:
                            view = self.for_guild(r["guild_id"])
                        except ValueError:
                            continue
                        name = market_mod.BY_KEY[r["symbol"]].name
                        view._ledger(
                            r["user_id"],
                            0,
                            view._balance(r["user_id"]),
                            f"帝愛の信用金利（{self.SIDE_NAME[side]} {name}）担保から-{charge:,}",
                        )
                        view._bank_income(charge, teiai.TAX_YIELD_SHARE)
                        done.append((r["guild_id"], r["user_id"], r["symbol"], side, charge))
        return done

    def margin_losscut(self, prices: dict) -> list:
        """全サーバーの口座を調べ、維持率が LOSSCUT_RATIO を下回る口座を、連鎖ロスカットする。prices は {銘柄: いまの価格}。[(サーバー, ユーザー, 銘柄, 向き, 損失)]。"""
        done = []
        with self._lock, self._conn:
            accounts = set()
            for table in self._MARGIN_TABLE.values():
                accounts.update(
                    (r["guild_id"], r["user_id"]) for r in self._conn.execute(f"SELECT guild_id, user_id FROM {table}")
                )
            for guild_id, user_id in sorted(accounts):
                try:
                    view = self.for_guild(guild_id)
                except ValueError:
                    print(f"出どころ不明の建玉は、ロスカットできません: guild={guild_id} user={user_id}")
                    continue
                for symbol, side, loss in view._losscut_user_locked(user_id, prices):
                    done.append((guild_id, user_id, symbol, side, loss))
        return done
