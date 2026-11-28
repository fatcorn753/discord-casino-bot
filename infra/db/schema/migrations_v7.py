"""v6 から v7 への移行処理（ledger の構造化と全ドメインの FK・CHECK・部分索引の付与）。"""

import re
import sqlite3

from infra.db.schema.ddl import (
    BONDS_ACTIVE_INDEX_DDL,
    BONDS_DDL,
    CDS_ACTIVE_INDEX_DDL,
    CDS_DDL,
    COMPANIES_DDL,
    COMPANY_STAKES_DDL,
    LEDGER_DDL,
    LEDGER_KIND_INDEX_DDL,
    OPTIONS_ACTIVE_INDEX_DDL,
    TEIAI_DEPOSITS_DDL,
    TEIAI_HOUSE_DDL,
    TEIAI_HOUSE_GRANTS_DDL,
    V7_INTEGRITY_INDEX_STATEMENTS,
)
from infra.db.schema.ddl.core import LEDGER_INDEX_DDL
from infra.db.schema.ddl.market import EARNINGS_MEMOS_DDL, EARNINGS_SHOP_DDL, OPTIONS_DDL
from infra.db.schema.ddl.social import (
    ARENA_FIGHTERS_DDL,
    ARENA_FIGHTERS_INDEX_DDL,
    POLL_VOTES_DDL,
    QUIZ_ANSWERS_DDL,
    RAFFLE_ENTRIES_DDL,
    STREAM_BETS_DDL,
    STREAM_ROUNDS_DDL,
)
from infra.db.schema.ddl.teiai import (
    KYOSAI_VOTES_DDL,
    TEIAI_MARKET_DAYS_DDL,
    TEIAI_MARKET_DDL,
    TEIAI_MARKET_INDEX_DDL,
)

_RE_TRANSFER_OUT = re.compile(r"^送金（送）→(\d+)$")
_RE_TRANSFER_IN = re.compile(r"^送金（受）←(\d+)$")
_RE_LOAN_LEND = re.compile(r"^貸付→(\d+)（#(\d+)）$")
_RE_LOAN_BORROW = re.compile(r"^借入←(\d+)（#(\d+)）$")
_RE_LOAN_REPAY = re.compile(r"^貸付の返済→(\d+)（#(\d+)）$")
_RE_LOAN_COLLECT = re.compile(r"^貸付の回収←(\d+)（#(\d+)）$")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def _has_fk(conn: sqlite3.Connection, table: str, ref_table: str) -> bool:
    return any(str(r[2]) == ref_table for r in conn.execute(f"PRAGMA foreign_key_list({table})"))


def _replace_table(conn: sqlite3.Connection, table: str, new_ddl: str, copy_sql: str) -> None:
    """RENAME による既存子テーブルの FK 書き換えを防ぐため、新テーブル作成→移行→旧削除→リネームを行う。"""
    tmp = f"{table}_v7new"
    conn.execute(f"DROP TABLE IF EXISTS {tmp}")
    ddl_tmp = new_ddl.replace(f"CREATE TABLE IF NOT EXISTS {table} ", f"CREATE TABLE {tmp} ", 1)
    conn.execute(ddl_tmp)
    conn.execute(copy_sql.replace(f" {table} ", f" {tmp} ", 1))
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")


def _classify_ledger_row(reason: str) -> tuple[str, int | None, int | None]:
    """従来の reason 文字列から構造化カラム (kind, counterparty_id, ref_id) を導出する。"""
    m = _RE_TRANSFER_OUT.match(reason)
    if m:
        return ("transfer_out", int(m.group(1)), None)
    m = _RE_TRANSFER_IN.match(reason)
    if m:
        return ("transfer_in", int(m.group(1)), None)
    if reason == "送金手数料":
        return ("transfer_fee", None, None)
    if reason == "初期支給":
        return ("initial_grant", None, None)
    if reason == "共済:配当":
        return ("kyosai_dividend", None, None)
    if reason == "帝愛信託:利息":
        return ("trust_interest", None, None)
    if reason.startswith("共済:超過損失の負担"):
        return ("kyosai_excess_charge", None, None)
    m = _RE_LOAN_LEND.match(reason)
    if m:
        return ("loan_lend", int(m.group(1)), int(m.group(2)))
    m = _RE_LOAN_BORROW.match(reason)
    if m:
        return ("loan_borrow", int(m.group(1)), int(m.group(2)))
    m = _RE_LOAN_REPAY.match(reason)
    if m:
        return ("loan_repay", int(m.group(1)), int(m.group(2)))
    m = _RE_LOAN_COLLECT.match(reason)
    if m:
        return ("loan_collect", int(m.group(1)), int(m.group(2)))
    if reason.startswith("賭け:"):
        return ("game_bet", None, None)
    if reason.startswith("追加の賭け:"):
        return ("game_bet_add", None, None)
    if reason.startswith("払い戻し:"):
        return ("game_payout", None, None)
    if reason.startswith("保険券で半額返金:"):
        return ("game_insurance", None, None)
    if reason.startswith("追加の支払い:"):
        return ("game_penalty", None, None)
    if reason.startswith(("返金:", "返金（中断したゲーム）:")):
        return ("game_refund", None, None)
    return ("", None, None)


