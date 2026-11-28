"""競走馬・所有権・帝愛競売・レース・出走登録・出走馬・馬券・組合せオッズの DDL。"""

HORSES_V1_DDL = """
    CREATE TABLE IF NOT EXISTS horses (          -- 馬主が持つ競走馬（v1 互換: v5 で所有・競売出品を分離）
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

HORSES_V1_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS horses_owner ON horses (guild_id, owner_id)"""

HORSES_DDL = """
    CREATE TABLE IF NOT EXISTS horses (          -- 競走馬の個体マスタ・成績（所有状態は子表へ分離）
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        status   TEXT    NOT NULL DEFAULT 'owned' CHECK (status IN ('owned', 'auction', 'retired', 'escrow')),
        name     TEXT    NOT NULL,
        coat     TEXT    NOT NULL,
        silks    TEXT    NOT NULL,
        speed    INTEGER NOT NULL,
        stamina  INTEGER NOT NULL,
        power    INTEGER NOT NULL,
        guts     INTEGER NOT NULL,
        races    INTEGER NOT NULL DEFAULT 0 CHECK (races >= 0),
        wins     INTEGER NOT NULL DEFAULT 0 CHECK (wins >= 0),
        places   INTEGER NOT NULL DEFAULT 0 CHECK (places >= 0),
        earnings INTEGER NOT NULL DEFAULT 0 CHECK (earnings >= 0),
        bonus    INTEGER NOT NULL DEFAULT 0 CHECK (bonus >= 0),
        created  REAL    NOT NULL,
        style    TEXT
    )"""

HORSE_OWNERSHIP_DDL = """
    CREATE TABLE IF NOT EXISTS horse_ownership ( -- 馬主と競走馬の所有関係（owner_id > 0 のみ保持）
        horse_id INTEGER PRIMARY KEY,
        guild_id INTEGER NOT NULL,
        owner_id INTEGER NOT NULL CHECK (owner_id > 0),
        FOREIGN KEY (horse_id) REFERENCES horses (id) ON DELETE CASCADE
    )"""

HORSE_OWNERSHIP_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_horse_ownership_owner ON horse_ownership (guild_id, owner_id)"""

HORSE_AUCTION_LISTINGS_DDL = """
    CREATE TABLE IF NOT EXISTS horse_auction_listings ( -- 帝愛の差押競売に出ている馬
        horse_id  INTEGER PRIMARY KEY,
        guild_id  INTEGER NOT NULL,
        listed_at REAL    NOT NULL,
        FOREIGN KEY (horse_id) REFERENCES horses (id) ON DELETE CASCADE
    )"""

HORSE_AUCTION_LISTINGS_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_horse_auction_listings_guild ON horse_auction_listings (guild_id, listed_at, horse_id)"""

HORSE_RACES_V1_DDL = """
    CREATE TABLE IF NOT EXISTS horse_races (     -- レース（v1 互換: v6 で UI 列を discord_posts へ分離）
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

HORSE_RACES_DDL = """
    CREATE TABLE IF NOT EXISTS horse_races (     -- レース（定時と臨時）
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id    INTEGER NOT NULL,
        kind        TEXT    NOT NULL CHECK (kind IN ('regular', 'special')),
        start_at    REAL    NOT NULL,
        entry_close REAL    NOT NULL,
        bet_close   REAL    NOT NULL,
        status      TEXT    NOT NULL CHECK (status IN ('entry', 'betting', 'done')),
        distance    INTEGER NOT NULL DEFAULT 0,
        going       TEXT    NOT NULL DEFAULT '',
        created     REAL    NOT NULL
    )"""

HORSE_RACES_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS horse_races_status ON horse_races (guild_id, status, start_at)"""

HORSE_ENTRIES_V1_DDL = """
    CREATE TABLE IF NOT EXISTS horse_entries (   -- 出走馬（v1 互換: v5 で締切前申込と出走確定を分離）
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

HORSE_RACE_REGISTRATIONS_DDL = """
    CREATE TABLE IF NOT EXISTS horse_race_registrations ( -- 出走申し込み（締切前のみ保持）
        race_id  INTEGER NOT NULL,
        horse_id INTEGER NOT NULL,
        owner_id INTEGER NOT NULL CHECK (owner_id > 0),
        name     TEXT    NOT NULL,
        coat     TEXT    NOT NULL,
        silks    TEXT    NOT NULL,
        PRIMARY KEY (race_id, horse_id),
        UNIQUE (race_id, owner_id),
        FOREIGN KEY (race_id) REFERENCES horse_races (id) ON DELETE CASCADE,
        FOREIGN KEY (horse_id) REFERENCES horses (id) ON DELETE CASCADE
    )"""

HORSE_ENTRIES_DDL = """
    CREATE TABLE IF NOT EXISTS horse_entries (   -- 出走確定馬（NPC は horse_id・owner_id が NULL）
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        race_id    INTEGER NOT NULL,
        lane       INTEGER NOT NULL CHECK (lane >= 1),
        horse_id   INTEGER,
        owner_id   INTEGER CHECK (owner_id IS NULL OR owner_id > 0),
        name       TEXT    NOT NULL,
        coat       TEXT    NOT NULL DEFAULT '',
        silks      TEXT    NOT NULL DEFAULT '',
        speed      INTEGER NOT NULL DEFAULT 50,
        stamina    INTEGER NOT NULL DEFAULT 50,
        power      INTEGER NOT NULL DEFAULT 50,
        guts       INTEGER NOT NULL DEFAULT 50,
        style      TEXT,
        form       INTEGER NOT NULL DEFAULT 0,
        races      INTEGER NOT NULL DEFAULT 0,
        wins       INTEGER NOT NULL DEFAULT 0,
        places     INTEGER NOT NULL DEFAULT 0,
        odds_win   REAL    NOT NULL DEFAULT 0,
        odds_place REAL    NOT NULL DEFAULT 0,
        finish     INTEGER NOT NULL DEFAULT 0,
        UNIQUE (race_id, lane),
        FOREIGN KEY (race_id) REFERENCES horse_races (id) ON DELETE CASCADE,
        FOREIGN KEY (horse_id) REFERENCES horses (id) ON DELETE SET NULL
    )"""

HORSE_ENTRIES_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS horse_entries_race ON horse_entries (race_id, lane)"""

