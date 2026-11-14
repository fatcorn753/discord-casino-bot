"""ゲーム・競馬・地下作業・ミッション・イベントカタログの Pydantic スキーマ。"""

from functools import lru_cache
from typing import Self

from pydantic import Field, model_validator

from infra.catalogs.loader import StrictModel, load_yaml_model


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
def games() -> GamesCatalog:
    return load_yaml_model("games.yaml", GamesCatalog)
