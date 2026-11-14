"""オプション（爆騰コール・暴落プット・ペア・ワラント）と裸売りの、購入・満期の決済・満期前の売却（Economy に混ぜ込む部品）。

表の定義は core/schema.py の options。値付けと数字は core/options.py。
相手は帝愛銀行（core/eco_bank.py）。

- 買い（long）: 掛け金は銀行に入り、配当と売却代金は銀行が払う（銀行の手元と預金の範囲まで）。損失は掛け金だけ
- 裸売り（short。コール・プットだけ）: 帝愛に売り、プレミアム（売値）を先にもらう。証拠金（行使価格×口数の20%）を預ける。
  満期の配当（売った側の損失）は、証拠金から払い、**足りない分は借金になる**（_owe）。途中でロスカットはないが、買い戻して閉じられる
"""

import math
import time
from typing import Optional

from core import market as market_mod
from core import options as opt
from core.eco_base import EcoBase


def name_of(row) -> str:
    """表示用の名前（買いは種類の名前、裸売りは「コール裸売り」など）。"""
    return opt.SHORT_NAMES[row["kind"]] if row["side"] == "short" else opt.KINDS[row["kind"]][0]


class OptionMixin(EcoBase):
    """オプションの売買と決済。"""

    def _option_events(self, now: float) -> list:
        """オプションの値付けに入れる出来事: ボラティリティ予告 + 満期までにある決算の発表（OPTION_HOURS の間）。"""
        return self.market_events(now) + self.earnings_jumps(now, opt.OPTION_HOURS * 3600)

    def _price_at(self, symbol: str, ts: float) -> Optional[float]:
        """ts 以前の、最後の価格（満期の価格）。その前の記録がなければ、いまの価格。"""
        r = self._conn.execute(
            "SELECT price FROM market_prices WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT 1", (symbol, ts)
        ).fetchone()
        if r:
            return r["price"]
        return self.latest_prices().get(symbol)

    def _open_premium(self, user_id: int) -> int:
        """有効な買いの、掛け金の合計。"""
        return self._conn.execute(
            "SELECT COALESCE(SUM(premium), 0) FROM options WHERE guild_id=? AND user_id=? AND status='active' AND side='long'",
            (self._g(), user_id),
        ).fetchone()[0]

    def _open_margin(self, user_id: int) -> int:
        """有効な裸売りの、証拠金の合計。"""
        return self._conn.execute(
            "SELECT COALESCE(SUM(margin), 0) FROM options WHERE guild_id=? AND user_id=? AND status='active' AND side='short'",
            (self._g(), user_id),
        ).fetchone()[0]

    # ------------------------------------------------------------------ #
    # 買い（コール・プット・ペア）
    # ------------------------------------------------------------------ #
    def option_buy(
        self,
        user_id: int,
        kind: str,
        symbol: str,
        amount: int,
        now: Optional[float] = None,
        symbol2: Optional[str] = None,
        strike_pct: Optional[float] = None,
    ) -> tuple:
        """amount（掛け金）の範囲で、買えるだけ買う。ペア・ワラントは、symbol が symbol2 より上がると当たり。

        strike_pct は、権利行使価格を現在値からずらす割合（0.03 で 3%）。省略すると、銘柄ごとの既定値（opt.STRIKE_BY_SYMBOL）。

        (成功か, 理由か {id, units, cost, strike, expiry, spot}) を返す。units は、コール・プットは口数、ペアは想定元本（ペリカ）。
        """
        now = time.time() if now is None else now
        if kind not in opt.KINDS or symbol not in market_mod.BY_KEY:
            return False, "指定が正しくありません。"
        if kind == opt.PAIR and (symbol2 is None or symbol2 not in market_mod.BY_KEY or symbol2 == symbol):
            return False, "ペア・ワラントは、ちがう2つの銘柄を選んでください。"
        if strike_pct is not None and not 0 < strike_pct <= opt.MAX_TARGET_PCT / 100:
            return False, f"目標割合は、1〜{opt.MAX_TARGET_PCT}% にしてください。"
        with self._lock, self._conn:
            self._ensure(user_id)
            if amount < opt.MIN_PREMIUM:
                return False, f"掛け金は {opt.MIN_PREMIUM:,}ペリカ 以上からです。"
            if self._debt(user_id) > 0:
                return False, f"借金が {self._debt(user_id):,}ペリカ 残っているので、買えません。"
            prices = self.latest_prices()
            spot = prices.get(symbol)
            spot2 = prices.get(symbol2) if kind == opt.PAIR and symbol2 is not None else None
            if spot is None or (kind == opt.PAIR and spot2 is None):
                return False, "まだ価格がありません。"
            seconds, events = opt.OPTION_HOURS * 3600, self._option_events(now)
            if kind == opt.PAIR:
                assert symbol2 is not None
                strike = spot
                rate = opt.pair_ask_rate(symbol, symbol2, seconds, now, events)
                units = int(amount / rate) if rate > 0 else 0  # 想定元本（ペリカ）
                cost = math.ceil(round(units * rate, 6))
            else:
                strike = opt.strike_for(
                    kind, spot, opt.default_strike_pct(symbol) if strike_pct is None else strike_pct
                )
                per_unit = opt.ask_per_unit(kind, symbol, spot, strike, seconds, now, events)
                units = int(amount / per_unit) if per_unit > 0 else 0
                cost = math.ceil(round(units * per_unit, 6))
            if units < 1:
                return False, "その掛け金では 1口も買えません。掛け金を増やしてください。"
            if self._balance(user_id) < cost:
                return False, f"残高が足りません（{cost:,}ペリカ必要・残高 {self._balance(user_id):,}ペリカ）。"
            if self._open_premium(user_id) + cost > opt.MAX_OPEN_PREMIUM:
                left = max(0, opt.MAX_OPEN_PREMIUM - self._open_premium(user_id))
                return (
                    False,
                    f"持てるオプションの掛け金は、合計 {opt.MAX_OPEN_PREMIUM:,}ペリカ までです（あと {left:,}ペリカ）。",
                )
            label = opt.KINDS[kind][0]
            self._apply(user_id, -cost, f"{label}購入:{market_mod.BY_KEY[symbol].name}")
            self._bank_income(cost, opt.YIELD_SHARE, now)
            expiry = now + seconds
            cur = self._conn.execute(
                "INSERT INTO options (guild_id, user_id, kind, symbol, strike, units, premium, expiry, spot, ts, symbol2, spot2) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self._g(), user_id, kind, symbol, strike, units, cost, expiry, spot, now, symbol2, spot2),
            )
            return True, {
                "id": cur.lastrowid,
                "units": units,
                "cost": cost,
                "strike": strike,
                "expiry": expiry,
                "spot": spot,
                "spot2": spot2,
            }

    # ------------------------------------------------------------------ #
    # 裸売り（コール・プット）
    # ------------------------------------------------------------------ #
    def option_write(
        self,
        user_id: int,
        kind: str,
        symbol: str,
        amount: int,
        now: Optional[float] = None,
        strike_pct: Optional[float] = None,
    ) -> tuple:
        """帝愛に売って、プレミアムを先にもらう（裸売り）。amount は預ける証拠金の上限。(成功か, 理由か {...}) を返す。

        証拠金 = 行使価格 × 口数 × MARGIN_RATE。満期に、売った側の損失を証拠金から払い、足りなければ借金になる。
        帝愛銀行が、プレミアムを払う（銀行の利用可能現金が足りなければ、売れない）。
        """
        now = time.time() if now is None else now
        if kind not in opt.SHORT_NAMES or symbol not in market_mod.BY_KEY:
            return False, "指定が正しくありません。"
        if strike_pct is not None and not 0 < strike_pct <= opt.MAX_TARGET_PCT / 100:
            return False, f"目標割合は、1〜{opt.MAX_TARGET_PCT}% にしてください。"
        with self._lock, self._conn:
            self._ensure(user_id)
            if amount < opt.MIN_PREMIUM:
                return False, f"証拠金は {opt.MIN_PREMIUM:,}ペリカ 以上からです。"
            if self._debt(user_id) > 0:
                return False, f"借金が {self._debt(user_id):,}ペリカ 残っているので、売れません。"
            spot = self.latest_prices().get(symbol)
            if spot is None:
                return False, "まだ価格がありません。"
            strike = opt.strike_for(kind, spot, opt.default_strike_pct(symbol) if strike_pct is None else strike_pct)
            seconds = opt.OPTION_HOURS * 3600
            per_margin = opt.margin_per_unit(strike)
            units = int(amount / per_margin)
            if units < 1:
                return False, "その証拠金では 1口も売れません。証拠金を増やしてください。"
            margin = math.ceil(round(units * per_margin, 6))
            bid = opt.bid_per_unit(kind, symbol, spot, strike, seconds, now, self._option_events(now))
            premium = int(bid * units)
            if self._balance(user_id) < margin:
                return False, f"証拠金が足りません（{margin:,}ペリカ必要・残高 {self._balance(user_id):,}ペリカ）。"
            if self._open_margin(user_id) + margin > opt.MAX_SHORT_MARGIN:
                left = max(0, opt.MAX_SHORT_MARGIN - self._open_margin(user_id))
                return (
                    False,
                    f"預けられる裸売りの証拠金は、合計 {opt.MAX_SHORT_MARGIN:,}ペリカ までです（あと {left:,}ペリカ）。",
                )
            available = int(self._bank_sync(now)["cash"] - self._bank_locks(now))
            if premium > available:
                return False, "いま帝愛銀行の手元の現金が足りず、プレミアムを払えません（準備率が低いとき）。"
            self._apply(user_id, -margin, f"{opt.SHORT_NAMES[kind]}の証拠金:{market_mod.BY_KEY[symbol].name}")
            paid = self._bank_pay(premium, now)
            if paid:
                self._apply(user_id, paid, f"{opt.SHORT_NAMES[kind]}のプレミアム:{market_mod.BY_KEY[symbol].name}")
            expiry = now + seconds
            cur = self._conn.execute(
                "INSERT INTO options (guild_id, user_id, kind, symbol, strike, units, premium, expiry, spot, ts, side, margin) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'short', ?)",
                (self._g(), user_id, kind, symbol, strike, units, paid, expiry, spot, now, margin),
            )
            return True, {
                "id": cur.lastrowid,
                "units": units,
                "premium": paid,
                "margin": margin,
                "strike": strike,
                "expiry": expiry,
                "spot": spot,
            }

    # ------------------------------------------------------------------ #
    # 評価
    # ------------------------------------------------------------------ #
    def option_close_cost(self, row, now: float, prices: dict) -> int:
        """裸売りを、いま買い戻すのにかかる額（買値。満期を過ぎていれば、満期の配当そのもの）。"""
        spot = prices.get(row["symbol"])
        if spot is None:
            return 0
        if row["expiry"] <= now:
            return opt.intrinsic(row["kind"], row["units"], spot, row["strike"])
        ask = opt.ask_per_unit(
            row["kind"], row["symbol"], spot, row["strike"], row["expiry"] - now, now, self._option_events(now)
        )
        return math.ceil(round(ask * row["units"], 6))

    def option_value(self, row, now: float, prices: dict) -> int:
        """いま手放したときの価値。買いは売値の手取り（満期を過ぎていれば、満期の価格での配当）。裸売りは、証拠金 − 買い戻しの費用（マイナスもある）。"""
        if row["side"] == "short":
            return row["margin"] - self.option_close_cost(row, now, prices)
        spot = prices.get(row["symbol"])
        if spot is None:
            return 0
        if row["kind"] == opt.PAIR:
            spot2 = prices.get(row["symbol2"])
            if spot2 is None:
                return 0
            if row["expiry"] <= now:
                value = opt.pair_intrinsic(row["units"], row["spot"], row["spot2"], spot, spot2)
            else:
                rate = opt.pair_bid_rate(
                    row["symbol"],
                    row["symbol2"],
                    spot / row["spot"],
                    spot2 / row["spot2"],
                    row["expiry"] - now,
                    now,
                    self._option_events(now),
                )
                value = int(rate * row["units"])
        elif row["expiry"] <= now:
            value = opt.intrinsic(row["kind"], row["units"], spot, row["strike"])
        else:
            bid = opt.bid_per_unit(
                row["kind"], row["symbol"], spot, row["strike"], row["expiry"] - now, now, self._option_events(now)
            )
            value = int(bid * row["units"])
        return min(value, row["premium"] * opt.PAYOUT_CAP_MULT)

    def option_values(self) -> dict:
        """{ユーザーID: 持っているオプションの評価額の合計}（このサーバー。ランキングの総資産に含める。裸売りの分は、マイナスになりうる）。"""
        with self._lock:
            now, prices, out = time.time(), self.latest_prices(), {}
            for r in self._conn.execute("SELECT * FROM options WHERE guild_id=? AND status='active'", (self._g(),)):
                out[r["user_id"]] = out.get(r["user_id"], 0) + self.option_value(r, now, prices)
            return out

    def option_list(self, user_id: int, recent: int = 5) -> dict:
        """有効なもの（満期が近い順）と、最近決着したもの（新しい順）。"""
        with self._lock:
            rows = [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM options WHERE guild_id=? AND user_id=? ORDER BY id", (self._g(), user_id)
                )
            ]
        active = sorted((r for r in rows if r["status"] == "active"), key=lambda r: r["expiry"])
        done = [r for r in reversed(rows) if r["status"] != "active"][:recent]
        return {"active": active, "done": done}

    # ------------------------------------------------------------------ #
    # 満期前に手放す（買いは売る・裸売りは買い戻す）
    # ------------------------------------------------------------------ #
    def _option_close_locked(self, user_id: int, option_id: int, now: float) -> tuple:
        row = self._conn.execute(
            "SELECT * FROM options WHERE id=? AND guild_id=? AND user_id=? AND status='active'",
            (option_id, self._g(), user_id),
        ).fetchone()
        if row is None:
            return False, "その番号のオプションは持っていません。"
        prices = self.latest_prices()
        if row["side"] == "short":
            cost = self.option_close_cost(row, now, prices)
            back = row["margin"] - cost
            self._bank_income(cost, 0.0, now)  # 買い戻しの費用は、銀行に入る
            # 先に決済済みにする（超えた分の支払いで差し押さえが走っても、同じ建玉を、もう一度買い戻そうとしない）
            self._conn.execute("UPDATE options SET status='sold', paid=? WHERE id=?", (max(0, back), option_id))
            if back > 0:
                self._apply(user_id, back, f"{name_of(row)}の買い戻し（証拠金の戻り）")
            elif back < 0:  # 買い戻しの費用が証拠金を超えた分は、強制執行と借金で払う
                self._owe(user_id, -back, f"{name_of(row)}の買い戻し")
            return True, back
        value = self.option_value(row, now, prices)
        paid = self._bank_pay(value, now) if value > 0 else 0
        if paid:
            self._apply(user_id, paid, f"{name_of(row)}売却:{market_mod.BY_KEY[row['symbol']].name}")
        self._conn.execute("UPDATE options SET status='sold', paid=? WHERE id=?", (paid, option_id))
        return True, paid

    def option_sell(self, user_id: int, option_id: int, now: Optional[float] = None) -> tuple:
        """満期の前に手放す。買いは売値で売り、裸売りは買値で買い戻す。(成功か, 理由か 受け取った額（裸売りは、証拠金の戻り。マイナスは、借金にした額）) を返す。"""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._ensure(user_id)
            held = self._conn.execute(
                "SELECT kind, symbol, side FROM options WHERE id=? AND guild_id=? AND user_id=? AND status='active'",
                (option_id, self._g(), user_id),
            ).fetchone()
            if (
                held and held["side"] != "short" and held["kind"] == "call"
            ):  # 持っているコールは、ストップ安では売れない
                if reason := self._pin_reason(held["symbol"], "sell_call"):
                    return False, reason
            return self._option_close_locked(user_id, option_id, now)

    # ------------------------------------------------------------------ #
    # 満期の決済
    # ------------------------------------------------------------------ #
    def option_settle(self, now: float) -> list:
        """満期を過ぎたオプションを決済する（このサーバー全員分）。決済した行（status と paid つき）を返す。

        買い: 満期の価格での配当（掛け金の PAYOUT_CAP_MULT 倍まで）を、銀行が払う（手元の現金と預金の範囲まで）。
        裸売り: 満期の配当を、預けた証拠金から払う。残りは戻り、足りない分は借金になる。払った配当は、銀行に入る。
        """
        out = []
        with self._lock, self._conn:
            for r in self._conn.execute(
                "SELECT * FROM options WHERE guild_id=? AND status='active' AND expiry<=? ORDER BY id", (self._g(), now)
            ).fetchall():
                final = self._price_at(r["symbol"], r["expiry"])
                final2 = self._price_at(r["symbol2"], r["expiry"]) if r["kind"] == opt.PAIR else None
                if final is None or (r["kind"] == opt.PAIR and final2 is None):
                    continue
                if r["kind"] == opt.PAIR:
                    assert final2 is not None
                    gross = opt.pair_intrinsic(r["units"], r["spot"], r["spot2"], final, final2)
                else:
                    gross = opt.intrinsic(r["kind"], r["units"], final, r["strike"])
                self._ensure(r["user_id"])
                if r["side"] == "short":
                    paid = max(0, r["margin"] - gross)  # 証拠金の戻り
                    self._bank_income(gross, 0.0, now)
                    self._conn.execute(  # 先に決済済みにする（損失の支払いで差し押さえが走っても、同じ建玉を、もう一度決済しない）
                        "UPDATE options SET status=?, paid=?, final=? WHERE id=?",
                        ("exercised" if gross > 0 else "expired", paid, final, r["id"]),
                    )
                    if paid:
                        self._apply(r["user_id"], paid, f"{name_of(r)}の満期（証拠金の戻り）")
                    if gross > r["margin"]:  # 損失が証拠金を超えた分は、強制執行と借金で払う
                        self._owe(r["user_id"], gross - r["margin"], f"{name_of(r)}の損失")
                    status = "exercised" if gross > 0 else "expired"
                else:
                    gross = min(gross, r["premium"] * opt.PAYOUT_CAP_MULT)
                    paid = self._bank_pay(gross, now) if gross > 0 else 0
                    if paid:
                        self._apply(
                            r["user_id"],
                            paid,
                            f"{name_of(r)}の配当:{market_mod.BY_KEY[r['symbol']].name}",
                            income=max(0, paid - r["premium"]),
                        )
                    status = "exercised" if gross > 0 else "expired"
                self._conn.execute(
                    "UPDATE options SET status=?, paid=?, final=? WHERE id=?", (status, paid, final, r["id"])
                )
                out.append({**dict(r), "status": status, "paid": paid, "final": final, "gross": gross})
        return out
