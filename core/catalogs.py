"""YAML カタログの読み込みと Pydantic による境界検証（SSOT）。"""

from functools import lru_cache
from pathlib import Path
from typing import Self, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

CATALOG_DIR = Path(__file__).resolve().parent.parent / "catalogs"
T = TypeVar("T", bound=BaseModel)


class StrictModel(BaseModel):
    """未定義キーを禁止し、読み込み後は書き換え不可にする基底モデル。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


def load_yaml_model(name: str, schema: type[T], *, root: Path | None = None) -> T:
    """YAML ファイルを読み込み、Pydantic スキーマで検証して返す。"""
    path = (root or CATALOG_DIR) / name
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if raw is None:
        raw = {}
    return schema.model_validate(raw)


# ------------------------------------------------------------------ #
# shop.yaml
# ------------------------------------------------------------------ #
class ShopItemConfig(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    price: int = Field(ge=0)
    value: int | str | None = None
    desc: str = ""
    limited: bool = False
    mark: str = ""


class StockRuleConfig(StrictModel):
    max: int = Field(gt=0)
    per_hour: float = Field(gt=0)


class ShopCatalog(StrictModel):
    kind_labels: dict[str, str]
    role_prefix: str = Field(min_length=1)
    insurance_cap: int = Field(gt=0)
    auto_used: tuple[str, ...]
    items: tuple[ShopItemConfig, ...]
    stock_rules: dict[str, StockRuleConfig]

    @model_validator(mode="after")
    def _validate_items(self) -> Self:
        ids = [item.id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("shop.yaml: duplicate item id")
        item_set = set(ids)
        for item in self.items:
            if item.kind not in self.kind_labels:
                raise ValueError(f"shop.yaml: unknown kind {item.kind!r} on {item.id!r}")
        for key in self.auto_used:
            if key not in item_set:
                raise ValueError(f"shop.yaml: auto_used references unknown item {key!r}")
        for key in self.stock_rules:
            if key not in item_set:
                raise ValueError(f"shop.yaml: stock_rules references unknown item {key!r}")
        return self


# ------------------------------------------------------------------ #
# skins.yaml
# ------------------------------------------------------------------ #
class SkinConfig(StrictModel):
    item_id: str = Field(min_length=1)
    games: tuple[str, ...] = Field(min_length=1)
    title_prefix: str
    start_line: str
    win_line: str
    lose_line: str
    dice_mark: str = ""
    role_win: dict[str, str] | None = None
    role_lose: dict[str, str] | None = None
    roll_comment: str = ""
    banner: str = ""


class SkinsCatalog(StrictModel):
    slot_of_game: dict[str, str]
    skins: tuple[SkinConfig, ...]


# ------------------------------------------------------------------ #
# relics.yaml
# ------------------------------------------------------------------ #
class RelicBuffConfig(StrictModel):
    effect: str = Field(min_length=1)
    description: str = Field(min_length=1)


class RelicsCatalog(StrictModel):
    buff_rate: float = Field(gt=0)
    merge_count: int = Field(ge=2)
    star_max: int = Field(ge=1)
    buffs: dict[str, RelicBuffConfig]


# ------------------------------------------------------------------ #
# bonds.yaml
# ------------------------------------------------------------------ #
class BondKindConfig(StrictModel):
    key: str = Field(min_length=1)
    name: str = Field(min_length=1)
    emoji: str = Field(min_length=1)
    hours: int = Field(gt=0)
    rate: int = Field(ge=0)
    default_pct: int = Field(ge=0, lt=100)
    max_total: int = Field(gt=0)
    sellable: bool


class BondsCatalog(StrictModel):
    min_amount: int = Field(gt=0)
    sale_discount_per_hour: float = Field(ge=0)
    kinds: tuple[BondKindConfig, ...] = Field(min_length=1)


# ------------------------------------------------------------------ #
# missions.yaml
# ------------------------------------------------------------------ #
class MissionConfig(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    target: int = Field(gt=0)
    reward: int = Field(gt=0)
    game: str | None = None
    tag: str | None = None


class WinTierConfig(StrictModel):
    min_ratio: float = Field(gt=0)
    message: str = Field(min_length=1)


class MissionsCatalog(StrictModel):
    blackjack_game: str = Field(min_length=1)
    games: tuple[str, ...] = Field(min_length=1)
    min_bet: int = Field(ge=0)
    complete_bonus: int = Field(ge=0)
    per_day: int = Field(gt=0)
    universal: tuple[MissionConfig, ...] = Field(min_length=1)
    other_game_play_target: int = Field(gt=0)
    other_game_play_reward: int = Field(gt=0)
    other_game_win_target: int = Field(gt=0)
    other_game_win_reward: int = Field(gt=0)
    blackjack_missions: tuple[MissionConfig, ...] = Field(min_length=1)
    big_win: MissionConfig
    jackpot_multiplier: int = Field(gt=0)
    win_tiers: tuple[WinTierConfig, ...] = Field(min_length=1)
    jackpot_art: str


# ------------------------------------------------------------------ #
# events.yaml
# ------------------------------------------------------------------ #
class EventGameConfig(StrictModel):
    title: str = Field(min_length=1)
    blurb: str = Field(min_length=1)


class EventsCatalog(StrictModel):
    duration_min: int = Field(gt=0)
    earliest_hour: int = Field(ge=0, lt=24)
    boost: float = Field(gt=0)
    bonus_cap: int = Field(gt=0)
    games: dict[str, EventGameConfig]


# ------------------------------------------------------------------ #
# market.yaml
# ------------------------------------------------------------------ #
class SymbolConfig(StrictModel):
    key: str = Field(min_length=1)
    name: str = Field(min_length=1)
    emoji: str = Field(min_length=1)
    start: float = Field(gt=0)
    sigma: float = Field(gt=0)
    drift: float = Field(ge=0)


class CorrelationConfig(StrictModel):
    pair: tuple[str, str]
    value: float = Field(ge=-1, le=1)


class TaxBracketConfig(StrictModel):
    limit: int | None = None
    rate: float = Field(ge=0, le=1)


class PeriodConfig(StrictModel):
    label: str = Field(min_length=1)
    seconds: int = Field(gt=0)


class MarketSection(StrictModel):
    min_price: int = Field(gt=0)
    max_price: int = Field(gt=0)
    event_interval_minutes: int = Field(gt=0)
    event_range: tuple[float, float]
    event_scale_max: float = Field(gt=0)
    unit: int = Field(gt=0)
    fee_rate: float = Field(ge=0)
    unit_name: str = Field(min_length=1)
    side_name: dict[str, str]
    symbols: tuple[SymbolConfig, ...] = Field(min_length=1)
    correlations: tuple[CorrelationConfig, ...]
    drift_band: float = Field(gt=1)
    daily_limit: float = Field(gt=0, lt=1)
    pin_locks: dict[int, tuple[str, ...]]
    pin_action_text: dict[str, str]
    pin_mark: dict[int, str]
    gains_tax_brackets: tuple[TaxBracketConfig, ...] = Field(min_length=1)
    borrow_rate_per_day: dict[str, float]
    interest_min_charge: int = Field(ge=0)
    risk_k: float = Field(gt=0)
    open_ratio: float = Field(gt=1)
    losscut_ratio: float = Field(gt=0)
    collateral_haircut: dict[str, float]
    levels: str = Field(min_length=2)
    periods: dict[str, PeriodConfig]


class OptionKindConfig(StrictModel):
    name: str = Field(min_length=1)
    emoji: str = Field(min_length=1)
    desc: str = Field(min_length=1)


class OptionsSection(StrictModel):
    option_hours: int = Field(gt=0)
    strike_otm: float = Field(gt=0)
    tick_seconds: int = Field(gt=0)
    spread: float = Field(ge=0)
    payout_cap_mult: int = Field(gt=0)
    min_premium: int = Field(gt=0)
    max_open_premium: int = Field(gt=0)
    yield_share: float = Field(ge=0, le=1)
    margin_rate: float = Field(gt=0)
    max_short_margin: int = Field(gt=0)
    kinds: dict[str, OptionKindConfig]
    short_names: dict[str, str]
    strike_by_symbol: dict[str, float]
    max_target_pct: int = Field(gt=0)


class MarketEventsSection(StrictModel):
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


class EarningsSection(StrictModel):
    symbols: tuple[str, ...] = Field(min_length=1)
    release_weekday: int = Field(ge=0, le=6)
    release_time: tuple[int, int]
    announce_weekday: int = Field(ge=0, le=6)
    announce_time: tuple[int, int]
    size_range: tuple[float, float]
    surprise_prob: float = Field(ge=0, le=1)
    surprise_range: tuple[float, float]
    memo_prob: float = Field(ge=0, le=1)
    memo_truth: float = Field(gt=0.5, le=1)
    shop_truth: float = Field(gt=0.5, le=1)
    shop_stock: int = Field(gt=0)


class MarketCatalog(StrictModel):
    market: MarketSection
    options: OptionsSection
    events: MarketEventsSection
    earnings: EarningsSection


# ------------------------------------------------------------------ #
# economy.yaml
# ------------------------------------------------------------------ #
class WalletSection(StrictModel):
    initial_balance: int = Field(ge=0)
    consumable_effects: dict[str, str]
    medals: dict[int, str]


class LoansSection(StrictModel):
    user_min_principal: int = Field(gt=0)
    min_term_days: int = Field(gt=0)
    max_term_days: int = Field(gt=0)
    offer_hours: int = Field(gt=0)
    max_borrower_open: int = Field(gt=0)
    max_lender_open: int = Field(gt=0)
    max_lender_active: int = Field(gt=0)
    remind_hours: int = Field(gt=0)
    late_min_rate_tenths: int = Field(ge=0)
    late_cap: int = Field(gt=0)


class QuizpaySection(StrictModel):
    user_min_minutes: int = Field(gt=0)
    user_max_minutes: int = Field(gt=0)
    user_default_minutes: int = Field(gt=0)
    min_participants: int = Field(gt=0)
    min_games_for_reward: int = Field(ge=0)
    open_limit: int = Field(gt=0)
    daily_limit: int = Field(gt=0)
    base_reward: int = Field(gt=0)
    max_bonus: int = Field(ge=0)
    target_rate: float = Field(gt=0, lt=1)
    reward_scale_base: int = Field(gt=0)
    participation_rate: float = Field(ge=0)


class WeeklySection(StrictModel):
    min_plays: int = Field(gt=0)
    announce_top: int = Field(gt=0)


class P2pSection(StrictModel):
    fee_rate: float = Field(ge=0, lt=1)
    max_active: int = Field(gt=0)
    fixed_days: int = Field(gt=0)
    auction_hours: tuple[int, ...] = Field(min_length=1)
    min_increment_rate: float = Field(ge=0)
    min_increment: int = Field(gt=0)
    snipe_seconds: int = Field(ge=0)
    pair_daily_limit: int = Field(gt=0)
    min_price: int = Field(gt=0)
    item_price_range: tuple[float, float]
    horse_price_range: tuple[int, int]
    escrow_owner: int
    tradable_kinds: tuple[str, ...] = Field(min_length=1)
    slot_of_kind: dict[str, str]


class EconomyCatalog(StrictModel):
    wallet: WalletSection
    loans: LoansSection
    quizpay: QuizpaySection
    weekly: WeeklySection
    p2p: P2pSection


# ------------------------------------------------------------------ #
# games.yaml
# ------------------------------------------------------------------ #
class SlotSymbolConfig(StrictModel):
    symbol: str = Field(min_length=1)
    weight: int = Field(gt=0)
    three_mult: int = Field(gt=0)


class SlotSection(StrictModel):
    symbols: tuple[SlotSymbolConfig, ...] = Field(min_length=1)
    cherry_two: float = Field(ge=0)
    cherry_one: float = Field(ge=0)


class RouletteBetConfig(StrictModel):
    label: str = Field(min_length=1)
    mult: int = Field(gt=0)


class RouletteSection(StrictModel):
    red: tuple[int, ...] = Field(min_length=1)
    bets: dict[str, RouletteBetConfig]
    color_emoji: dict[str, str]


class PachinkoRoundConfig(StrictModel):
    name: str = Field(min_length=1)
    mult: int = Field(gt=0)
    prob: float = Field(gt=0, le=1)


class ProbBonusConfig(StrictModel):
    prob: float = Field(ge=0, le=1)
    bonus: float = Field(ge=0)


class PachinkoSection(StrictModel):
    reel_symbols: list[str] = Field(min_length=1)
    small: list[str] = Field(min_length=1)
    medium: list[str] = Field(min_length=1)
    balls_per_set: int = Field(gt=0)
    p_start: float = Field(gt=0, le=1)
    max_sets: int = Field(gt=0)
    analysis_max: int = Field(gt=0)
    rounds: tuple[PachinkoRoundConfig, ...] = Field(min_length=1)
    continue_prob: float = Field(ge=0, lt=1)
    omen_table: dict[str, ProbBonusConfig]
    tier_table: dict[str, ProbBonusConfig]
    hit_target: float = Field(gt=0, lt=1)
    omen_text: dict[str, str]

    @model_validator(mode="after")
    def _validate_probabilities(self) -> Self:
        if abs(sum(r.prob for r in self.rounds) - 1.0) > 1e-6:
            raise ValueError("games.yaml: pachinko.rounds probabilities must sum to 1.0")
        if abs(sum(v.prob for v in self.omen_table.values()) - 1.0) > 1e-6:
            raise ValueError("games.yaml: pachinko.omen_table probabilities must sum to 1.0")
        if abs(sum(v.prob for v in self.tier_table.values()) - 1.0) > 1e-6:
            raise ValueError("games.yaml: pachinko.tier_table probabilities must sum to 1.0")
        return self


class RtplevelSection(StrictModel):
    games: tuple[str, ...] = Field(min_length=1)
    levels: tuple[int, ...] = Field(min_length=1)
    default_level: int = Field(gt=0)
    step: float = Field(gt=0)
    chinchiro_rtp: float = Field(gt=0)
    chinchiro_w: float = Field(gt=0)
    blackjack_rtp: float = Field(gt=0)
    blackjack_w: float = Field(gt=0)
    roulette_default_kind: str = Field(min_length=1)


class GamesCatalog(StrictModel):
    slot: SlotSection
    roulette: RouletteSection
    pachinko: PachinkoSection
    rtplevel: RtplevelSection


# ------------------------------------------------------------------ #
# help.yaml
# ------------------------------------------------------------------ #
class HelpHomeConfig(StrictModel):
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    footer_template: str = Field(min_length=1)


class HelpButtonConfig(StrictModel):
    key: str = Field(min_length=1)
    label: str = Field(min_length=1)


class HelpPageConfig(StrictModel):
    title: str = Field(min_length=1)
    lines: tuple[str, ...] = Field(min_length=1)


class HelpCatalog(StrictModel):
    home: HelpHomeConfig
    buttons: tuple[HelpButtonConfig, ...] = Field(min_length=1)
    pages: dict[str, HelpPageConfig]

    @model_validator(mode="after")
    def _validate_buttons(self) -> Self:
        for btn in self.buttons:
            if btn.key != "home" and btn.key not in self.pages:
                raise ValueError(f"help.yaml: button references unknown page {btn.key!r}")
        return self


# ------------------------------------------------------------------ #
# Cached accessors
# ------------------------------------------------------------------ #
@lru_cache(maxsize=1)
def shop() -> ShopCatalog:
    return load_yaml_model("shop.yaml", ShopCatalog)


@lru_cache(maxsize=1)
def skins() -> SkinsCatalog:
    return load_yaml_model("skins.yaml", SkinsCatalog)


@lru_cache(maxsize=1)
def relics() -> RelicsCatalog:
    return load_yaml_model("relics.yaml", RelicsCatalog)


@lru_cache(maxsize=1)
def bonds() -> BondsCatalog:
    return load_yaml_model("bonds.yaml", BondsCatalog)


@lru_cache(maxsize=1)
def missions() -> MissionsCatalog:
    return load_yaml_model("missions.yaml", MissionsCatalog)


@lru_cache(maxsize=1)
def events() -> EventsCatalog:
    return load_yaml_model("events.yaml", EventsCatalog)


@lru_cache(maxsize=1)
def market() -> MarketCatalog:
    return load_yaml_model("market.yaml", MarketCatalog)


@lru_cache(maxsize=1)
def economy() -> EconomyCatalog:
    return load_yaml_model("economy.yaml", EconomyCatalog)


@lru_cache(maxsize=1)
def games() -> GamesCatalog:
    return load_yaml_model("games.yaml", GamesCatalog)


@lru_cache(maxsize=1)
def help_pages() -> HelpCatalog:
    return load_yaml_model("help.yaml", HelpCatalog)
