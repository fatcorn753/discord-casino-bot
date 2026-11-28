"""オプション（爆騰コール・暴落プット・ペア・ワラント）と裸売りの、購入・満期の決済・満期前の売却。

表の定義は infra/db/schema/tables.py の options。値付けと数字は domain/options.py。
相手は帝愛銀行（infra/db/bank.py）。
"""

import math
from collections.abc import Mapping
from typing import Any

from domain import market as market_mod
from domain import options as opt
from domain.errors import Outcome, fail
from domain.options import name_of as name_of
from domain.ports.bridges import IBankCashFlow, IDebtOps, IOptionHoldings, NoOpDebtOps
from domain.ports.contracts import HoldingBook, OptionBuyResult, OptionRow, OptionSettleRow, OptionWriteResult
from domain.ports.repos import IOptionRepo
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo
from infra.db.earnings import EarningsRepo
from infra.db.market import MarketDataRepo
from infra.db.market_events import MarketEventRepo

_SELECT_OPTION = (
    "SELECT o.id, o.guild_id, o.user_id, o.kind, o.symbol, o.strike, o.units, "
    "o.premium, o.expiry, o.spot, o.ts, o.status, o.paid, o.final, "
    "op.symbol2, op.spot2, o.side, COALESCE(sm.margin, 0) AS margin "
    "FROM options o "
    "LEFT JOIN option_pairs op ON op.option_id = o.id "
    "LEFT JOIN option_short_margins sm ON sm.option_id = o.id"
)


