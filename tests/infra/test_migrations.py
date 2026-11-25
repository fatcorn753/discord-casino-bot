"""v0 旧スキーマからの無損失マイグレーション・冪等性・ロールバック・CLI 事前検証のテスト。"""

import hashlib
import os
import sqlite3

import pytest

import manage
from domain import shop
from domain.types import INITIAL_BALANCE, PART_KEYS, asset_groups
from infra.db import schema
from infra.db.repositories import open_repositories

pytestmark = pytest.mark.real_scope

G = 10
U1 = 1
U2 = 2
NOW = 1_700_000_000.0
EXPIRY_FUTURE = NOW + 7 * 86400.0
DAY = "2026-11-10"

# v1 以前（PR 直前）の主要テーブル＋死に列・死にテーブル・autoplay を含む v0 スナップショット DDL
V0_SNAPSHOT_SQL = """
CREATE TABLE users (
    guild_id         INTEGER NOT NULL,
    user_id          INTEGER NOT NULL,
    balance          INTEGER NOT NULL,
    daily_streak     INTEGER NOT NULL DEFAULT 0,
    last_daily       TEXT,
    games_played     INTEGER NOT NULL DEFAULT 0,
    total_wagered    INTEGER NOT NULL DEFAULT 0,
    total_won        INTEGER NOT NULL DEFAULT 0,
    biggest_win      INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL,
    last_slot        INTEGER,
    debt             INTEGER NOT NULL DEFAULT 0,
    debt_principal   INTEGER NOT NULL DEFAULT 0,
    grace_penalty    REAL NOT NULL DEFAULT 0,
    grace_penalty_at REAL NOT NULL DEFAULT 0,
    bankrupt_at      REAL,
    blacklist_until  REAL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE inventory (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    item_id  TEXT NOT NULL,
    qty      INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id, item_id)
);
CREATE TABLE profile (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    title       TEXT,
    card_color  TEXT,
    card_icon   TEXT,
    skin_dice   TEXT,
    skin_table  TEXT,
    daily_boost INTEGER NOT NULL DEFAULT 0,
    insurance   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL DEFAULT 0,
    user_id       INTEGER NOT NULL,
    delta         INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    reason        TEXT NOT NULL,
    ts            REAL NOT NULL
);
CREATE TABLE escrow (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL DEFAULT 0,
    user_id    INTEGER NOT NULL,
    amount     INTEGER NOT NULL,
    game       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE jackpot (
    guild_id    INTEGER PRIMARY KEY,
    pool        INTEGER NOT NULL DEFAULT 0,
    last_user   INTEGER,
    last_amount INTEGER NOT NULL DEFAULT 0,
    last_at     REAL NOT NULL DEFAULT 0
);
CREATE TABLE horses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    owner_id   INTEGER NOT NULL,
    name       TEXT NOT NULL,
    coat       TEXT NOT NULL,
    silks      TEXT NOT NULL,
    speed      INTEGER NOT NULL,
    stamina    INTEGER NOT NULL,
    power      INTEGER NOT NULL,
    guts       INTEGER NOT NULL,
    races      INTEGER NOT NULL DEFAULT 0,
    wins       INTEGER NOT NULL DEFAULT 0,
    places     INTEGER NOT NULL DEFAULT 0,
    earnings   INTEGER NOT NULL DEFAULT 0,
    bonus      INTEGER NOT NULL DEFAULT 0,
    created    REAL NOT NULL,
    listed_at  REAL,
    rented_day TEXT,
    style      TEXT
);
CREATE TABLE horse_races (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    grade       TEXT NOT NULL DEFAULT '',
    start_at    REAL NOT NULL,
    entry_close REAL NOT NULL,
    bet_close   REAL NOT NULL,
    status      TEXT NOT NULL,
    distance    INTEGER NOT NULL DEFAULT 0,
    going       TEXT NOT NULL DEFAULT '',
    channel_id  INTEGER NOT NULL DEFAULT 0,
    message_id  INTEGER NOT NULL DEFAULT 0,
    created     REAL NOT NULL
);
CREATE TABLE horse_entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id    INTEGER NOT NULL,
    lane       INTEGER NOT NULL DEFAULT -1,
    horse_id   INTEGER NOT NULL DEFAULT 0,
    owner_id   INTEGER NOT NULL DEFAULT 0,
    name       TEXT NOT NULL,
    coat       TEXT NOT NULL DEFAULT '',
    silks      TEXT NOT NULL DEFAULT '',
    speed      INTEGER NOT NULL DEFAULT 50,
    stamina    INTEGER NOT NULL DEFAULT 50,
    power      INTEGER NOT NULL DEFAULT 50,
    guts       INTEGER NOT NULL DEFAULT 50,
    tactic     TEXT NOT NULL DEFAULT 'pace',
    style      TEXT,
    form       INTEGER NOT NULL DEFAULT 0,
    races      INTEGER NOT NULL DEFAULT 0,
    wins       INTEGER NOT NULL DEFAULT 0,
    places     INTEGER NOT NULL DEFAULT 0,
    odds_win   REAL NOT NULL DEFAULT 0,
    odds_place REAL NOT NULL DEFAULT 0,
    finish     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE horse_bets (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    race_id  INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    kind     TEXT NOT NULL,
    lane     INTEGER NOT NULL,
    picks    TEXT NOT NULL DEFAULT '',
    stake    INTEGER NOT NULL,
    odds     REAL NOT NULL,
    payout   INTEGER NOT NULL DEFAULT 0,
    settled  INTEGER NOT NULL DEFAULT 0,
    created  REAL NOT NULL
);
CREATE TABLE horse_odds (
    race_id INTEGER NOT NULL,
    kind    TEXT NOT NULL,
    key     TEXT NOT NULL,
    odds    REAL NOT NULL,
    PRIMARY KEY (race_id, kind, key)
);
CREATE TABLE labor (
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    pick         INTEGER NOT NULL DEFAULT 0,
    glove        INTEGER NOT NULL DEFAULT 0,
    helmet       INTEGER NOT NULL DEFAULT 0,
    fatigue      REAL NOT NULL DEFAULT 0,
    fatigue_at   REAL NOT NULL DEFAULT 0,
    pot          INTEGER NOT NULL DEFAULT 0,
    pot_workers  INTEGER NOT NULL DEFAULT 0,
    donated      INTEGER NOT NULL DEFAULT 0,
    relics       TEXT NOT NULL DEFAULT '',
    day          TEXT NOT NULL DEFAULT '',
    day_earned   INTEGER NOT NULL DEFAULT 0,
    day_click    INTEGER NOT NULL DEFAULT 0,
    last_click   REAL NOT NULL DEFAULT 0,
    rest_until   REAL NOT NULL DEFAULT 0,
    clicks       INTEGER NOT NULL DEFAULT 0,
    earned_total INTEGER NOT NULL DEFAULT 0,
    apprentice   INTEGER NOT NULL DEFAULT 0,
    miner        INTEGER NOT NULL DEFAULT 0,
    craftsman    INTEGER NOT NULL DEFAULT 0,
    foreman      INTEGER NOT NULL DEFAULT 0,
    dorm         INTEGER NOT NULL DEFAULT 0,
    idle_at      REAL NOT NULL DEFAULT 0,
    renew        INTEGER NOT NULL DEFAULT 1,
    ores         TEXT NOT NULL DEFAULT '',
    wage         TEXT NOT NULL DEFAULT 'normal',
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE labor_workers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    expires_at REAL NOT NULL,
    legacy     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE seized_items (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id  INTEGER NOT NULL,
    item_id   TEXT NOT NULL,
    qty       INTEGER NOT NULL,
    listed_at REAL NOT NULL
);
CREATE TABLE guild_users (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE autoplay (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    pot      INTEGER NOT NULL
);
CREATE TABLE teiai_bank (
    guild_id        INTEGER PRIMARY KEY,
    cash            REAL NOT NULL DEFAULT 0,
    loaned          REAL NOT NULL DEFAULT 0,
    ts              REAL NOT NULL DEFAULT 0,
    thermo_load     REAL NOT NULL DEFAULT 0,
    thermo_capacity REAL NOT NULL DEFAULT 0,
    thermo_ts       REAL NOT NULL DEFAULT 0,
    bailout_debt    REAL NOT NULL DEFAULT 0,
    thermo_burned   REAL NOT NULL DEFAULT 0,
    thermo_bailed   REAL NOT NULL DEFAULT 0,
    trust_ts        REAL NOT NULL DEFAULT 0,
    trust_accrued   REAL NOT NULL DEFAULT 0,
    trust_paid      REAL NOT NULL DEFAULT 0,
    excess_unpaid   REAL NOT NULL DEFAULT 0,
    rf              REAL NOT NULL DEFAULT 0,
    rf_ts           REAL NOT NULL DEFAULT 0,
    kyosai_div_ts   REAL NOT NULL DEFAULT 0,
    kyosai_paid     REAL NOT NULL DEFAULT 0,
    kyosai_charged  REAL NOT NULL DEFAULT 0,
    kyosai_house_ts REAL NOT NULL DEFAULT 0,
    trust_premium   REAL
);
CREATE TABLE teiai_house (
    guild_id   INTEGER PRIMARY KEY,
    balance    INTEGER NOT NULL DEFAULT 0,
    skimmed    INTEGER NOT NULL DEFAULT 0,
    grant_day  TEXT NOT NULL DEFAULT '',
    grant_used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE teiai_deposits (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    amount   INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE kyosai_units (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    units    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE companies (
    guild_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    level         INTEGER NOT NULL DEFAULT 1,
    funds         INTEGER NOT NULL DEFAULT 0,
    stock         REAL NOT NULL DEFAULT 0,
    stock_ts      REAL NOT NULL,
    upkeep_day    TEXT NOT NULL,
    unpaid_days   INTEGER NOT NULL DEFAULT 0,
    sold_total    INTEGER NOT NULL DEFAULT 0,
    created       REAL NOT NULL,
    dividend_rate REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE company_stakes (
    guild_id    INTEGER NOT NULL,
    owner_id    INTEGER NOT NULL,
    investor_id INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    dividends   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, owner_id, investor_id)
);
CREATE TABLE market_prices (
    symbol TEXT NOT NULL DEFAULT 'cs',
    ts     REAL NOT NULL,
    price  INTEGER NOT NULL,
    event  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, ts)
);
CREATE TABLE crypto (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    symbol   TEXT NOT NULL DEFAULT 'cs',
    units    INTEGER NOT NULL,
    cost     INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id, symbol)
);
CREATE TABLE crypto_short (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    symbol      TEXT NOT NULL DEFAULT 'cs',
    units       INTEGER NOT NULL,
    entry_value REAL NOT NULL,
    margin      INTEGER NOT NULL,
    interest_at REAL NOT NULL DEFAULT 0,
    accrued     REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, symbol)
);
CREATE TABLE crypto_long (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    symbol      TEXT NOT NULL,
    units       INTEGER NOT NULL,
    entry_value REAL NOT NULL,
    margin      INTEGER NOT NULL,
    interest_at REAL NOT NULL DEFAULT 0,
    accrued     REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, symbol)
);
CREATE TABLE bonds (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    principal INTEGER NOT NULL,
    due       REAL NOT NULL,
    status    TEXT NOT NULL DEFAULT 'active',
    paid      INTEGER NOT NULL DEFAULT 0,
    ts        REAL NOT NULL,
    checked   REAL,
    rate      REAL
);
CREATE TABLE options (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    kind     TEXT NOT NULL,
    symbol   TEXT NOT NULL,
    strike   REAL NOT NULL,
    units    INTEGER NOT NULL,
    premium  INTEGER NOT NULL,
    expiry   REAL NOT NULL,
    spot     REAL NOT NULL,
    ts       REAL NOT NULL,
    status   TEXT NOT NULL DEFAULT 'active',
    paid     INTEGER NOT NULL DEFAULT 0,
    final    REAL,
    symbol2  TEXT,
    spot2    REAL,
    side     TEXT NOT NULL DEFAULT 'long',
    margin   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE loans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    lender_id     INTEGER NOT NULL,
    borrower_id   INTEGER NOT NULL,
    principal     INTEGER NOT NULL,
    rate_tenths   INTEGER NOT NULL,
    term_days     INTEGER NOT NULL,
    status        TEXT NOT NULL,
    created       REAL NOT NULL,
    offer_expires REAL NOT NULL,
    accepted_at   REAL,
    due           REAL,
    paid          INTEGER NOT NULL DEFAULT 0,
    reminded      INTEGER NOT NULL DEFAULT 0,
    channel_id    INTEGER NOT NULL DEFAULT 0,
    message_id    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE market_listings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    seller_id  INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    ref        TEXT NOT NULL,
    qty        INTEGER NOT NULL DEFAULT 1,
    mode       TEXT NOT NULL,
    price      INTEGER NOT NULL,
    bid        INTEGER NOT NULL DEFAULT 0,
    bidder_id  INTEGER NOT NULL DEFAULT 0,
    created    REAL NOT NULL,
    end_at     REAL NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    buyer_id   INTEGER NOT NULL DEFAULT 0,
    sold_price INTEGER NOT NULL DEFAULT 0,
    closed_at  REAL,
    channel_id INTEGER NOT NULL DEFAULT 0,
    message_id INTEGER NOT NULL DEFAULT 0,
    post_done  INTEGER NOT NULL DEFAULT 0
);
"""


