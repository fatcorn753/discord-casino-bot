"""v5 から v6 への移行処理（残余ドメイン 5NF サブタイプ／番兵分離 ＋ Discord UI 分離）。"""

import json
import sqlite3

from infra.db.schema.ddl import (
    DISCORD_POSTS_DDL,
    KYOSAI_STATE_DDL,
    SCHEMA_FLAGS_DDL,
    V6_SPLIT_STATEMENTS,
)
from infra.db.schema.ddl.discord_ui import MANUAL_EVENT_DDL, STREAM_VOICE_CHANNELS_DDL
from infra.db.schema.ddl.horse import HORSE_RACES_DDL, HORSE_RACES_INDEX_DDL
from infra.db.schema.ddl.market import OPTION_PAIRS_DDL, OPTION_SHORT_MARGINS_DDL, OPTIONS_V6_DDL
from infra.db.schema.ddl.social import (
    ARENA_BETS_DDL,
    ARENA_BETS_INDEX_DDL,
    ARENA_BETS_USER_INDEX_DDL,
    ARENA_NPC_BETS_DDL,
    ARENA_NPC_BETS_INDEX_DDL,
    LOAN_TERMS_DDL,
    LOANS_DDL,
    LOANS_INDEX_DDL,
    MARKET_AUCTION_BIDS_BIDDER_INDEX_DDL,
    MARKET_AUCTION_BIDS_DDL,
    MARKET_CLOSURES_DDL,
    MARKET_LISTINGS_DDL,
    MARKET_LISTINGS_INDEX_DDL,
    POLLS_DDL,
    QUIZ_CHOICES_DDL,
    QUIZ_QUESTIONS_DDL,
    QUIZ_USER_SUBMISSIONS_DDL,
    QUIZ_USER_SUBMISSIONS_INDEX_DDL,
    RAFFLE_DDL,
    STREAMS_DDL,
    new_quiz_order,
)
from infra.db.schema.ddl.teiai import (
    BOND_DEFAULT_CHECKS_DDL,
    BONDS_V6_DDL,
    KYOSAI_MOTIONS_DDL,
    KYOSAI_UNITS_DDL,
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def _replace_table(conn: sqlite3.Connection, table: str, new_ddl: str, copy_sql: str) -> None:
    """RENAME による既存子テーブルの FK 書き換えを防ぐため、新テーブル作成→移行→旧削除→リネームを行う。"""
    tmp = f"{table}_v6new"
    conn.execute(f"DROP TABLE IF EXISTS {tmp}")
    ddl_tmp = new_ddl.replace(f"CREATE TABLE IF NOT EXISTS {table} ", f"CREATE TABLE {tmp} ", 1)
    conn.execute(ddl_tmp)
    conn.execute(copy_sql.replace(f" {table} ", f" {tmp} ", 1))
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")


def _extract_posts(
    conn: sqlite3.Connection,
    table: str,
    entity_type: str,
    id_col: str,
    *,
    has_post_done: bool = False,
) -> None:
    cols = _table_cols(conn, table)
    if "channel_id" not in cols:
        return
    msg_expr = "MAX(0, message_id)" if "message_id" in cols else "0"
    done_expr = "CASE WHEN post_done != 0 THEN 1 ELSE 0 END" if has_post_done and "post_done" in cols else "0"
    where = "channel_id > 0 OR message_id > 0" if "message_id" in cols else "channel_id > 0"
    if has_post_done and "post_done" in cols:
        where += " OR post_done != 0"
    conn.execute(
        f"INSERT OR REPLACE INTO discord_posts (entity_type, entity_id, channel_id, message_id, post_done) "
        f"SELECT ?, {id_col}, MAX(0, channel_id), {msg_expr}, {done_expr} FROM {table} WHERE {where}",
        (entity_type,),
    )


def _migrate_weekly_state(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA_FLAGS_DDL)
    if not _table_exists(conn, "weekly_state"):
        return
    if conn.execute("SELECT 1 FROM weekly_state WHERE guild_id=0").fetchone() is not None:
        conn.execute(
            "INSERT INTO schema_flags (key, value) VALUES ('weekly_backfilled', '1') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        conn.execute("DELETE FROM weekly_state WHERE guild_id=0")


def _migrate_manual_event(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "manual_event"):
        conn.execute(MANUAL_EVENT_DDL)
        return
    if "channel_id" not in _table_cols(conn, "manual_event"):
        return
    _extract_posts(conn, "manual_event", "manual_event", "guild_id")
    _replace_table(
        conn,
        "manual_event",
        MANUAL_EVENT_DDL,
        "INSERT INTO manual_event (guild_id, game, boost, starts, ends, ended) "
        "SELECT guild_id, game, boost, starts, ends, CASE WHEN ended != 0 THEN 1 ELSE 0 END FROM manual_event",
    )


def _migrate_horse_races(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "horse_races"):
        conn.execute(HORSE_RACES_DDL)
        conn.execute(HORSE_RACES_INDEX_DDL)
        return
    if "channel_id" not in _table_cols(conn, "horse_races"):
        return
    _extract_posts(conn, "horse_races", "horse_race", "id")
    _replace_table(
        conn,
        "horse_races",
        HORSE_RACES_DDL,
        "INSERT INTO horse_races (id, guild_id, kind, start_at, entry_close, bet_close, status, distance, going, created) "
        "SELECT id, guild_id, kind, start_at, entry_close, bet_close, status, distance, going, created FROM horse_races",
    )
    conn.execute(HORSE_RACES_INDEX_DDL)


def _migrate_kyosai(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "kyosai_state"):
        conn.execute(KYOSAI_STATE_DDL)
    elif "house_units" not in _table_cols(conn, "kyosai_state"):
        _replace_table(
            conn,
            "kyosai_state",
            KYOSAI_STATE_DDL,
            "INSERT INTO kyosai_state (guild_id, div_ts, paid, charged, house_units) "
            "SELECT guild_id, div_ts, paid, charged, 0 FROM kyosai_state",
        )
    if _table_exists(conn, "kyosai_units"):
        has_non_positive = (
            conn.execute("SELECT 1 FROM kyosai_units WHERE user_id <= 0 LIMIT 1").fetchone() is not None
        )
        if has_non_positive:
            conn.execute(
                "INSERT INTO kyosai_state (guild_id, house_units) "
                "SELECT guild_id, COALESCE(SUM(MAX(0, units)), 0) FROM kyosai_units "
                "WHERE user_id <= 0 GROUP BY guild_id "
                "ON CONFLICT(guild_id) DO UPDATE SET house_units = kyosai_state.house_units + excluded.house_units"
            )
            _replace_table(
                conn,
                "kyosai_units",
                KYOSAI_UNITS_DDL,
                "INSERT INTO kyosai_units (guild_id, user_id, units) "
                "SELECT guild_id, user_id, MAX(0, units) FROM kyosai_units WHERE user_id > 0",
            )
    if _table_exists(conn, "kyosai_motions") and "channel_id" in _table_cols(conn, "kyosai_motions"):
        _extract_posts(conn, "kyosai_motions", "kyosai_motion", "id")
        _replace_table(
            conn,
            "kyosai_motions",
            KYOSAI_MOTIONS_DDL,
            "INSERT INTO kyosai_motions (id, guild_id, proposer, premium, created, closes, status, yes, no) "
            "SELECT id, guild_id, proposer, premium, created, closes, status, yes, no FROM kyosai_motions",
        )


def _migrate_bonds(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "bonds"):
        conn.execute(BONDS_V6_DDL)
        conn.execute(BOND_DEFAULT_CHECKS_DDL)
        return
    if "checked" not in _table_cols(conn, "bonds"):
        conn.execute(BOND_DEFAULT_CHECKS_DDL)
        return
    checked_rows = [
        (int(r[0]), float(r[1]))
        for r in conn.execute("SELECT id, checked FROM bonds WHERE checked IS NOT NULL").fetchall()
    ]
    _replace_table(
        conn,
        "bonds",
        BONDS_V6_DDL,
        "INSERT INTO bonds (id, guild_id, user_id, kind, principal, due, status, paid, ts, rate) "
        "SELECT id, guild_id, user_id, kind, MAX(1, principal), due, status, MAX(0, paid), ts, rate FROM bonds",
    )
    conn.execute(BOND_DEFAULT_CHECKS_DDL)
    if checked_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO bond_default_checks (bond_id, checked_at) VALUES (?, ?)", checked_rows
        )


def _migrate_options(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "options"):
        conn.execute(OPTIONS_V6_DDL)
        conn.execute(OPTION_PAIRS_DDL)
        conn.execute(OPTION_SHORT_MARGINS_DDL)
        return
    cols = _table_cols(conn, "options")
    if "symbol2" not in cols and "margin" not in cols:
        conn.execute(OPTION_PAIRS_DDL)
        conn.execute(OPTION_SHORT_MARGINS_DDL)
        return
    pair_rows = (
        [
            (int(r[0]), str(r[1]), float(r[2]))
            for r in conn.execute(
                "SELECT id, symbol2, spot2 FROM options WHERE symbol2 IS NOT NULL AND spot2 IS NOT NULL"
            ).fetchall()
        ]
        if {"symbol2", "spot2"}.issubset(cols)
        else []
    )
    margin_rows = (
        [(int(r[0]), int(r[1])) for r in conn.execute("SELECT id, margin FROM options WHERE margin > 0").fetchall()]
        if "margin" in cols
        else []
    )
    _replace_table(
        conn,
        "options",
        OPTIONS_V6_DDL,
        "INSERT INTO options (id, guild_id, user_id, kind, symbol, strike, units, premium, expiry, spot, ts, status, paid, final, side) "
        "SELECT id, guild_id, user_id, kind, symbol, strike, MAX(1, units), MAX(0, premium), expiry, spot, ts, "
        "status, MAX(0, paid), final, side FROM options",
    )
    conn.execute(OPTION_PAIRS_DDL)
    conn.execute(OPTION_SHORT_MARGINS_DDL)
    if pair_rows:
        conn.executemany("INSERT OR REPLACE INTO option_pairs (option_id, symbol2, spot2) VALUES (?, ?, ?)", pair_rows)
    if margin_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO option_short_margins (option_id, margin) VALUES (?, ?)", margin_rows
        )