class OptionRepo(ScopedRepo, IOptionRepo, IOptionHoldings):
    """オプションの売買と決済。"""

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        market: MarketDataRepo,
        market_events: MarketEventRepo,
        earnings: EarningsRepo,
        bank: IBankCashFlow,
        debt: IDebtOps | None = None,
        guild_id: int = 0,
        strict: bool = False,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._market = market
        self._market_events = market_events
        self._earnings = earnings
        self._bank = bank
        self._debt: IDebtOps = debt or NoOpDebtOps()

    def _option_events(self, now: float) -> list[Any]:
        """オプションの値付けに入れる出来事: ボラティリティ予告 + 満期までにある決算の発表（OPTION_HOURS の間）。"""
        return self._market_events.market_events(now) + self._earnings.earnings_jumps(now, opt.OPTION_HOURS * 3600)

    def _price_at(self, symbol: str, ts: float) -> float | None:
        """ts 以前の、最後の価格（満期の価格）。その前の記録がなければ、いまの価格。"""
        v = self._scalar(
            "SELECT price FROM market_prices WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT 1", (symbol, ts)
        )
        if v is not None:
            return v
        return self._market.latest_prices().get(symbol)

    def _open_premium(self, guild_id: int, user_id: int) -> int:
        """有効な買いの、掛け金の合計。"""
        return self._scalar(
            "SELECT COALESCE(SUM(premium), 0) FROM options WHERE guild_id=? AND user_id=? AND status='active' AND side='long'",
            (guild_id, user_id),
        )

    def _open_margin(self, guild_id: int, user_id: int) -> int:
        """有効な裸売りの、証拠金の合計。"""
        return self._scalar(
            "SELECT COALESCE(SUM(sm.margin), 0) "
            "FROM options o JOIN option_short_margins sm ON sm.option_id = o.id "
            "WHERE o.guild_id=? AND o.user_id=? AND o.status='active' AND o.side='short'",
            (guild_id, user_id),
        )

    def option_buy(
        self,
        user_id: int,
        kind: str,
        symbol: str,
        amount: int,
        now: float,
        symbol2: str | None = None,
        strike_pct: float | None = None,
    ) -> Outcome[OptionBuyResult]:
        """amount（掛け金）の範囲で、買えるだけ買う。ペア・ワラントは、symbol が symbol2 より上がると当たり。"""
        if kind not in opt.KINDS or symbol not in market_mod.BY_KEY:
            return False, fail("invalid_spec")
        if kind == opt.PAIR and (symbol2 is None or symbol2 not in market_mod.BY_KEY or symbol2 == symbol):
            return False, fail("option_pair_same_symbol")
        if strike_pct is not None and not 0 < strike_pct <= opt.MAX_TARGET_PCT / 100:
            return False, fail("option_target_pct_range", max_pct=opt.MAX_TARGET_PCT)
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            if amount < opt.MIN_PREMIUM:
                return False, fail("option_min_premium", min_premium=opt.MIN_PREMIUM)
            debt = self._account.debt(gid, user_id)
            if debt > 0:
                return False, fail("debt_blocks", debt=debt, verb="買えません")
            prices = self._market.latest_prices()
            spot = prices.get(symbol)
            spot2 = prices.get(symbol2) if kind == opt.PAIR and symbol2 is not None else None
            if spot is None or (kind == opt.PAIR and spot2 is None):
                return False, fail("market_no_price")
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
                return False, fail("option_buy_too_small")
            bal = self._account.raw_balance(gid, user_id)
            if bal < cost:
                return False, fail("option_insufficient_balance", cost=cost, balance=bal)
            open_prem = self._open_premium(gid, user_id)
            if open_prem + cost > opt.MAX_OPEN_PREMIUM:
                left = max(0, opt.MAX_OPEN_PREMIUM - open_prem)
                return False, fail("option_max_open_premium", max_open=opt.MAX_OPEN_PREMIUM, left=left)
            label = opt.KINDS[kind][0]
            self._account.apply(gid, user_id, -cost, f"{label}購入:{market_mod.BY_KEY[symbol].name}")
            self._bank.bank_income_for(gid, cost, now)
            expiry = now + seconds
            cur = self._conn.execute(
                "INSERT INTO options (guild_id, user_id, kind, symbol, strike, units, premium, expiry, spot, ts, side) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'long')",
                (gid, user_id, kind, symbol, strike, units, cost, expiry, spot, now),
            )
            option_id = cur.lastrowid
            assert option_id is not None
            if kind == opt.PAIR and symbol2 is not None and spot2 is not None:
                self._conn.execute(
                    "INSERT INTO option_pairs (option_id, symbol2, spot2) VALUES (?, ?, ?)",
                    (option_id, symbol2, spot2),
                )
            return True, {
                "id": option_id,
                "units": units,
                "cost": cost,
                "strike": strike,
                "expiry": expiry,
                "spot": spot,
                "spot2": spot2,
            }

    def option_write(
        self,
        user_id: int,
        kind: str,
        symbol: str,
        amount: int,
        now: float,
        strike_pct: float | None = None,
    ) -> Outcome[OptionWriteResult]:
        """帝愛に売って、プレミアムを先にもらう（裸売り）。amount は預ける証拠金の上限。(成功か, 理由か {...}) を返す。"""
        if kind not in opt.SHORT_NAMES or symbol not in market_mod.BY_KEY:
            return False, fail("invalid_spec")
        if strike_pct is not None and not 0 < strike_pct <= opt.MAX_TARGET_PCT / 100:
            return False, fail("option_target_pct_range", max_pct=opt.MAX_TARGET_PCT)
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            if amount < opt.MIN_PREMIUM:
                return False, fail("option_min_margin", min_premium=opt.MIN_PREMIUM)
            debt = self._account.debt(gid, user_id)
            if debt > 0:
                return False, fail("debt_blocks", debt=debt, verb="売れません")
            spot = self._market.latest_prices().get(symbol)
            if spot is None:
                return False, fail("market_no_price")
            strike = opt.strike_for(kind, spot, opt.default_strike_pct(symbol) if strike_pct is None else strike_pct)
            seconds = opt.OPTION_HOURS * 3600
            per_margin = opt.margin_per_unit(strike)
            units = int(amount / per_margin)
            if units < 1:
                return False, fail("option_write_too_small")
            margin = math.ceil(round(units * per_margin, 6))
            bid = opt.bid_per_unit(kind, symbol, spot, strike, seconds, now, self._option_events(now))
            premium = int(bid * units)
            bal = self._account.raw_balance(gid, user_id)
            if bal < margin:
                return False, fail("margin_insufficient_collateral", need=margin, balance=bal)
            open_margin = self._open_margin(gid, user_id)
            if open_margin + margin > opt.MAX_SHORT_MARGIN:
                left = max(0, opt.MAX_SHORT_MARGIN - open_margin)
                return False, fail("option_max_short_margin", max_margin=opt.MAX_SHORT_MARGIN, left=left)
            available = int(self._bank.bank_sync_for(gid, now)["cash"] - self._bank.bank_locks_for(gid, now))
            if premium > available:
                return False, fail("option_bank_no_cash")
            self._account.apply(
                gid, user_id, -margin, f"{opt.SHORT_NAMES[kind]}の証拠金:{market_mod.BY_KEY[symbol].name}"
            )
            paid = self._bank.bank_pay_for(gid, premium, now)
            if paid:
                self._account.apply(
                    gid, user_id, paid, f"{opt.SHORT_NAMES[kind]}のプレミアム:{market_mod.BY_KEY[symbol].name}"
                )
            expiry = now + seconds
            cur = self._conn.execute(
                "INSERT INTO options (guild_id, user_id, kind, symbol, strike, units, premium, expiry, spot, ts, side) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'short')",
                (gid, user_id, kind, symbol, strike, units, paid, expiry, spot, now),
            )
            option_id = cur.lastrowid
            assert option_id is not None
            self._conn.execute(
                "INSERT INTO option_short_margins (option_id, margin) VALUES (?, ?)",
                (option_id, margin),
            )
            return True, {
                "id": option_id,
                "units": units,
                "premium": paid,
                "margin": margin,
                "strike": strike,
                "expiry": expiry,
                "spot": spot,
            }

    def option_close_cost(self, row: OptionRow, now: float, prices: Mapping[str, float]) -> int:
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

    def option_value(self, row: OptionRow, now: float, prices: Mapping[str, float]) -> int:
        """いま手放したときの価値。買いは売値の手取り（満期を過ぎていれば、満期の価格での配当）。裸売りは、証拠金 − 買い戻しの費用（マイナスもある）。"""
        if row["side"] == "short":
            return row["margin"] - self.option_close_cost(row, now, prices)
        spot = prices.get(row["symbol"])
        if spot is None:
            return 0
        if row["kind"] == opt.PAIR:
            symbol2 = row["symbol2"]
            row_spot2 = row["spot2"]
            if symbol2 is None or row_spot2 is None:
                return 0
            spot2 = prices.get(symbol2)
            if spot2 is None:
                return 0
            if row["expiry"] <= now:
                value = opt.pair_intrinsic(row["units"], row["spot"], row_spot2, spot, spot2)
            else:
                rate = opt.pair_bid_rate(
                    row["symbol"],
                    symbol2,
                    spot / row["spot"],
                    spot2 / row_spot2,
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

    def option_values(self) -> dict[int, int]:
        """{ユーザーID: 持っているオプションの評価額の合計}（このサーバー。ランキングの総資産に含める。裸売りの分は、マイナスになりうる）。"""
        with self._lock:
            out: dict[int, int] = {}
            self.collect_values(self._g(), out)
            return out

    def collect_values(self, guild_id: int, totals: dict[int, int]) -> None:
        with self._lock:
            now, prices = self._now(), self._market.latest_prices()
            for r in self._all(f"{_SELECT_OPTION} WHERE o.guild_id=? AND o.status='active'", (guild_id,)):
                totals[r["user_id"]] = totals.get(r["user_id"], 0) + self.option_value(r, now, prices)

    def option_list(self, user_id: int, recent: int = 5) -> HoldingBook:
        """有効なもの（満期が近い順）と、最近決着したもの（新しい順）。"""
        with self._lock:
            rows = self._all(f"{_SELECT_OPTION} WHERE o.guild_id=? AND o.user_id=? ORDER BY o.id", (self._g(), user_id))
        active = sorted((r for r in rows if r["status"] == "active"), key=lambda r: r["expiry"])
        done = [r for r in reversed(rows) if r["status"] != "active"][:recent]
        return {"active": active, "done": done}

    def active_option_ids_for(self, guild_id: int, user_id: int) -> list[int]:
        """持っている（status='active' の）建玉の ID（古い順）。呼び出し側のトランザクションの中で使う。"""
        return [
            r["id"]
            for r in self._all(
                "SELECT id FROM options WHERE guild_id=? AND user_id=? AND status='active' ORDER BY id",
                (guild_id, user_id),
            )
        ]

    def option_close_locked_for(self, guild_id: int, user_id: int, option_id: int, now: float) -> Outcome[int]:
        row = self._one(
            f"{_SELECT_OPTION} WHERE o.id=? AND o.guild_id=? AND o.user_id=? AND o.status='active'",
            (option_id, guild_id, user_id),
        )
        if row is None:
            return False, fail("option_not_found")
        prices = self._market.latest_prices()
        if row["side"] == "short":
            cost = self.option_close_cost(row, now, prices)
            back = row["margin"] - cost
            self._bank.bank_income_for(guild_id, cost, now)  # 買い戻しの費用は、銀行に入る
            # 先に決済済みにする（超えた分の支払いで差し押さえが走っても、同じ建玉を、もう一度買い戻そうとしない）
            self._conn.execute("UPDATE options SET status='sold', paid=? WHERE id=?", (max(0, back), option_id))
            if back > 0:
                self._account.apply(guild_id, user_id, back, f"{name_of(row)}の買い戻し（証拠金の戻り）")
            elif back < 0:  # 買い戻しの費用が証拠金を超えた分は、強制執行と借金で払う
                self._debt.owe_for(guild_id, user_id, -back, f"{name_of(row)}の買い戻し")
            return True, back
        value = self.option_value(row, now, prices)
        paid = self._bank.bank_pay_for(guild_id, value, now) if value > 0 else 0
        if paid:
            self._account.apply(guild_id, user_id, paid, f"{name_of(row)}売却:{market_mod.BY_KEY[row['symbol']].name}")
        self._conn.execute("UPDATE options SET status='sold', paid=? WHERE id=?", (paid, option_id))
        return True, paid

    def option_sell(self, user_id: int, option_id: int, now: float) -> Outcome[int]:
        """満期の前に手放す。買いは売値で売り、裸売りは買値で買い戻す。(成功か, 理由か 受け取った額（裸売りは、証拠金の戻り。マイナスは、借金にした額）) を返す。"""
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            held = self._one(
                "SELECT kind, symbol, side FROM options WHERE id=? AND guild_id=? AND user_id=? AND status='active'",
                (option_id, gid, user_id),
            )
            # 持っているコールは、ストップ安では売れない
            if (
                held
                and held["side"] != "short"
                and held["kind"] == "call"
                and (reason := self._market.pin_reason(held["symbol"], "sell_call"))
            ):
                return False, reason
            return self.option_close_locked_for(gid, user_id, option_id, now)

    def option_settle(self, now: float) -> list[OptionSettleRow]:
        """満期を過ぎたオプションを決済する（このサーバー全員分）。決済した行（status と paid つき）を返す。"""
        out: list[OptionSettleRow] = []
        with self._db.tx():
            gid = self._g()
            due: list[tuple[OptionRow, float, int]] = []
            rows: list[OptionRow] = self._all(
                f"{_SELECT_OPTION} WHERE o.guild_id=? AND o.status='active' AND o.expiry<=? ORDER BY o.id", (gid, now)
            )
            for r in rows:
                final = self._price_at(r["symbol"], r["expiry"])
                symbol2 = r["symbol2"] if r["kind"] == opt.PAIR else None
                final2 = self._price_at(symbol2, r["expiry"]) if symbol2 is not None else None
                if final is None or (r["kind"] == opt.PAIR and final2 is None):
                    continue  # 満期の価格が読めない建玉は、今回は決済しない
                match r["kind"]:
                    case opt.PAIR:
                        assert final2 is not None and r["spot2"] is not None
                        gross = opt.pair_intrinsic(r["units"], r["spot"], r["spot2"], final, final2)
                    case _:
                        gross = opt.intrinsic(r["kind"], r["units"], final, r["strike"])
                if r["side"] != "short":
                    gross = min(gross, r["premium"] * opt.PAYOUT_CAP_MULT)
                due.append((r, final, gross))
            # 同時刻の満期は、売り手からの入金と買い手への支払いを純額にして、サーモスタットに1回だけ通す
            # （1件ずつ通すと、建玉の id 順で burn・bailout が変わるため）。入金を先に済ませ、支払いは
            # bank_pay_batch_for でまとめて払う（信託の第一損失のあとで、超過損失の分だけ熱浴が肩代わりする。#33）
            inflow = sum(g for r, _, g in due if r["side"] == "short")
            longs = [(r, g) for r, _, g in due if r["side"] != "short"]
            paid_of: dict[int, int] = {}
            shortfalls: list[tuple[int, int, str]] = []
            for r, final, gross in sorted(due, key=lambda d: d[0]["side"] != "short"):  # 売り手（入金）が先
                if r["side"] != "short" and not paid_of:
                    pays = self._bank.bank_pay_batch_for(gid, [g for _, g in longs], now, offset=inflow)
                    paid_of = {lr["id"]: p for (lr, _), p in zip(longs, pays, strict=True)}
                self._account.ensure(gid, r["user_id"])
                status = "exercised" if gross > 0 else "expired"
                match r["side"]:
                    case "short":
                        paid = max(0, r["margin"] - gross)  # 証拠金の戻り
                        self._bank.bank_income_for(gid, gross, now, thermo=False)
                        if paid:
                            self._account.apply(gid, r["user_id"], paid, f"{name_of(r)}の満期（証拠金の戻り）")
                        if gross > r["margin"]:  # 損失が証拠金を超えた分は、全建玉の確定後に強制執行と借金で払う
                            shortfalls.append((r["user_id"], gross - r["margin"], f"{name_of(r)}の損失"))
                    case _:
                        paid = paid_of[r["id"]]
                        if paid:
                            self._account.apply(
                                gid,
                                r["user_id"],
                                paid,
                                f"{name_of(r)}の配当:{market_mod.BY_KEY[r['symbol']].name}",
                                income=max(0, paid - r["premium"]),
                            )
                self._conn.execute(
                    "UPDATE options SET status=?, paid=?, final=? WHERE id=?", (status, paid, final, r["id"])
                )
                out.append({**r, "status": status, "paid": paid, "final": final, "gross": gross})
            if not longs and inflow:
                self._bank.bank_thermo_batch_for(gid, inflow, now)
            # 同一バッチの満期建玉をすべて確定させてから強制執行する（処理中の建玉が差し押さえで二重決済されるのを防ぐ）
            for uid, shortage, reason in shortfalls:
                self._debt.owe_for(gid, uid, shortage, reason)
        return sorted(out, key=lambda o: o["id"])
