"""旧版スキーマからの列追加・サーバー単位移行・廃止テーブル精算。"""

import json
import os
import sqlite3
import time
from typing import Optional

from domain import labor
from domain.types import INITIAL_BALANCE
from infra.db.schema.tables import INVENTORY_DDL, PROFILE_DDL, USERS_DDL, new_quiz_order


def add_missing_columns(conn: sqlite3.Connection) -> None:
    """作成済みの古い表に、あとから増えた列を足す。"""
    # 作成済みの古い表に、列を足す（初回の版には元のニックネームの記録がなかった）
    cp = {r["name"] for r in conn.execute("PRAGMA table_info(chart_posts)")}
    for col, ddl in (
        ("guild_id", "INTEGER NOT NULL DEFAULT 0"),
        ("period", "TEXT NOT NULL DEFAULT '24h'"),
        ("ts", "REAL NOT NULL DEFAULT 0"),
        ("live", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if col not in cp:
            conn.execute(f"ALTER TABLE chart_posts ADD COLUMN {col} {ddl}")
    me = {r["name"] for r in conn.execute("PRAGMA table_info(manual_event)")}
    if "channel_id" not in me:
        conn.execute("ALTER TABLE manual_event ADD COLUMN channel_id INTEGER NOT NULL DEFAULT 0")
    qq = {r["name"] for r in conn.execute("PRAGMA table_info(quiz_questions)")}
    if "shown_order" not in qq:
        conn.execute("ALTER TABLE quiz_questions ADD COLUMN shown_order TEXT NOT NULL DEFAULT ''")
    for col in ("author_id", "guild_id", "channel_id", "message_id", "settled", "earned"):
        if col not in qq:
            conn.execute(f"ALTER TABLE quiz_questions ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
    for col in ("creator_name", "msg_right", "msg_wrong"):
        if col not in qq:
            conn.execute(f"ALTER TABLE quiz_questions ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    for r in conn.execute(
        "SELECT id FROM quiz_questions WHERE shown_order=''"
    ).fetchall():  # 並びが未設定の古い問題に、1回だけ決めて固定する
        conn.execute("UPDATE quiz_questions SET shown_order=? WHERE id=?", (json.dumps(new_quiz_order()), r["id"]))
    hs = {r["name"] for r in conn.execute("PRAGMA table_info(horses)")}
    if "bonus" not in hs:
        conn.execute("ALTER TABLE horses ADD COLUMN bonus INTEGER NOT NULL DEFAULT 0")
    if "rented_day" not in hs:
        conn.execute("ALTER TABLE horses ADD COLUMN rented_day TEXT")
    if "tactic" not in hs:
        conn.execute("ALTER TABLE horses ADD COLUMN tactic TEXT NOT NULL DEFAULT 'pace'")
    hr = {r["name"] for r in conn.execute("PRAGMA table_info(horse_races)")}
    if "grade" not in hr:
        conn.execute("ALTER TABLE horse_races ADD COLUMN grade TEXT NOT NULL DEFAULT ''")
    he = {r["name"] for r in conn.execute("PRAGMA table_info(horse_entries)")}
    for col in ("races", "wins", "places"):
        if col not in he:
            conn.execute(f"ALTER TABLE horse_entries ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
    if "tactic" not in he:
        conn.execute("ALTER TABLE horse_entries ADD COLUMN tactic TEXT NOT NULL DEFAULT 'pace'")
    us = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    if "last_slot" not in us:
        conn.execute("ALTER TABLE users ADD COLUMN last_slot INTEGER")
    if "debt" not in us:
        conn.execute("ALTER TABLE users ADD COLUMN debt INTEGER NOT NULL DEFAULT 0")
    if "checked" not in {r["name"] for r in conn.execute("PRAGMA table_info(bonds)")}:
        conn.execute("ALTER TABLE bonds ADD COLUMN checked REAL")
    opt_cols = {r["name"] for r in conn.execute("PRAGMA table_info(options)")}
    for col, ddl in (
        ("symbol2", "TEXT"),
        ("spot2", "REAL"),
        ("side", "TEXT NOT NULL DEFAULT 'long'"),
        ("margin", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if opt_cols and col not in opt_cols:
            conn.execute(f"ALTER TABLE options ADD COLUMN {col} {ddl}")
    ls = {r["name"] for r in conn.execute("PRAGMA table_info(labor)")}
    for col, ddl in (
        ("apprentice", "INTEGER NOT NULL DEFAULT 0"),
        ("miner", "INTEGER NOT NULL DEFAULT 0"),
        ("craftsman", "INTEGER NOT NULL DEFAULT 0"),
        ("foreman", "INTEGER NOT NULL DEFAULT 0"),
        ("dorm", "INTEGER NOT NULL DEFAULT 0"),
        ("wage", "TEXT NOT NULL DEFAULT 'normal'"),
        ("idle_at", "REAL NOT NULL DEFAULT 0"),
        ("renew", "INTEGER NOT NULL DEFAULT 1"),
        ("ores", "TEXT NOT NULL DEFAULT ''"),
    ):
        if col not in ls:
            conn.execute(f"ALTER TABLE labor ADD COLUMN {col} {ddl}")
    _migrate_labor_counts(conn)
    if "pot_workers" not in {r["name"] for r in conn.execute("PRAGMA table_info(labor)")}:
        conn.execute("ALTER TABLE labor ADD COLUMN pot_workers INTEGER NOT NULL DEFAULT 0")
    if "donated" not in {r["name"] for r in conn.execute("PRAGMA table_info(labor)")}:
        conn.execute("ALTER TABLE labor ADD COLUMN donated INTEGER NOT NULL DEFAULT 0")
    if "day_click" not in {r["name"] for r in conn.execute("PRAGMA table_info(labor)")}:
        conn.execute("ALTER TABLE labor ADD COLUMN day_click INTEGER NOT NULL DEFAULT 0")
    if "relics" not in {r["name"] for r in conn.execute("PRAGMA table_info(labor)")}:
        conn.execute("ALTER TABLE labor ADD COLUMN relics TEXT NOT NULL DEFAULT ''")
    ml = {r["name"] for r in conn.execute("PRAGMA table_info(market_listings)")}
    for col in ("channel_id", "message_id", "post_done"):
        if col not in ml:
            conn.execute(f"ALTER TABLE market_listings ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
    _refund_archaeologists(conn)  # 専用の「発掘員」は、なくなった（ふつうの作業員が、発掘品を見つける）
    if "legacy" not in {r["name"] for r in conn.execute("PRAGMA table_info(labor_workers)")}:
        conn.execute("ALTER TABLE labor_workers ADD COLUMN legacy INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE labor_workers SET legacy=1")  # この列を足す前に雇われていた人は、すべて旧ルールの契約
        _refund_legacy_contracts(conn)
    for table in ("crypto_short", "crypto_long"):  # 信用金利の列（建玉の数え始めの時刻と、まだ引いていない端数）
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and "interest_at" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN interest_at REAL NOT NULL DEFAULT 0")
            conn.execute(f"ALTER TABLE {table} ADD COLUMN accrued REAL NOT NULL DEFAULT 0")
    if "debt_interest_at" not in us:
        conn.execute("ALTER TABLE users ADD COLUMN debt_interest_at REAL NOT NULL DEFAULT 0")
    if "grace_until" not in us:
        conn.execute("ALTER TABLE users ADD COLUMN grace_until REAL")
    ps = {r["name"] for r in conn.execute("PRAGMA table_info(play_state)")}
    for col, ddl in (
        ("day_streak", "INTEGER NOT NULL DEFAULT 0"),
        ("streak_day", "TEXT"),
        ("plays", "INTEGER NOT NULL DEFAULT 0"),
        ("wins", "INTEGER NOT NULL DEFAULT 0"),
        ("net_total", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if col not in ps:
            conn.execute(f"ALTER TABLE play_state ADD COLUMN {col} {ddl}")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nick_prefs)")}
    for col in ("original_nick", "applied_nick"):
        if col not in cols:
            conn.execute(f"ALTER TABLE nick_prefs ADD COLUMN {col} TEXT")
    if "enabled" not in cols:  # 既存の行は、称号の表示を自分で選んだ人
        conn.execute("ALTER TABLE nick_prefs ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
    answers = {r["name"] for r in conn.execute("PRAGMA table_info(quiz_answers)")}
    if "counted" not in answers:
        conn.execute("ALTER TABLE quiz_answers ADD COLUMN counted INTEGER NOT NULL DEFAULT 1")
    profile_cols = {r["name"] for r in conn.execute("PRAGMA table_info(profile)")}
    for col in ("skin_dice", "skin_table"):
        if col not in profile_cols:
            conn.execute(f"ALTER TABLE profile ADD COLUMN {col} TEXT")
    users_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    for col in ("bankrupt_at", "blacklist_until", "escape_at"):
        if col not in users_cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} REAL")
    if "escape_tries" not in users_cols:
        conn.execute("ALTER TABLE users ADD COLUMN escape_tries INTEGER NOT NULL DEFAULT 0")
    for table, col, ddl in (
        ("horse_bets", "picks", "TEXT NOT NULL DEFAULT ''"),
        ("horses", "listed_at", "REAL"),
    ):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    _add_symbol_columns(conn)


# 銘柄を足す前の表（主キーに symbol がない）。CSトークンだけだった頃の記録は、すべて symbol='cs' として移す
_SYMBOL_TABLES = {
    "market_prices": (
        "CREATE TABLE market_prices (symbol TEXT NOT NULL DEFAULT 'cs', ts REAL NOT NULL, price INTEGER NOT NULL, "
        "event INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (symbol, ts))",
        "ts, price, event",
    ),
    "crypto": (
        "CREATE TABLE crypto (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, symbol TEXT NOT NULL DEFAULT 'cs', "
        "units INTEGER NOT NULL, cost INTEGER NOT NULL, PRIMARY KEY (guild_id, user_id, symbol))",
        "guild_id, user_id, units, cost",
    ),
    "crypto_short": (
        "CREATE TABLE crypto_short (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, symbol TEXT NOT NULL DEFAULT 'cs', "
        "units INTEGER NOT NULL, entry_value REAL NOT NULL, margin INTEGER NOT NULL, PRIMARY KEY (guild_id, user_id, symbol))",
        "guild_id, user_id, units, entry_value, margin",
    ),
}


def _migrate_labor_counts(conn: sqlite3.Connection, now: Optional[float] = None) -> None:
    """作業員を「人数の列」で持っていた版から、契約つきの1人1行の表（labor_workers）へ移す。いまの人は、今から7日の契約にする（何度動かしても安全）。"""
    now = time.time() if now is None else now
    for r in conn.execute(
        "SELECT guild_id, user_id, apprentice, miner, craftsman, foreman FROM labor "
        "WHERE apprentice>0 OR miner>0 OR craftsman>0 OR foreman>0"
    ).fetchall():
        for kind in ("apprentice", "miner", "craftsman", "foreman"):
            for _ in range(r[kind]):
                conn.execute(
                    "INSERT INTO labor_workers (guild_id, user_id, kind, expires_at) VALUES (?, ?, ?, ?)",
                    (r["guild_id"], r["user_id"], kind, now + 7 * 86400.0),
                )
        conn.execute(
            "UPDATE labor SET apprentice=0, miner=0, craftsman=0, foreman=0 WHERE guild_id=? AND user_id=?",
            (r["guild_id"], r["user_id"]),
        )


ARCHAEOLOGIST_FEE = 2_400  # なくなった「発掘員」の、契約料（返金の額）


def _refund_archaeologists(conn: sqlite3.Connection, now: Optional[float] = None) -> None:
    """専用の「発掘員」を雇っていた人の契約料を返して、契約を解約する（何度動かしても安全。いなければ何もしない）。"""
    now = time.time() if now is None else now
    refunds: dict = {}
    for r in conn.execute("SELECT guild_id, user_id FROM labor_workers WHERE kind='archaeologist'").fetchall():
        key = (r["guild_id"], r["user_id"])
        refunds[key] = refunds.get(key, 0) + ARCHAEOLOGIST_FEE
    for (guild_id, user_id), amount in refunds.items():
        row = conn.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()
        if row is not None:
            after = row["balance"] + amount
            conn.execute("UPDATE users SET balance=? WHERE guild_id=? AND user_id=?", (after, guild_id, user_id))
            conn.execute(
                "INSERT INTO ledger (guild_id, user_id, delta, balance_after, reason, ts) VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, user_id, amount, after, "地下作業:契約料の返金（発掘員の廃止）", now),
            )
    conn.execute("DELETE FROM labor_workers WHERE kind='archaeologist'")


def _refund_legacy_contracts(conn: sqlite3.Connection, now: Optional[float] = None) -> None:
    """鉱石版より前に雇った作業員の契約料を、いまの値段で返して、契約を解約する（ルール変更の補償）。列を足したときに、1回だけ動く。"""
    now = time.time() if now is None else now
    refunds: dict = {}
    for r in conn.execute("SELECT id, guild_id, user_id, kind FROM labor_workers WHERE legacy=1").fetchall():
        key = (r["guild_id"], r["user_id"])
        refunds[key] = refunds.get(key, 0) + labor.CONTRACT_FEE.get(r["kind"], 0)
    for (guild_id, user_id), amount in refunds.items():
        row = conn.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()
        if row is not None and amount > 0:
            after = row["balance"] + amount
            conn.execute("UPDATE users SET balance=? WHERE guild_id=? AND user_id=?", (after, guild_id, user_id))
            conn.execute(
                "INSERT INTO ledger (guild_id, user_id, delta, balance_after, reason, ts) VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, user_id, amount, after, "地下作業:契約料の返金（ルール変更で解約）", now),
            )
    conn.execute("DELETE FROM labor_workers WHERE legacy=1")


def _add_symbol_columns(conn: sqlite3.Connection) -> None:
    """市場・保有・空売りの表に、銘柄の列を足す（主キーが変わるので、表を作り直して、記録を写す）。チャートの記録は、列を足すだけ。"""
    for table, (ddl, columns) in _SYMBOL_TABLES.items():
        if "symbol" in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
            continue
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        conn.execute(ddl)
        conn.execute(f"INSERT INTO {table} (symbol, {columns}) SELECT 'cs', {columns} FROM {table}_old")
        conn.execute(f"DROP TABLE {table}_old")
    if "symbol" not in {r["name"] for r in conn.execute("PRAGMA table_info(chart_posts)")}:
        conn.execute("ALTER TABLE chart_posts ADD COLUMN symbol TEXT NOT NULL DEFAULT 'cs'")


def migrate_to_guild_scope(conn: sqlite3.Connection, db_path: str, legacy_guild_id: Optional[int]) -> None:
    """サーバー別になる前の記録（users に guild_id が無い）を、指定したサーバーのものとして引き継ぐ。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    if not cols or "guild_id" in cols:
        return  # 新規、または移行済み
    if legacy_guild_id is None:
        raise RuntimeError(
            "古い形式の記録があります。引き継ぎ先のサーバーIDを、config.json の legacy_guild_id に設定してください。"
        )
    gid = int(legacy_guild_id)
    if gid <= 0:
        raise ValueError(f"legacy_guild_id が不正です: {legacy_guild_id!r}")
    backup = db_path + ".before-guild-scope"
    if not os.path.exists(backup):  # 万一に備えて、移行の前に記録を丸ごと退避する
        tmp = backup + ".tmp"  # 途中で落ちた不完全なファイルを、退避済みと誤認しないよう最後に改名する
        dst = sqlite3.connect(tmp)
        conn.backup(dst)
        dst.close()
        os.replace(tmp, backup)
    have = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE users RENAME TO users_old")
        conn.execute(USERS_DDL)
        conn.execute(
            "INSERT INTO users (guild_id, user_id, balance, daily_streak, last_daily, games_played, total_wagered, "
            "total_won, biggest_win, created_at) SELECT ?, user_id, balance, daily_streak, last_daily, games_played, "
            "total_wagered, total_won, biggest_win, created_at FROM users_old",
            (gid,),
        )
        conn.execute("DROP TABLE users_old")
        conn.execute("DROP INDEX IF EXISTS ledger_user")
        for t in ("ledger", "escrow"):  # 無い表は、あとの create_tables が新しい形で作る
            if t in have:
                conn.execute(f"ALTER TABLE {t} ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 0")
                conn.execute(f"UPDATE {t} SET guild_id=?", (gid,))
        if "inventory" in have:
            conn.execute("ALTER TABLE inventory RENAME TO inventory_old")
            conn.execute(INVENTORY_DDL)
            conn.execute(
                "INSERT INTO inventory (guild_id, user_id, item_id, qty) SELECT ?, user_id, item_id, qty FROM inventory_old",
                (gid,),
            )
            conn.execute("DROP TABLE inventory_old")
        if "profile" in have:
            conn.execute("ALTER TABLE profile RENAME TO profile_old")
            conn.execute(PROFILE_DDL)
            conn.execute(
                "INSERT INTO profile (guild_id, user_id, title, card_color, card_icon, daily_boost, insurance) "
                "SELECT ?, user_id, title, card_color, card_icon, daily_boost, insurance FROM profile_old",
                (gid,),
            )
            conn.execute("DROP TABLE profile_old")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def retire_autoplay(conn: sqlite3.Connection, now: Optional[float] = None) -> None:
    """廃止した自動プレイの表が残っていたら、預け中の分を残高に戻してから表を消す（預けた額を失わせない）。"""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='autoplay'").fetchone():
        return
    ts = time.time() if now is None else now
    for row in conn.execute("SELECT guild_id, user_id, pot FROM autoplay").fetchall():
        if row["pot"] > 0:
            gid, uid, pot = row["guild_id"], row["user_id"], row["pot"]
            cur = conn.execute(
                "INSERT OR IGNORE INTO users (guild_id, user_id, balance, created_at) VALUES (?, ?, ?, ?)",
                (gid, uid, INITIAL_BALANCE, ts),
            )
            if cur.rowcount:
                conn.execute(
                    "INSERT INTO ledger (guild_id, user_id, delta, balance_after, reason, ts) VALUES (?, ?, ?, ?, ?, ?)",
                    (gid, uid, INITIAL_BALANCE, INITIAL_BALANCE, "初期支給", ts),
                )
            before = conn.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (gid, uid)).fetchone()[
                "balance"
            ]
            after = before + pot
            conn.execute("UPDATE users SET balance=? WHERE guild_id=? AND user_id=?", (after, gid, uid))
            conn.execute(
                "INSERT INTO ledger (guild_id, user_id, delta, balance_after, reason, ts) VALUES (?, ?, ?, ?, ?, ?)",
                (gid, uid, pot, after, "自動プレイ精算", ts),
            )
    conn.execute("DROP TABLE autoplay")