def _migrate_loans(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "loans"):
        conn.execute(LOANS_DDL)
        conn.execute(LOANS_INDEX_DDL)
        conn.execute(LOAN_TERMS_DDL)
        return
    cols = _table_cols(conn, "loans")
    if "accepted_at" not in cols and "channel_id" not in cols:
        conn.execute(LOAN_TERMS_DDL)
        return
    _extract_posts(conn, "loans", "loan", "id")
    term_rows = [
        (int(r[0]), float(r[1]), float(r[2]), int(r[3]), int(r[4]))
        for r in conn.execute(
            "SELECT id, COALESCE(accepted_at, created), "
            "COALESCE(due, COALESCE(accepted_at, created) + term_days * 86400.0), "
            "MAX(0, paid), CASE WHEN reminded != 0 THEN 1 ELSE 0 END "
            "FROM loans WHERE accepted_at IS NOT NULL OR status IN ('active', 'overdue', 'repaid', 'discharged')"
        ).fetchall()
    ]
    _replace_table(
        conn,
        "loans",
        LOANS_DDL,
        "INSERT INTO loans (id, guild_id, lender_id, borrower_id, principal, rate_tenths, term_days, status, created, offer_expires) "
        "SELECT id, guild_id, CASE WHEN lender_id > 0 THEN lender_id ELSE NULL END, borrower_id, "
        "MAX(1, principal), MAX(0, rate_tenths), MAX(1, term_days), status, created, offer_expires FROM loans",
    )
    conn.execute(LOANS_INDEX_DDL)
    conn.execute(LOAN_TERMS_DDL)
    if term_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO loan_terms (loan_id, accepted_at, due, paid, reminded) VALUES (?, ?, ?, ?, ?)",
            term_rows,
        )


