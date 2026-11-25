"""ドメイン分割された SQLite DDL の統合エントリ。"""

import sqlite3

from infra.db.schema.ddl import (
    company,
    core,
    discord_ui,
    horse,
    labor,
    market,
    shop,
    social,
    teiai,
    telemetry,
)
from infra.db.schema.ddl.company import (
    COMPANIES_DDL,
    COMPANIES_V1_DDL,
    COMPANY_STAKES_DDL,
    COMPANY_STAKES_V1_DDL,
)
from infra.db.schema.ddl.core import (
    ACCOUNTS_DDL,
    LEDGER_DDL,
    LEDGER_KIND_INDEX_DDL,
    LEDGER_V1_DDL,
    USER_DAILY_DDL,
    USER_PLAY_STATS_DDL,
    USERS_DDL,
)
from infra.db.schema.ddl.discord_ui import (
    DISCORD_POSTS_DDL,
    MANUAL_EVENT_DDL,
    MANUAL_EVENT_V1_DDL,
    SCHEMA_FLAGS_DDL,
    STREAM_VOICE_CHANNELS_DDL,
)
from infra.db.schema.ddl.horse import (
    HORSE_AUCTION_LISTINGS_DDL,
    HORSE_AUCTION_LISTINGS_INDEX_DDL,
    HORSE_BET_PICKS_DDL,
    HORSE_BETS_DDL,
    HORSE_BETS_INDEX_DDL,
    HORSE_BETS_V1_DDL,
    HORSE_COMBO2_ODDS_DDL,
    HORSE_ENTRIES_DDL,
    HORSE_ENTRIES_INDEX_DDL,
    HORSE_ENTRIES_V1_DDL,
    HORSE_ODDS_V1_DDL,
    HORSE_OWNERSHIP_DDL,
    HORSE_OWNERSHIP_INDEX_DDL,
    HORSE_RACE_REGISTRATIONS_DDL,
    HORSE_RACES_DDL,
    HORSE_RACES_V1_DDL,
    HORSE_TRIO_ODDS_DDL,
    HORSES_DDL,
    HORSES_V1_DDL,
    HORSES_V1_INDEX_DDL,
)
from infra.db.schema.ddl.labor import (
    LABOR_DAILY_DDL,
    LABOR_DAILY_RANK_INDEX_DDL,
    LABOR_FATIGUE_DDL,
    LABOR_PENDING_ORES_DDL,
    LABOR_PENDING_RELICS_DDL,
    LABOR_POT_DDL,
    LABOR_TOTALS_DDL,
    LABOR_UPGRADES_DDL,
    LABOR_V1_DDL,
)
from infra.db.schema.ddl.market import (
    CRYPTO_LONG_DDL,
    CRYPTO_SHORT_DDL,
    MARGIN_POSITIONS_DDL,
    OPTION_PAIRS_DDL,
    OPTION_SHORT_MARGINS_DDL,
    OPTIONS_ACTIVE_INDEX_DDL,
    OPTIONS_DDL,
    OPTIONS_V1_DDL,
)
from infra.db.schema.ddl.shop import INVENTORY_DDL, PROFILE_DDL, SHOP_STOCK_DDL, USER_BUFFS_DDL, USER_EQUIPMENT_DDL
from infra.db.schema.ddl.social import (
    ARENA_BETS_DDL,
    ARENA_BETS_V1_DDL,
    ARENA_NPC_BETS_DDL,
    ARENA_NPC_BETS_INDEX_DDL,
    LOAN_TERMS_DDL,
    LOANS_DDL,
    LOANS_V1_DDL,
    MARKET_AUCTION_BIDS_BIDDER_INDEX_DDL,
    MARKET_AUCTION_BIDS_DDL,
    MARKET_CLOSURES_DDL,
    MARKET_LISTINGS_DDL,
    MARKET_LISTINGS_V1_DDL,
    POLLS_DDL,
    POLLS_V1_DDL,
    QUIZ_CHOICES_DDL,
    QUIZ_QUESTIONS_DDL,
    QUIZ_QUESTIONS_V1_DDL,
    QUIZ_USER_SUBMISSIONS_DDL,
    QUIZ_USER_SUBMISSIONS_INDEX_DDL,
    RAFFLE_DDL,
    RAFFLE_V1_DDL,
    STREAMS_DDL,
    STREAMS_V1_DDL,
    new_quiz_order,
)
from infra.db.schema.ddl.teiai import (
    BOND_DEFAULT_CHECKS_DDL,
    BONDS_ACTIVE_INDEX_DDL,
    BONDS_DDL,
    BONDS_V1_DDL,
    CDS_ACTIVE_INDEX_DDL,
    CDS_DDL,
    CDS_V1_DDL,
    KYOSAI_MOTIONS_DDL,
    KYOSAI_MOTIONS_V1_DDL,
    KYOSAI_STATE_DDL,
    KYOSAI_STATE_V3_DDL,
    KYOSAI_UNITS_DDL,
    KYOSAI_UNITS_V1_DDL,
    TEIAI_BANK_DDL,
    TEIAI_BANK_V1_DDL,
    TEIAI_BANKRUPTCIES_DDL,
    TEIAI_DEBTS_DDL,
    TEIAI_DEPOSITS_DDL,
    TEIAI_DEPOSITS_V1_DDL,
    TEIAI_ESCAPES_DDL,
    TEIAI_GRACE_ACTIVE_INDEX_DDL,
    TEIAI_GRACE_DDL,
    TEIAI_HOUSE_DDL,
    TEIAI_HOUSE_GRANTS_DDL,
    TEIAI_HOUSE_V1_DDL,
    TEIAI_THERMOSTAT_DDL,
    TEIAI_TRUST_DDL,
)

