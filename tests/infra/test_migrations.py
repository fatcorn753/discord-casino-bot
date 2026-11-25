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
    c.execute(
        "INSERT INTO profile (guild_id, user_id, title, skin_dice, daily_boost, insurance) "
        "VALUES (?, ?, 'title_rookie', 'skin_kuro', 2, 1)",
        (G, U1),
    )
    c.execute("INSERT INTO jackpot (guild_id, pool) VALUES (?, 8000)", (G,))
    c.execute(
        "INSERT INTO teiai_bank (guild_id, cash, loaned, ts, thermo_load, thermo_capacity, thermo_ts, "
        "thermo_burned, thermo_bailed, trust_ts, trust_accrued, trust_paid, excess_unpaid, rf, rf_ts, "
        "kyosai_div_ts, kyosai_paid, kyosai_charged, bailout_debt, kyosai_house_ts, trust_premium) "
        "VALUES (?, 150000.0, 5000.0, ?, 12.5, 50000.0, ?, 321.0, 45.0, ?, 1.5, 678.0, 89.0, 0.02, ?, ?, "
        "1234.0, 567.0, 99.0, 123.0, 0.015)",
        (G, NOW, NOW, NOW, NOW, NOW),
    )
    c.execute(
        "INSERT INTO teiai_house (guild_id, balance, skimmed, grant_day, grant_used) VALUES (?, 25000, 777, ?, 4000)",
        (G, DAY),
    )
    c.execute("INSERT INTO teiai_deposits (guild_id, user_id, amount) VALUES (?, ?, 10000)", (G, U1))
    c.execute("INSERT INTO kyosai_units (guild_id, user_id, units) VALUES (?, 0, 9000), (?, ?, 1000)", (G, G, U1))
    c.execute(
        "INSERT INTO horses (id, guild_id, owner_id, name, coat, silks, speed, stamina, power, guts, created, listed_at, rented_day) "
        "VALUES (1, ?, ?, 'ハヤブサ', '鹿毛', '赤', 60, 60, 60, 60, ?, NULL, NULL), "
        "(2, ?, -1, 'アラシ', '栗毛', '青', 55, 55, 55, 55, ?, ?, NULL), "
        "(3, ?, -2, 'インタイ', '黒鹿毛', '白', 50, 50, 50, 50, ?, NULL, NULL)",
        (G, U1, NOW, G, NOW, NOW, G, NOW),
    )
    c.execute(
        "INSERT INTO horse_races (id, guild_id, kind, grade, start_at, entry_close, bet_close, status, created) "
        "VALUES (1, ?, 'regular', 'G1', ?, ?, ?, 'done', ?), "
        "(2, ?, 'regular', '', ?, ?, ?, 'betting', ?), "
        "(3, ?, 'special', '', ?, ?, ?, 'entry', ?)",
        (
            G,
            NOW - 10,
            NOW - 20,
            NOW - 15,
            NOW - 30,
            G,
            EXPIRY_FUTURE,
            NOW + 3600,
            NOW + 7200,
            NOW,
            G,
            EXPIRY_FUTURE,
            NOW + 1800,
            NOW + 3600,
            NOW,
        ),
    )
    c.execute(
        "INSERT INTO horse_entries (race_id, lane, horse_id, owner_id, name, tactic) "
        "VALUES (1, 1, 1, ?, 'ハヤブサ', 'pace'), "
        "(2, 1, 1, ?, 'ハヤブサ', 'pace'), "
        "(2, 2, 0, 0, '帝愛2号', 'pace'), "
        "(3, -1, 1, ?, 'ハヤブサ', 'pace')",
        (U1, U1, U1),
    )
    c.execute(
        "INSERT INTO horse_bets (guild_id, race_id, user_id, kind, lane, picks, stake, odds, settled, created) "
        "VALUES (?, 2, ?, 'win', 1, '', 400, 2.5, 0, ?), "
        "(?, 2, ?, 'exacta', 1, '1>2', 200, 12.0, 0, ?), "
        "(?, 2, ?, 'trio', 1, '1-2-3', 100, 18.0, 0, ?)",
        (G, U1, NOW, G, U1, NOW, G, U1, NOW),
    )
    c.execute(
        "INSERT INTO horse_odds (race_id, kind, key, odds) VALUES "
        "(1, 'quinella', '1-2', 5.0), (2, 'quinella', '1-2', 4.5), "
        "(1, 'trio', '1-2-3', 20.0), (2, 'trio', '1-2-3', 18.0)"
    )
    c.execute(
        "INSERT INTO labor (guild_id, user_id, pick, glove, helmet, pot, pot_workers, donated, relics, day, "
        "day_earned, day_click, last_click, rest_until, clicks, earned_total, apprentice, miner, dorm, idle_at, ores, wage) "
        "VALUES (?, ?, 1, 1, 2, 650, 100, 300, '{\"relic_fossil\": 1}', ?, 450, 5, ?, 99.0, 20, 1200, 2, 1, 1, ?, "
        "'{\"copper\": 2}', 'high')",
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
        assert eco.db.conn.execute("PRAGMA user_version").fetchone()[0] == schema.CURRENT_SCHEMA_VERSION == 5
        assert eco.db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert eco.db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {r[0] for r in eco.db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "guild_users" not in tables and "users" not in tables
        assert "labor" not in tables and "profile" not in tables
        assert {"crypto_short", "crypto_long", "horse_odds"}.isdisjoint(tables)
        assert {
            "accounts",
            "user_daily",
            "user_play_stats",
            "teiai_debts",
            "teiai_grace",
            "teiai_bankruptcies",
            "teiai_escapes",
            "teiai_thermostat",
            "teiai_trust",
            "kyosai_state",
            "teiai_house_grants",
            "labor_upgrades",
            "labor_fatigue",
            "labor_pot",
            "labor_daily",
            "labor_totals",
            "labor_pending_ores",
            "labor_pending_relics",
            "user_equipment",
            "user_buffs",
            "margin_positions",
            "horse_ownership",
            "horse_auction_listings",
            "horse_race_registrations",
            "horse_bet_picks",
            "horse_combo2_odds",
            "horse_trio_odds",
        } <= tables
        assert not os.path.exists(f"{path}.before-v1")
        assert not os.path.exists(f"{path}.before-v2")
        assert not os.path.exists(f"{path}.before-v3")
        assert not os.path.exists(f"{path}.before-v4")
        assert not os.path.exists(f"{path}.before-v5")
        assert eco.for_guild(G).wallet.balance(U1) == INITIAL_BALANCE
    finally:
        eco.close()


def test_v0_to_v1_lossless_migration_drops_dead_schema_and_preserves_assets(tmp_path):
    path = str(tmp_path / "legacy.db")
    build_legacy_v0_db(path)

    eco = open_repositories(path, strict=True)
    try:
        conn = eco.db.conn
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert os.path.exists(f"{path}.before-v1")
        assert os.path.exists(f"{path}.before-v2")
        assert os.path.exists(f"{path}.before-v3")
        assert os.path.exists(f"{path}.before-v4")
        assert os.path.exists(f"{path}.before-v5")

        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "guild_users" not in tables and "autoplay" not in tables and "users" not in tables
        assert "labor" not in tables and "profile" not in tables
        assert {"crypto_short", "crypto_long", "horse_odds"}.isdisjoint(tables)

        removed = {
            "teiai_bank": {"bailout_debt", "kyosai_house_ts"},
            "horses": {"rented_day", "owner_id", "listed_at"},
            "horse_races": {"grade"},
            "horse_entries": {"tactic"},
            "horse_bets": {"lane", "picks"},
            "labor_workers": {"legacy"},
        }
        for table, dead_cols in removed.items():
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert not (dead_cols & cols), f"{table} に廃止列が残っている: {dead_cols & cols}"

        # 不要行の掃除: qty<=0 の seized_items と、完了レースの組合せオッズだけが消える
        seized = conn.execute("SELECT qty FROM seized_items ORDER BY id").fetchall()
        assert [r[0] for r in seized] == [4]
        odds2 = conn.execute("SELECT race_id FROM horse_combo2_odds").fetchall()
        odds3 = conn.execute("SELECT race_id FROM horse_trio_odds").fetchall()
        assert [r[0] for r in odds2] == [2] and [r[0] for r in odds3] == [2]

        # autoplay (750) は残高に返金され、作業員の旧人数列は労働契約へ引き継がれる
        g = eco.for_guild(G)
        assert g.wallet.balance(U1) == 50_750
        assert g.debt.debt(U1) == 2_000
        assert g.bank.bank_status(NOW)["cash"] == pytest.approx(150_000.0)
        assert g.kyosai.units_of_for(G, U1) == 1_000 and g.kyosai.units_of_for(G, 0) == 9_000

        state = g.labor.labor_state(U1, NOW, DAY)
        assert (state["apprentice"], state["miner"], state["craftsman"]) == (2, 1, 1)
        assert state["pick"] == 1 and state["glove"] == 1 and state["dorm"] == 1
        assert state["pot"] == 650 and state["ores"] == {"copper": 2} and state["relics"] == {"relic_fossil": 1}
        assert state["donated"] == 300 and state["day_earned"] == 450 and state["clicks"] == 20
        prof = g.shop.get_profile(U1, raw=True)
        assert prof["title"] == "title_rookie" and prof["skin_dice"] == "skin_kuro"
        assert prof["daily_boost"] == 2 and prof["insurance"] == 1
        assert g.margin.short_position(U1, "cs") == (1000, 10.0, 1500)
        assert g.margin.long_position(U1, "cs") == (1000, 10.0, 2000)
        assert [h["name"] for h in g.horses.auction_horses(NOW)] == ["アラシ"]
        h3 = g.horses.horse_get(3)
        assert h3 is not None and h3["owner_id"] == -2
        assert [e["horse_id"] for e in g.races.race_entries(3, registered_only=True)] == [1]
        assert [(b["kind"], b["lane"], b["picks"]) for b in g.races.race_bets(U1, 2)] == [
            ("win", 1, ""),
            ("exacta", 1, "1>2"),
            ("trio", 1, "1-2-3"),
        ]

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
        assert parts["tools"] == 2_300
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
        assert (
            check.execute("SELECT balance FROM accounts WHERE guild_id=? AND user_id=?", (G, U1)).fetchone()[0]
            == INITIAL_BALANCE + 123
        )
    finally:
        check.close()


def test_failing_migration_step_rolls_back_atomically(tmp_path):
    path = str(tmp_path / "rollback.db")
    eco = open_repositories(path, strict=True)
    eco.for_guild(G).wallet.credit(U1, 500, "事前残高")
    eco.close()

    def broken_step(conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE accounts SET balance = 0")
        raise RuntimeError("boom during migration")

    steps = (*schema.MIGRATIONS, schema.MigrationStep(6, "broken_v6", broken_step))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(RuntimeError, match="boom"):
            schema.initialize_db(conn, path, steps=steps, target_version=6)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            conn.execute("SELECT balance FROM accounts WHERE guild_id=? AND user_id=?", (G, U1)).fetchone()[0]
            == INITIAL_BALANCE + 500
        )
    finally:
        conn.close()


def test_v1_to_v2_splits_all_user_domains_losslessly(tmp_path):
    import time

    now_ts = time.time()
    path = str(tmp_path / "v1_only.db")
    c = sqlite3.connect(path)
    schema.create_v1_missing_tables(c)
    c.execute(
        "INSERT INTO users (guild_id, user_id, balance, daily_streak, last_daily, last_slot, "
        "games_played, total_wagered, total_won, biggest_win, created_at, debt, debt_principal, "
        "debt_interest_at, grace_until, grace_penalty, grace_penalty_at, bankrupt_at, blacklist_until, "
        "escape_at, escape_tries) "
        "VALUES (?, ?, 42000, 4, '2026-11-09', 200, 12, 9000, 11000, 3500, ?, 18000, 15000, ?, ?, 1.5, ?, ?, ?, ?, 2)",
        (
            G,
            U1,
            now_ts,
            now_ts - 10,
            now_ts + 86400,
            now_ts - 20,
            now_ts - 500,
            now_ts + 86400,
            now_ts - 300,
        ),
    )
    c.execute("PRAGMA user_version = 1")
    c.commit()
    c.close()

    eco = open_repositories(path, strict=True)
    try:
        conn = eco.db.conn
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert not os.path.exists(f"{path}.before-v1")
        assert os.path.exists(f"{path}.before-v2")
        assert os.path.exists(f"{path}.before-v3")
        assert os.path.exists(f"{path}.before-v4")
        assert os.path.exists(f"{path}.before-v5")
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'").fetchone() is None

        st = eco.for_guild(G).wallet.stats(U1)
        assert st["balance"] == 42_000
        assert st["daily_streak"] == 4 and st["last_daily"] == "2026-11-09" and st["last_slot"] == 200
        assert (st["games_played"], st["total_wagered"], st["total_won"], st["biggest_win"]) == (12, 9000, 11000, 3500)
        assert st["debt"] == 18_000 and st["debt_principal"] == 15_000
        assert st["grace_penalty"] == pytest.approx(1.5)
        assert st["bankrupt_at"] == pytest.approx(now_ts - 500)
        assert st["blacklist_until"] == pytest.approx(now_ts + 86400)
        assert eco.for_guild(G).debt.escape_left(U1) >= 0
        esc = conn.execute("SELECT tries, last_attempt_at FROM teiai_escapes WHERE guild_id=? AND user_id=?", (G, U1)).fetchone()
        assert esc is not None and esc["tries"] == 2 and esc["last_attempt_at"] == pytest.approx(now_ts - 300)
        grc = conn.execute("SELECT grace_until FROM teiai_grace WHERE guild_id=? AND user_id=?", (G, U1)).fetchone()
        assert grc is not None and grc["grace_until"] == pytest.approx(now_ts + 86400)
    finally:
        eco.close()


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
                "trio": {"1-2-3": 12.0},
            },
        )
        assert eco.db.conn.execute("SELECT COUNT(*) FROM horse_combo2_odds WHERE race_id=?", (rid,)).fetchone()[0] == 1
        assert eco.db.conn.execute("SELECT COUNT(*) FROM horse_trio_odds WHERE race_id=?", (rid,)).fetchone()[0] == 1
        assert g.races.race_run(rid, tz_hours=9.0) is not None
        assert eco.db.conn.execute("SELECT COUNT(*) FROM horse_combo2_odds WHERE race_id=?", (rid,)).fetchone()[0] == 0
        assert eco.db.conn.execute("SELECT COUNT(*) FROM horse_trio_odds WHERE race_id=?", (rid,)).fetchone()[0] == 0
    finally:
        eco.close()


