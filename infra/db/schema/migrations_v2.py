"""v1 から v2 への移行処理（users 神テーブルを 7 表へ 5NF 分離）。"""

import sqlite3

from infra.db.schema.ddl import V2_USER_SPLIT_STATEMENTS


def migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """旧 users 表から accounts・user_daily・user_play_stats・teiai_* の 7 表へ無損失移行する。"""
    for stmt in V2_USER_SPLIT_STATEMENTS:
        conn.execute(stmt)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone() is None:
        return
    conn.execute(
        "INSERT OR IGNORE INTO accounts (guild_id, user_id, balance, created_at) "
        "SELECT guild_id, user_id, MAX(0, balance), created_at FROM users"
    )
    conn.execute(
        "INSERT OR IGNORE INTO user_daily (guild_id, user_id, streak, last_daily, last_slot) "
        "SELECT guild_id, user_id, MAX(0, daily_streak), last_daily, last_slot FROM users "
        "WHERE daily_streak > 0 OR last_daily IS NOT NULL OR last_slot IS NOT NULL"
    )
    conn.execute(
        "INSERT OR IGNORE INTO user_play_stats (guild_id, user_id, games_played, total_wagered, total_won, biggest_win) "
        "SELECT guild_id, user_id, MAX(0, games_played), MAX(0, total_wagered), MAX(0, total_won), MAX(0, biggest_win) FROM users "
        "WHERE games_played > 0 OR total_wagered > 0 OR total_won > 0 OR biggest_win > 0"
    )
    conn.execute(
        "INSERT OR IGNORE INTO teiai_debts (guild_id, user_id, debt, principal, interest_at) "
        "SELECT guild_id, user_id, MAX(0, debt), MAX(0, debt_principal), debt_interest_at FROM users "
        "WHERE debt > 0 OR debt_principal > 0 OR debt_interest_at > 0"
    )
    conn.execute(
        "INSERT OR IGNORE INTO teiai_grace (guild_id, user_id, grace_until, penalty, penalty_at) "
        "SELECT guild_id, user_id, grace_until, MAX(0.0, grace_penalty), grace_penalty_at FROM users "
        "WHERE grace_until IS NOT NULL OR grace_penalty > 0 OR grace_penalty_at > 0"
    )
    conn.execute(
        "INSERT OR IGNORE INTO teiai_bankruptcies (guild_id, user_id, bankrupt_at, blacklist_until) "
        "SELECT guild_id, user_id, bankrupt_at, blacklist_until FROM users WHERE bankrupt_at IS NOT NULL"
    )
    conn.execute(
        "INSERT OR IGNORE INTO teiai_escapes (guild_id, user_id, last_attempt_at, tries) "
        "SELECT guild_id, user_id, escape_at, MAX(0, escape_tries) FROM users "
        "WHERE escape_at IS NOT NULL OR escape_tries > 0"
    )
    conn.execute("DROP TABLE users")
