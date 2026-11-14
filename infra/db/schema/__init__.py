"""DB スキーマ定義とマイグレーション。"""

from infra.db.schema.migrations import (
    ARCHAEOLOGIST_FEE,
    _migrate_labor_counts,
    _refund_archaeologists,
    _refund_legacy_contracts,
    add_missing_columns,
    migrate_to_guild_scope,
    retire_autoplay,
)
from infra.db.schema.tables import INVENTORY_DDL, PROFILE_DDL, USERS_DDL, create_tables, new_quiz_order

__all__ = [
    "ARCHAEOLOGIST_FEE",
    "INVENTORY_DDL",
    "PROFILE_DDL",
    "USERS_DDL",
    "_migrate_labor_counts",
    "_refund_archaeologists",
    "_refund_legacy_contracts",
    "add_missing_columns",
    "create_tables",
    "migrate_to_guild_scope",
    "new_quiz_order",
    "retire_autoplay",
]