def _migrate_market_listings(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "market_listings"):
        conn.execute(MARKET_LISTINGS_DDL)
        conn.execute(MARKET_LISTINGS_INDEX_DDL)
        conn.execute(MARKET_AUCTION_BIDS_DDL)
        conn.execute(MARKET_AUCTION_BIDS_BIDDER_INDEX_DDL)
        conn.execute(MARKET_CLOSURES_DDL)
        return
    cols = _table_cols(conn, "market_listings")
    if "bid" not in cols and "channel_id" not in cols:
        conn.execute(MARKET_AUCTION_BIDS_DDL)
        conn.execute(MARKET_AUCTION_BIDS_BIDDER_INDEX_DDL)
        conn.execute(MARKET_CLOSURES_DDL)
        return
    _extract_posts(conn, "market_listings", "market_listing", "id", has_post_done=True)
    bid_rows = [
        (int(r[0]), int(r[1]), int(r[2]))
        for r in conn.execute("SELECT id, bidder_id, bid FROM market_listings WHERE bidder_id > 0 AND bid > 0").fetchall()
    ]
    closure_rows = [
        (int(r[0]), float(r[1]), int(r[2]) if r[2] is not None else None, int(r[3]))
        for r in conn.execute(
            "SELECT id, COALESCE(closed_at, end_at), "
            "CASE WHEN buyer_id > 0 THEN buyer_id ELSE NULL END, MAX(0, sold_price) "
            "FROM market_listings WHERE closed_at IS NOT NULL OR status != 'active'"
        ).fetchall()
    ]
    _replace_table(
        conn,
        "market_listings",
        MARKET_LISTINGS_DDL,
        "INSERT INTO market_listings (id, guild_id, seller_id, kind, ref, qty, mode, price, created, end_at, status) "
        "SELECT id, guild_id, CASE WHEN seller_id > 0 THEN seller_id ELSE NULL END, "
        "kind, ref, MAX(1, qty), mode, MAX(1, price), created, end_at, status FROM market_listings",
    )
    conn.execute(MARKET_LISTINGS_INDEX_DDL)
    conn.execute(MARKET_AUCTION_BIDS_DDL)
    conn.execute(MARKET_AUCTION_BIDS_BIDDER_INDEX_DDL)
    conn.execute(MARKET_CLOSURES_DDL)
    if bid_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO market_auction_bids (listing_id, bidder_id, amount) VALUES (?, ?, ?)", bid_rows
        )
    if closure_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO market_closures (listing_id, closed_at, buyer_id, sold_price) VALUES (?, ?, ?, ?)",
            closure_rows,
        )