HORSE_BETS_V1_DDL = """
    CREATE TABLE IF NOT EXISTS horse_bets (      -- 馬券（v1 互換: v5 で買い目を horse_bet_picks へ分離）
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

HORSE_BETS_DDL = """
    CREATE TABLE IF NOT EXISTS horse_bets (      -- 馬券（買い目は horse_bet_picks で 1NF 管理）
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        race_id  INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        kind     TEXT    NOT NULL CHECK (kind IN ('win', 'place', 'quinella', 'wide', 'exacta', 'trio')),
        stake    INTEGER NOT NULL CHECK (stake > 0),
        odds     REAL    NOT NULL,
        payout   INTEGER NOT NULL DEFAULT 0 CHECK (payout >= 0),
        settled  INTEGER NOT NULL DEFAULT 0 CHECK (settled IN (0, 1)),
        created  REAL    NOT NULL,
        FOREIGN KEY (race_id) REFERENCES horse_races (id) ON DELETE CASCADE
    )"""

HORSE_BETS_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS horse_bets_race ON horse_bets (race_id, user_id)"""

HORSE_BET_PICKS_DDL = """
    CREATE TABLE IF NOT EXISTS horse_bet_picks ( -- 馬券ごとの選択枠番（position=1..3）
        bet_id   INTEGER NOT NULL,
        position INTEGER NOT NULL CHECK (position >= 1),
        lane     INTEGER NOT NULL CHECK (lane >= 1),
        PRIMARY KEY (bet_id, position),
        UNIQUE (bet_id, lane),
        FOREIGN KEY (bet_id) REFERENCES horse_bets (id) ON DELETE CASCADE
    )"""

HORSE_ODDS_V1_DDL = """
    CREATE TABLE IF NOT EXISTS horse_odds (      -- 組み合わせの馬券のオッズ（v1 互換: v5 で 2頭・3頭表へ分離）
        race_id INTEGER NOT NULL,
        kind    TEXT NOT NULL,
        key     TEXT NOT NULL,
        odds    REAL NOT NULL,
        PRIMARY KEY (race_id, kind, key)
    )"""

HORSE_COMBO2_ODDS_DDL = """
    CREATE TABLE IF NOT EXISTS horse_combo2_odds ( -- 2頭組合せ（馬連・ワイド・馬単）のオッズ
        race_id INTEGER NOT NULL,
        kind    TEXT    NOT NULL CHECK (kind IN ('quinella', 'wide', 'exacta')),
        lane1   INTEGER NOT NULL CHECK (lane1 >= 1),
        lane2   INTEGER NOT NULL CHECK (lane2 >= 1 AND lane2 != lane1),
        odds    REAL    NOT NULL,
        PRIMARY KEY (race_id, kind, lane1, lane2),
        FOREIGN KEY (race_id) REFERENCES horse_races (id) ON DELETE CASCADE
    )"""

HORSE_TRIO_ODDS_DDL = """
    CREATE TABLE IF NOT EXISTS horse_trio_odds ( -- 3連複のオッズ
        race_id INTEGER NOT NULL,
        lane1   INTEGER NOT NULL CHECK (lane1 >= 1),
        lane2   INTEGER NOT NULL CHECK (lane2 > lane1),
        lane3   INTEGER NOT NULL CHECK (lane3 > lane2),
        odds    REAL    NOT NULL,
        PRIMARY KEY (race_id, lane1, lane2, lane3),
        FOREIGN KEY (race_id) REFERENCES horse_races (id) ON DELETE CASCADE
    )"""

STATEMENTS: tuple[str, ...] = (
    HORSES_DDL,
    HORSE_OWNERSHIP_DDL,
    HORSE_OWNERSHIP_INDEX_DDL,
    HORSE_AUCTION_LISTINGS_DDL,
    HORSE_AUCTION_LISTINGS_INDEX_DDL,
    HORSE_RACES_DDL,
    HORSE_RACES_INDEX_DDL,
    HORSE_RACE_REGISTRATIONS_DDL,
    HORSE_ENTRIES_DDL,
    HORSE_ENTRIES_INDEX_DDL,
    HORSE_BETS_DDL,
    HORSE_BETS_INDEX_DDL,
    HORSE_BET_PICKS_DDL,
    HORSE_COMBO2_ODDS_DDL,
    HORSE_TRIO_ODDS_DDL,
)
