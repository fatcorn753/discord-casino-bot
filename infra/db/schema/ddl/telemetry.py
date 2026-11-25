"""パチンコの統計・テレメトリ記録の DDL。"""

PACHINKO_SPINS_DDL = """
    CREATE TABLE IF NOT EXISTS pachinko_spins (     -- パチンコの1回転ごとの記録（設計した当たり率と実績を比べる統計用）
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        ts       REAL    NOT NULL,
        omen     TEXT    NOT NULL,            -- 予告の色（'' は予告なし）
        tier     TEXT    NOT NULL,            -- リーチの格（'' はリーチなし）
        hit      INTEGER NOT NULL CHECK (hit IN (0, 1))             -- 当たったら 1
    )"""

PACHINKO_SETS_DDL = """
    CREATE TABLE IF NOT EXISTS pachinko_sets (      -- パチンコの1セット（100発）ごとの記録
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id  INTEGER NOT NULL,
        user_id   INTEGER NOT NULL,
        ts        REAL    NOT NULL,
        bet       INTEGER NOT NULL CHECK (bet >= 0),           -- そのセットの賭け金
        balls     INTEGER NOT NULL CHECK (balls >= 0),           -- 打った玉の数
        starts    INTEGER NOT NULL CHECK (starts >= 0),           -- スタートに入った玉の数（回転数）
        hits      INTEGER NOT NULL CHECK (hits >= 0),           -- 大当たりの始まりの数
        chains    INTEGER NOT NULL CHECK (chains >= 0),           -- 大当たりのラウンドの合計（連チャンを含む）
        chain_max INTEGER NOT NULL CHECK (chain_max >= 0),           -- 1回の大当たりでの最大連チャン
        mult      INTEGER NOT NULL CHECK (mult >= 0)            -- 払い戻しの倍数（上限をかけたあと。還元率レベルをかける前）
    )"""

PACHINKO_SPINS_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_pachinko_spins ON pachinko_spins (guild_id, ts)"""

PACHINKO_SETS_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_pachinko_sets ON pachinko_sets (guild_id, ts)"""

STATEMENTS: tuple[str, ...] = (
    PACHINKO_SPINS_DDL,
    PACHINKO_SETS_DDL,
    PACHINKO_SPINS_INDEX_DDL,
    PACHINKO_SETS_INDEX_DDL,
)
