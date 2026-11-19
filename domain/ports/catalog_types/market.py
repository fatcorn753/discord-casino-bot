"""証券・オプション・市場イベント・決算カタログの公称データ契約。"""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolContract:
    key: str
    name: str
    emoji: str
    start: float
    sigma: float
    drift: float


@dataclass(frozen=True)
class CorrelationContract:
    pair: tuple[str, str]
    value: float


@dataclass(frozen=True)
class TaxBracketContract:
    limit: int | None
    rate: float


@dataclass(frozen=True)
class PeriodContract:
    label: str
    seconds: int


@dataclass(frozen=True)
class MarketSectionContract:
    min_price: int
    max_price: int
    keep_days: int
    confirm_from: int
    event_interval_minutes: int
    event_range: tuple[float, float]
    event_scale_max: float
    unit: int
    fee_rate: float
    unit_name: str
    side_name: Mapping[str, str]
    symbols: tuple[SymbolContract, ...]
    correlations: tuple[CorrelationContract, ...]
    drift_band: float
    daily_limit: float
    pin_locks: Mapping[int, tuple[str, ...]]
    pin_mark: Mapping[int, str]
    short_leverages: tuple[int, ...]
    short_min_margin: int
    gains_tax_brackets: tuple[TaxBracketContract, ...]
    borrow_rate_per_day: Mapping[str, float]
    interest_min_charge: int
    risk_k: float
    open_ratio: float
    losscut_ratio: float
    collateral_haircut: Mapping[str, float]
    levels: str
    periods: Mapping[str, PeriodContract]


@dataclass(frozen=True)
class OptionKindContract:
    name: str
    emoji: str
    desc: str


@dataclass(frozen=True)
class OptionsSectionContract:
    option_hours: int
    strike_otm: float
    tick_seconds: int
    spread: float
    payout_cap_mult: int
    min_premium: int
    max_open_premium: int
    margin_rate: float
    max_short_margin: int
    kinds: Mapping[str, OptionKindContract]
    short_names: Mapping[str, str]
    strike_by_symbol: Mapping[str, float]
    max_target_pct: int


@dataclass(frozen=True)
class MarketEventsSectionContract:
    lead_min: int
    duration_min: int
    factor: float
    announce_interval_minutes: int
    cooldown_min: int
    priced_at_announce: float
    keep_days: int
    alert_window: int
    z_alert: float
    min_abs_corr: float


@dataclass(frozen=True)
class EarningsSectionContract:
    symbols: tuple[str, ...]
    release_weekday: int
    release_time: tuple[int, int]
    announce_weekday: int
    announce_time: tuple[int, int]
    size_range: tuple[float, float]
    surprise_prob: float
    surprise_range: tuple[float, float]
    memo_prob: float
    memo_lead_hours: float
    memo_truth: float
    shop_truth: float
    shop_stock: int


@dataclass(frozen=True)
class MarketCatalogContract:
    market: MarketSectionContract
    options: OptionsSectionContract
    events: MarketEventsSectionContract
    earnings: EarningsSectionContract
