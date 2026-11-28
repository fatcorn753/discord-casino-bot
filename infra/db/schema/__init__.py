"""DB スキーマ定義とマイグレーション。"""

from infra.db.schema.invariants import capture_invariants, diff_invariants, verify_invariants
from infra.db.schema.migrations import (
    ARCHAEOLOGIST_FEE,
    _migrate_labor_counts,
    _refund_archaeologists,
    _refund_legacy_contracts,
    add_missing_columns,
    migrate_to_guild_scope,
    retire_autoplay,
)
from infra.db.schema.runner import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    MigrationReport,
    MigrationStep,
    backup_database,
    initialize_db,
)
from infra.db.schema.tables import (
    INVENTORY_DDL,
    PROFILE_DDL,
    SHOP_STOCK_DDL,
    USERS_DDL,
    create_tables,
    create_v1_missing_tables,
    new_quiz_order,
)

__all__ = [
    "ARCHAEOLOGIST_FEE",
    "CURRENT_SCHEMA_VERSION",
    "INVENTORY_DDL",
    "MIGRATIONS",
    "MigrationReport",
    "MigrationStep",
    "PROFILE_DDL",
    "SHOP_STOCK_DDL",
    "USERS_DDL",
    "_migrate_labor_counts",
    "_refund_archaeologists",
    "_refund_legacy_contracts",
    "add_missing_columns",
    "backup_database",
    "capture_invariants",
    "create_tables",
    "create_v1_missing_tables",
    "diff_invariants",
    "initialize_db",
    "migrate_to_guild_scope",
    "new_quiz_order",
    "retire_autoplay",
    "verify_invariants",
]