def _decode_quiz_order(raw_order: str, n: int) -> list[int]:
    if raw_order:
        try:
            parsed = json.loads(raw_order)
            if isinstance(parsed, list) and len(parsed) == n and set(parsed) == set(range(n)):
                return [int(x) for x in parsed]
        except (ValueError, TypeError):
            pass
    return new_quiz_order(n)


def _migrate_quiz(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "quiz_questions"):
        conn.execute(QUIZ_QUESTIONS_DDL)
        conn.execute(QUIZ_CHOICES_DDL)
        conn.execute(QUIZ_USER_SUBMISSIONS_DDL)
        conn.execute(QUIZ_USER_SUBMISSIONS_INDEX_DDL)
        return
    cols = _table_cols(conn, "quiz_questions")
    if "choices" not in cols:
        conn.execute(QUIZ_CHOICES_DDL)
        conn.execute(QUIZ_USER_SUBMISSIONS_DDL)
        conn.execute(QUIZ_USER_SUBMISSIONS_INDEX_DDL)
        return
    _extract_posts(conn, "quiz_questions", "quiz", "id")
    choice_rows: list[tuple[int, int, str, int]] = []
    sub_rows: list[tuple[int, int, int, int, int]] = []
    order_expr = "shown_order" if "shown_order" in cols else "''"
    author_expr = "author_id" if "author_id" in cols else "0"
    guild_expr = "guild_id" if "guild_id" in cols else "0"
    settled_expr = "settled" if "settled" in cols else "0"
    earned_expr = "earned" if "earned" in cols else "0"
    cname_expr = "creator_name" if "creator_name" in cols else "''"
    mright_expr = "msg_right" if "msg_right" in cols else "''"
    mwrong_expr = "msg_wrong" if "msg_wrong" in cols else "''"
    for r in conn.execute(
        f"SELECT id, choices, {order_expr}, {author_expr}, {guild_expr}, {settled_expr}, {earned_expr} FROM quiz_questions"
    ).fetchall():
        qid = int(r[0])
        choices = [str(c) for c in json.loads(str(r[1]))]
        pos_of = {choice_idx: pos for pos, choice_idx in enumerate(_decode_quiz_order(str(r[2] or ""), len(choices)))}
        choice_rows.extend((qid, idx, lbl, pos_of[idx]) for idx, lbl in enumerate(choices))
        if int(r[3]) > 0:
            sub_rows.append(
                (qid, int(r[4]), int(r[3]), 1 if int(r[5]) else 0, max(0, int(r[6])))
            )
    _replace_table(
        conn,
        "quiz_questions",
        QUIZ_QUESTIONS_DDL,
        f"INSERT INTO quiz_questions (id, question, explanation, creator_name, msg_right, msg_wrong, reward, expires, created) "
        f"SELECT id, question, explanation, {cname_expr}, {mright_expr}, {mwrong_expr}, reward, expires, created FROM quiz_questions",
    )
    conn.execute(QUIZ_CHOICES_DDL)
    conn.execute(QUIZ_USER_SUBMISSIONS_DDL)
    conn.execute(QUIZ_USER_SUBMISSIONS_INDEX_DDL)
    if choice_rows:
        conn.execute("DELETE FROM quiz_choices WHERE question_id NOT IN (SELECT id FROM quiz_questions)")
        conn.executemany(
            "INSERT OR REPLACE INTO quiz_choices (question_id, choice_idx, label, display_pos) VALUES (?, ?, ?, ?)",
            choice_rows,
        )
    if sub_rows:
        conn.execute("DELETE FROM quiz_user_submissions WHERE question_id NOT IN (SELECT id FROM quiz_questions)")
        conn.executemany(
            "INSERT OR REPLACE INTO quiz_user_submissions (question_id, guild_id, author_id, settled, earned) VALUES (?, ?, ?, ?, ?)",
            sub_rows,
        )