def build_legacy_v0_db(path: str) -> None:
    """全ドメインに実データ・死に列・死にテーブル・掃除対象行が入った v0 DB を構築する。"""
    c = sqlite3.connect(path)
    c.executescript(V0_SNAPSHOT_SQL)
    c.execute(
        "INSERT INTO users (guild_id, user_id, balance, debt, debt_principal, created_at) "
        "VALUES (?, ?, 50000, 2000, 1500, ?)",
        (G, U1, NOW),
    )
    c.execute(
        "INSERT INTO users (guild_id, user_id, balance, bankrupt_at, blacklist_until, created_at) "
        "VALUES (?, ?, 12000, ?, ?, ?)",
        (G, U2, NOW - 100, EXPIRY_FUTURE, NOW),
    )
    c.execute(
        "INSERT INTO ledger (guild_id, user_id, delta, balance_after, reason, ts) VALUES (?, ?, 50000, 50000, '初期', ?)",
        (G, U1, NOW),
    )
    c.execute("INSERT INTO escrow (guild_id, user_id, amount, game, created_at) VALUES (?, ?, 300, 'slot', ?)", (G, U1, NOW))
    c.execute("INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, 3)", (G, U1, shop.INSURANCE))
    c.execute("INSERT INTO profile (guild_id, user_id, title) VALUES (?, ?, 'title_rookie')", (G, U1))
    c.execute("INSERT INTO jackpot (guild_id, pool) VALUES (?, 8000)", (G,))
    c.execute(
        "INSERT INTO teiai_bank (guild_id, cash, loaned, ts, thermo_ts, trust_ts, rf_ts, kyosai_div_ts, bailout_debt, kyosai_house_ts) "
        "VALUES (?, 150000.0, 5000.0, ?, ?, ?, ?, ?, 99.0, 123.0)",
        (G, NOW, NOW, NOW, NOW, NOW),
    )
    c.execute("INSERT INTO teiai_house (guild_id, balance, skimmed) VALUES (?, 25000, 777)", (G,))
    c.execute("INSERT INTO teiai_deposits (guild_id, user_id, amount) VALUES (?, ?, 10000)", (G, U1))
    c.execute("INSERT INTO kyosai_units (guild_id, user_id, units) VALUES (?, 0, 9000), (?, ?, 1000)", (G, G, U1))
    c.execute(
        "INSERT INTO horses (guild_id, owner_id, name, coat, silks, speed, stamina, power, guts, created, rented_day) "
        "VALUES (?, ?, 'ハヤブサ', '鹿毛', '赤', 60, 60, 60, 60, ?, NULL)",
        (G, U1, NOW),
    )
    c.execute(
        "INSERT INTO horse_races (id, guild_id, kind, grade, start_at, entry_close, bet_close, status, created) "
        "VALUES (1, ?, 'regular', 'G1', ?, ?, ?, 'done', ?), (2, ?, 'regular', '', ?, ?, ?, 'betting', ?)",
        (G, NOW - 10, NOW - 20, NOW - 15, NOW - 30, G, EXPIRY_FUTURE, NOW + 3600, NOW + 7200, NOW),
    )
    c.execute("INSERT INTO horse_entries (race_id, lane, horse_id, owner_id, name, tactic) VALUES (1, 1, 1, ?, 'ハヤブサ', 'pace')", (U1,))
    c.execute("INSERT INTO horse_entries (race_id, lane, horse_id, owner_id, name, tactic) VALUES (2, 1, 1, ?, 'ハヤブサ', 'pace')", (U1,))
    c.execute(
        "INSERT INTO horse_bets (guild_id, race_id, user_id, kind, lane, stake, odds, settled, created) "
        "VALUES (?, 2, ?, 'win', 1, 400, 2.5, 0, ?)",
        (G, U1, NOW),
    )
    c.execute("INSERT INTO horse_odds (race_id, kind, key, odds) VALUES (1, 'quinella', '1-2', 5.0), (2, 'quinella', '1-2', 4.5)")
    c.execute(
        "INSERT INTO labor (guild_id, user_id, pick, helmet, pot, pot_workers, relics, day, last_click, rest_until, "
        "apprentice, miner, dorm, idle_at, ores, wage) VALUES (?, ?, 1, 2, 650, 100, '{\"relic_fossil\": 1}', ?, ?, 99.0, 2, 1, 1, ?, '{\"copper\": 2}', 'high')",
        (G, U1, DAY, NOW, NOW),
    )
    c.execute(
        "INSERT INTO labor_workers (guild_id, user_id, kind, expires_at, legacy) VALUES (?, ?, 'craftsman', ?, 0)",
        (G, U1, EXPIRY_FUTURE),
    )
    c.execute(
        "INSERT INTO companies (guild_id, user_id, name, kind, funds, stock, stock_ts, upkeep_day, created) "
        "VALUES (?, ?, '帝愛商事', 'stand', 40000, 0.0, ?, ?, ?)",
        (G, U1, NOW, DAY, NOW),
    )
    c.execute("INSERT INTO company_stakes (guild_id, owner_id, investor_id, amount) VALUES (?, ?, ?, 12000)", (G, U1, U2))
    c.execute("INSERT INTO market_prices (symbol, ts, price, event) VALUES ('cs', ?, 10, 0)", (NOW,))
    c.execute("INSERT INTO crypto (guild_id, user_id, symbol, units, cost) VALUES (?, ?, 'cs', 4000, 4000)", (G, U1))
    c.execute("INSERT INTO crypto_short (guild_id, user_id, symbol, units, entry_value, margin) VALUES (?, ?, 'cs', 1000, 10.0, 1500)", (G, U1))
    c.execute("INSERT INTO crypto_long (guild_id, user_id, symbol, units, entry_value, margin) VALUES (?, ?, 'cs', 1000, 10.0, 2000)", (G, U1))
    c.execute("INSERT INTO bonds (guild_id, user_id, kind, principal, due, ts) VALUES (?, ?, 'normal', 3000, ?, ?)", (G, U1, EXPIRY_FUTURE, NOW))
    c.execute(
        "INSERT INTO options (guild_id, user_id, kind, symbol, strike, units, premium, expiry, spot, ts, side, margin) "
        "VALUES (?, ?, 'call', 'cs', 10.0, 1000, 100, ?, 10.0, ?, 'short', 1200)",
        (G, U1, EXPIRY_FUTURE, NOW),
    )
    c.execute(
        "INSERT INTO loans (guild_id, lender_id, borrower_id, principal, rate_tenths, term_days, status, created, offer_expires, accepted_at, due) "
        "VALUES (?, ?, ?, 5000, 10, 7, 'active', ?, ?, ?, ?)",
        (G, U1, U2, NOW, EXPIRY_FUTURE, NOW, EXPIRY_FUTURE),
    )
    c.execute("INSERT INTO seized_items (guild_id, item_id, qty, listed_at) VALUES (?, ?, 0, ?), (?, ?, 4, ?)", (G, shop.INSURANCE, NOW, G, shop.INSURANCE, NOW))
    c.execute("INSERT INTO guild_users (guild_id, user_id) VALUES (?, ?), (?, ?)", (G, U1, G, U2))
    c.execute("INSERT INTO autoplay (guild_id, user_id, pot) VALUES (?, ?, 750)", (G, U1))
    c.execute(
        "INSERT INTO market_listings (guild_id, seller_id, kind, ref, qty, mode, price, bid, bidder_id, created, end_at, status) "
        "VALUES (?, ?, 'item', ?, 1, 'auction', 500, 600, ?, ?, ?, 'active')",
        (G, U1, shop.INSURANCE, U2, NOW, EXPIRY_FUTURE),
    )
    c.execute("PRAGMA user_version = 0")
    c.commit()
    c.close()


