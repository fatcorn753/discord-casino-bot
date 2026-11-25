"""地下作業（クリッカー）・掘り出し履歴・作業員契約の DDL。"""

LABOR_DDL = """
    CREATE TABLE IF NOT EXISTS labor (           -- 地下作業（クリッカー）。工具・疲労・まだ受け取っていない稼ぎ
        guild_id     INTEGER NOT NULL,
        user_id      INTEGER NOT NULL,
        pick         INTEGER NOT NULL DEFAULT 0,
        glove        INTEGER NOT NULL DEFAULT 0,
        fatigue      REAL NOT NULL DEFAULT 0,
        fatigue_at   REAL NOT NULL DEFAULT 0,
        pot          INTEGER NOT NULL DEFAULT 0,   -- 受け取る前の稼ぎ
        pot_workers  INTEGER NOT NULL DEFAULT 0,   -- そのうち、作業員が稼いだ分（作業員の稼ぎと鉱石の売値。受け取ると 0 に戻る）
        donated      INTEGER NOT NULL DEFAULT 0,   -- 慈善団体への寄付（帝愛のピンハネ）の、これまでの合計
        relics       TEXT NOT NULL DEFAULT '',     -- 受け取る前の発掘品（JSON {品のID: 個数}。受け取ると持ち物に入る）
        day          TEXT NOT NULL DEFAULT '',
        day_earned   INTEGER NOT NULL DEFAULT 0,   -- 今日の稼ぎ（画面に出す額）
        day_click    INTEGER NOT NULL DEFAULT 0,   -- 今日の「働く」を押した回数（1日の上限の判定。作業員の分は入らない）
        last_click   REAL NOT NULL DEFAULT 0,
        clicks       INTEGER NOT NULL DEFAULT 0,
        earned_total INTEGER NOT NULL DEFAULT 0,
        dorm         INTEGER NOT NULL DEFAULT 0,   -- 寮の段（1段ごとに、雇える人数 +1）
        idle_at      REAL NOT NULL DEFAULT 0,      -- 作業員の稼ぎを、最後に数えた時刻
        renew        INTEGER NOT NULL DEFAULT 1,   -- 作業員の契約の自動更新（1=オン）
        ores         TEXT NOT NULL DEFAULT '',     -- 受け取る前の稼ぎに含まれる鉱石（JSON {鉱石: 個数}）
        PRIMARY KEY (guild_id, user_id)
    )"""

LABOR_FINDS_DDL = """
    CREATE TABLE IF NOT EXISTS labor_finds (      -- 作業員の掘り出しの履歴（古いものは消す）
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id  INTEGER NOT NULL,
        user_id   INTEGER NOT NULL,
        at        REAL NOT NULL,
        kind      TEXT NOT NULL,                   -- 掘った作業員の種類
        ore       TEXT NOT NULL,
        qty       INTEGER NOT NULL DEFAULT 1,
        collected INTEGER NOT NULL DEFAULT 0        -- 受け取り（労働 collect）済みなら 1。画面には、まだのものだけ出す
    )"""

LABOR_FINDS_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS labor_finds_owner ON labor_finds (guild_id, user_id, at)"""

LABOR_CLICK_HOURS_DDL = """
    CREATE TABLE IF NOT EXISTS labor_click_hours (  -- 「働く」を押した記録（1時間ごとの集計）。自動操作の疑いを、持ち主が確かめるためのもの
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        hour     INTEGER NOT NULL,                -- epoch秒 ÷ 3600
        clicks   INTEGER NOT NULL DEFAULT 0,
        pay      INTEGER NOT NULL DEFAULT 0,
        gaps     INTEGER NOT NULL DEFAULT 0,      -- 間隔を数えた回数（前の押下から10分以内のもの）
        sum_gap  REAL NOT NULL DEFAULT 0,
        sum_gap2 REAL NOT NULL DEFAULT 0,         -- 間隔の2乗の合計（ばらつきを出す）
        min_gap  REAL,
        PRIMARY KEY (guild_id, user_id, hour)
    )"""

LABOR_WORKERS_DDL = """
    CREATE TABLE IF NOT EXISTS labor_workers (    -- 雇っている作業員（1人1行。契約の期限つき）
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        kind       TEXT NOT NULL,
        expires_at REAL NOT NULL
    )"""

LABOR_WORKERS_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS labor_workers_owner ON labor_workers (guild_id, user_id)"""

STATEMENTS: tuple[str, ...] = (
    LABOR_DDL,
    LABOR_FINDS_DDL,
    LABOR_FINDS_INDEX_DDL,
    LABOR_CLICK_HOURS_DDL,
    LABOR_WORKERS_DDL,
    LABOR_WORKERS_INDEX_DDL,
)
