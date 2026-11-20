"""経済共通・帝愛・債券カタログの公称データ契約。"""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class BondKindContract:
    key: str
    name: str
    emoji: str
    hours: int
    rate: int
    default_pct: int
    max_total: int
    sellable: bool


@dataclass(frozen=True)
class BondsCatalogContract:
    min_amount: int
    sale_discount_per_hour: float
    kinds: tuple[BondKindContract, ...]


@dataclass(frozen=True)
class TeiaiStatusContract:
    label: str
    mark: str | None
    name: str | None


@dataclass(frozen=True)
class KyosaiSectionContract:
    target_ratio: float
    dividend_share: float
    dividend_cap: int
    gate_ratio: float
    dormant_days: int
    total_units: int
    auction_lot: int
    auction_hours: int
    auction_min_bid: int
    offer_hours: int
    motion_hours: int
    premium_limit: float
    motion_min_yes_share: float
    motion_min_units: int
    motion_max_open_per_user: int
    motion_cooldown_hours: float


@dataclass(frozen=True)
class ThermostatSectionContract:
    enforce: bool
    cool_days: float
    memory_ratio: float
    capacity_min: int
    coupling: float


@dataclass(frozen=True)
class TeiaiCatalogContract:
    seed: int
    labor_skim_rate: float
    horse_prize_skim_rate: float
    skim_rate: float
    repay_rate: float
    bankrupt_floor: int
    blacklist_days: int
    escape_chance: float
    escape_min_debt: int
    escape_fine_rate: float
    escape_fine_min: int
    escape_max_tries: int
    escape_cooldown_hours: int
    auction_start: float
    auction_step: float
    auction_floor: float
    market_slots_per_day: int
    market_auction_prob: float
    market_auction_rate: tuple[float, float]
    market_auction_hours: tuple[float, float]
    market_auction_end: float
    market_fixed_rate: tuple[float, float]
    market_fixed_hours: float
    market_fixed_qty: tuple[int, int]
    market_black_prob: float
    market_black_worse: float
    market_black_better: float
    seize_rate: float
    debt_rate_per_day: float
    debt_interest_max_days: int
    grace_debt: int
    grace_hours: int
    bank_min_deposit: int
    reserve_ratio: float
    loan_recovery_per_hour: float
    lock_debt_rate: float
    loss_share: float
    trust_premium_per_day: float
    rf_min_pct: float
    rf_max_pct: float
    rf_alpha: float
    rf_kappa_per_day: float
    rf_sigma: float
    run_warn_ratio: float
    run_danger_ratio: float
    cds_min_stake: int
    cds_min_odds: float
    cds_max_odds: float
    cds_term_days: int
    cds_self_cap: int
    cds_payout_cap_rate: float
    wealthy_assets: int
    statuses: Mapping[str, TeiaiStatusContract]
    thermostat: ThermostatSectionContract
    kyosai: KyosaiSectionContract


@dataclass(frozen=True)
class WalletSectionContract:
    initial_balance: int
    daily_base: int
    daily_streak_bonus: int
    daily_streak_cap: int
    daily_slot_hours: int
    max_notices: int
    consumable_effects: Mapping[str, str]


@dataclass(frozen=True)
class TransferSectionContract:
    fee_rate: float
    daily_limit: int
    confirm_from: int
    min_games: int
    board_interval_minutes: int


@dataclass(frozen=True)
class LoansSectionContract:
    user_min_principal: int
    min_term_days: int
    max_term_days: int
    offer_hours: int
    max_borrower_open: int
    max_lender_open: int
    max_lender_active: int
    remind_hours: int
    late_min_rate_tenths: int
    late_cap: int


@dataclass(frozen=True)
class QuizpaySectionContract:
    user_min_minutes: int
    user_max_minutes: int
    user_default_minutes: int
    min_participants: int
    min_games_for_reward: int
    open_limit: int
    daily_limit: int
    base_reward: int
    max_bonus: int
    target_rate: float
    reward_scale_base: int
    participation_rate: float
    max_questions: int
    max_question_len: int
    max_choice_len: int
    max_explanation_len: int
    max_verdict_len: int
    max_minutes: int
    default_reward: int
    letters: str


@dataclass(frozen=True)
class RaffleSectionContract:
    min_fee: int
    random_fee_range: tuple[int, int]
    default_minutes: int
    max_minutes: int
    shown_entrants: int


@dataclass(frozen=True)
class WeeklySectionContract:
    min_plays: int
    announce_top: int
    check_minutes: int


@dataclass(frozen=True)
class P2pSectionContract:
    fee_rate: float
    max_active: int
    fixed_days: int
    auction_hours: tuple[int, ...]
    min_increment_rate: float
    min_increment: int
    snipe_seconds: int
    pair_daily_limit: int
    min_price: int
    item_price_range: tuple[float, float]
    horse_price_range: tuple[int, int]
    escrow_owner: int
    confirm_from: int
    tradable_kinds: tuple[str, ...]
    slot_of_kind: Mapping[str, str]


@dataclass(frozen=True)
class PollSectionContract:
    rig_cost: int
    default_minutes: int
    max_minutes: int
    max_open_per_user: int
    title_max: int
    max_count: int


@dataclass(frozen=True)
class EconomyCatalogContract:
    wallet: WalletSectionContract
    transfer: TransferSectionContract
    loans: LoansSectionContract
    quizpay: QuizpaySectionContract
    raffle: RaffleSectionContract
    weekly: WeeklySectionContract
    p2p: P2pSectionContract
    poll: PollSectionContract