def _migrate_raffle(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "raffle"):
        conn.execute(RAFFLE_DDL)
        return
    if "channel_id" not in _table_cols(conn, "raffle"):
        return
    _extract_posts(conn, "raffle", "raffle", "id")
    _replace_table(
        conn,
        "raffle",
        RAFFLE_DDL,
        "INSERT INTO raffle (id, guild_id, host_id, fee, closes, status) "
        "SELECT id, guild_id, host_id, fee, closes, status FROM raffle",
    )


def _migrate_polls(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "polls"):
        conn.execute(POLLS_DDL)
        return
    if "channel_id" not in _table_cols(conn, "polls"):
        return
    _extract_posts(conn, "polls", "poll", "id")
    _replace_table(
        conn,
        "polls",
        POLLS_DDL,
        "INSERT INTO polls (id, guild_id, creator, title, closes, adj_yes, adj_no, rigs, status) "
        "SELECT id, guild_id, creator, title, closes, adj_yes, adj_no, MAX(0, rigs), status FROM polls",
    )


def _migrate_streams(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "streams"):
        conn.execute(STREAMS_DDL)
        conn.execute(STREAM_VOICE_CHANNELS_DDL)
        return
    cols = _table_cols(conn, "streams")
    if "channel_id" not in cols and "voice_channel_id" not in cols:
        conn.execute(STREAM_VOICE_CHANNELS_DDL)
        return
    _extract_posts(conn, "streams", "stream", "id")
    vc_rows = (
        [
            (int(r[0]), int(r[1]), 1 if int(r[2]) else 0)
            for r in conn.execute(
                "SELECT id, voice_channel_id, created_vc FROM streams WHERE voice_channel_id > 0"
            ).fetchall()
        ]
        if {"voice_channel_id", "created_vc"}.issubset(cols)
        else []
    )
    _replace_table(
        conn,
        "streams",
        STREAMS_DDL,
        "INSERT INTO streams (id, guild_id, streamer, title, url, started, ends, tips, tip_count, status) "
        "SELECT id, guild_id, streamer, title, url, started, ends, MAX(0, tips), MAX(0, tip_count), status FROM streams",
    )
    conn.execute(STREAM_VOICE_CHANNELS_DDL)
    if vc_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO stream_voice_channels (stream_id, voice_channel_id, created_vc) VALUES (?, ?, ?)",
            vc_rows,
        )


