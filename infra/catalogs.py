"""YAML カタログの読み込みと Pydantic による境界検証（SSOT）。"""

from functools import lru_cache
from pathlib import Path
from typing import Self, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from infra.paths import CATALOGS_DIR

CATALOG_DIR = CATALOGS_DIR
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


class ShopTabConfig(StrictModel):
    label: str = Field(min_length=1)
    kinds: tuple[str, ...] = ()


class ShopTierConfig(StrictModel):
    max_price: int | None = None
    label: str = Field(min_length=1)


class P2pFilterConfig(StrictModel):
    label: str = Field(min_length=1)
    kind: str | None = None
    mode: str | None = None


class ShopUiConfig(StrictModel):
    slot_of_kind: dict[str, str]
    unequip_names: dict[str, str]
    tabs: dict[str, ShopTabConfig]
    tab_notes: dict[str, str]
    tiers: tuple[ShopTierConfig, ...] = Field(min_length=1)
    section_names: dict[str, str]
    p2p_filters: dict[str, P2pFilterConfig]
    p2p_orders: dict[str, str]


class ShopCatalog(StrictModel):
    kind_labels: dict[str, str]
    role_prefix: str = Field(min_length=1)
    insurance_cap: int = Field(gt=0)
    auto_used: tuple[str, ...]
    items: tuple[ShopItemConfig, ...]
    stock_rules: dict[str, StockRuleConfig]
    confirm_from: int = Field(ge=0)
    status_nick_minutes: int = Field(gt=0)
    status_nick_per_guild: int = Field(gt=0)
    ui: ShopUiConfig

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
# labor.yaml
# ------------------------------------------------------------------ #
class ToolConfig(StrictModel):
    key: str = Field(min_length=1)
    name: str = Field(min_length=1)
    mark: str = Field(min_length=1)
    effect: str = Field(min_length=1)