V2_USER_SPLIT_STATEMENTS: tuple[str, ...] = (
    ACCOUNTS_DDL,
    USER_DAILY_DDL,
    USER_PLAY_STATS_DDL,
    TEIAI_DEBTS_DDL,
    TEIAI_GRACE_DDL,
    TEIAI_GRACE_ACTIVE_INDEX_DDL,
    TEIAI_BANKRUPTCIES_DDL,
    TEIAI_ESCAPES_DDL,
)
V3_TEIAI_SPLIT_STATEMENTS: tuple[str, ...] = (
    TEIAI_THERMOSTAT_DDL,
    TEIAI_TRUST_DDL,
    KYOSAI_STATE_V3_DDL,
    TEIAI_HOUSE_GRANTS_DDL,
)
V4_LABOR_PROFILE_STATEMENTS: tuple[str, ...] = (
    LABOR_UPGRADES_DDL,
    LABOR_FATIGUE_DDL,
    LABOR_POT_DDL,
    LABOR_DAILY_DDL,
    LABOR_DAILY_RANK_INDEX_DDL,
    LABOR_TOTALS_DDL,
    LABOR_PENDING_ORES_DDL,
    LABOR_PENDING_RELICS_DDL,
    USER_EQUIPMENT_DDL,
    USER_BUFFS_DDL,
)
V5_MARGIN_HORSE_STATEMENTS: tuple[str, ...] = (
    MARGIN_POSITIONS_DDL,
    HORSE_OWNERSHIP_DDL,
    HORSE_OWNERSHIP_INDEX_DDL,
    HORSE_AUCTION_LISTINGS_DDL,
    HORSE_AUCTION_LISTINGS_INDEX_DDL,
    HORSE_RACE_REGISTRATIONS_DDL,
    HORSE_BET_PICKS_DDL,
    HORSE_COMBO2_ODDS_DDL,
    HORSE_TRIO_ODDS_DDL,
)
V6_SPLIT_STATEMENTS: tuple[str, ...] = (
    SCHEMA_FLAGS_DDL,
    DISCORD_POSTS_DDL,
    STREAM_VOICE_CHANNELS_DDL,
    BOND_DEFAULT_CHECKS_DDL,
    OPTION_PAIRS_DDL,
    OPTION_SHORT_MARGINS_DDL,
    LOAN_TERMS_DDL,
    MARKET_AUCTION_BIDS_DDL,
    MARKET_AUCTION_BIDS_BIDDER_INDEX_DDL,
    MARKET_CLOSURES_DDL,
    QUIZ_CHOICES_DDL,
    QUIZ_USER_SUBMISSIONS_DDL,
    QUIZ_USER_SUBMISSIONS_INDEX_DDL,
    ARENA_NPC_BETS_DDL,
    ARENA_NPC_BETS_INDEX_DDL,
)
V7_INTEGRITY_INDEX_STATEMENTS: tuple[str, ...] = (
    LEDGER_KIND_INDEX_DDL,
    CDS_ACTIVE_INDEX_DDL,
    BONDS_ACTIVE_INDEX_DDL,
    OPTIONS_ACTIVE_INDEX_DDL,
)
_V1_SKIP_SET = frozenset(
    (
        *V2_USER_SPLIT_STATEMENTS,
        *V3_TEIAI_SPLIT_STATEMENTS,
        *V4_LABOR_PROFILE_STATEMENTS,
        *V5_MARGIN_HORSE_STATEMENTS,
        *V6_SPLIT_STATEMENTS,
        *V7_INTEGRITY_INDEX_STATEMENTS,
        LEDGER_DDL,
        COMPANIES_DDL,
        COMPANY_STAKES_DDL,
        TEIAI_DEPOSITS_DDL,
        CDS_DDL,
        TEIAI_BANK_DDL,
        TEIAI_HOUSE_DDL,
        KYOSAI_STATE_DDL,
        HORSES_DDL,
        HORSE_ENTRIES_DDL,
        HORSE_BETS_DDL,
        HORSE_RACES_DDL,
        MANUAL_EVENT_DDL,
        BONDS_DDL,
        KYOSAI_UNITS_DDL,
        KYOSAI_MOTIONS_DDL,
        OPTIONS_DDL,
        LOANS_DDL,
        MARKET_LISTINGS_DDL,
        QUIZ_QUESTIONS_DDL,
        RAFFLE_DDL,
        POLLS_DDL,
        STREAMS_DDL,
        ARENA_BETS_DDL,
    )
)

