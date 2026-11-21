"""ゲーム・競馬・地下作業・ミッション・イベントカタログの公称データ契約。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class MissionContract:
    id: str
    name: str
    kind: str
    target: int
    reward: int
    game: str | None
    tag: str | None


@dataclass(frozen=True)
class MissionsCatalogContract:
    blackjack_game: str
    games: tuple[str, ...]
    min_bet: int
    complete_bonus: int
    per_day: int
    universal: tuple[MissionContract, ...]
    other_game_play_target: int
    other_game_play_reward: int
    other_game_win_target: int
    other_game_win_reward: int
    blackjack_missions: tuple[MissionContract, ...]
    big_win: MissionContract
    jackpot_multiplier: int


@dataclass(frozen=True)
class EventsCatalogContract:
    duration_min: int
    earliest_hour: int
    boost: float
    bonus_cap: int
    games: tuple[str, ...]


@dataclass(frozen=True)
class ToolContract:
    key: str
    name: str
    mark: str
    effect: str


@dataclass(frozen=True)
class CritOreContract:
    id: str
    name: str
    mark: str
    weight: int
    value: int


@dataclass(frozen=True)
class WorkerContract:
    key: str
    name: str
    mark: str
    rate: float
    contract_fee: int
    ore_power: float
    relic_power: float


@dataclass(frozen=True)
class OreContract:
    id: str
    name: str
    mark: str
    prob: float | None
    value: int


@dataclass(frozen=True)
class RelicDropContract:
    id: str
    name: str
    mark: str
    prob: float
    value: int


@dataclass(frozen=True)
class LaborCatalogContract:
    tools: tuple[ToolContract, ...]
    max_level: int
    upgrade_cost: tuple[int, ...]
    max_clicks_per_day: int
    over_cap_pay: int
    base_pay: int
    pay_per_level: int
    fatigue_max: float
    fatigue_gain: float
    gain_cut_per_level: float
    late_gain_cut_per_level: float
    early_levels: int
    min_pay_rate: float
    recover_per_sec: float
    drink_recover: float
    click_interval: float
    crit_base: float
    crit_per_level: float
    crit_worker_bonus: float
    crit_max: float
    crit_mult: int
    crit_ore_chance: float
    crit_ores: tuple[CritOreContract, ...]
    combo_size: int
    combo_bonus: float
    terrain_kinds: tuple[str, ...]
    terrain_hit_bonus: float
    terrain_fatigue_cut: float
    terrain_silver_mult: int
    workers: tuple[WorkerContract, ...]
    contract_days: int
    foreman_luck: float
    coal_p: float
    ores: tuple[OreContract, ...]
    flat_ores: tuple[str, ...]
    ore_big: tuple[str, ...]
    relics: tuple[RelicDropContract, ...]
    relic_big: tuple[str, ...]
    slots_base: int
    dorm_cost: tuple[int, ...]
    dorm_max: int
    stock_hours: float
    click_gap_max: float
    mech_cv_max: float
    mech_min_clicks: int
    suspect_hours: int


@dataclass(frozen=True)
class HorseStatContract:
    key: str
    name: str
    default: int
    style: str


@dataclass(frozen=True)
class GoingContract:
    name: str
    penalty: float
    weight: int


@dataclass(frozen=True)
class FormContract:
    value: int
    mark: str
    mult: float
    odds_mult: float


@dataclass(frozen=True)
class StyleContract:
    key: str
    name: str
    mark: str
    early: float
    late: float


@dataclass(frozen=True)
class HorseCatalogContract:
    stats: tuple[HorseStatContract, ...]
    stat_total: int
    stat_min: int
    stat_max: int
    carrot_points: int
    gold_carrot_points: int
    gold_carrot_max: int
    field_size: int
    name_max: int
    max_horses: int
    coats: Sequence[str]
    silks: Sequence[str]
    distances: tuple[int, ...]
    goings: tuple[GoingContract, ...]
    forms: tuple[FormContract, ...]
    styles: tuple[StyleContract, ...]
    pace_factor: float
    takeout: float
    odds_sims: int
    odds_sim_weight: float
    max_odds: float
    min_stake: int
    max_stake: int
    max_race_stake: int
    entry_fee: int
    prizes: tuple[int, int, int]
    major_hour: int
    major_prizes: tuple[int, int, int]
    major_house_topup: int
    ticks: int
    max_ticks: int
    fatigue_max: float
    noise: float
    luck: float
    weekday_hours: tuple[int, ...]
    weekend_hours: tuple[int, ...]
    entry_open_min: int
    entry_lead_min: int
    bet_lead_sec: int
    bet_kinds: Mapping[str, str]
    multi_kinds: tuple[str, ...]
    combo_size: Mapping[str, int]
    combo_takeout: float
    max_combo_odds: float
    record_prior_runs: int
    first_run_factor: float
    place_cap: float
    ability_prior_weight: float
    max_races: int
    retire_win: int
    retire_place: int
    retire_loss: int
    retire_max: int
    retire_fee_max: int
    npc_names: Sequence[str]
    banned_name_chars: str


@dataclass(frozen=True)
class GamesCommonContract:
    min_bet: int


@dataclass(frozen=True)
class SlotSymbolContract:
    symbol: str
    weight: int
    three_mult: int


@dataclass(frozen=True)
class SlotSectionContract:
    symbols: tuple[SlotSymbolContract, ...]
    cherry_two: float
    cherry_one: float


@dataclass(frozen=True)
class RouletteBetContract:
    label: str
    mult: int


@dataclass(frozen=True)
class RouletteSectionContract:
    red: tuple[int, ...]
    bets: Mapping[str, RouletteBetContract]


@dataclass(frozen=True)
class BlackjackSectionContract:
    suits: Sequence[str]
    ranks: Sequence[str]


@dataclass(frozen=True)
class ChinchiroRoleContract:
    name: str
    mult: int


@dataclass(frozen=True)
class ChinchiroSectionContract:
    max_rolls: int
    roles: Mapping[str, ChinchiroRoleContract]
    hifumi_factor: int


@dataclass(frozen=True)
class PachinkoRoundContract:
    name: str
    mult: int
    prob: float


@dataclass(frozen=True)
class ProbBonusContract:
    prob: float
    bonus: float


@dataclass(frozen=True)
class PachinkoSectionContract:
    reel_symbols: Sequence[str]
    small: Sequence[str]
    medium: Sequence[str]
    balls_per_set: int
    p_start: float
    max_sets: int
    analysis_max: int
    rounds: tuple[PachinkoRoundContract, ...]
    continue_prob: float
    omen_table: Mapping[str, ProbBonusContract]
    tier_table: Mapping[str, ProbBonusContract]
    hit_target: float
    revive_chance: Mapping[str, float]
    pseudo_counts: Mapping[str, Sequence[int]]


@dataclass(frozen=True)
class RtplevelSectionContract:
    games: tuple[str, ...]
    levels: tuple[int, ...]
    default_level: int
    step: float
    chinchiro_rtp: float
    chinchiro_w: float
    blackjack_rtp: float
    blackjack_w: float
    roulette_default_kind: str


@dataclass(frozen=True)
class JackpotContract:
    share: float
    pelica_per_certain: int
    max_chance: float
    min_pool: int


@dataclass(frozen=True)
class GamesCatalogContract:
    common: GamesCommonContract
    jackpot: JackpotContract
    slot: SlotSectionContract
    roulette: RouletteSectionContract
    blackjack: BlackjackSectionContract
    chinchiro: ChinchiroSectionContract
    pachinko: PachinkoSectionContract
    rtplevel: RtplevelSectionContract
