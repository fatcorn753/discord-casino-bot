"""v0（旧スキーマ）から v1 への移行処理（死に列・死にテーブル・不要行の掃除）。"""

import sqlite3

from infra.db.schema.ddl import create_v1_missing_tables

# v1 で廃止する列（テーブル名, 列名一覧）
_DEAD_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("teiai_bank", ("bailout_debt", "kyosai_house_ts")),
    ("horses", ("rented_day",)),
    ("horse_races", ("grade",)),
    ("horse_entries", ("tactic",)),
    ("labor", ("helmet", "rest_until", "apprentice", "miner", "craftsman", "foreman", "wage")),
    ("labor_workers", ("legacy",)),
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _drop_columns_for_table(conn: sqlite3.Connection, table: str, cols: tuple[str, ...]) -> None:
    existing = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
    if not existing:
        return
    for col in cols:
        if col in existing:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")


def _purge_dead_rows(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "seized_items"):
        conn.execute("DELETE FROM seized_items WHERE qty <= 0")
    if _table_exists(conn, "horse_odds") and _table_exists(conn, "horse_races"):
        conn.execute("DELETE FROM horse_odds WHERE race_id IN (SELECT id FROM horse_races WHERE status = 'done')")


def migrate_v0_to_v1(conn: sqlite3.Connection) -> None:
    """v0 スキーマから v1（死に列・死にテーブル除去と組合せオッズ・在庫0行掃除）へ移行する。"""
    create_v1_missing_tables(conn)
    for table, cols in _DEAD_COLUMNS:
        _drop_columns_for_table(conn, table, cols)
    conn.execute("DROP TABLE IF EXISTS guild_users")
    _purge_dead_rows(conn)
