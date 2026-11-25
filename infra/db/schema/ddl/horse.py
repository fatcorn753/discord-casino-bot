"""競走馬・レース・出走・馬券・組合せオッズの DDL。"""

HORSES_DDL = """
    CREATE TABLE IF NOT EXISTS horses (          -- 馬主が持つ競走馬
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id  INTEGER NOT NULL,
        owner_id  INTEGER NOT NULL,
        name      TEXT NOT NULL,
        coat      TEXT NOT NULL,
        silks     TEXT NOT NULL,
        speed     INTEGER NOT NULL,
        stamina   INTEGER NOT NULL,
        power     INTEGER NOT NULL,
        guts      INTEGER NOT NULL,
        races     INTEGER NOT NULL DEFAULT 0,
        wins      INTEGER NOT NULL DEFAULT 0,
        places    INTEGER NOT NULL DEFAULT 0,   -- 3着以内
        earnings  INTEGER NOT NULL DEFAULT 0,   -- 賞金の合計
        bonus     INTEGER NOT NULL DEFAULT 0,   -- 金にんじんで増えた、ステータス合計の増加分
        created   REAL NOT NULL,
        listed_at REAL,                         -- 破産で差し押さえられて、帝愛の競売に出た時刻（owner_id=-1 のとき）
        style     TEXT                          -- 選んだ脚質（nige/senko/sashi/oikomi）。NULL なら、ステータスから自動
    )"""

HORSES_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS horses_owner ON horses (guild_id, owner_id)"""

HORSE_RACES_DDL = """
    CREATE TABLE IF NOT EXISTS horse_races (     -- レース（定時と臨時）
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id    INTEGER NOT NULL,
        kind        TEXT NOT NULL,              -- regular / special
        start_at    REAL NOT NULL,
        entry_close REAL NOT NULL,
        bet_close   REAL NOT NULL,
        status      TEXT NOT NULL,              -- entry / betting / done
        distance    INTEGER NOT NULL DEFAULT 0,
        going       TEXT NOT NULL DEFAULT '',
        channel_id  INTEGER NOT NULL DEFAULT 0,
        message_id  INTEGER NOT NULL DEFAULT 0,
        created     REAL NOT NULL
    )"""

HORSE_RACES_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS horse_races_status ON horse_races (guild_id, status, start_at)"""

HORSE_ENTRIES_DDL = """
    CREATE TABLE IF NOT EXISTS horse_entries (   -- 出走馬（申し込みと、レース時点のステータスのコピー）
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        race_id    INTEGER NOT NULL,
        lane       INTEGER NOT NULL DEFAULT -1,  -- 枠番（-1 は、締切前の申し込み）
        horse_id   INTEGER NOT NULL DEFAULT 0,   -- 0 は帝愛の馬（NPC）
        owner_id   INTEGER NOT NULL DEFAULT 0,
        name       TEXT NOT NULL,
        coat       TEXT NOT NULL DEFAULT '',
        silks      TEXT NOT NULL DEFAULT '',
        speed      INTEGER NOT NULL DEFAULT 50,
        stamina    INTEGER NOT NULL DEFAULT 50,
        power      INTEGER NOT NULL DEFAULT 50,
        guts       INTEGER NOT NULL DEFAULT 50,
        style      TEXT,                         -- 脚質のコピー（締切後のにんじん・脚質変更の影響を受けない）
        form       INTEGER NOT NULL DEFAULT 0,
        races      INTEGER NOT NULL DEFAULT 0,   -- 戦績のコピー（オッズに使う。帝愛の馬は作りもの）
        wins       INTEGER NOT NULL DEFAULT 0,
        places     INTEGER NOT NULL DEFAULT 0,
        odds_win   REAL NOT NULL DEFAULT 0,
        odds_place REAL NOT NULL DEFAULT 0,
        finish     INTEGER NOT NULL DEFAULT 0    -- 着順（0 は、まだ）
    )"""

HORSE_ENTRIES_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS horse_entries_race ON horse_entries (race_id, lane)"""

HORSE_BETS_DDL = """
    CREATE TABLE IF NOT EXISTS horse_bets (      -- 馬券
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        race_id  INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        kind     TEXT NOT NULL,                 -- win / place / quinella / wide / exacta / trio
        lane     INTEGER NOT NULL,              -- 単勝・複勝の枠（組み合わせの馬券では、最初の枠）
        picks    TEXT NOT NULL DEFAULT '',      -- 組み合わせの馬券の選び方（domain/horse.py の combo_key。例 "2-5"・"5>2"）
        stake    INTEGER NOT NULL,
        odds     REAL NOT NULL,                 -- 賭けた時点のオッズ（確定）
        payout   INTEGER NOT NULL DEFAULT 0,
        settled  INTEGER NOT NULL DEFAULT 0,
        created  REAL NOT NULL
    )"""

HORSE_BETS_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS horse_bets_race ON horse_bets (race_id, user_id)"""

HORSE_ODDS_DDL = """
    CREATE TABLE IF NOT EXISTS horse_odds (      -- 組み合わせの馬券のオッズ（レースごと。単勝・複勝は horse_entries にある）
        race_id INTEGER NOT NULL,
        kind    TEXT NOT NULL,
        key     TEXT NOT NULL,
        odds    REAL NOT NULL,
        PRIMARY KEY (race_id, kind, key)
    )"""

STATEMENTS: tuple[str, ...] = (
    HORSES_DDL,
    HORSES_INDEX_DDL,
    HORSE_RACES_DDL,
    HORSE_RACES_INDEX_DDL,
    HORSE_ENTRIES_DDL,
    HORSE_ENTRIES_INDEX_DDL,
    HORSE_BETS_DDL,
    HORSE_BETS_INDEX_DDL,
    HORSE_ODDS_DDL,
)
