"""v3 から v4 への移行処理（labor の 1NF/5NF 分離・JSON 廃止、profile の縦持ち 5NF 分離）。"""

import json
import sqlite3

from infra.db.schema.ddl import V4_LABOR_PROFILE_STATEMENTS

_PROFILE_SLOTS = ("title", "card_color", "card_icon", "skin_dice", "skin_table")
_PROFILE_BUFFS = ("daily_boost", "insurance")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _parse_json_counts(raw: str) -> list[tuple[str, int]]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(parsed, dict):
        return []
    items: list[tuple[str, int]] = []
    for k, v in parsed.items():
        if isinstance(k, str) and k and isinstance(v, (int, float)) and int(v) > 0:
            items.append((k, int(v)))
    return items


def _migrate_pending_maps(conn: sqlite3.Connection) -> None:
    ore_rows: list[tuple[int, int, str, int]] = []
    relic_rows: list[tuple[int, int, str, int]] = []
    for r in conn.execute("SELECT guild_id, user_id, ores, relics FROM labor WHERE ores != '' OR relics != ''"):
        gid, uid = int(r[0]), int(r[1])
        for ore, qty in _parse_json_counts(str(r[2] or "")):
            ore_rows.append((gid, uid, ore, qty))
        for item_id, qty in _parse_json_counts(str(r[3] or "")):
            relic_rows.append((gid, uid, item_id, qty))
    if ore_rows:
        conn.executemany(
            "INSERT OR IGNORE INTO labor_pending_ores (guild_id, user_id, ore, qty) VALUES (?, ?, ?, ?)",
            ore_rows,
        )
    if relic_rows:
        conn.executemany(
            "INSERT OR IGNORE INTO labor_pending_relics (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, ?)",
            relic_rows,
        )


def _migrate_labor_table(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "labor"):
        return
    conn.execute(
        "INSERT OR IGNORE INTO labor_upgrades (guild_id, user_id, pick, glove, dorm, renew) "
        "SELECT guild_id, user_id, MAX(0, pick), MAX(0, glove), MAX(0, dorm), CASE WHEN renew = 0 THEN 0 ELSE 1 END "
        "FROM labor WHERE pick > 0 OR glove > 0 OR dorm > 0 OR renew != 1"
    )
    conn.execute(
        "INSERT OR IGNORE INTO labor_fatigue (guild_id, user_id, fatigue, fatigue_at, last_click) "
        "SELECT guild_id, user_id, MAX(0.0, fatigue), fatigue_at, last_click "
        "FROM labor WHERE fatigue > 0 OR fatigue_at > 0 OR last_click > 0"
    )
    conn.execute(
        "INSERT OR IGNORE INTO labor_pot (guild_id, user_id, pot, pot_workers, idle_at) "
        "SELECT guild_id, user_id, MAX(0, pot), MAX(0, pot_workers), idle_at "
        "FROM labor WHERE pot > 0 OR pot_workers > 0 OR idle_at > 0"
    )
    conn.execute(
        "INSERT OR IGNORE INTO labor_daily (guild_id, user_id, day, earned, clicks) "
        "SELECT guild_id, user_id, day, MAX(0, day_earned), MAX(0, day_click) "
        "FROM labor WHERE (day != '' AND (day_earned > 0 OR day_click > 0)) OR day_earned > 0 OR day_click > 0"
    )
    conn.execute(
        "INSERT OR IGNORE INTO labor_totals (guild_id, user_id, clicks, earned_total, donated) "
        "SELECT guild_id, user_id, MAX(0, clicks), MAX(0, earned_total), MAX(0, donated) "
        "FROM labor WHERE clicks > 0 OR earned_total > 0 OR donated > 0"
    )
    _migrate_pending_maps(conn)
    conn.execute("DROP TABLE labor")


def _migrate_profile_table(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "profile"):
        return
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(profile)")}
    for slot in _PROFILE_SLOTS:
        if slot in cols:
            conn.execute(
                f"INSERT OR IGNORE INTO user_equipment (guild_id, user_id, slot, item_id) "
                f"SELECT guild_id, user_id, ?, {slot} FROM profile "
                f"WHERE {slot} IS NOT NULL AND {slot} != ''",
                (slot,),
            )
    for buff in _PROFILE_BUFFS:
        if buff in cols:
            conn.execute(
                f"INSERT OR IGNORE INTO user_buffs (guild_id, user_id, buff, charges) "
                f"SELECT guild_id, user_id, ?, {buff} FROM profile WHERE {buff} > 0",
                (buff,),
            )
    conn.execute("DROP TABLE profile")


def migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """旧 labor / profile から v4 の 9 表へ無損失移行する。"""
    for stmt in V4_LABOR_PROFILE_STATEMENTS:
        conn.execute(stmt)
    _migrate_labor_table(conn)
    _migrate_profile_table(conn)
