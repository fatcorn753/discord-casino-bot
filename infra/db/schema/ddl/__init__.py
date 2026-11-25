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
from infra.db.schema.ddl.core import ACCOUNTS_DDL, USER_DAILY_DDL, USER_PLAY_STATS_DDL, USERS_DDL
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
from infra.db.schema.ddl.market import CRYPTO_LONG_DDL, CRYPTO_SHORT_DDL, MARGIN_POSITIONS_DDL
from infra.db.schema.ddl.shop import INVENTORY_DDL, PROFILE_DDL, SHOP_STOCK_DDL, USER_BUFFS_DDL, USER_EQUIPMENT_DDL
from infra.db.schema.ddl.social import new_quiz_order
from infra.db.schema.ddl.teiai import (
    KYOSAI_STATE_DDL,
    TEIAI_BANK_DDL,
    TEIAI_BANK_V1_DDL,
    TEIAI_BANKRUPTCIES_DDL,
    TEIAI_DEBTS_DDL,
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
    KYOSAI_STATE_DDL,
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
_V1_SKIP_SET = frozenset(
    (
        *V2_USER_SPLIT_STATEMENTS,
        *V3_TEIAI_SPLIT_STATEMENTS,
        *V4_LABOR_PROFILE_STATEMENTS,
        *V5_MARGIN_HORSE_STATEMENTS,
        TEIAI_BANK_DDL,
        TEIAI_HOUSE_DDL,
        HORSES_DDL,
        HORSE_ENTRIES_DDL,
        HORSE_BETS_DDL,
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
    "INVENTORY_DDL",
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
    "create_all_tables",
    "create_v1_missing_tables",
    "new_quiz_order",
]