def test_fresh_db_initializes_directly_to_v1_without_backup(tmp_path):
    path = str(tmp_path / "fresh.db")
    eco = open_repositories(path, strict=True)
    try:
        assert eco.db.conn.execute("PRAGMA user_version").fetchone()[0] == schema.CURRENT_SCHEMA_VERSION
        assert eco.db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = {r[0] for r in eco.db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "guild_users" not in tables
        assert not os.path.exists(f"{path}.before-v1")
        assert eco.for_guild(G).wallet.balance(U1) == INITIAL_BALANCE
    finally:
        eco.close()


def test_v0_to_v1_lossless_migration_drops_dead_schema_and_preserves_assets(tmp_path):
    path = str(tmp_path / "legacy.db")
    build_legacy_v0_db(path)

    eco = open_repositories(path, strict=True)
    try:
        conn = eco.db.conn
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert os.path.exists(f"{path}.before-v1")

        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "guild_users" not in tables and "autoplay" not in tables

        removed = {
            "teiai_bank": {"bailout_debt", "kyosai_house_ts"},
            "horses": {"rented_day"},
            "horse_races": {"grade"},
            "horse_entries": {"tactic"},
            "labor": {"helmet", "rest_until", "apprentice", "miner", "craftsman", "foreman", "wage"},
            "labor_workers": {"legacy"},
        }
        for table, dead_cols in removed.items():
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert not (dead_cols & cols), f"{table} に廃止列が残っている: {dead_cols & cols}"

        # 不要行の掃除: qty<=0 の seized_items と、完了レースの horse_odds だけが消える
        seized = conn.execute("SELECT qty FROM seized_items ORDER BY id").fetchall()
        assert [r[0] for r in seized] == [4]
        odds = conn.execute("SELECT race_id FROM horse_odds").fetchall()
        assert [r[0] for r in odds] == [2]

        # autoplay (750) は残高に返金され、作業員の旧人数列は労働契約へ引き継がれる
        g = eco.for_guild(G)
        assert g.wallet.balance(U1) == 50_750
        assert g.debt.debt(U1) == 2_000
        assert g.bank.bank_status(NOW)["cash"] == pytest.approx(150_000.0)
        assert g.kyosai.units_of_for(G, U1) == 1_000 and g.kyosai.units_of_for(G, 0) == 9_000

        state = g.labor.labor_state(U1, NOW, DAY)
        assert (state["apprentice"], state["miner"], state["craftsman"]) == (2, 1, 1)
        assert state["pot"] == 650 and state["ores"] == {"copper": 2} and state["relics"] == {"relic_fossil": 1}

        # B/S（asset_parts / asset_groups）および escrow テーブルの整合性確認
        assert conn.execute("SELECT COALESCE(SUM(amount), 0) FROM escrow").fetchone()[0] == 300
        all_parts = g.assets.asset_parts(NOW)
        parts = all_parts[U1]
        assert set(parts) == set(PART_KEYS)
        assert parts["cash"] == 50_750
        assert parts["deposit"] == 10_000
        assert parts["pot"] == 650
        assert parts["crypto"] == 3_540
        assert parts["bonds"] == 3_000
        assert parts["options"] == 1_200
        assert parts["items"] == 8_000
        assert parts["relics"] == 1_500
        assert parts["horses"] == 30_000
        assert parts["tools"] == 2_150
        assert parts["company"] > 40_000
        assert parts["loans_lent"] == 5_050
        assert parts["debt"] == -2_000
        assert all_parts[U2]["escrow"] == 600
        assert all_parts[U2]["loans_owed"] == -5_050
        assert asset_groups(parts)["liabilities"] == 2_000
    finally:
        eco.close()


def test_migration_is_idempotent_on_reopen(tmp_path):
    path = str(tmp_path / "legacy.db")
    build_legacy_v0_db(path)

    eco1 = open_repositories(path, strict=True)
    snap1 = schema.capture_invariants(eco1.db.conn)
    eco1.close()

    eco2 = open_repositories(path, strict=True)
    try:
        snap2 = schema.capture_invariants(eco2.db.conn)
        assert schema.diff_invariants(snap1, snap2) == []
        report = schema.initialize_db(eco2.db.conn, path)
        assert report.applied == () and report.end_version == schema.CURRENT_SCHEMA_VERSION
    finally:
        eco2.close()


def test_future_schema_version_is_rejected_without_touching_db(tmp_path):
    path = str(tmp_path / "future.db")
    eco = open_repositories(path, strict=True)
    eco.for_guild(G).wallet.credit(U1, 123, "保存")
    eco.db.conn.execute("PRAGMA user_version = 999")
    eco.db.conn.commit()
    eco.close()

    with pytest.raises(RuntimeError, match="999"):
        open_repositories(path, strict=True)
    assert not os.path.exists(f"{path}.before-v999")
    check = sqlite3.connect(path)
    try:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 999
        assert check.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (G, U1)).fetchone()[0] == INITIAL_BALANCE + 123
    finally:
        check.close()