def _migrate_arena_bets(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "arena_bets"):
        conn.execute(ARENA_BETS_DDL)
        conn.execute(ARENA_BETS_INDEX_DDL)
        conn.execute(ARENA_BETS_USER_INDEX_DDL)
        conn.execute(ARENA_NPC_BETS_DDL)
        conn.execute(ARENA_NPC_BETS_INDEX_DDL)
        return
    if _table_exists(conn, "arena_npc_bets"):
        return
    npc_rows = [
        (int(r[0]), str(r[1]), int(r[2]), int(r[3]), 1 if int(r[4]) else 0, float(r[5]))
        for r in conn.execute(
            "SELECT b.tournament_id, b.stage, b.fighter_id, b.amount, b.settled, b.created "
            "FROM arena_bets b "
            "JOIN arena_tournaments t ON t.id = b.tournament_id "
            "JOIN arena_fighters f ON f.id = b.fighter_id "
            "WHERE b.user_id <= 0 AND b.amount > 0"
        ).fetchall()
    ]
    _replace_table(
        conn,
        "arena_bets",
        ARENA_BETS_DDL,
        "INSERT INTO arena_bets (id, tournament_id, stage, fighter_id, user_id, amount, payout, settled, created) "
        "SELECT b.id, b.tournament_id, b.stage, b.fighter_id, b.user_id, b.amount, "
        "MAX(0, b.payout), CASE WHEN b.settled != 0 THEN 1 ELSE 0 END, b.created "
        "FROM arena_bets b "
        "JOIN arena_tournaments t ON t.id = b.tournament_id "
        "JOIN arena_fighters f ON f.id = b.fighter_id "
        "WHERE b.user_id > 0 AND b.amount > 0",
    )
    conn.execute(ARENA_BETS_INDEX_DDL)
    conn.execute(ARENA_BETS_USER_INDEX_DDL)
    conn.execute(ARENA_NPC_BETS_DDL)
    conn.execute(ARENA_NPC_BETS_INDEX_DDL)
    if npc_rows:
        conn.executemany(
            "INSERT INTO arena_npc_bets (tournament_id, stage, fighter_id, amount, settled, created) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            npc_rows,
        )


def migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
    """残余ドメインの 5NF サブタイプ・番兵分離と Discord UI 情報の分離を適用する。"""
    conn.execute(DISCORD_POSTS_DDL)
    _migrate_weekly_state(conn)
    _migrate_manual_event(conn)
    _migrate_horse_races(conn)
    _migrate_kyosai(conn)
    _migrate_bonds(conn)
    _migrate_options(conn)
    _migrate_loans(conn)
    _migrate_market_listings(conn)
    _migrate_quiz(conn)
    _migrate_raffle(conn)
    _migrate_polls(conn)
    _migrate_streams(conn)
    _migrate_arena_bets(conn)
    for stmt in V6_SPLIT_STATEMENTS:
        conn.execute(stmt)
