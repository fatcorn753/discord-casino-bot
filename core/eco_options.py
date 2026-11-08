"""オプション（爆騰コール・暴落プット）の購入・満期の決済・満期前の売却（Economy に混ぜ込む部品）。

表の定義は core/schema.py の options。値付けと数字は core/options.py。
相手は帝愛銀行（core/eco_bank.py）: 掛け金は銀行に入り、配当と売却代金は銀行が払う（銀行の手元と預金の範囲まで。足りなければ、その分は払えない）。
損失は、払った掛け金だけ（途中のロスカットや追証はない）。
"""

import math
import time
from typing import Optional

from core import market as market_mod
from core import options as opt


class OptionMixin:
    """オプションの売買と決済。"""

    def _price_at(self, symbol: str, ts: float) -> Optional[float]:
        """ts 以前の、最後の価格（満期の価格）。その前の記録がなければ、いまの価格。"""
        r = self._conn.execute(
            "SELECT price FROM market_prices WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT 1", (symbol, ts)
        ).fetchone()
        if r:
            return r["price"]
        return self.latest_prices().get(symbol)

    def _open_premium(self, user_id: int) -> int:
        return self._conn.execute(
            "SELECT COALESCE(SUM(premium), 0) FROM options WHERE guild_id=? AND user_id=? AND status='active'",
            (self._g(), user_id),
        ).fetchone()[0]

    def option_buy(self, user_id: int, kind: str, symbol: str, amount: int, now: Optional[float] = None) -> tuple:
        """amount（掛け金）の範囲で、買えるだけ買う。(成功か, 理由か {id, units, cost, strike, expiry, spot}) を返す。"""
        now = time.time() if now is None else now
        if kind not in opt.KINDS or symbol not in market_mod.BY_KEY:
            return False, "指定が正しくありません。"
        with self._lock, self._conn:
            self._ensure(user_id)
            if amount < opt.MIN_PREMIUM:
                return False, f"掛け金は {opt.MIN_PREMIUM:,}ペリカ 以上からです。"
            if self._debt(user_id) > 0:
                return False, f"借金が {self._debt(user_id):,}ペリカ 残っているので、買えません。"
            spot = self.latest_prices().get(symbol)
            if spot is None:
                return False, "まだ価格がありません。"
            strike = opt.strike_for(kind, spot)
            seconds = opt.OPTION_HOURS * 3600
            per_unit = opt.ask_per_unit(kind, symbol, spot, strike, seconds, now, self.market_events(now))
            units = int(amount / per_unit) if per_unit > 0 else 0
            if units < 1:
                return False, "その掛け金では 1口も買えません。掛け金を増やしてください。"
            cost = math.ceil(round(units * per_unit, 6))
            if self._balance(user_id) < cost:
                return False, f"残高が足りません（{cost:,}ペリカ必要・残高 {self._balance(user_id):,}ペリカ）。"
            if self._open_premium(user_id) + cost > opt.MAX_OPEN_PREMIUM:
                left = max(0, opt.MAX_OPEN_PREMIUM - self._open_premium(user_id))
                return (
                    False,
                    f"持てるオプションの掛け金は、合計 {opt.MAX_OPEN_PREMIUM:,}ペリカ までです（あと {left:,}ペリカ）。",
                )
            name = opt.KINDS[kind][0]
            self._apply(user_id, -cost, f"{name}購入:{market_mod.BY_KEY[symbol].name}")
            self._bank_income(cost, opt.YIELD_SHARE, now)
            expiry = now + seconds
            cur = self._conn.execute(
                "INSERT INTO options (guild_id, user_id, kind, symbol, strike, units, premium, expiry, spot, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self._g(), user_id, kind, symbol, strike, units, cost, expiry, spot, now),
            )
            return True, {
                "id": cur.lastrowid,
                "units": units,
                "cost": cost,
                "strike": strike,
                "expiry": expiry,
                "spot": spot,
            }

    def option_value(self, row, now: float, prices: dict) -> int:
        """いま売ったときの手取り（売値 = 公正価格 −手数料）。満期を過ぎていれば、満期の価格での配当。"""
        spot = prices.get(row["symbol"])
        if spot is None:
            return 0
        if row["expiry"] <= now:
            value = opt.intrinsic(row["kind"], row["units"], spot, row["strike"])
        else:
            bid = opt.bid_per_unit(
                row["kind"], row["symbol"], spot, row["strike"], row["expiry"] - now, now, self.market_events(now)
            )
            value = int(bid * row["units"])
        return min(value, row["premium"] * opt.PAYOUT_CAP_MULT)

    def option_values(self) -> dict:
        """{ユーザーID: 持っているオプションの、いま売ったときの手取りの合計}（このサーバー。ランキングの総資産に含める）。"""
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

    def _option_sell_locked(self, user_id: int, option_id: int, now: float) -> tuple:
        row = self._conn.execute(
            "SELECT * FROM options WHERE id=? AND guild_id=? AND user_id=? AND status='active'",
            (option_id, self._g(), user_id),
        ).fetchone()
        if row is None:
            return False, "その番号のオプションは持っていません。"
        value = self.option_value(row, now, self.latest_prices())
        paid = self._bank_pay(value, now) if value > 0 else 0
        if paid:
            self._apply(user_id, paid, f"{opt.KINDS[row['kind']][0]}売却:{market_mod.BY_KEY[row['symbol']].name}")
        self._conn.execute("UPDATE options SET status='sold', paid=? WHERE id=?", (paid, option_id))
        return True, paid

    def option_sell(self, user_id: int, option_id: int, now: Optional[float] = None) -> tuple:
        """満期の前に、時価（売値）で売る。(成功か, 理由か受け取った額) を返す。"""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._ensure(user_id)
            return self._option_sell_locked(user_id, option_id, now)

    def option_settle(self, now: float) -> list:
        """満期を過ぎたオプションを決済する（このサーバー全員分）。決済した行（status と paid つき）を返す。

        満期の価格が権利行使価格を超えて（コール）／下回って（プット）いれば、その差が配当（掛け金の PAYOUT_CAP_MULT 倍まで）。
        配当は銀行が払う（手元の現金と預金の範囲まで）。
        """
        out = []
        with self._lock, self._conn:
            for r in self._conn.execute(
                "SELECT * FROM options WHERE guild_id=? AND status='active' AND expiry<=? ORDER BY id", (self._g(), now)
            ).fetchall():
                final = self._price_at(r["symbol"], r["expiry"])
                if final is None:
                    continue
                gross = min(
                    opt.intrinsic(r["kind"], r["units"], final, r["strike"]), r["premium"] * opt.PAYOUT_CAP_MULT
                )
                paid = self._bank_pay(gross, now) if gross > 0 else 0
                if paid:
                    self._ensure(r["user_id"])
                    self._apply(
                        r["user_id"],
                        paid,
                        f"{opt.KINDS[r['kind']][0]}の配当:{market_mod.BY_KEY[r['symbol']].name}",
                        income=max(0, paid - r["premium"]),
                    )
                status = "exercised" if gross > 0 else "expired"
                self._conn.execute(
                    "UPDATE options SET status=?, paid=?, final=? WHERE id=?", (status, paid, final, r["id"])
                )
                out.append({**dict(r), "status": status, "paid": paid, "final": final})
        return out