ALL_STATEMENTS: tuple[str, ...] = (
    *core.STATEMENTS,
    *shop.STATEMENTS,
    *teiai.STATEMENTS,
    *market.STATEMENTS,
    *horse.STATEMENTS,
    *labor.STATEMENTS,
    *company.STATEMENTS,
    *social.STATEMENTS,
    *discord_ui.STATEMENTS,
    *telemetry.STATEMENTS,
)


def create_all_tables(conn: sqlite3.Connection) -> None:
    """すべての表と索引を、1文ずつ安全に作成する（無ければ作る）。"""
    for stmt in ALL_STATEMENTS:
        conn.execute(stmt)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return any(str(r[1]) == col for r in conn.execute(f"PRAGMA table_info({table})"))


def create_v1_missing_tables(conn: sqlite3.Connection) -> None:
    """v0 -> v1 移行用に、v2 以降の分離テーブルを除いた不足テーブル（旧 users 等）を作成する。"""
    if not _has_table(conn, "accounts"):
        conn.execute(USERS_DDL)
    if not _has_table(conn, "teiai_thermostat"):
        conn.execute(TEIAI_BANK_V1_DDL)
    if not _has_table(conn, "teiai_house_grants"):
        conn.execute(TEIAI_HOUSE_V1_DDL)
    if not _has_table(conn, "labor_upgrades"):
        conn.execute(LABOR_V1_DDL)
    if not _has_table(conn, "user_equipment"):
        conn.execute(PROFILE_DDL)
    if not _has_table(conn, "margin_positions"):
        conn.execute(CRYPTO_SHORT_DDL)
        conn.execute(CRYPTO_LONG_DDL)
    if not _has_table(conn, "horse_ownership"):
        conn.execute(HORSES_V1_DDL)
        conn.execute(HORSES_V1_INDEX_DDL)
    if not _has_table(conn, "horse_race_registrations"):
        conn.execute(HORSE_ENTRIES_V1_DDL)
    if not _has_table(conn, "horse_bet_picks"):
        conn.execute(HORSE_BETS_V1_DDL)
    if not _has_table(conn, "horse_combo2_odds"):
        conn.execute(HORSE_ODDS_V1_DDL)
    if not _has_table(conn, "discord_posts"):
        conn.execute(HORSE_RACES_V1_DDL)
        conn.execute(MANUAL_EVENT_V1_DDL)
        conn.execute(KYOSAI_MOTIONS_V1_DDL)
        conn.execute(RAFFLE_V1_DDL)
        conn.execute(POLLS_V1_DDL)
    if not _has_col(conn, "kyosai_state", "house_units"):
        conn.execute(KYOSAI_UNITS_V1_DDL)
    if not _has_table(conn, "bond_default_checks"):
        conn.execute(BONDS_V1_DDL)
    if not _has_table(conn, "option_pairs"):
        conn.execute(OPTIONS_V1_DDL)
    if not _has_table(conn, "loan_terms"):
        conn.execute(LOANS_V1_DDL)
    if not _has_table(conn, "market_closures"):
        conn.execute(MARKET_LISTINGS_V1_DDL)
    if not _has_table(conn, "quiz_choices"):
        conn.execute(QUIZ_QUESTIONS_V1_DDL)
    if not _has_table(conn, "stream_voice_channels"):
        conn.execute(STREAMS_V1_DDL)
    if not _has_table(conn, "arena_npc_bets"):
        conn.execute(ARENA_BETS_V1_DDL)
    if not _has_col(conn, "ledger", "kind"):
        conn.execute(LEDGER_V1_DDL)
        conn.execute(COMPANIES_V1_DDL)
        conn.execute(COMPANY_STAKES_V1_DDL)
        conn.execute(TEIAI_DEPOSITS_V1_DDL)
        conn.execute(CDS_V1_DDL)
    for stmt in ALL_STATEMENTS:
        if stmt not in _V1_SKIP_SET:
            conn.execute(stmt)


__all__ = [
    "ALL_STATEMENTS",
    "HORSES_DDL",
    "HORSE_BETS_DDL",
    "HORSE_BETS_INDEX_DDL",
    "HORSE_ENTRIES_DDL",
    "HORSE_ENTRIES_INDEX_DDL",
    "HORSE_RACES_DDL",
    "INVENTORY_DDL",
    "KYOSAI_STATE_DDL",
    "KYOSAI_STATE_V3_DDL",
    "MARGIN_POSITIONS_DDL",
    "PROFILE_DDL",
    "SHOP_STOCK_DDL",
    "TEIAI_BANK_DDL",
    "TEIAI_HOUSE_DDL",
    "USERS_DDL",
    "V2_USER_SPLIT_STATEMENTS",
    "V3_TEIAI_SPLIT_STATEMENTS",
    "V4_LABOR_PROFILE_STATEMENTS",
    "V5_MARGIN_HORSE_STATEMENTS",
    "V6_SPLIT_STATEMENTS",
    "V7_INTEGRITY_INDEX_STATEMENTS",
    "create_all_tables",
    "create_v1_missing_tables",
    "new_quiz_order",
]