def test_v2_to_v3_splits_teiai_bank_and_house_losslessly(tmp_path):
    path = str(tmp_path / "v2_only.db")
    c = sqlite3.connect(path)
    schema.create_v1_missing_tables(c)
    from infra.db.schema.migrations_v2 import migrate_v1_to_v2

    migrate_v1_to_v2(c)
    c.execute(
        "INSERT INTO teiai_bank (guild_id, cash, loaned, ts, thermo_load, thermo_capacity, thermo_ts, "
        "thermo_burned, thermo_bailed, trust_ts, trust_accrued, trust_paid, excess_unpaid, rf, rf_ts, "
        "kyosai_div_ts, kyosai_paid, kyosai_charged, trust_premium) "
        "VALUES (?, 80000.0, 20000.0, ?, 15.0, 60000.0, ?, 111.0, 222.0, ?, 3.5, 444.0, 55.0, 0.03, ?, ?, "
        "777.0, 888.0, 0.025)",
        (G, NOW, NOW, NOW, NOW, NOW),
    )
    c.execute(
        "INSERT INTO teiai_house (guild_id, balance, skimmed, grant_day, grant_used) VALUES (?, 19000, 500, ?, 3200)",
        (G, DAY),
    )
    c.execute("PRAGMA user_version = 2")
    c.commit()
    c.close()

    eco = open_repositories(path, strict=True)
    try:
        conn = eco.db.conn
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert not os.path.exists(f"{path}.before-v2")
        assert os.path.exists(f"{path}.before-v3")
        assert os.path.exists(f"{path}.before-v4")
        assert os.path.exists(f"{path}.before-v5")

        tb_cols = {r[1] for r in conn.execute("PRAGMA table_info(teiai_bank)")}
        assert tb_cols == {"guild_id", "cash", "loaned", "ts"}
        th_cols = {r[1] for r in conn.execute("PRAGMA table_info(teiai_house)")}
        assert th_cols == {"guild_id", "balance", "skimmed"}

        g = eco.for_guild(G)
        st = g.bank.bank_status(NOW)
        assert st["cash"] == pytest.approx(80_000.0) and st["loaned"] == pytest.approx(20_000.0)
        assert st["thermostat"]["burned"] == pytest.approx(111.0)
        assert st["thermostat"]["bailed"] == pytest.approx(222.0)
        assert st["trust_paid"] == pytest.approx(444.0)
        assert st["excess_unpaid"] == pytest.approx(55.0)
        assert g.bank.trust_premium_for(G) == pytest.approx(0.025)
        ks = g.kyosai.kyosai_status(U1, NOW)
        assert ks["paid"] == pytest.approx(777.0) and ks["charged"] == pytest.approx(888.0)
        assert g.bank.house_account() == {"balance": 19_000, "skimmed": 500}
        assert g.bank.house_grant_left_for(G, DAY, 10_000) == 6_800
    finally:
        eco.close()


