"""SQLite テーブル定義（DDL）と初期化の互換ファサード。"""

from infra.db.schema.ddl import (
    INVENTORY_DDL,
    PROFILE_DDL,
    SHOP_STOCK_DDL,
    USERS_DDL,
    create_all_tables,
    create_v1_missing_tables,
    new_quiz_order,
)

create_tables = create_all_tables

__all__ = [
    "INVENTORY_DDL",
    "PROFILE_DDL",
    "SHOP_STOCK_DDL",
    "USERS_DDL",
    "create_tables",
    "create_v1_missing_tables",
    "new_quiz_order",
]
