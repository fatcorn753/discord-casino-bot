"""証券・オプション・市場イベント・決算カタログの Pydantic スキーマ。"""

from collections.abc import Mapping
from functools import lru_cache

from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from domain.ports.catalog_types.market import (
    CorrelationContract,
    EarningsSectionContract,
    MarketCatalogContract,
    MarketEventsSectionContract,
    MarketSectionContract,
    OptionKindContract,
    OptionsSectionContract,
    PeriodContract,
    SymbolContract,
    TaxBracketContract,
)
from infra.catalogs.loader import STRICT_CONFIG, load_yaml_model


# ------------------------------------------------------------------ #
# market.yaml
# ------------------------------------------------------------------ #
@pydantic_dataclass(frozen=True, config=STRICT_CONFIG, kw_only=True)
class SymbolConfig(SymbolContract):
    key: str = Field(min_length=1)
    name: str = Field(min_length=1)
    emoji: str = Field(min_length=1)
    start: float = Field(gt=0)
    sigma: float = Field(gt=0)
    drift: float = Field(ge=0)


@pydantic_dataclass(frozen=True, config=STRICT_CONFIG, kw_only=True)
class CorrelationConfig(CorrelationContract):
    pair: tuple[str, str]
    value: float = Field(ge=-1, le=1)


@pydantic_dataclass(frozen=True, config=STRICT_CONFIG, kw_only=True)
class TaxBracketConfig(TaxBracketContract):
    limit: int | None = None
    rate: float = Field(ge=0, le=1)


@pydantic_dataclass(frozen=True, config=STRICT_CONFIG, kw_only=True)
class PeriodConfig(PeriodContract):
    label: str = Field(min_length=1)
    seconds: int = Field(gt=0)


@pydantic_dataclass(frozen=True, config=STRICT_CONFIG, kw_only=True)
class MarketSection(MarketSectionContract):
    min_price: int = Field(gt=0)
    max_price: int = Field(gt=0)
    keep_days: int = Field(gt=0)
    confirm_from: int = Field(ge=0)
    event_interval_minutes: int = Field(gt=0)
    event_range: tuple[float, float]
    event_scale_max: float = Field(gt=0)
    unit: int = Field(gt=0)
    fee_rate: float = Field(ge=0)
    unit_name: str = Field(min_length=1)
    side_name: Mapping[str, str]
    symbols: tuple[SymbolConfig, ...] = Field(min_length=1)
    correlations: tuple[CorrelationConfig, ...]
    drift_band: float = Field(gt=1)
    daily_limit: float = Field(gt=0, lt=1)
    pin_locks: Mapping[int, tuple[str, ...]]
    pin_mark: Mapping[int, str]
    short_leverages: tuple[int, ...] = Field(min_length=1)
    short_min_margin: int = Field(gt=0)
    gains_tax_brackets: tuple[TaxBracketConfig, ...] = Field(min_length=1)
    borrow_rate_per_day: Mapping[str, float]
    interest_min_charge: int = Field(ge=0)
    risk_k: float = Field(gt=0)
    open_ratio: float = Field(gt=1)
    losscut_ratio: float = Field(gt=0)
    collateral_haircut: Mapping[str, float]
    levels: str = Field(min_length=2)
    periods: Mapping[str, PeriodConfig]


@pydantic_dataclass(frozen=True, config=STRICT_CONFIG, kw_only=True)
class OptionKindConfig(OptionKindContract):
    name: str = Field(min_length=1)
    emoji: str = Field(min_length=1)
    desc: str = Field(min_length=1)


@pydantic_dataclass(frozen=True, config=STRICT_CONFIG, kw_only=True)
class OptionsSection(OptionsSectionContract):
    option_hours: int = Field(gt=0)
    strike_otm: float = Field(gt=0)
    tick_seconds: int = Field(gt=0)
    spread: float = Field(ge=0)
    payout_cap_mult: int = Field(gt=0)
    min_premium: int = Field(gt=0)
    max_open_premium: int = Field(gt=0)
    margin_rate: float = Field(gt=0)
    max_short_margin: int = Field(gt=0)
    kinds: Mapping[str, OptionKindConfig]
    short_names: Mapping[str, str]
    strike_by_symbol: Mapping[str, float]
    max_target_pct: int = Field(gt=0)


@pydantic_dataclass(frozen=True, config=STRICT_CONFIG, kw_only=True)
class MarketEventsSection(MarketEventsSectionContract):
    lead_min: int = Field(gt=0)
    duration_min: int = Field(gt=0)
    factor: float = Field(gt=0)
    announce_interval_minutes: int = Field(gt=0)
    cooldown_min: int = Field(ge=0)
    priced_at_announce: float = Field(ge=0, le=1)
    keep_days: int = Field(gt=0)
    alert_window: int = Field(gt=0)
    z_alert: float = Field(gt=0)
    min_abs_corr: float = Field(ge=0, le=1)


@pydantic_dataclass(frozen=True, config=STRICT_CONFIG, kw_only=True)
class EarningsSection(EarningsSectionContract):
    symbols: tuple[str, ...] = Field(min_length=1)
    release_weekday: int = Field(ge=0, le=6)
    release_time: tuple[int, int]
    announce_weekday: int = Field(ge=0, le=6)
    announce_time: tuple[int, int]
    size_range: tuple[float, float]
    surprise_prob: float = Field(ge=0, le=1)
    surprise_range: tuple[float, float]
    memo_prob: float = Field(ge=0, le=1)
    memo_lead_hours: float = Field(gt=0)
    memo_truth: float = Field(gt=0.5, le=1)
    shop_truth: float = Field(gt=0.5, le=1)
    shop_stock: int = Field(gt=0)


@pydantic_dataclass(frozen=True, config=STRICT_CONFIG, kw_only=True)
class MarketCatalog(MarketCatalogContract):
    market: MarketSection
    options: OptionsSection
    events: MarketEventsSection
    earnings: EarningsSection


@lru_cache(maxsize=1)
def market() -> MarketCatalog:
    return load_yaml_model("market.yaml", MarketCatalog)
