"""v4 から v5 への移行処理（信用建玉の統合 margin_positions・競馬テーブル群の 5NF 正規化）。"""

import sqlite3

from domain import horse as horse_mod
from infra.db.schema.ddl import (
    HORSE_BETS_DDL,
    HORSE_BETS_INDEX_DDL,
    HORSE_ENTRIES_DDL,
    HORSE_ENTRIES_INDEX_DDL,
    HORSES_DDL,
    MARGIN_POSITIONS_DDL,
)
from infra.db.schema.ddl.horse import (
    HORSE_AUCTION_LISTINGS_DDL,
    HORSE_AUCTION_LISTINGS_INDEX_DDL,
    HORSE_BET_PICKS_DDL,
    HORSE_COMBO2_ODDS_DDL,
    HORSE_OWNERSHIP_DDL,
    HORSE_OWNERSHIP_INDEX_DDL,
    HORSE_RACE_REGISTRATIONS_DDL,
    HORSE_TRIO_ODDS_DDL,
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_margin_positions(conn: sqlite3.Connection) -> None:
    conn.execute(MARGIN_POSITIONS_DDL)
    for side, old_table in (("short", "crypto_short"), ("long", "crypto_long")):
        if not _table_exists(conn, old_table):
            continue
        cols = _table_cols(conn, old_table)
        sym_expr = "symbol" if "symbol" in cols else "'cs'"
        iat_expr = "interest_at" if "interest_at" in cols else "0.0"
        acc_expr = "accrued" if "accrued" in cols else "0.0"
        conn.execute(
            f"INSERT OR IGNORE INTO margin_positions "
            f"(guild_id, user_id, symbol, side, units, entry_value, margin, interest_at, accrued) "
            f"SELECT guild_id, user_id, {sym_expr}, ?, units, entry_value, MAX(0, margin), {iat_expr}, {acc_expr} "
            f"FROM {old_table} WHERE units > 0",
            (side,),
        )
        conn.execute(f"DROP TABLE {old_table}")


def _migrate_horses(conn: sqlite3.Connection) -> None:
    if "owner_id" not in _table_cols(conn, "horses"):
        for stmt in (
            HORSE_OWNERSHIP_DDL,
            HORSE_OWNERSHIP_INDEX_DDL,
            HORSE_AUCTION_LISTINGS_DDL,
            HORSE_AUCTION_LISTINGS_INDEX_DDL,
        ):
            conn.execute(stmt)
        return
    # SQLite の RENAME による子表 FK 書き換えを防ぐため、親を再作成してから子表を作る
    conn.execute("ALTER TABLE horses RENAME TO horses_v4")
    conn.execute(HORSES_DDL)
    for stmt in (
        HORSE_OWNERSHIP_DDL,
        HORSE_OWNERSHIP_INDEX_DDL,
        HORSE_AUCTION_LISTINGS_DDL,
        HORSE_AUCTION_LISTINGS_INDEX_DDL,
    ):
        conn.execute(stmt)
    conn.execute(
        "INSERT INTO horses "
        "(id, guild_id, status, name, coat, silks, speed, stamina, power, guts, races, wins, places, earnings, bonus, created, style) "
        "SELECT id, guild_id, "
        "CASE WHEN owner_id > 0 THEN 'owned' WHEN owner_id = -1 THEN 'auction' "
        "WHEN owner_id = -3 THEN 'escrow' ELSE 'retired' END, "
        "name, coat, silks, speed, stamina, power, guts, "
        "MAX(0, races), MAX(0, wins), MAX(0, places), MAX(0, earnings), MAX(0, bonus), created, style "
        "FROM horses_v4"
    )
    conn.execute(
        "INSERT OR IGNORE INTO horse_ownership (horse_id, guild_id, owner_id) "
        "SELECT id, guild_id, owner_id FROM horses_v4 WHERE owner_id > 0"
    )
    conn.execute(
        "INSERT OR IGNORE INTO horse_auction_listings (horse_id, guild_id, listed_at) "
        "SELECT id, guild_id, COALESCE(listed_at, created) FROM horses_v4 WHERE owner_id = -1"
    )
    conn.execute("DROP TABLE horses_v4")


def _migrate_horse_entries(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "horse_entries") or _table_exists(conn, "horse_race_registrations"):
        conn.execute(HORSE_RACE_REGISTRATIONS_DDL)
        return
    conn.execute("ALTER TABLE horse_entries RENAME TO horse_entries_v4")
    conn.execute(HORSE_RACE_REGISTRATIONS_DDL)
    conn.execute(HORSE_ENTRIES_DDL)
    conn.execute(HORSE_ENTRIES_INDEX_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO horse_race_registrations (race_id, horse_id, owner_id, name, coat, silks) "
        "SELECT e.race_id, e.horse_id, e.owner_id, e.name, e.coat, e.silks FROM horse_entries_v4 e "
        "JOIN horse_races r ON r.id = e.race_id "
        "JOIN horses h ON h.id = e.horse_id "
        "WHERE e.lane < 0 AND e.owner_id > 0"
    )
    conn.execute(
        "INSERT OR IGNORE INTO horse_entries "
        "(id, race_id, lane, horse_id, owner_id, name, coat, silks, speed, stamina, power, guts, "
        "style, form, races, wins, places, odds_win, odds_place, finish) "
        "SELECT e.id, e.race_id, e.lane, "
        "CASE WHEN e.horse_id > 0 AND EXISTS (SELECT 1 FROM horses h WHERE h.id = e.horse_id) "
        "THEN e.horse_id ELSE NULL END, "
        "CASE WHEN e.owner_id > 0 THEN e.owner_id ELSE NULL END, "
        "e.name, e.coat, e.silks, e.speed, e.stamina, e.power, e.guts, "
        "e.style, e.form, e.races, e.wins, e.places, e.odds_win, e.odds_place, e.finish "
        "FROM horse_entries_v4 e JOIN horse_races r ON r.id = e.race_id WHERE e.lane >= 1"
    )
    conn.execute("DROP TABLE horse_entries_v4")


def _parse_bet_lanes(kind: str, lane: int, picks: str) -> list[int]:
    if kind in ("win", "place"):
        return [lane] if lane >= 1 else []
    sep = ">" if kind == "exacta" else "-"
    if picks:
        raw = [int(x) for x in picks.split(sep) if x]
        return [ln for ln in raw if ln >= 1]
    return [lane] if lane >= 1 else []


def _migrate_horse_bets(conn: sqlite3.Connection) -> None:
    if "picks" not in _table_cols(conn, "horse_bets"):
        conn.execute(HORSE_BET_PICKS_DDL)
        return
    conn.execute("ALTER TABLE horse_bets RENAME TO horse_bets_v4")
    conn.execute(HORSE_BETS_DDL)
    conn.execute(HORSE_BETS_INDEX_DDL)
    conn.execute(HORSE_BET_PICKS_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO horse_bets "
        "(id, guild_id, race_id, user_id, kind, stake, odds, payout, settled, created) "
        "SELECT b.id, b.guild_id, b.race_id, b.user_id, b.kind, MAX(1, b.stake), b.odds, "
        "MAX(0, b.payout), CASE WHEN b.settled != 0 THEN 1 ELSE 0 END, b.created "
        "FROM horse_bets_v4 b JOIN horse_races r ON r.id = b.race_id"
    )
    picks_rows: list[tuple[int, int, int]] = []
    for r in conn.execute(
        "SELECT b.id, b.kind, b.lane, b.picks FROM horse_bets_v4 b JOIN horse_races r ON r.id = b.race_id"
    ):
        bet_id = int(r[0])
        lanes = _parse_bet_lanes(str(r[1]), int(r[2]), str(r[3] or ""))
        for pos, ln in enumerate(lanes, start=1):
            picks_rows.append((bet_id, pos, ln))
    if picks_rows:
        conn.executemany("INSERT OR IGNORE INTO horse_bet_picks (bet_id, position, lane) VALUES (?, ?, ?)", picks_rows)
    conn.execute("DROP TABLE horse_bets_v4")


def _parse_odds_key(kind: str, key: str) -> tuple[int, ...] | None:
    sep = ">" if kind == "exacta" else "-"
    parts = [int(x) for x in key.split(sep) if x]
    need = horse_mod.COMBO_SIZE.get(kind, 0)
    if len(parts) != need or len(set(parts)) != need or any(ln < 1 for ln in parts):
        return None
    if kind == "exacta":
        return tuple(parts)
    return tuple(sorted(parts))


def _migrate_horse_odds(conn: sqlite3.Connection) -> None:
    conn.execute(HORSE_COMBO2_ODDS_DDL)
    conn.execute(HORSE_TRIO_ODDS_DDL)
    if not _table_exists(conn, "horse_odds"):
        return
    combo2: list[tuple[int, str, int, int, float]] = []
    trio: list[tuple[int, int, int, int, float]] = []
    for r in conn.execute(
        "SELECT o.race_id, o.kind, o.key, o.odds FROM horse_odds o JOIN horse_races r ON r.id = o.race_id"
    ):
        race_id, kind, key, odds = int(r[0]), str(r[1]), str(r[2]), float(r[3])
        lanes = _parse_odds_key(kind, key)
        if lanes is None:
            continue
        if len(lanes) == 2:
            combo2.append((race_id, kind, lanes[0], lanes[1], odds))
        elif len(lanes) == 3:
            trio.append((race_id, lanes[0], lanes[1], lanes[2], odds))
    if combo2:
        conn.executemany(
            "INSERT OR IGNORE INTO horse_combo2_odds (race_id, kind, lane1, lane2, odds) VALUES (?, ?, ?, ?, ?)",
            combo2,
        )
    if trio:
        conn.executemany(
            "INSERT OR IGNORE INTO horse_trio_odds (race_id, lane1, lane2, lane3, odds) VALUES (?, ?, ?, ?, ?)",
            trio,
        )
    conn.execute("DROP TABLE horse_odds")


def migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    """crypto_short/long を統合し、競馬ドメインを 5NF（所有・競売・申込・買い目・オッズ分離）へ移行する。"""
    _migrate_margin_positions(conn)
    _migrate_horses(conn)
    _migrate_horse_entries(conn)
    _migrate_horse_bets(conn)
    _migrate_horse_odds(conn)
