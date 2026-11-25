"""PRAGMA user_version ベースの連番スキーママイグレーションランナー。"""

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from infra.db.schema.ddl import create_all_tables, create_v1_missing_tables
from infra.db.schema.invariants import capture_invariants, verify_invariants
from infra.db.schema.migrations import add_missing_columns, migrate_to_guild_scope, retire_autoplay
from infra.db.schema.migrations_v1 import migrate_v0_to_v1
from infra.db.schema.migrations_v2 import migrate_v1_to_v2
from infra.db.schema.migrations_v3 import migrate_v2_to_v3
from infra.db.schema.migrations_v4 import migrate_v3_to_v4
from infra.db.schema.migrations_v5 import migrate_v4_to_v5
from infra.db.schema.migrations_v6 import migrate_v5_to_v6
from infra.db.schema.migrations_v7 import migrate_v6_to_v7

CURRENT_SCHEMA_VERSION = 7


@dataclass(frozen=True)
class MigrationStep:
    """単一バージョンのスキーマ移行ステップ。"""

    version: int
    name: str
    fn: Callable[[sqlite3.Connection], None]
    prepare: Callable[[sqlite3.Connection], None] | None = None


@dataclass(frozen=True)
class MigrationReport:
    """DB 初期化・マイグレーションの実行結果レポート。"""

    start_version: int
    end_version: int
    fresh: bool
    backup_paths: tuple[str, ...]
    applied: tuple[str, ...]
    invariants_checked: bool


def _prepare_v0(conn: sqlite3.Connection) -> None:
    """v0 DB の欠落表・欠落列・廃止テーブル（autoplay）を統合前に整合させる。"""
    create_v1_missing_tables(conn)
    add_missing_columns(conn)
    retire_autoplay(conn)


MIGRATIONS: tuple[MigrationStep, ...] = (
    MigrationStep(
        version=1,
        name="v0_to_v1_clean_dead_schema",
        fn=migrate_v0_to_v1,
        prepare=_prepare_v0,
    ),
    MigrationStep(
        version=2,
        name="v1_to_v2_split_users",
        fn=migrate_v1_to_v2,
    ),
    MigrationStep(
        version=3,
        name="v2_to_v3_split_teiai_bank_house",
        fn=migrate_v2_to_v3,
    ),
    MigrationStep(
        version=4,
        name="v3_to_v4_split_labor_profile",
        fn=migrate_v3_to_v4,
    ),
    MigrationStep(
        version=5,
        name="v4_to_v5_margin_and_horse_5nf",
        fn=migrate_v4_to_v5,
    ),
    MigrationStep(
        version=6,
        name="v5_to_v6_subtypes_sentinels_and_discord_ui",
        fn=migrate_v5_to_v6,
    ),
    MigrationStep(
        version=7,
        name="v6_to_v7_ledger_structured_and_integrity_fks",
        fn=migrate_v6_to_v7,
    ),
)


def backup_database(conn: sqlite3.Connection, db_path: str, tag: str) -> str | None:
    """移行前の DB を SQLite Backup API で一時ファイル経由のアトミック保存する。"""
    if not db_path or db_path == ":memory:":
        return None
    backup = f"{db_path}.{tag}"
    if not os.path.exists(backup):
        tmp = f"{backup}.tmp"
        dst = sqlite3.connect(tmp)
        try:
            conn.backup(dst)
        finally:
            dst.close()
        os.replace(tmp, backup)
    return backup


def _has_user_tables(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone()
    return row is not None


def _current_user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0


def _init_fresh_db(conn: sqlite3.Connection, target_version: int) -> MigrationReport:
    conn.execute("BEGIN IMMEDIATE")
    try:
        create_all_tables(conn)
        conn.execute(f"PRAGMA user_version = {int(target_version)}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return MigrationReport(
        start_version=0,
        end_version=target_version,
        fresh=True,
        backup_paths=(),
        applied=(),
        invariants_checked=False,
    )


def _run_step(conn: sqlite3.Connection, db_path: str, step: MigrationStep) -> str | None:
    backup = backup_database(conn, db_path, f"before-v{step.version}")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if step.prepare is not None:
            step.prepare(conn)
        before = capture_invariants(conn)
        step.fn(conn)
        after = capture_invariants(conn)
        verify_invariants(before, after)
        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_errors:
            raise RuntimeError(f"外部キー整合性チェックに失敗しました ({step.name}): {len(fk_errors)}件")
        conn.execute(f"PRAGMA user_version = {int(step.version)}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return backup


def _cleanup_stray_autoplay(conn: sqlite3.Connection) -> None:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='autoplay'").fetchone():
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        retire_autoplay(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def initialize_db(
    conn: sqlite3.Connection,
    db_path: str,
    legacy_guild_id: int | None = None,
    *,
    steps: tuple[MigrationStep, ...] = MIGRATIONS,
    target_version: int = CURRENT_SCHEMA_VERSION,
) -> MigrationReport:
    """スキーマ版を確認し、新規作成または vN までの無損失マイグレーションを適用する。"""
    start_ver = _current_user_version(conn)
    if start_ver > target_version:
        raise RuntimeError(f"DB のスキーマ版 ({start_ver}) がこのコードの対応版 ({target_version}) より新しいです。")
    if start_ver == 0 and not _has_user_tables(conn):
        return _init_fresh_db(conn, target_version)
    if start_ver == 0:
        migrate_to_guild_scope(conn, db_path, legacy_guild_id)
    backups: list[str] = []
    applied: list[str] = []
    current = start_ver
    for step in steps:
        if current < step.version <= target_version:
            backup = _run_step(conn, db_path, step)
            if backup is not None:
                backups.append(backup)
            applied.append(step.name)
            current = step.version
    if not applied:
        _cleanup_stray_autoplay(conn)
    return MigrationReport(
        start_version=start_ver,
        end_version=current,
        fresh=False,
        backup_paths=tuple(backups),
        applied=tuple(applied),
        invariants_checked=bool(applied),
    )
