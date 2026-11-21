"""証券市場・現物・信用・債券・オプション・決算のリポジトリポート契約。"""

import random
from collections.abc import Mapping
from typing import TYPE_CHECKING, Optional

from domain.errors import Outcome
from domain.ports.bridges import (
    IMarginCollateralGuard,
    ISpotCollateralReader,
)
from domain.ports.contracts import (
    BondQuote,
    BondRow,
    ChartBoardRow,
    ChartPostRow,
    EarningsAnnouncement,
    EarningsAnnounceRow,
    EarningsJump,
    EarningsRow,
    HoldingBook,
    InsiderStock,
    MarginPosition,
    MarginStatus,
    MarketEventPlan,
    MarketEventRow,
    OptionBuyResult,
    OptionRow,
    OptionSettleRow,
    OptionWriteResult,
    PricePoint,
    SpotHolding,
)
from domain.ports.repos.base import IGuildScopedRepo

if TYPE_CHECKING:
    from domain.earnings import MemoInfo
    from domain.types import CryptoResult


class IMarketDataRepo(IGuildScopedRepo):
    def latest_prices(self) -> dict[str, float]:
        raise NotImplementedError

    def market_latest(self, symbol: str = "cs") -> Optional[PricePoint]:
        raise NotImplementedError

    def market_set(self, ts: float, price: float, event: int = 0, symbol: str = "cs") -> None:
        raise NotImplementedError

    def market_fair_get(self, symbol: str) -> Optional[float]:
        raise NotImplementedError

    def market_fair_set(self, symbol: str, fair: float, pin: int, ts: float) -> None:
        raise NotImplementedError

    def market_pin(self, symbol: str) -> int:
        raise NotImplementedError

    def market_history(self, since: float, symbol: str = "cs") -> list[PricePoint]:
        raise NotImplementedError

    def market_prune(self, before: float) -> None:
        raise NotImplementedError

    def swap_chart_post(
        self, channel_id: int, message_id: int, guild_id: int, period: str, ts: float, symbol: str = "cs"
    ) -> Optional[int]:
        raise NotImplementedError

    def live_chart_posts(self) -> list[ChartPostRow]:
        raise NotImplementedError

    def end_chart_post(self, channel_id: int) -> None:
        raise NotImplementedError

    def delete_chart_post(self, channel_id: int) -> None:
        raise NotImplementedError

    def delete_guild_chart_posts(self, guild_id: int) -> None:
        raise NotImplementedError

    def prune_chart_posts(self, before_ts: float) -> None:
        raise NotImplementedError

    def list_chart_boards(self) -> list[ChartBoardRow]:
        raise NotImplementedError

    def delete_chart_board(self, guild_id: int) -> None:
        raise NotImplementedError


class IMarketEventRepo(IGuildScopedRepo):
    def market_events(self, now: float) -> list[MarketEventRow]:
        raise NotImplementedError

    def vol_multipliers(self, now: float) -> dict[str, float]:
        raise NotImplementedError

    def event_step(
        self, now: float, rng: Optional[random.Random] = None, force: Optional[MarketEventPlan] = None
    ) -> Optional[MarketEventRow]:
        raise NotImplementedError


class IEarningsRepo(IGuildScopedRepo):
    def earnings_ensure(
        self, now: float, tz_hours: float, rng: Optional[random.Random] = None
    ) -> Optional[EarningsRow]:
        raise NotImplementedError

    def earnings_jumps(self, now: float, seconds: float) -> list[EarningsJump]:
        raise NotImplementedError

    def earnings_upcoming(self, now: float) -> Optional[EarningsAnnouncement]:
        raise NotImplementedError

    def earnings_to_announce(self, now: float) -> list[EarningsAnnounceRow]:
        raise NotImplementedError

    def earnings_release_due(self, now: float) -> list[EarningsRow]:
        raise NotImplementedError

    def earnings_memo(self, user_id: int, now: float, rng: Optional[random.Random] = None) -> Optional["MemoInfo"]:
        raise NotImplementedError

    def insider_stock(self, now: float) -> Optional[InsiderStock]:
        raise NotImplementedError

    def insider_buy(self, user_id: int, now: float, rng: Optional[random.Random] = None) -> "Outcome[MemoInfo]":
        raise NotImplementedError


class ICryptoRepo(IGuildScopedRepo, ISpotCollateralReader):
    def collect_values(self, guild_id: int, totals: dict[int, int]) -> None:
        raise NotImplementedError

    def crypto_holding(self, user_id: int, symbol: str = "cs") -> SpotHolding:
        raise NotImplementedError

    def holdings(self, user_id: int) -> list[tuple[str, int, int]]:
        raise NotImplementedError

    def crypto_holders(self, limit: int = 10, symbol: str = "cs") -> list[tuple[int, int, int]]:
        raise NotImplementedError

    def crypto_values(self) -> dict[int, int]:
        raise NotImplementedError

    def crypto_buy(self, user_id: int, amount: int, price: float, symbol: str = "cs") -> "CryptoResult":
        raise NotImplementedError

    def crypto_sell(self, user_id: int, units: Optional[int], price: float, symbol: str = "cs") -> "CryptoResult":
        raise NotImplementedError


