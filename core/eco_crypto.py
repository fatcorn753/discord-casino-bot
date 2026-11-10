"""証券（CSトークン・TEIO重工・NUMAバイオ・GOLD地金）の売買・空売り・チャート掲示（Economy に混ぜ込む部品）。

表の定義は core/schema.py にある。銘柄は、symbol（core/market.py の BY_KEY のキー）で指定する。省略すると CSトークン。
"""

import math
from typing import Optional

from core import market as market_mod
from core import teiai
from core.eco_types import CryptoResult
from core.market import DEFAULT


class CryptoMixin:
    """証券の価格・売買・空売り・チャート掲示。"""

    # ------------------------------------------------------------------ #
    # 証券（銘柄ごとの価格・現物）                                        #
    # ------------------------------------------------------------------ #
    def market_latest(self, symbol: str = DEFAULT) -> Optional[tuple]:
        """いまの価格 (時刻, 価格, 出来事)。まだ無ければ None。時計が戻っても、最後に記録した行を最新とする。"""
        with self._lock:
            r = self._conn.execute(
                "SELECT ts, price, event FROM market_prices WHERE symbol=? ORDER BY rowid DESC LIMIT 1", (symbol,)
            ).fetchone()
            return (r["ts"], r["price"], r["event"]) if r else None

    def latest_prices(self) -> dict:
        """いまの全銘柄の価格 {銘柄: 価格}。まだ価格がない銘柄は入らない。"""
        with self._lock:
            return {k: p for k in market_mod.BY_KEY if (p := (self.market_latest(k) or (0, 0, 0))[1])}

    def market_set(self, ts: float, price: float, event: int = 0, symbol: str = DEFAULT) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO market_prices (symbol, ts, price, event) VALUES (?, ?, ?, ?)",
                (symbol, ts, price, event),
            )

    def market_history(self, since: float, symbol: str = DEFAULT) -> list:
        """since（UNIX時間）以降の価格の列 [(時刻, 価格, 出来事), ...]。古い順。"""
        with self._lock:
            return [
                (r["ts"], r["price"], r["event"])
                for r in self._conn.execute(
                    "SELECT ts, price, event FROM market_prices WHERE symbol=? AND ts>=? ORDER BY ts", (symbol, since)
                )
            ]

    def market_prune(self, before: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM market_prices WHERE ts<?", (before,))

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

    def crypto_buy(self, user_id: int, amount: int, price: float, symbol: str = DEFAULT) -> "CryptoResult":
        """amount ペリカの範囲で、price の価格で買えるだけ買う（手数料込み）。"""
        with self._lock, self._conn:
            self._ensure(user_id)
            if amount <= 0 or price <= 0:
                return CryptoResult(False, "金額が正しくありません。")
            if self._debt(user_id) > 0:
                return CryptoResult(
                    False,
                    f"借金が {self._debt(user_id):,}ペリカ 残っているので、買えません。",
                    balance=self._balance(user_id),
                )
            if self._balance(user_id) < amount:
                return CryptoResult(
                    False,
                    f"ペリカが足りません（残高 {self._balance(user_id):,}ペリカ）。",
                    balance=self._balance(user_id),
                )
            units, total, fee = market_mod.buy_quote(amount, price)
            if units < 1:
                return CryptoResult(
                    False, "その金額では 0.001枚も買えません。金額を増やしてください。", balance=self._balance(user_id)
                )
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

    def crypto_sell(self, user_id: int, units: Optional[int], price: float, symbol: str = DEFAULT) -> "CryptoResult":
        """units 口を売る（None なら全部）。受け取る額は手数料を引いたもの。"""
        with self._lock, self._conn:
            reason = self._collateral_drop_reason(user_id, symbol, units, price)
            if reason:
                return CryptoResult(False, reason)
            return self._crypto_sell_locked(user_id, units, price, symbol, taxable=True)

    def _collateral_drop_reason(self, user_id: int, symbol: str, units: Optional[int], price: float) -> str:
        """現物を売ると、担保が減って維持率が OPEN_RATIO を下回るなら、その理由（問題なければ空文字）。信用取引がなければ常に問題なし。"""
        have, _cost = self.crypto_holding(user_id, symbol)
        if have <= 0 or not self._margin_rows(user_id):
            return ""
        sold = have if units is None else min(units, have)
        drop = market_mod.sell_quote(sold, price)[0] * market_mod.COLLATERAL_HAIRCUT["spot"]
        after = self._margin_status_locked(user_id, {**self.latest_prices(), symbol: price}, drop_collateral=drop)
        if after["ratio"] is not None and after["ratio"] < market_mod.OPEN_RATIO:
            return (
                f"信用取引の担保になっているので、売れません（売ると維持率が {after['ratio']:.0%}・{market_mod.OPEN_RATIO:.0%} 以上が必要）。"
                "先に建玉を決済してください。"
            )
        return ""

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
    ) -> "CryptoResult":
        """crypto_sell の本体（呼び出し側が、ロックとトランザクションの中で使う）。taxable=True なら、利益に税がかかる。"""
        name = market_mod.BY_KEY[symbol].name
        have, cost = self.crypto_holding(user_id, symbol)
        if have <= 0:
            return CryptoResult(False, f"{name}を持っていません。")
        units = have if units is None else units
        if units < 1 or units > have:
            return CryptoResult(False, f"売れるのは 0.001〜{market_mod.lots(have)} です（持っている分）。")
        proceeds, fee = market_mod.sell_quote(units, price)
        if proceeds < 1 and units != have:
            return CryptoResult(False, "その数量では受け取れる額が 1ペリカ未満です。もう少し多く売ってください。")
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

    # ------------------------------------------------------------------ #
    # 信用取引（空売りと信用買い）とクロスマージン                        #
    # ------------------------------------------------------------------ #
    SHORT_LEVERAGES = (1, 2, 3, 5)
    MARGIN_LEVERAGES = SHORT_LEVERAGES
    SHORT_MIN_MARGIN = 10
    _MARGIN_TABLE = {"short": "crypto_short", "long": "crypto_long"}  # 表の名前は固定の文字列だけ（SQL に埋め込むため）
    SIDE_NAME = {"short": "空売り", "long": "信用買い"}

    @staticmethod
    def margin_pnl(side: str, units: int, entry_value: float, price: float) -> int:
        """建玉の含み損益。空売りは価格が下がるとプラス、信用買いは上がるとプラス。"""
        now = units * price / market_mod.UNIT
        return round(entry_value - now) if side == "short" else round(now - entry_value)

    @staticmethod
    def short_pnl(units: int, entry_value: float, price: float) -> int:
        """空売りの含み損益（価格が下がるとプラス）。"""
        return CryptoMixin.margin_pnl("short", units, entry_value, price)

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
                        pnl = self.margin_pnl(side, r["units"], r["entry_value"], prices[r["symbol"]])
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
            equity += margin + self.margin_pnl(side, units, entry, prices[symbol])
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
    ) -> "CryptoResult":
        """units 口を建てる。担保 = 総額 ÷ レバレッジ（切り上げ）を、残高から預かる。口座の維持率が OPEN_RATIO を下回る建玉は受け付けない。"""
        label = self.SIDE_NAME[side]
        with self._lock, self._conn:
            self._ensure(user_id)
            if leverage not in self.MARGIN_LEVERAGES or units < 1 or price <= 0:
                return CryptoResult(False, "指定が正しくありません。")
            if self._debt(user_id) > 0:
                return CryptoResult(
                    False,
                    f"借金が {self._debt(user_id):,}ペリカ 残っているので、{label}はできません。",
                    balance=self._balance(user_id),
                )
            other = "long" if side == "short" else "short"
            if self.margin_position(user_id, symbol, other)[0] > 0:
                return CryptoResult(
                    False,
                    f"{market_mod.BY_KEY[symbol].name}の{self.SIDE_NAME[other]}を建てています。先に決済してください。",
                    balance=self._balance(user_id),
                )
            notional = units * price / market_mod.UNIT
            margin = math.ceil(round(notional / leverage, 6))
            if margin < self.SHORT_MIN_MARGIN:
                return CryptoResult(
                    False,
                    f"担保が {self.SHORT_MIN_MARGIN}ペリカ 未満になる量です。枚数を増やしてください。",
                    balance=self._balance(user_id),
                )
            if self._balance(user_id) < margin:
                return CryptoResult(
                    False,
                    f"担保が足りません（{margin:,}ペリカ必要・残高 {self._balance(user_id):,}ペリカ）。",
                    balance=self._balance(user_id),
                )
            prices = {**self.latest_prices(), symbol: price}
            after = self._margin_status_locked(user_id, prices, add=(side, symbol, notional, margin))
            if after["ratio"] is not None and after["ratio"] < market_mod.OPEN_RATIO:
                return CryptoResult(
                    False,
                    f"口座の維持率が足りなくなります（建てると {after['ratio']:.0%}・{market_mod.OPEN_RATIO:.0%} 以上が必要）。"
                    "枚数を減らすか、レバレッジを下げるか、現物や債券を担保に持ってください。",
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

    def short_open(
        self, user_id: int, units: int, price: float, leverage: int, symbol: str = DEFAULT
    ) -> "CryptoResult":
        """units 口を売り建てる。"""
        return self._margin_open(user_id, "short", units, price, leverage, symbol)

    def long_open(self, user_id: int, units: int, price: float, leverage: int, symbol: str = DEFAULT) -> "CryptoResult":
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
    ) -> "CryptoResult":
        """建玉を units 口、決済する（None なら全部）。担保 + 損益が戻る。owe=True なら、損失が担保を超えた分は、強制執行と借金で払わせる。

        taxable=True なら、利益（pnl > 0）に税がかかる（戻る額から引く）。
        """
        have, entry_value, margin = self.margin_position(user_id, symbol, side)
        if have <= 0:
            return CryptoResult(False, f"{self.SIDE_NAME[side]}を建てていません。")
        units = have if units is None else units
        if units < 1 or units > have:
            return CryptoResult(False, f"決済できるのは 0.001〜{market_mod.lots(have)} です（建てている分）。")
        whole = units == have
        part_margin = margin if whole else round(margin * units / have)
        part_entry = entry_value if whole else entry_value * units / have
        pnl = self.margin_pnl(side, units, part_entry, price)
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
    ) -> "CryptoResult":
        return self._margin_close_locked(user_id, "short", units, price, label, symbol)

    def margin_close(
        self, user_id: int, side: str, units: Optional[int], price: float, symbol: str = DEFAULT
    ) -> "CryptoResult":
        """units 口を決済する（None なら全部）。担保 + 損益が戻る。担保を超えた損失は、借金になりうる。"""
        with self._lock, self._conn:
            self._ensure(user_id)
            return self._margin_close_locked(
                user_id, side, units, price, f"{self.SIDE_NAME[side]}決済", symbol, owe=True, taxable=True
            )

    def short_close(self, user_id: int, units: Optional[int], price: float, symbol: str = DEFAULT) -> "CryptoResult":
        return self.margin_close(user_id, "short", units, price, symbol)

    def long_close(self, user_id: int, units: Optional[int], price: float, symbol: str = DEFAULT) -> "CryptoResult":
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
                rows, key=lambda r: (self.margin_pnl(r[0], r[2], r[3], prices[r[1]]), r[1], r[0])
            )
            res = self._margin_close_locked(
                user_id, side, None, prices[symbol], f"{self.SIDE_NAME[side]}ロスカット", symbol, owe=True
            )
            if not res.ok:
                break
            self._note(
                user_id,
                f"💥 口座の維持率が {market_mod.LOSSCUT_RATIO:.0%} を下回り、{market_mod.BY_KEY[symbol].name}の{self.SIDE_NAME[side]}が強制決済されました"
                f"（損益 {res.profit:+,}ペリカ）。",
            )
            cut.append((symbol, side, -res.profit))
        return cut

    def margin_cut_map(self, prices: Optional[dict] = None) -> list:
        """このサーバーの、信用建玉のロスカット水準の地図 [(銘柄, 向き, ロスカットされる価格, 建玉額), ...]。

        各建玉について、その銘柄の価格だけが逆に動いたとき、口座の維持率が LOSSCUT_RATIO を割る価格を、二分探索で求める
        （買い建ては下へ、空売りは上へ。±50% の範囲で見つからなければ入れない）。持ち主は分からない形で、まとめて見せるための材料。
        """
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

    def swap_chart_post(
        self, channel_id: int, message_id: int, guild_id: int, period: str, ts: float, symbol: str = DEFAULT
    ) -> Optional[int]:
        """そのチャンネルのチャートを新しいものに入れ替え（更新中にする）、前のメッセージID（無ければ None）を返す。"""
        with self._lock, self._conn:
            prev = self._conn.execute("SELECT message_id FROM chart_posts WHERE channel_id=?", (channel_id,)).fetchone()
            self._conn.execute(
                "INSERT INTO chart_posts (channel_id, message_id, guild_id, period, ts, live, symbol) VALUES (?, ?, ?, ?, ?, 1, ?) "
                "ON CONFLICT(channel_id) DO UPDATE SET message_id=excluded.message_id, guild_id=excluded.guild_id, "
                "period=excluded.period, ts=excluded.ts, live=1, symbol=excluded.symbol",
                (channel_id, message_id, guild_id, period, ts, symbol),
            )
            return prev["message_id"] if prev else None

    def live_chart_posts(self) -> list:
        """自動で更新中のチャート。"""
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM chart_posts WHERE live=1")]

    def end_chart_post(self, channel_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE chart_posts SET live=0 WHERE channel_id=?", (channel_id,))

    def delete_chart_post(self, channel_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM chart_posts WHERE channel_id=?", (channel_id,))

    def delete_guild_chart_posts(self, guild_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM chart_posts WHERE guild_id=?", (guild_id,))

    def prune_chart_posts(self, before_ts: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM chart_posts WHERE ts<? AND live=0", (before_ts,))

    # 昔の「チャート掲示」（廃止）の後始末用
    def list_chart_boards(self) -> list:
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM chart_boards")]

    def delete_chart_board(self, guild_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM chart_boards WHERE guild_id=?", (guild_id,))
