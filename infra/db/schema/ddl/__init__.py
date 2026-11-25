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
from infra.db.schema.ddl.core import USERS_DDL
from infra.db.schema.ddl.shop import INVENTORY_DDL, PROFILE_DDL, SHOP_STOCK_DDL
from infra.db.schema.ddl.social import new_quiz_order

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


__all__ = [
    "ALL_STATEMENTS",
    "INVENTORY_DDL",
    "PROFILE_DDL",
    "SHOP_STOCK_DDL",
    "USERS_DDL",
    "create_all_tables",
    "new_quiz_order",
]