def _migrate_ledger(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "ledger"):
        conn.execute(LEDGER_DDL)
        conn.execute(LEDGER_INDEX_DDL)
        conn.execute(LEDGER_KIND_INDEX_DDL)
        return
    if "kind" in _table_cols(conn, "ledger") and _has_fk(conn, "ledger", "accounts"):
        return
    rows = [
        (int(r[0]), int(r[1]), int(r[2]), int(r[3]), int(r[4]), str(r[5]), float(r[6]), *_classify_ledger_row(str(r[5])))
        for r in conn.execute(
            "SELECT id, guild_id, user_id, delta, balance_after, reason, ts FROM ledger l "
            "WHERE EXISTS (SELECT 1 FROM accounts a WHERE a.guild_id=l.guild_id AND a.user_id=l.user_id)"
        ).fetchall()
    ]
    tmp = "ledger_v7new"
    conn.execute(f"DROP TABLE IF EXISTS {tmp}")
    conn.execute(LEDGER_DDL.replace("CREATE TABLE IF NOT EXISTS ledger ", f"CREATE TABLE {tmp} ", 1))
    conn.executemany(
        f"INSERT INTO {tmp} (id, guild_id, user_id, delta, balance_after, reason, ts, kind, counterparty_id, ref_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.execute("DROP TABLE ledger")
    conn.execute(f"ALTER TABLE {tmp} RENAME TO ledger")
    conn.execute(LEDGER_INDEX_DDL)
    conn.execute(LEDGER_KIND_INDEX_DDL)


def _migrate_companies(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "companies"):
        conn.execute(COMPANIES_DDL)
        return
    if _has_fk(conn, "companies", "accounts"):
        return
    _replace_table(
        conn,
        "companies",
        COMPANIES_DDL,
        "INSERT INTO companies (guild_id, user_id, name, kind, level, funds, stock, stock_ts, upkeep_day, unpaid_days, sold_total, created, dividend_rate) "
        "SELECT c.guild_id, c.user_id, c.name, c.kind, MAX(1, c.level), MAX(0, c.funds), MAX(0, c.stock), c.stock_ts, "
        "c.upkeep_day, MAX(0, c.unpaid_days), MAX(0, c.sold_total), c.created, MAX(0, c.dividend_rate) "
        "FROM companies c "
        "WHERE EXISTS (SELECT 1 FROM accounts a WHERE a.guild_id=c.guild_id AND a.user_id=c.user_id)",
    )


def _migrate_company_stakes(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "company_stakes"):
        conn.execute(COMPANY_STAKES_DDL)
        return
    if _has_fk(conn, "company_stakes", "companies") and _has_fk(conn, "company_stakes", "accounts"):
        return
    _replace_table(
        conn,
        "company_stakes",
        COMPANY_STAKES_DDL,
        "INSERT INTO company_stakes (guild_id, owner_id, investor_id, amount, dividends) "
        "SELECT s.guild_id, s.owner_id, s.investor_id, s.amount, MAX(0, s.dividends) "
        "FROM company_stakes s "
        "WHERE s.owner_id != s.investor_id AND s.amount > 0 "
        "AND EXISTS (SELECT 1 FROM companies c WHERE c.guild_id=s.guild_id AND c.user_id=s.owner_id) "
        "AND EXISTS (SELECT 1 FROM accounts a WHERE a.guild_id=s.guild_id AND a.user_id=s.investor_id)",
    )


def _migrate_teiai_house_grants(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "teiai_house"):
        conn.execute(TEIAI_HOUSE_DDL)
    if not _table_exists(conn, "teiai_house_grants"):
        conn.execute(TEIAI_HOUSE_GRANTS_DDL)
        return
    conn.execute("INSERT OR IGNORE INTO teiai_house (guild_id) SELECT DISTINCT guild_id FROM teiai_house_grants")
    if _has_fk(conn, "teiai_house_grants", "teiai_house"):
        return
    _replace_table(
        conn,
        "teiai_house_grants",
        TEIAI_HOUSE_GRANTS_DDL,
        "INSERT INTO teiai_house_grants (guild_id, day, used) "
        "SELECT g.guild_id, g.day, MAX(0, g.used) FROM teiai_house_grants g "
        "WHERE EXISTS (SELECT 1 FROM teiai_house h WHERE h.guild_id=g.guild_id)",
    )


def _migrate_teiai_deposits(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "teiai_deposits"):
        conn.execute(TEIAI_DEPOSITS_DDL)
        return
    if _has_fk(conn, "teiai_deposits", "accounts"):
        return
    _replace_table(
        conn,
        "teiai_deposits",
        TEIAI_DEPOSITS_DDL,
        "INSERT INTO teiai_deposits (guild_id, user_id, amount) "
        "SELECT d.guild_id, d.user_id, MAX(0, d.amount) FROM teiai_deposits d "
        "WHERE EXISTS (SELECT 1 FROM accounts a WHERE a.guild_id=d.guild_id AND a.user_id=d.user_id)",
    )


def _migrate_cds(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "cds"):
        conn.execute(CDS_DDL)
        conn.execute(CDS_ACTIVE_INDEX_DDL)
        return
    if not _has_fk(conn, "cds", "accounts"):
        _replace_table(
            conn,
            "cds",
            CDS_DDL,
            "INSERT INTO cds (id, guild_id, buyer_id, target_id, stake, odds, ts, expires, status, paid) "
            "SELECT c.id, c.guild_id, c.buyer_id, c.target_id, MAX(1, c.stake), c.odds, c.ts, c.expires, "
            "CASE WHEN c.status IN ('active', 'paid', 'expired', 'void') THEN c.status ELSE 'void' END, MAX(0, c.paid) "
            "FROM cds c "
            "WHERE EXISTS (SELECT 1 FROM accounts a WHERE a.guild_id=c.guild_id AND a.user_id=c.buyer_id) "
            "AND EXISTS (SELECT 1 FROM accounts a WHERE a.guild_id=c.guild_id AND a.user_id=c.target_id)",
        )
    conn.execute(CDS_ACTIVE_INDEX_DDL)


def _migrate_bonds(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "bonds"):
        conn.execute(BONDS_DDL)
        conn.execute(BONDS_ACTIVE_INDEX_DDL)
        return
    if not _has_fk(conn, "bonds", "accounts"):
        _replace_table(
            conn,
            "bonds",
            BONDS_DDL,
            "INSERT INTO bonds (id, guild_id, user_id, kind, principal, due, status, paid, ts, rate) "
            "SELECT b.id, b.guild_id, b.user_id, b.kind, MAX(1, b.principal), b.due, "
            "CASE WHEN b.status IN ('active', 'paid', 'defaulted', 'cancelled') THEN b.status ELSE 'cancelled' END, "
            "MAX(0, b.paid), b.ts, b.rate FROM bonds b "
            "WHERE EXISTS (SELECT 1 FROM accounts a WHERE a.guild_id=b.guild_id AND a.user_id=b.user_id)",
        )
    conn.execute(BONDS_ACTIVE_INDEX_DDL)


def _migrate_options(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "options"):
        conn.execute(OPTIONS_DDL)
        conn.execute(OPTIONS_ACTIVE_INDEX_DDL)
        return
    if not _has_fk(conn, "options", "accounts"):
        _replace_table(
            conn,
            "options",
            OPTIONS_DDL,
            "INSERT INTO options (id, guild_id, user_id, kind, symbol, strike, units, premium, expiry, spot, ts, status, paid, final, side) "
            "SELECT o.id, o.guild_id, o.user_id, o.kind, o.symbol, o.strike, MAX(1, o.units), MAX(0, o.premium), o.expiry, "
            "o.spot, o.ts, CASE WHEN o.status IN ('active', 'exercised', 'expired', 'sold') THEN o.status ELSE 'expired' END, "
            "MAX(0, o.paid), o.final, CASE WHEN o.side IN ('long', 'short') THEN o.side ELSE 'long' END "
            "FROM options o "
            "WHERE EXISTS (SELECT 1 FROM accounts a WHERE a.guild_id=o.guild_id AND a.user_id=o.user_id)",
        )
    conn.execute(OPTIONS_ACTIVE_INDEX_DDL)


def _migrate_kyosai_votes(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "kyosai_votes"):
        conn.execute(KYOSAI_VOTES_DDL)
        return
    if _has_fk(conn, "kyosai_votes", "kyosai_motions"):
        return
    _replace_table(
        conn,
        "kyosai_votes",
        KYOSAI_VOTES_DDL,
        "INSERT INTO kyosai_votes (motion_id, user_id, yes) "
        "SELECT v.motion_id, v.user_id, CASE WHEN v.yes != 0 THEN 1 ELSE 0 END FROM kyosai_votes v "
        "WHERE EXISTS (SELECT 1 FROM kyosai_motions m WHERE m.id=v.motion_id)",
    )


def _migrate_teiai_market(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "teiai_market_days"):
        conn.execute(TEIAI_MARKET_DAYS_DDL)
    if not _table_exists(conn, "teiai_market"):
        conn.execute(TEIAI_MARKET_DDL)
        conn.execute(TEIAI_MARKET_INDEX_DDL)
        return
    conn.execute("INSERT OR IGNORE INTO teiai_market_days (guild_id, day) SELECT DISTINCT guild_id, day FROM teiai_market")
    if not _has_fk(conn, "teiai_market", "teiai_market_days"):
        _replace_table(
            conn,
            "teiai_market",
            TEIAI_MARKET_DDL,
            "INSERT INTO teiai_market (id, guild_id, day, item_id, mode, qty, start_price, end_price, black, listed_at, expires_at) "
            "SELECT m.id, m.guild_id, m.day, m.item_id, m.mode, MAX(0, m.qty), MAX(1, m.start_price), MAX(1, m.end_price), "
            "CASE WHEN m.black != 0 THEN 1 ELSE 0 END, m.listed_at, m.expires_at FROM teiai_market m "
            "WHERE EXISTS (SELECT 1 FROM teiai_market_days d WHERE d.guild_id=m.guild_id AND d.day=m.day)",
        )
    conn.execute(TEIAI_MARKET_INDEX_DDL)


def _migrate_quiz_answers(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "quiz_answers"):
        conn.execute(QUIZ_ANSWERS_DDL)
        return
    if _has_fk(conn, "quiz_answers", "quiz_questions"):
        return
    _replace_table(
        conn,
        "quiz_answers",
        QUIZ_ANSWERS_DDL,
        "INSERT INTO quiz_answers (question_id, guild_id, user_id, correct, ts, counted) "
        "SELECT q.question_id, q.guild_id, q.user_id, CASE WHEN q.correct != 0 THEN 1 ELSE 0 END, q.ts, "
        "CASE WHEN q.counted != 0 THEN 1 ELSE 0 END FROM quiz_answers q "
        "WHERE EXISTS (SELECT 1 FROM quiz_questions qq WHERE qq.id=q.question_id)",
    )


def _migrate_raffle_entries(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "raffle_entries"):
        conn.execute(RAFFLE_ENTRIES_DDL)
        return
    if _has_fk(conn, "raffle_entries", "raffle"):
        return
    _replace_table(
        conn,
        "raffle_entries",
        RAFFLE_ENTRIES_DDL,
        "INSERT INTO raffle_entries (raffle_id, user_id) "
        "SELECT r.raffle_id, r.user_id FROM raffle_entries r "
        "WHERE EXISTS (SELECT 1 FROM raffle f WHERE f.id=r.raffle_id)",
    )


def _migrate_poll_votes(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "poll_votes"):
        conn.execute(POLL_VOTES_DDL)
        return
    if _has_fk(conn, "poll_votes", "polls"):
        return
    _replace_table(
        conn,
        "poll_votes",
        POLL_VOTES_DDL,
        "INSERT INTO poll_votes (poll_id, user_id, yes) "
        "SELECT p.poll_id, p.user_id, CASE WHEN p.yes != 0 THEN 1 ELSE 0 END FROM poll_votes p "
        "WHERE EXISTS (SELECT 1 FROM polls pp WHERE pp.id=p.poll_id)",
    )


def _migrate_stream_rounds(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "stream_rounds"):
        conn.execute(STREAM_ROUNDS_DDL)
        return
    if _has_fk(conn, "stream_rounds", "streams"):
        return
    _replace_table(
        conn,
        "stream_rounds",
        STREAM_ROUNDS_DDL,
        "INSERT INTO stream_rounds (id, stream_id, guild_id, label, status, created, settled) "
        "SELECT s.id, s.stream_id, s.guild_id, s.label, "
        "CASE WHEN s.status IN ('open', 'locked', 'win', 'lose', 'void') THEN s.status ELSE 'void' END, "
        "s.created, s.settled FROM stream_rounds s "
        "WHERE EXISTS (SELECT 1 FROM streams st WHERE st.id=s.stream_id)",
    )


def _migrate_stream_bets(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "stream_bets"):
        conn.execute(STREAM_BETS_DDL)
        return
    if _has_fk(conn, "stream_bets", "stream_rounds"):
        return
    _replace_table(
        conn,
        "stream_bets",
        STREAM_BETS_DDL,
        "INSERT INTO stream_bets (round_id, user_id, side, amount) "
        "SELECT b.round_id, b.user_id, CASE WHEN b.side IN ('win', 'lose') THEN b.side ELSE 'win' END, MAX(1, b.amount) "
        "FROM stream_bets b "
        "WHERE EXISTS (SELECT 1 FROM stream_rounds r WHERE r.id=b.round_id)",
    )


def _migrate_arena_fighters(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "arena_fighters"):
        conn.execute(ARENA_FIGHTERS_DDL)
        conn.execute(ARENA_FIGHTERS_INDEX_DDL)
        return
    if not _has_fk(conn, "arena_fighters", "arena_tournaments"):
        _replace_table(
            conn,
            "arena_fighters",
            ARENA_FIGHTERS_DDL,
            "INSERT INTO arena_fighters (id, tournament_id, name, base_power, group_no, stage, bet_total, result) "
            "SELECT f.id, f.tournament_id, f.name, f.base_power, f.group_no, "
            "CASE WHEN f.stage IN ('semi', 'final', 'out') THEN f.stage ELSE 'out' END, MAX(0, f.bet_total), "
            "CASE WHEN f.result IN ('won', 'lost') THEN f.result ELSE NULL END "
            "FROM arena_fighters f "
            "WHERE EXISTS (SELECT 1 FROM arena_tournaments t WHERE t.id=f.tournament_id)",
        )
    conn.execute(ARENA_FIGHTERS_INDEX_DDL)


def _migrate_earnings_memos(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "earnings_memos"):
        conn.execute(EARNINGS_MEMOS_DDL)
        return
    if _has_fk(conn, "earnings_memos", "earnings"):
        return
    _replace_table(
        conn,
        "earnings_memos",
        EARNINGS_MEMOS_DDL,
        "INSERT INTO earnings_memos (event_id, guild_id, user_id, hint) "
        "SELECT m.event_id, m.guild_id, m.user_id, CASE WHEN m.hint < 0 THEN -1 ELSE 1 END FROM earnings_memos m "
        "WHERE EXISTS (SELECT 1 FROM earnings e WHERE e.id=m.event_id)",
    )


def _migrate_earnings_shop(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "earnings_shop"):
        conn.execute(EARNINGS_SHOP_DDL)
        return
    if _has_fk(conn, "earnings_shop", "earnings"):
        return
    _replace_table(
        conn,
        "earnings_shop",
        EARNINGS_SHOP_DDL,
        "INSERT INTO earnings_shop (event_id, guild_id, user_id, hint, price, ts) "
        "SELECT s.event_id, s.guild_id, s.user_id, CASE WHEN s.hint < 0 THEN -1 ELSE 1 END, MAX(0, s.price), s.ts "
        "FROM earnings_shop s "
        "WHERE EXISTS (SELECT 1 FROM earnings e WHERE e.id=s.event_id)",
    )


def migrate_v6_to_v7(conn: sqlite3.Connection) -> None:
    """ledger を構造化し、全ドメイン表へ FK・CHECK・部分索引を付与する。"""
    _migrate_ledger(conn)
    _migrate_companies(conn)
    _migrate_company_stakes(conn)
    _migrate_teiai_house_grants(conn)
    _migrate_teiai_deposits(conn)
    _migrate_cds(conn)
    _migrate_bonds(conn)
    _migrate_options(conn)
    _migrate_kyosai_votes(conn)
    _migrate_teiai_market(conn)
    _migrate_quiz_answers(conn)
    _migrate_raffle_entries(conn)
    _migrate_poll_votes(conn)
    _migrate_stream_rounds(conn)
    _migrate_stream_bets(conn)
    _migrate_arena_fighters(conn)
    _migrate_earnings_memos(conn)
    _migrate_earnings_shop(conn)
    for stmt in V7_INTEGRITY_INDEX_STATEMENTS:
        conn.execute(stmt)