class IMarginRepo(IGuildScopedRepo, IMarginCollateralGuard):
    def margin_ratio_for(self, guild_id: int, user_id: int) -> Optional[float]:
        raise NotImplementedError

    def collect_values(self, guild_id: int, totals: dict[int, int]) -> None:
        raise NotImplementedError

    def margin_position(self, user_id: int, symbol: str, side: str) -> MarginPosition:
        raise NotImplementedError

    def short_position(self, user_id: int, symbol: str = "cs") -> MarginPosition:
        raise NotImplementedError

    def long_position(self, user_id: int, symbol: str = "cs") -> MarginPosition:
        raise NotImplementedError

    def margin_positions(self, user_id: int) -> list[tuple[str, str, int, float, int]]:
        raise NotImplementedError

    def short_positions(self, user_id: int) -> list[tuple[str, int, float, int]]:
        raise NotImplementedError

    def long_positions(self, user_id: int) -> list[tuple[str, int, float, int]]:
        raise NotImplementedError

    def short_holders(self, limit: int = 10, symbol: str = "cs") -> list[tuple[int, int, int]]:
        raise NotImplementedError

    def margin_equity(self) -> dict[int, int]:
        raise NotImplementedError

    def margin_status(self, user_id: int, prices: Optional[Mapping[str, float]] = None) -> MarginStatus:
        raise NotImplementedError

    def short_open(self, user_id: int, units: int, price: float, leverage: int, symbol: str = "cs") -> "CryptoResult":
        raise NotImplementedError

    def long_open(self, user_id: int, units: int, price: float, leverage: int, symbol: str = "cs") -> "CryptoResult":
        raise NotImplementedError

    def margin_close(
        self, user_id: int, side: str, units: Optional[int], price: float, symbol: str = "cs"
    ) -> "CryptoResult":
        raise NotImplementedError

    def short_close(self, user_id: int, units: Optional[int], price: float, symbol: str = "cs") -> "CryptoResult":
        raise NotImplementedError

    def long_close(self, user_id: int, units: Optional[int], price: float, symbol: str = "cs") -> "CryptoResult":
        raise NotImplementedError

    def margin_cut_map(self, prices: Optional[Mapping[str, float]] = None) -> list[tuple[str, str, float, int]]:
        raise NotImplementedError

    def margin_interest(self, now: float) -> list[tuple[int, int, str, str, int]]:
        raise NotImplementedError

    def margin_losscut(self, prices: Mapping[str, float]) -> list[tuple[int, int, str, str, int]]:
        raise NotImplementedError


class IBondRepo(IGuildScopedRepo):
    def collect_values(self, guild_id: int, totals: dict[int, int]) -> None:
        raise NotImplementedError

    def bond_buy(self, user_id: int, kind_key: str, amount: int, now: float) -> Outcome[str]:
        raise NotImplementedError

    def bond_values(self) -> dict[int, int]:
        raise NotImplementedError

    def bond_list(self, user_id: int, recent: int = 5) -> HoldingBook[BondRow]:
        raise NotImplementedError

    def bond_settle(self, now: float, rng: Optional[random.Random] = None) -> list[BondRow]:
        raise NotImplementedError

    def bond_price(self, row: BondRow, now: float) -> int:
        raise NotImplementedError

    def bond_quote(self, kind_key: str, amount: int, now: float) -> BondQuote:
        raise NotImplementedError

    def bond_cancel(self, user_id: int, bond_id: int, now: Optional[float] = None) -> Outcome[int]:
        raise NotImplementedError


class IOptionRepo(IGuildScopedRepo):
    def collect_values(self, guild_id: int, totals: dict[int, int]) -> None:
        raise NotImplementedError

    def option_buy(
        self,
        user_id: int,
        kind: str,
        symbol: str,
        amount: int,
        now: float,
        symbol2: Optional[str] = None,
        strike_pct: Optional[float] = None,
    ) -> Outcome[OptionBuyResult]:
        raise NotImplementedError

    def option_write(
        self,
        user_id: int,
        kind: str,
        symbol: str,
        amount: int,
        now: float,
        strike_pct: Optional[float] = None,
    ) -> Outcome[OptionWriteResult]:
        raise NotImplementedError

    def option_close_cost(self, row: OptionRow, now: float, prices: Mapping[str, float]) -> int:
        raise NotImplementedError

    def option_value(self, row: OptionRow, now: float, prices: Mapping[str, float]) -> int:
        raise NotImplementedError

    def option_values(self) -> dict[int, int]:
        raise NotImplementedError

    def option_list(self, user_id: int, recent: int = 5) -> HoldingBook[OptionRow]:
        raise NotImplementedError

    def option_sell(self, user_id: int, option_id: int, now: float) -> Outcome[int]:
        raise NotImplementedError

    def option_settle(self, now: float) -> list[OptionSettleRow]:
        raise NotImplementedError