def test_v3_to_v4_splits_labor_and_profile_losslessly(tmp_path):
    path = str(tmp_path / "v3_only.db")
    c = sqlite3.connect(path)
    schema.create_v1_missing_tables(c)
    from infra.db.schema.migrations_v2 import migrate_v1_to_v2
    from infra.db.schema.migrations_v3 import migrate_v2_to_v3

    migrate_v1_to_v2(c)
    migrate_v2_to_v3(c)
    c.execute(
        "INSERT INTO accounts (guild_id, user_id, balance, created_at) VALUES (?, ?, 10000, ?)",
        (G, U1, NOW),
    )
    c.execute(
        "INSERT INTO labor (guild_id, user_id, pick, glove, fatigue, fatigue_at, pot, pot_workers, "
        "donated, relics, day, day_earned, day_click, last_click, clicks, earned_total, dorm, idle_at, renew, ores) "
        "VALUES (?, ?, 2, 1, 15.0, ?, 900, 300, 120, '{\"relic_fossil\": 2}', ?, 500, 8, ?, 40, 2500, 1, ?, 0, '{\"copper\": 3}')",
        (G, U1, NOW, DAY, NOW - 5, NOW),
    )
    c.execute(
        "INSERT INTO profile (guild_id, user_id, title, card_color, card_icon, skin_dice, skin_table, daily_boost, insurance) "
        "VALUES (?, ?, 'title_rookie', 'card_gold', NULL, 'skin_kuro', 'skin_vip', 3, 2)",
        (G, U1),
    )
    c.execute("PRAGMA user_version = 3")
    c.commit()
    c.close()

    eco = open_repositories(path, strict=True)
    try:
        conn = eco.db.conn
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert not os.path.exists(f"{path}.before-v3")
        assert os.path.exists(f"{path}.before-v4")
        assert os.path.exists(f"{path}.before-v5")
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='labor'").fetchone() is None
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='profile'").fetchone() is None

        g = eco.for_guild(G)
        st = g.labor.labor_state(U1, NOW, DAY)
        assert (st["pick"], st["glove"], st["dorm"], st["renew"]) == (2, 1, 1, 0)
        assert st["pot"] == 900 and st["pot_workers"] == 300 and st["donated"] == 120
        assert st["day_earned"] == 500 and st["day_click"] == 8
        assert st["clicks"] == 40 and st["earned_total"] == 2500
        assert st["ores"] == {"copper": 3} and st["relics"] == {"relic_fossil": 2}
        assert g.labor.labor_top(day=DAY)["rows"] == [(1, U1, 500, 40)]

        prof = g.shop.get_profile(U1, raw=True)
        assert prof["title"] == "title_rookie" and prof["card_color"] == "card_gold"
        assert prof["card_icon"] is None and prof["skin_dice"] == "skin_kuro" and prof["skin_table"] == "skin_vip"
        assert prof["daily_boost"] == 3 and prof["insurance"] == 2
    finally:
        eco.close()


