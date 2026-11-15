"""証券・債券・オプション市場のアプリケーションサービス（価格進行・集計・スナップショット）。"""

import time
from typing import Any, Optional

from domain import bonds, earnings, market, market_events
from domain import options as opt
from domain.options import name_of
from domain.ports import IMarketPort
from infra.clock import offset_hours


class MarketService:
    """市場価格の進行・ボラティリティ/決算更新・保有スナップショットを提供する。"""

    def __init__(self, economy: IMarketPort, config: dict[str, Any]) -> None:
        self._economy = economy
        self._config = config

    def latest_price(self, symbol: str = market.DEFAULT) -> float:
        latest = self._economy.market_latest(symbol)
        if latest is None:
            start = market.BY_KEY[symbol].start
            self._economy.market_set(time.time(), start, 0, symbol)
            return start
        return latest[1]

    def latest_prices(self) -> dict[str, float]:
        return {s.key: self.latest_price(s.key) for s in market.SYMBOLS}

    def day_reference(self, now: float) -> dict[str, float]:
        """値幅制限の基準になる約24時間前の価格 {銘柄: 価格}。"""
        out: dict[str, float] = {}
        for sym in market.SYMBOLS:
            hist = self._economy.market_history(now - 86400, sym.key)
            if hist:
                out[sym.key] = hist[0][1]
        return out

    def step_prices(self, earnings_results: list, rng) -> tuple:
        """全銘柄の価格を1ステップ進め、(新価格辞書, 有事フラグ) を返す。"""
        current = self.latest_prices()
        fair = {k: self._economy.market_fair_get(k) or p for k, p in current.items()}
        last_ts = max((self._economy.market_latest(k) or (0,))[0] for k in current)
        now = time.time()
        announced = self._economy.event_step(now, rng)
        if announced is not None:
            print(
                f"ボラティリティ予告: {announced['symbol']} {market_events.LEAD_MIN}分後から{market_events.DURATION_MIN}分"
            )
        self._economy.earnings_ensure(now, offset_hours(self._config))
        new_fair, event = market.next_prices(fair, rng, self._economy.vol_multipliers(now))
        for r in self._economy.earnings_release_due(now):
            hit = new_fair[r["symbol"]] * earnings.factor(r["direction"], r["size"])
            new_fair[r["symbol"]] = min(market.MAX_PRICE, max(market.MIN_PRICE, round(hit, 2)))
            earnings_results.append(r)
        ref = self.day_reference(now)
        new = {k: round(v, 2) for k, v in market.limit_to_day(new_fair, ref).items()}
        pins = market.pin_states(new_fair, ref)
        ts = max(time.time(), last_ts + 0.001)
        for key, p in new.items():
            self._economy.market_set(ts, p, event, key)
            self._economy.market_fair_set(key, new_fair[key], pins.get(key, 0), ts)
        return new, event

    def histories(self, minutes: int, now: Optional[float] = None) -> dict[str, list]:
        now = time.time() if now is None else now
        since = now - minutes * 60
        return {s.key: self._economy.market_history(since, s.key) for s in market.SYMBOLS}

    def positions(self, guild_id: int, uid: int) -> list[tuple[str, str]]:
        """決済可能な持ち物 [(コード, 表示ラベル), ...]。"""
        eco = self._economy.for_guild(guild_id)
        out: list[tuple[str, str]] = []
        for symbol, _units, _cost in eco.holdings(uid):
            out.append((f"spot:{symbol}", f"{market.BY_KEY[symbol].emoji} {market.BY_KEY[symbol].name} 全売却"))
        for side, symbol, *_ in eco.margin_positions(uid):
            name = market.BY_KEY[symbol]
            out.append((f"{side}:{symbol}", f"{name.emoji} {name.name} {market.SIDE_NAME[side]}全決済"))
        for r in eco.option_list(uid)["active"]:
            short = r["side"] == "short"
            icon = "🎰" if short else opt.KINDS[r["kind"]][1]
            out.append((f"opt:{r['id']}", f"{icon} {name_of(r)} #{r['id']} {'買い戻し' if short else '売却'}"))
        for r in eco.bond_list(uid)["active"]:
            k = bonds.KINDS[r["kind"]]
            if k.sellable:
                out.append((f"bond:{r['id']}", f"{k.emoji} {k.name} #{r['id']} 売却"))
        return out

    def holdings_snapshot(self, guild_id: int, uid: int, rng) -> dict[str, Any]:
        """保有画面表示用に満期債券・オプションを決済したうえで全保有スナップショットを返す。"""
        now = time.time()
        guild_eco = self._economy.for_guild(guild_id)
        guild_eco.bond_settle(now, rng)
        guild_eco.option_settle(now)
        prices = self.latest_prices()
        options = guild_eco.option_list(uid)
        spots = {symbol: (units, cost) for symbol, units, cost in guild_eco.holdings(uid)}
        margins = {
            (side, symbol): (units, entry, margin)
            for side, symbol, units, entry, margin in guild_eco.margin_positions(uid)
        }
        bond_data = guild_eco.bond_list(uid)
        option_quotes = {
            r["id"]: (
                guild_eco.option_close_cost(r, now, prices)
                if r["side"] == "short"
                else guild_eco.option_value(r, now, prices)
            )
            for r in options["active"]
        }
        bond_quotes = {
            r["id"]: guild_eco.bond_price(r, now) for r in bond_data["active"] if bonds.KINDS[r["kind"]].sellable
        }
        margin_status = guild_eco.margin_status(uid, prices) if margins else None
        return {
            "prices": prices,
            "spots": spots,
            "margins": margins,
            "options": options,
            "bond_data": bond_data,
            "option_quotes": option_quotes,
            "bond_quotes": bond_quotes,
            "margin_status": margin_status,
        }
