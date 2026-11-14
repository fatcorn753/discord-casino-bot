"""経済共通・帝愛・債券カタログの Pydantic スキーマ。"""

from functools import lru_cache

from pydantic import Field

from infra.catalogs.loader import StrictModel, load_yaml_model


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


@lru_cache(maxsize=1)
def bonds() -> BondsCatalog:
    return load_yaml_model("bonds.yaml", BondsCatalog)


@lru_cache(maxsize=1)
def teiai() -> TeiaiCatalog:
    return load_yaml_model("teiai.yaml", TeiaiCatalog)


@lru_cache(maxsize=1)
def economy() -> EconomyCatalog:
    return load_yaml_model("economy.yaml", EconomyCatalog)
