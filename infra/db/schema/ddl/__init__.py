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
from infra.db.schema.ddl.shop import INVENTORY_DDL, PROFILE_DDL, SHOP_STOCK_DDL
from infra.db.schema.ddl.social import new_quiz_order
from infra.db.schema.ddl.teiai import (
    TEIAI_BANKRUPTCIES_DDL,
    TEIAI_DEBTS_DDL,
    TEIAI_ESCAPES_DDL,
    TEIAI_GRACE_ACTIVE_INDEX_DDL,
    TEIAI_GRACE_DDL,
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
_V2_USER_SPLIT_SET = frozenset(V2_USER_SPLIT_STATEMENTS)

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


def create_v1_missing_tables(conn: sqlite3.Connection) -> None:
    """v0 -> v1 移行用に、v2 分離テーブルを除いた不足テーブル（旧 users を含む）を作成する。"""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone() is None:
        conn.execute(USERS_DDL)
    for stmt in ALL_STATEMENTS:
        if stmt not in _V2_USER_SPLIT_SET:
            conn.execute(stmt)


__all__ = [
    "ALL_STATEMENTS",
    "INVENTORY_DDL",
    "PROFILE_DDL",
    "SHOP_STOCK_DDL",
    "USERS_DDL",
    "V2_USER_SPLIT_STATEMENTS",
    "create_all_tables",
    "create_v1_missing_tables",
    "new_quiz_order",
]