class CritOreConfig(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    mark: str = Field(min_length=1)
    weight: int = Field(gt=0)
    value: int = Field(gt=0)


class WorkerConfig(StrictModel):
    key: str = Field(min_length=1)
    name: str = Field(min_length=1)
    mark: str = Field(min_length=1)
    rate: float = Field(ge=0)
    contract_fee: int = Field(gt=0)
    ore_power: float = Field(ge=0)
    relic_power: float = Field(ge=0)


class OreConfig(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    mark: str = Field(min_length=1)
    prob: float | None = Field(default=None, gt=0, le=1)
    value: int = Field(gt=0)


class RelicDropConfig(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    mark: str = Field(min_length=1)
    prob: float = Field(gt=0, le=1)
    value: int = Field(gt=0)


class LaborCatalog(StrictModel):
    tools: tuple[ToolConfig, ...] = Field(min_length=1)
    max_level: int = Field(gt=0)
    upgrade_cost: tuple[int, ...] = Field(min_length=1)
    max_clicks_per_day: int = Field(gt=0)
    over_cap_pay: int = Field(ge=0)
    base_pay: int = Field(gt=0)
    pay_per_level: int = Field(ge=0)
    fatigue_max: float = Field(gt=0)
    fatigue_gain: float = Field(ge=0)
    gain_cut_per_level: float = Field(ge=0)
    late_gain_cut_per_level: float = Field(ge=0)
    early_levels: int = Field(ge=0)
    min_pay_rate: float = Field(gt=0, le=1)
    recover_per_sec: float = Field(gt=0)
    drink_recover: float = Field(gt=0)
    click_interval: float = Field(ge=0)
    crit_base: float = Field(ge=0)
    crit_per_level: float = Field(ge=0)
    crit_worker_bonus: float = Field(ge=0)
    crit_max: float = Field(gt=0, le=1)
    crit_mult: int = Field(gt=0)
    crit_ore_chance: float = Field(ge=0, le=1)
    crit_ores: tuple[CritOreConfig, ...] = Field(min_length=1)
    combo_size: int = Field(gt=0)
    combo_bonus: float = Field(ge=0)
    terrain_kinds: tuple[str, ...] = Field(min_length=1)
    terrain_hit_bonus: float = Field(ge=0)
    terrain_fatigue_cut: float = Field(ge=0, lt=1)
    terrain_silver_mult: int = Field(gt=0)
    workers: tuple[WorkerConfig, ...] = Field(min_length=1)
    contract_days: int = Field(gt=0)
    foreman_luck: float = Field(ge=0)
    coal_p: float = Field(gt=0, le=1)
    ores: tuple[OreConfig, ...] = Field(min_length=1)
    flat_ores: tuple[str, ...]
    ore_big: tuple[str, ...]
    relics: tuple[RelicDropConfig, ...] = Field(min_length=1)
    relic_big: tuple[str, ...]
    slots_base: int = Field(gt=0)
    dorm_cost: tuple[int, ...] = Field(min_length=1)
    dorm_max: int = Field(gt=0)
    stock_hours: float = Field(gt=0)
    click_gap_max: float = Field(gt=0)
    mech_cv_max: float = Field(gt=0)
    mech_min_clicks: int = Field(gt=0)
    suspect_hours: int = Field(gt=0)

    @model_validator(mode="after")
    def _validate_lengths(self) -> Self:
        if len(self.upgrade_cost) != self.max_level:
            raise ValueError("labor.yaml: upgrade_cost length must match max_level")
        if len(self.dorm_cost) != self.dorm_max:
            raise ValueError("labor.yaml: dorm_cost length must match dorm_max")
        return self


# ------------------------------------------------------------------ #
# horse.yaml
# ------------------------------------------------------------------ #
class HorseStatConfig(StrictModel):
    key: str = Field(min_length=1)
    name: str = Field(min_length=1)
    default: int = Field(gt=0)
    style: str = Field(min_length=1)


class GoingConfig(StrictModel):
    name: str = Field(min_length=1)
    penalty: float = Field(ge=0)
    weight: int = Field(gt=0)


class FormConfig(StrictModel):
    value: int
    mark: str = Field(min_length=1)
    mult: float = Field(gt=0)
    odds_mult: float = Field(gt=0)


class StyleConfig(StrictModel):
    key: str = Field(min_length=1)
    name: str = Field(min_length=1)
    mark: str = Field(min_length=1)
    early: float
    late: float


class HorseCatalog(StrictModel):
    stats: tuple[HorseStatConfig, ...] = Field(min_length=1)
    stat_total: int = Field(gt=0)
    rental_stat_penalty: int = Field(ge=0)
    stat_min: int = Field(gt=0)
    stat_max: int = Field(gt=0)
    carrot_points: int = Field(gt=0)
    gold_carrot_points: int = Field(gt=0)
    gold_carrot_max: int = Field(gt=0)
    field_size: int = Field(ge=3)
    name_max: int = Field(gt=0)
    max_horses: int = Field(gt=0)
    coats: list[str] = Field(min_length=1)
    silks: list[str] = Field(min_length=1)
    distances: tuple[int, ...] = Field(min_length=1)
    goings: tuple[GoingConfig, ...] = Field(min_length=1)
    forms: tuple[FormConfig, ...] = Field(min_length=1)
    styles: tuple[StyleConfig, ...] = Field(min_length=1)
    pace_factor: float = Field(ge=0)
    takeout: float = Field(ge=0, lt=1)
    odds_sims: int = Field(gt=0)
    max_odds: float = Field(gt=1)
    min_stake: int = Field(gt=0)
    max_stake: int = Field(gt=0)
    max_race_stake: int = Field(gt=0)
    entry_fee: int = Field(ge=0)
    prizes: tuple[int, int, int]
    major_hour: int = Field(ge=0, lt=24)
    major_prizes: tuple[int, int, int]
    major_house_topup: int = Field(ge=0)
    ticks: int = Field(gt=0)
    max_ticks: int = Field(gt=0)
    fatigue_max: float = Field(gt=0, le=1)
    noise: float = Field(ge=0)
    luck: float = Field(ge=0)
    weekday_hours: tuple[int, ...] = Field(min_length=1)
    weekend_hours: tuple[int, ...] = Field(min_length=1)
    entry_open_min: int = Field(gt=0)
    entry_lead_min: int = Field(gt=0)
    bet_lead_sec: int = Field(gt=0)
    bet_kinds: dict[str, str]
    multi_kinds: tuple[str, ...]
    combo_size: dict[str, int]
    combo_takeout: float = Field(ge=0, lt=1)
    max_combo_odds: float = Field(gt=1)
    record_prior_runs: int = Field(ge=0)
    first_run_factor: float = Field(gt=0, le=1)
    place_cap: float = Field(gt=0, le=1)
    max_races: int = Field(gt=0)
    retire_win: int = Field(ge=0)
    retire_place: int = Field(ge=0)
    retire_loss: int = Field(ge=0)
    retire_max: int = Field(ge=0)
    retire_fee_max: int = Field(ge=0)
    npc_names: list[str] = Field(min_length=8)
    banned_name_chars: str


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
    keep_days: int = Field(gt=0)
    confirm_from: int = Field(ge=0)
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
# teiai.yaml
# ------------------------------------------------------------------ #
class TeiaiStatusConfig(StrictModel):
    label: str = Field(min_length=1)
    mark: str | None = None
    name: str | None = None


class TeiaiCatalog(StrictModel):
    seed: int = Field(ge=0)
    labor_skim_rate: float = Field(ge=0, le=1)
    horse_prize_skim_rate: float = Field(ge=0, le=1)
    horse_rental_extra_skim_rate: float = Field(ge=0, le=1)
    skim_rate: float = Field(ge=0, le=1)
    repay_rate: float = Field(ge=0, le=1)
    bankrupt_floor: int = Field(ge=0)
    blacklist_days: int = Field(gt=0)
    escape_chance: float = Field(gt=0, le=1)
    escape_min_debt: int = Field(gt=0)
    escape_fine_rate: float = Field(ge=0)
    escape_fine_min: int = Field(ge=0)
    escape_max_tries: int = Field(gt=0)
    escape_cooldown_hours: int = Field(gt=0)
    auction_start: float = Field(gt=0)
    auction_step: float = Field(ge=0)
    auction_floor: float = Field(gt=0)
    auction_yield_share: float = Field(ge=0, le=1)
    seize_rate: float = Field(gt=0, le=1)
    debt_rate_per_day: float = Field(ge=0)
    debt_interest_max_days: int = Field(gt=0)
    grace_debt: int = Field(gt=0)
    grace_hours: int = Field(gt=0)
    bank_min_deposit: int = Field(gt=0)
    reserve_ratio: float = Field(gt=0, le=1)
    loan_recovery_per_hour: float = Field(ge=0)
    lock_debt_rate: float = Field(ge=0)
    loss_share: float = Field(ge=0, le=1)
    premium_yield_share: float = Field(ge=0, le=1)
    tax_yield_share: float = Field(ge=0, le=1)
    skim_yield_share: float = Field(ge=0, le=1)
    run_warn_ratio: float = Field(ge=0)
    run_danger_ratio: float = Field(ge=0)
    cds_min_stake: int = Field(gt=0)
    cds_min_odds: float = Field(gt=1)
    cds_max_odds: float = Field(gt=1)
    cds_term_days: int = Field(gt=0)
    cds_self_cap: int = Field(gt=0)
    cds_payout_cap_rate: float = Field(gt=0)
    wealthy_assets: int = Field(gt=0)
    statuses: dict[str, TeiaiStatusConfig]
    escape_frame_seconds: float = Field(gt=0)
    escape_ship: list[str] = Field(min_length=1)
    escape_waves: list[str] = Field(min_length=2)
    escape_progress: list[str] = Field(min_length=1)
    escape_success_art: str
    escape_fail_art: str
    panel_level_mark: dict[str, str]
    panel_level_text: dict[str, str]


# ------------------------------------------------------------------ #
# economy.yaml
# ------------------------------------------------------------------ #
class WalletSection(StrictModel):
    initial_balance: int = Field(ge=0)
    daily_base: int = Field(gt=0)
    daily_streak_bonus: int = Field(ge=0)
    daily_streak_cap: int = Field(ge=0)
    daily_slot_hours: int = Field(gt=0)
    max_notices: int = Field(gt=0)
    consumable_effects: dict[str, str]
    medals: dict[int, str]
    part_labels: dict[str, str]
    grace_lock_text: str = Field(min_length=1)


class TransferSection(StrictModel):
    fee_rate: float = Field(ge=0, lt=1)
    daily_limit: int = Field(gt=0)
    confirm_from: int = Field(ge=0)
    min_games: int = Field(ge=0)
    board_interval_minutes: int = Field(gt=0)


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
    max_questions: int = Field(gt=0)
    max_question_len: int = Field(gt=0)
    max_choice_len: int = Field(gt=0)
    max_explanation_len: int = Field(gt=0)
    max_verdict_len: int = Field(gt=0)
    max_minutes: int = Field(gt=0)
    default_reward: int = Field(gt=0)
    default_right: str = Field(min_length=1)
    default_wrong: str = Field(min_length=1)
    letters: str = Field(min_length=4)


class RaffleSection(StrictModel):
    min_fee: int = Field(gt=0)
    random_fee_range: tuple[int, int]
    default_minutes: int = Field(gt=0)
    max_minutes: int = Field(gt=0)
    shown_entrants: int = Field(gt=0)


class WeeklySection(StrictModel):
    min_plays: int = Field(gt=0)
    announce_top: int = Field(gt=0)
    check_minutes: int = Field(gt=0)


class P2pEndedTextConfig(StrictModel):
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)


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
    confirm_from: int = Field(ge=0)
    tradable_kinds: tuple[str, ...] = Field(min_length=1)
    slot_of_kind: dict[str, str]
    ended_text: dict[str, P2pEndedTextConfig]


class EconomyCatalog(StrictModel):
    wallet: WalletSection
    transfer: TransferSection
    loans: LoansSection
    quizpay: QuizpaySection
    raffle: RaffleSection
    weekly: WeeklySection
    p2p: P2pSection


# ------------------------------------------------------------------ #
# games.yaml
# ------------------------------------------------------------------ #
class GamesCommonSection(StrictModel):
    min_bet: int = Field(gt=0)
    simple_step_seconds: float = Field(gt=0)


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


class BlackjackResultConfig(StrictModel):
    text: str = Field(min_length=1)
    color: str = Field(min_length=1)


class BlackjackFlairConfig(StrictModel):
    blackjack: str = Field(min_length=1)
    doubled: str = Field(min_length=1)
    twenty_one: str = Field(min_length=1)


class BlackjackSection(StrictModel):
    suits: list[str] = Field(min_length=1)
    ranks: list[str] = Field(min_length=1)
    title: str = Field(min_length=1)
    result_text: dict[str, BlackjackResultConfig]
    flair: BlackjackFlairConfig


class ChinchiroRoleConfig(StrictModel):
    name: str = Field(min_length=1)
    mult: int = Field(gt=0)


class ChinchiroSection(StrictModel):
    max_rolls: int = Field(gt=0)
    dice_faces: dict[int, str]
    roles: dict[str, ChinchiroRoleConfig]
    hifumi_factor: int = Field(gt=0)
    title: str = Field(min_length=1)
    step_seconds: float = Field(gt=0)
    win_lines: dict[str, str]
    lose_lines: dict[str, str]


class PachinkoRoundConfig(StrictModel):
    name: str = Field(min_length=1)
    mult: int = Field(gt=0)
    prob: float = Field(gt=0, le=1)


class ProbBonusConfig(StrictModel):
    prob: float = Field(ge=0, le=1)
    bonus: float = Field(ge=0)


class PachinkoRankConfig(StrictModel):
    floor: int = Field(ge=0)
    label: str = Field(min_length=1)


class PachinkoStoryConfig(StrictModel):
    title: str = Field(min_length=1)
    sub: str = Field(min_length=1)


class PachinkoReviveConfig(StrictModel):
    line: str = Field(min_length=1)
    sub: str = Field(min_length=1)


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
    title: str = Field(min_length=1)
    step_seconds: float = Field(gt=0)
    frame_budget: int = Field(gt=0)
    shown_rounds: int = Field(gt=0)
    extra_sets: tuple[int, ...] = Field(min_length=1)
    banner: dict[int, str]
    ranks: tuple[PachinkoRankConfig, ...] = Field(min_length=1)
    spinning: str = Field(min_length=1)
    step_text: list[str] = Field(min_length=1)
    step_icon: list[str] = Field(min_length=1)
    steps_of: dict[str, int]
    cutin: dict[str, list[str]]
    stories: dict[str, list[PachinkoStoryConfig]]
    normal_whispers: list[str] = Field(min_length=1)
    revive_chance: dict[str, float]
    revive_text: list[PachinkoReviveConfig] = Field(min_length=1)
    pseudo_counts: dict[str, list[int]]
    flavor: dict[str, str]
    omen_names: dict[str, str]
    tier_names: dict[str, str]

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
    level_names: dict[int, str]


class GamesCatalog(StrictModel):
    common: GamesCommonSection
    slot: SlotSection
    roulette: RouletteSection
    blackjack: BlackjackSection
    chinchiro: ChinchiroSection
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
def labor() -> LaborCatalog:
    return load_yaml_model("labor.yaml", LaborCatalog)


@lru_cache(maxsize=1)
def horse() -> HorseCatalog:
    return load_yaml_model("horse.yaml", HorseCatalog)


@lru_cache(maxsize=1)
def market() -> MarketCatalog:
    return load_yaml_model("market.yaml", MarketCatalog)


@lru_cache(maxsize=1)
def teiai() -> TeiaiCatalog:
    return load_yaml_model("teiai.yaml", TeiaiCatalog)


@lru_cache(maxsize=1)
def economy() -> EconomyCatalog:
    return load_yaml_model("economy.yaml", EconomyCatalog)


@lru_cache(maxsize=1)
def games() -> GamesCatalog:
    return load_yaml_model("games.yaml", GamesCatalog)


@lru_cache(maxsize=1)
def help_pages() -> HelpCatalog:
    return load_yaml_model("help.yaml", HelpCatalog)