def test_v4_to_v5_merges_margin_and_normalizes_horse_racing_losslessly(tmp_path):
    path = str(tmp_path / "v4_only.db")
    c = sqlite3.connect(path)
    schema.create_v1_missing_tables(c)
    from infra.db.schema.migrations_v2 import migrate_v1_to_v2
    from infra.db.schema.migrations_v3 import migrate_v2_to_v3
    from infra.db.schema.migrations_v4 import migrate_v3_to_v4

    migrate_v1_to_v2(c)
    migrate_v2_to_v3(c)
    migrate_v3_to_v4(c)
    c.execute("INSERT INTO accounts (guild_id, user_id, balance, created_at) VALUES (?, ?, 50000, ?)", (G, U1, NOW))
    c.execute(
        "INSERT INTO crypto_short (guild_id, user_id, symbol, units, entry_value, margin, interest_at, accrued) "
        "VALUES (?, ?, 'cs', 2000, 20.0, 1200, ?, 1.5)",
        (G, U1, NOW),
    )
    c.execute(
        "INSERT INTO crypto_long (guild_id, user_id, symbol, units, entry_value, margin, interest_at, accrued) "
        "VALUES (?, ?, 'nv', 3000, 60.0, 2400, ?, 2.5)",
        (G, U1, NOW),
    )
    c.execute(
        "INSERT INTO horses (id, guild_id, owner_id, name, coat, silks, speed, stamina, power, guts, created, listed_at) "
        "VALUES (10, ?, ?, 'カチドキ', '鹿毛', '赤', 65, 55, 50, 50, ?, NULL), "
        "(11, ?, -1, 'セリウマ', '白毛', '青', 50, 50, 50, 50, ?, ?)",
        (G, U1, NOW, G, NOW, NOW - 60),
    )
    c.execute(
        "INSERT INTO horse_races (id, guild_id, kind, start_at, entry_close, bet_close, status, created) "
        "VALUES (10, ?, 'regular', ?, ?, ?, 'betting', ?), (11, ?, 'special', ?, ?, ?, 'entry', ?)",
        (G, EXPIRY_FUTURE, NOW + 100, NOW + 200, NOW, G, EXPIRY_FUTURE, NOW + 100, NOW + 200, NOW),
    )
    c.execute(
        "INSERT INTO horse_entries (race_id, lane, horse_id, owner_id, name, coat, silks) "
        "VALUES (10, 1, 10, ?, 'カチドキ', '鹿毛', '赤'), (10, 2, 0, 0, '帝愛2号', '栗毛', '白'), "
        "(11, -1, 10, ?, 'カチドキ', '鹿毛', '赤')",
        (U1, U1),
    )
    c.execute(
        "INSERT INTO horse_bets (id, guild_id, race_id, user_id, kind, lane, picks, stake, odds, settled, created) "
        "VALUES (20, ?, 10, ?, 'exacta', 2, '2>1', 500, 9.5, 0, ?)",
        (G, U1, NOW),
    )
    c.execute(
        "INSERT INTO horse_odds (race_id, kind, key, odds) VALUES (10, 'exacta', '2>1', 9.5), (10, 'trio', '1-2-3', 14.0)"
    )
    c.execute("PRAGMA user_version = 4")
    c.commit()
    c.close()

    eco = open_repositories(path, strict=True)
    try:
        conn = eco.db.conn
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert not os.path.exists(f"{path}.before-v4")
        assert os.path.exists(f"{path}.before-v5")
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='crypto_short'").fetchone() is None
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='horse_odds'").fetchone() is None

        g = eco.for_guild(G)
        assert g.margin.short_position(U1, "cs") == (2000, 20.0, 1200)
        assert g.margin.long_position(U1, "nv") == (3000, 60.0, 2400)
        assert [h["id"] for h in g.horses.horse_list(U1)] == [10]
        assert [h["id"] for h in g.horses.auction_horses(NOW)] == [11]
        assert g.races.race_odds(10, "exacta", [2, 1]) == pytest.approx(9.5)
        assert g.races.race_odds(10, "trio", [3, 1, 2]) == pytest.approx(14.0)
        assert [e["horse_id"] for e in g.races.race_entries(11, registered_only=True)] == [10]
        bets = g.races.race_bets(U1, 10)
        assert [(b["kind"], b["lane"], b["picks"], b["stake"]) for b in bets] == [("exacta", 2, "2>1", 500)]
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
        assert check.execute("PRAGMA user_version").fetchone()[0] == 5
    finally:
        check.close()