def test_failing_migration_step_rolls_back_atomically(tmp_path):
    path = str(tmp_path / "rollback.db")
    eco = open_repositories(path, strict=True)
    eco.for_guild(G).wallet.credit(U1, 500, "事前残高")
    eco.close()

    def broken_step(conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE users SET balance = 0")
        raise RuntimeError("boom during migration")

    steps = (*schema.MIGRATIONS, schema.MigrationStep(2, "broken_v2", broken_step))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(RuntimeError, match="boom"):
            schema.initialize_db(conn, path, steps=steps, target_version=2)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            conn.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (G, U1)).fetchone()[0]
            == INITIAL_BALANCE + 500
        )
    finally:
        conn.close()


def test_runtime_auction_and_race_purge_zero_qty_and_done_odds(tmp_path):
    path = str(tmp_path / "runtime.db")
    eco = open_repositories(path, strict=True)
    try:
        g = eco.for_guild(G)
        g.wallet.credit(U1, 100_000, "資金")
        with eco.db.tx():
            g.auction.auction_list_locked_for(G, shop.INSURANCE, 2, NOW)
        res = g.auction.auction_buy(U1, shop.INSURANCE, qty=2, now=NOW)
        assert res.ok
        assert eco.db.conn.execute("SELECT COUNT(*) FROM seized_items WHERE guild_id=?", (G,)).fetchone()[0] == 0

        rid = g.races.race_create_special(NOW, entry_min=5, bet_min=5)
        closed = g.races.race_close_entries(rid)
        assert closed is not None
        g.races.race_set_odds(
            rid,
            {
                "win": {str(e["lane"]): 2.0 for e in g.races.race_entries(rid)},
                "place": {str(e["lane"]): 1.5 for e in g.races.race_entries(rid)},
                "quinella": {"1-2": 5.0},
            },
        )
        assert eco.db.conn.execute("SELECT COUNT(*) FROM horse_odds WHERE race_id=?", (rid,)).fetchone()[0] == 1
        assert g.races.race_run(rid, tz_hours=9.0) is not None
        assert eco.db.conn.execute("SELECT COUNT(*) FROM horse_odds WHERE race_id=?", (rid,)).fetchone()[0] == 0
    finally:
        eco.close()


def test_manage_migrate_check_never_modifies_source_db(tmp_path, capsys):
    path = str(tmp_path / "prod_v0.db")
    build_legacy_v0_db(path)
    before_digest = hashlib.sha256(open(path, "rb").read()).hexdigest()

    rc_check = manage.main(["migrate", "--db", path, "--check"])
    assert rc_check == 0
    out_check = capsys.readouterr().out
    assert "integrity=ok" in out_check and "idempotent=ok" in out_check
    assert hashlib.sha256(open(path, "rb").read()).hexdigest() == before_digest
    assert not os.path.exists(f"{path}.before-v1")

    rc_apply = manage.main(["migrate", "--db", path])
    assert rc_apply == 0
    out_apply = capsys.readouterr().out
    assert "integrity=ok" in out_apply
    assert os.path.exists(f"{path}.before-v1")
    check = sqlite3.connect(path)
    try:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        check.close()
