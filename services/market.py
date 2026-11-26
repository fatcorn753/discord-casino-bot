"""証券・債券・オプション市場のアプリケーションサービス（価格進行・集計・スナップショット）。"""

import logging
import random
from collections.abc import Mapping

from domain import bonds, earnings, market, market_events
from domain import options as opt
from domain.errors import Outcome
from domain.options import name_of
from domain.ports.clock import IClock
from domain.ports.contracts import (
    BondQuote,
    ChartBoardRow,
    ChartPostRow,
    EarningsAnnouncement,
    EarningsAnnounceRow,
    EarningsJump,
    EarningsRow,
    HoldingsSnapshot,
    MarginPosition,
    MarginStatus,
    MarketEventRow,
    OptionBuyResult,
    OptionRow,
    OptionWriteResult,
    PricePoint,
    SpotHolding,
)
from domain.ports.repos import (
    IBondRepo,
    ICryptoRepo,
    IEarningsRepo,
    IMarginRepo,
    IMarketDataRepo,
    IMarketEventRepo,
    IOptionRepo,
)
from domain.types import CryptoResult

_log = logging.getLogger(__name__)


class MarketService:
    """市場価格の進行・ボラティリティ/決算更新・保有スナップショットを提供する。"""

    def __init__(
        self,
        market_data: IMarketDataRepo,
        market_events_repo: IMarketEventRepo,
        earnings_repo: IEarningsRepo,
        crypto: ICryptoRepo,
        margin: IMarginRepo,
        bonds_repo: IBondRepo,
        options_repo: IOptionRepo,
        clock: IClock,
    ) -> None:
        self._market = market_data
        self._market_events = market_events_repo
        self._earnings = earnings_repo
        self._crypto = crypto
        self._margin = margin
        self._bonds = bonds_repo
        self._options = options_repo
        self.clock = clock

    def latest_price(self, symbol: str = market.DEFAULT) -> float:
        latest = self._market.market_latest(symbol)
        if latest is None:
            start = market.BY_KEY[symbol].start
            self._market.market_set(self.clock.now_ts(), start, 0, symbol)
            return start
        return latest[1]

    def latest_prices(self) -> dict[str, float]:
        return {s.key: self.latest_price(s.key) for s in market.SYMBOLS}

    def day_reference(self, now: float) -> dict[str, float]:
        """値幅制限の基準になる約24時間前の価格 {銘柄: 価格}。"""
        out: dict[str, float] = {}
        for sym in market.SYMBOLS:
            hist = self._market.market_history(now - 86400, sym.key)
            if hist:
                out[sym.key] = hist[0][1]
        return out

    def step_prices(self, earnings_results: list[EarningsRow], rng: random.Random) -> tuple[dict[str, float], int]:
        """全銘柄の価格を1ステップ進め、(新価格辞書, 有事フラグ) を返す。"""
        current = self.latest_prices()
        fair = {k: self._market.market_fair_get(k) or p for k, p in current.items()}
        last_ts = max((self._market.market_latest(k) or (0,))[0] for k in current)
        now = self.clock.now_ts()
        announced = self._market_events.event_step(now, rng)
        if announced is not None:
            _log.info(
                f"ボラティリティ予告: {announced['symbol']} {market_events.LEAD_MIN}分後から{market_events.DURATION_MIN}分"
            )
        self._earnings.earnings_ensure(now, self.clock.offset_hours())
        new_fair, event = market.next_prices(fair, rng, self._market_events.vol_multipliers(now))
        for r in self._earnings.earnings_release_due(now):
            hit = new_fair[r["symbol"]] * earnings.factor(r["direction"], r["size"])
            new_fair[r["symbol"]] = min(market.MAX_PRICE, max(market.MIN_PRICE, round(hit, 2)))
            earnings_results.append(r)
        ref = self.day_reference(now)
        new = {k: round(v, 2) for k, v in market.limit_to_day(new_fair, ref).items()}
        pins = market.pin_states(new_fair, ref)
        ts = max(self.clock.now_ts(), last_ts + 0.001)
        for key, p in new.items():
            self._market.market_set(ts, p, event, key)
            self._market.market_fair_set(key, new_fair[key], pins.get(key, 0), ts)
        return new, event

    def histories(self, minutes: int, now: float | None = None) -> dict[str, list[PricePoint]]:
        now = self.clock.now_ts() if now is None else now
        since = now - minutes * 60
        return {s.key: self._market.market_history(since, s.key) for s in market.SYMBOLS}

    def positions(self, guild_id: int, uid: int) -> list[tuple[str, str]]:
        """決済可能な持ち物 [(コード, 表示ラベル), ...]。現物・信用は割合で、オプション・債券は全部だけ決済できる。"""
        crypto_g = self._crypto.for_guild(guild_id)
        margin_g = self._margin.for_guild(guild_id)
        options_g = self._options.for_guild(guild_id)
        bonds_g = self._bonds.for_guild(guild_id)
        out: list[tuple[str, str]] = []
        for symbol, _units, _cost in crypto_g.holdings(uid):
            out.append((f"spot:{symbol}", f"{market.BY_KEY[symbol].emoji} {market.BY_KEY[symbol].name} 現物"))
        for side, symbol, *_ in margin_g.margin_positions(uid):
            name = market.BY_KEY[symbol]
            out.append((f"{side}:{symbol}", f"{name.emoji} {name.name} {market.SIDE_NAME[side]}"))
        for r in options_g.option_list(uid)["active"]:
            short = r["side"] == "short"
            icon = "🎰" if short else opt.KINDS[r["kind"]][1]
            out.append((f"opt:{r['id']}", f"{icon} {name_of(r)} #{r['id']} {'買い戻し' if short else '売却'}"))
        for r in bonds_g.bond_list(uid)["active"]:
            k = bonds.KINDS[r["kind"]]
            if k.sellable:
                out.append((f"bond:{r['id']}", f"{k.emoji} {k.name} #{r['id']} 売却"))
        return out

    def holdings_snapshot(self, guild_id: int, uid: int, rng: random.Random) -> HoldingsSnapshot:
        """保有画面表示用に満期債券・オプションを決済したうえで全保有スナップショットを返す。"""
        now = self.clock.now_ts()
        crypto_g = self._crypto.for_guild(guild_id)
        margin_g = self._margin.for_guild(guild_id)
        bonds_g = self._bonds.for_guild(guild_id)
        options_g = self._options.for_guild(guild_id)
        bonds_g.bond_settle(now, rng)
        options_g.option_settle(now)
        prices = self.latest_prices()
        options = options_g.option_list(uid)
        spots = {symbol: (units, cost) for symbol, units, cost in crypto_g.holdings(uid)}
        margins = {
            (side, symbol): (units, entry, margin)
            for side, symbol, units, entry, margin in margin_g.margin_positions(uid)
        }
        bond_data = bonds_g.bond_list(uid)
        option_quotes = {
            r["id"]: (
                options_g.option_close_cost(r, now, prices)
                if r["side"] == "short"
                else options_g.option_value(r, now, prices)
            )
            for r in options["active"]
        }
        bond_quotes = {
            r["id"]: bonds_g.bond_price(r, now) for r in bond_data["active"] if bonds.KINDS[r["kind"]].sellable
        }
        margin_status = margin_g.margin_status(uid, prices) if margins else None
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

    # ------------------------------ 定時処理・チャート掲示（Cog の tick から） ------------------------------ #
    def accrue_and_losscut(self, now: float, prices: dict[str, float]) -> list[tuple[int, int, str, str, int]]:
        """信用取引の金利を付け、維持率を割った建玉を強制決済する。決済した (guild, user, symbol, side, loss) の一覧。"""
        self._margin.margin_interest(now)
        return self._margin.margin_losscut(prices)

    def prune(self, before: float) -> None:
        """古い価格の記録とチャート掲示の記録を消す。"""
        self._market.market_prune(before)
        self._market.prune_chart_posts(before)

    def settle_maturities(self, guild_id: int, now: float, rng: random.Random) -> None:
        """満期の債券の入金・倒産抽選と、満期のオプションの決済。"""
        self._bonds.for_guild(guild_id).bond_settle(now, rng)
        self._options.for_guild(guild_id).option_settle(now)

    def market_history(self, since: float, symbol: str) -> list[PricePoint]:
        return self._market.market_history(since, symbol)

    def market_pin(self, symbol: str) -> int:
        return self._market.market_pin(symbol)

    def market_events(self, now: float) -> list[MarketEventRow]:
        return self._market_events.market_events(now)

    def option_events(self, now: float) -> list[MarketEventRow | EarningsJump]:
        """オプションの値付けに含める出来事（ボラティリティ予告 + 満期までの決算ジャンプ）。"""
        return list(self._market_events.market_events(now)) + list(
            self._earnings.earnings_jumps(now, opt.OPTION_HOURS * 3600)
        )

    def earnings_upcoming(self, now: float) -> EarningsAnnouncement | None:
        return self._earnings.earnings_upcoming(now)

    def earnings_to_announce(self, now: float) -> list[EarningsAnnounceRow]:
        return self._earnings.earnings_to_announce(now)

    def crypto_holders(self, guild_id: int, rows: int, symbol: str) -> list[tuple[int, int, int]]:
        return self._crypto.for_guild(guild_id).crypto_holders(rows, symbol)

    def short_holders(self, guild_id: int, rows: int, symbol: str) -> list[tuple[int, int, int]]:
        return self._margin.for_guild(guild_id).short_holders(rows, symbol)

    def margin_cut_map(self, guild_id: int, prices: dict[str, float]) -> list[tuple[str, str, float, int]]:
        return self._margin.for_guild(guild_id).margin_cut_map(prices)

    def live_chart_posts(self) -> list[ChartPostRow]:
        return self._market.live_chart_posts()

    def swap_chart_post(
        self, channel_id: int, message_id: int, guild_id: int, period: str, now: float, symbol: str
    ) -> int | None:
        return self._market.swap_chart_post(channel_id, message_id, guild_id, period, now, symbol)

    def end_chart_post(self, channel_id: int) -> None:
        self._market.end_chart_post(channel_id)

    def delete_chart_post(self, channel_id: int) -> None:
        self._market.delete_chart_post(channel_id)

    def delete_guild_chart_posts(self, guild_id: int) -> None:
        self._market.delete_guild_chart_posts(guild_id)

    def list_chart_boards(self) -> list[ChartBoardRow]:
        return self._market.list_chart_boards()

    def delete_chart_board(self, guild_id: int) -> None:
        self._market.delete_chart_board(guild_id)

    # ------------------------------ 現物 ------------------------------ #
    def crypto_holding(self, guild_id: int, uid: int, symbol: str) -> SpotHolding:
        return self._crypto.for_guild(guild_id).crypto_holding(uid, symbol)

    def crypto_buy(self, guild_id: int, uid: int, amount: int, price: float, symbol: str) -> CryptoResult:
        return self._crypto.for_guild(guild_id).crypto_buy(uid, amount, price, symbol)

    def crypto_sell(self, guild_id: int, uid: int, units: int | None, price: float, symbol: str) -> CryptoResult:
        return self._crypto.for_guild(guild_id).crypto_sell(uid, units, price, symbol)

    # ------------------------------ 信用取引 ------------------------------ #
    def margin_position(self, guild_id: int, uid: int, symbol: str, side: str) -> MarginPosition:
        return self._margin.for_guild(guild_id).margin_position(uid, symbol, side)

    def margin_open(
        self, guild_id: int, uid: int, side: str, units: int, price: float, leverage: int, symbol: str
    ) -> CryptoResult:
        """side が "short" なら空売り、それ以外は信用買いを建てる。"""
        repo = self._margin.for_guild(guild_id)
        return (repo.short_open if side == "short" else repo.long_open)(uid, units, price, leverage, symbol)

    def margin_close(
        self, guild_id: int, uid: int, side: str, units: int | None, price: float, symbol: str
    ) -> CryptoResult:
        return self._margin.for_guild(guild_id).margin_close(uid, side, units, price, symbol)

    def margin_status(self, guild_id: int, uid: int, prices: Mapping[str, float]) -> MarginStatus:
        return self._margin.for_guild(guild_id).margin_status(uid, prices)

    # ------------------------------ オプション ------------------------------ #
    def option_buy(
        self,
        guild_id: int,
        uid: int,
        kind: str,
        symbol: str,
        amount: int,
        *,
        symbol2: str | None = None,
        strike_pct: float | None = None,
    ) -> Outcome[OptionBuyResult]:
        return self._options.for_guild(guild_id).option_buy(
            uid, kind, symbol, amount, self.clock.now_ts(), symbol2=symbol2, strike_pct=strike_pct
        )

    def option_write(
        self, guild_id: int, uid: int, kind: str, symbol: str, amount: int, *, strike_pct: float | None = None
    ) -> Outcome[OptionWriteResult]:
        return self._options.for_guild(guild_id).option_write(
            uid, kind, symbol, amount, self.clock.now_ts(), strike_pct=strike_pct
        )

    def option_settle(self, guild_id: int, now: float) -> None:
        self._options.for_guild(guild_id).option_settle(now)

    def active_options(self, guild_id: int, uid: int) -> list[OptionRow]:
        return self._options.for_guild(guild_id).option_list(uid)["active"]

    def option_close_cost(self, guild_id: int, row: OptionRow, now: float, prices: dict[str, float]) -> int:
        return self._options.for_guild(guild_id).option_close_cost(row, now, prices)

    def option_value(self, guild_id: int, row: OptionRow, now: float, prices: dict[str, float]) -> int:
        return self._options.for_guild(guild_id).option_value(row, now, prices)

    def option_sell(self, guild_id: int, uid: int, option_id: int) -> Outcome[int]:
        return self._options.for_guild(guild_id).option_sell(uid, option_id, self.clock.now_ts())

    # ------------------------------ 債券 ------------------------------ #
    def bond_settle(self, guild_id: int, now: float, rng: random.Random) -> None:
        self._bonds.for_guild(guild_id).bond_settle(now, rng)

    def bond_buy(self, guild_id: int, uid: int, kind: str, amount: int, now: float) -> Outcome[str]:
        return self._bonds.for_guild(guild_id).bond_buy(uid, kind, amount, now)

    def bond_quote(self, guild_id: int, kind: str, amount: int, now: float) -> BondQuote:
        return self._bonds.for_guild(guild_id).bond_quote(kind, amount, now)

    def bond_cancel(self, guild_id: int, uid: int, bond_id: int) -> Outcome[int]:
        return self._bonds.for_guild(guild_id).bond_cancel(uid, bond_id)
