"""口座・デイリー・プレイ成績・元帳・エスクロー・ジャックポット・ミッション・RTP の DDL。"""

LEGACY_USERS_V1_DDL = """
    CREATE TABLE IF NOT EXISTS users (
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
        last_slot        INTEGER,                 -- 最後にボーナスを受け取った「時間帯」の番号（6時間ごとの受け取り用）
        debt             INTEGER NOT NULL DEFAULT 0,   -- 帝愛からの借金（シニア債権）。収入から天引きで返済する（domain/teiai.py）
        debt_principal   INTEGER NOT NULL DEFAULT 0,   -- 借金の元本（利息の上限計算用。元本の2倍が上限）
        debt_interest_at REAL NOT NULL DEFAULT 0,      -- トイチ利息をここまで数えた時刻
        grace_until      REAL,                         -- 破産猶予の終了時刻
        grace_penalty    REAL NOT NULL DEFAULT 0,      -- 破産猶予ペナルティ（入るたび +1、時間経過で減衰）
        grace_penalty_at REAL NOT NULL DEFAULT 0,      -- 最後に破産猶予に入った時刻（ペナルティ減衰の起点）
        bankrupt_at      REAL,                         -- 自己破産した時刻（NULL なら破産者ではない）
        blacklist_until  REAL,                         -- 破産者の身分が終わる時刻（完済のときに、完済から7日後を入れる）
        escape_at        REAL,                         -- 最後に逃亡を試みた時刻
        escape_tries     INTEGER NOT NULL DEFAULT 0,   -- これまでの逃亡試行回数
        PRIMARY KEY (guild_id, user_id)
    )"""

USERS_DDL = LEGACY_USERS_V1_DDL

ACCOUNTS_DDL = """
    CREATE TABLE IF NOT EXISTS accounts (
        guild_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        balance    INTEGER NOT NULL CHECK (balance >= 0),
        created_at REAL NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    )"""

USER_DAILY_DDL = """
    CREATE TABLE IF NOT EXISTS user_daily (
        guild_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        streak     INTEGER NOT NULL DEFAULT 0 CHECK (streak >= 0),
        last_daily TEXT,
        last_slot  INTEGER,
        PRIMARY KEY (guild_id, user_id),
        FOREIGN KEY (guild_id, user_id) REFERENCES accounts (guild_id, user_id) ON DELETE CASCADE
    )"""

USER_PLAY_STATS_DDL = """
    CREATE TABLE IF NOT EXISTS user_play_stats (
        guild_id      INTEGER NOT NULL,
        user_id       INTEGER NOT NULL,
        games_played  INTEGER NOT NULL DEFAULT 0 CHECK (games_played >= 0),
        total_wagered INTEGER NOT NULL DEFAULT 0 CHECK (total_wagered >= 0),
        total_won     INTEGER NOT NULL DEFAULT 0 CHECK (total_won >= 0),
        biggest_win   INTEGER NOT NULL DEFAULT 0 CHECK (biggest_win >= 0),
        PRIMARY KEY (guild_id, user_id),
        FOREIGN KEY (guild_id, user_id) REFERENCES accounts (guild_id, user_id) ON DELETE CASCADE
    )"""

LEDGER_V1_DDL = """
    CREATE TABLE IF NOT EXISTS ledger (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id      INTEGER NOT NULL DEFAULT 0,
        user_id       INTEGER NOT NULL,
        delta         INTEGER NOT NULL,
        balance_after INTEGER NOT NULL,
        reason        TEXT NOT NULL,
        ts            REAL NOT NULL
    )"""

LEDGER_DDL = """
    CREATE TABLE IF NOT EXISTS ledger (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id        INTEGER NOT NULL DEFAULT 0,
        user_id         INTEGER NOT NULL,
        delta           INTEGER NOT NULL,
        balance_after   INTEGER NOT NULL,
        reason          TEXT    NOT NULL,
        ts              REAL    NOT NULL,
        kind            TEXT    NOT NULL DEFAULT '',
        counterparty_id INTEGER,
        ref_id          INTEGER,
        FOREIGN KEY (guild_id, user_id) REFERENCES accounts (guild_id, user_id) ON DELETE CASCADE
    )"""

LEDGER_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS ledger_user ON ledger (guild_id, user_id, id)"""

LEDGER_KIND_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_ledger_user_kind_ts ON ledger (guild_id, user_id, kind, ts)"""

JACKPOT_DDL = """
    CREATE TABLE IF NOT EXISTS jackpot (          -- 共有ジャックポット（サーバーごと。domain/games/jackpot.py）
        guild_id    INTEGER PRIMARY KEY,
        pool        INTEGER NOT NULL DEFAULT 0 CHECK (pool >= 0),  -- いまの積み立て
        last_user   INTEGER,                     -- 最後に当てた人
        last_amount INTEGER NOT NULL DEFAULT 0 CHECK (last_amount >= 0),
        last_at     REAL    NOT NULL DEFAULT 0
    )"""

ESCROW_DDL = """
    CREATE TABLE IF NOT EXISTS escrow (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL DEFAULT 0,
        user_id    INTEGER NOT NULL,
        amount     INTEGER NOT NULL CHECK (amount > 0),
        game       TEXT NOT NULL,
        created_at REAL NOT NULL
    )"""

PLAY_STATE_DDL = """
    CREATE TABLE IF NOT EXISTS play_state (
        guild_id    INTEGER NOT NULL,
        user_id     INTEGER NOT NULL,
        win_streak  INTEGER NOT NULL DEFAULT 0 CHECK (win_streak >= 0),
        best_streak INTEGER NOT NULL DEFAULT 0 CHECK (best_streak >= 0),
        day_streak  INTEGER NOT NULL DEFAULT 0 CHECK (day_streak >= 0),      -- ミッション用: その日の、賭け金が最低額以上の連勝
        streak_day  TEXT,
        plays       INTEGER NOT NULL DEFAULT 0 CHECK (plays >= 0),       -- 成績の集計（勝率・損益）は、この記録を付け始めてからの分
        wins        INTEGER NOT NULL DEFAULT 0 CHECK (wins >= 0),
        net_total   INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    )"""

MISSIONS_DDL = """
    CREATE TABLE IF NOT EXISTS missions (
        guild_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        day        TEXT    NOT NULL,
        mission_id TEXT    NOT NULL,
        progress   INTEGER NOT NULL DEFAULT 0 CHECK (progress >= 0),
        done       INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1)),
        PRIMARY KEY (guild_id, user_id, day, mission_id)
    )"""

RTP_LEVEL_DDL = """
    CREATE TABLE IF NOT EXISTS rtp_level (          -- ゲームごとの還元率レベル（1〜6。3は標準＝保存しない）
        guild_id INTEGER NOT NULL,
        game     TEXT    NOT NULL,
        level    INTEGER NOT NULL CHECK (level BETWEEN 1 AND 6),
        PRIMARY KEY (guild_id, game)
    )"""

STATEMENTS: tuple[str, ...] = (
    ACCOUNTS_DDL,
    USER_DAILY_DDL,
    USER_PLAY_STATS_DDL,
    LEDGER_DDL,
    LEDGER_INDEX_DDL,
    LEDGER_KIND_INDEX_DDL,
    JACKPOT_DDL,
    ESCROW_DDL,
    PLAY_STATE_DDL,
    MISSIONS_DDL,
    RTP_LEVEL_DDL,
)
