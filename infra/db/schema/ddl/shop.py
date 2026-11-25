"""ショップ・持ち物・プロフィール・ニックネーム設定の DDL。"""

INVENTORY_DDL = """
    CREATE TABLE IF NOT EXISTS inventory (
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        item_id  TEXT    NOT NULL,
        qty      INTEGER NOT NULL,
        PRIMARY KEY (guild_id, user_id, item_id)
    )"""

SHOP_STOCK_DDL = """
    CREATE TABLE IF NOT EXISTS shop_stock (        -- ショップの在庫（買える数）。1人ずつ別々。時間で補充される（domain/shop.py の STOCK_RULES）
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        item_id  TEXT    NOT NULL,
        stock    REAL    NOT NULL,                  -- updated の時点の在庫（小数）
        updated  REAL    NOT NULL,
        PRIMARY KEY (guild_id, user_id, item_id)
    )"""

PROFILE_DDL = """
    CREATE TABLE IF NOT EXISTS profile (
        guild_id    INTEGER NOT NULL,
        user_id     INTEGER NOT NULL,
        title       TEXT,
        card_color  TEXT,
        card_icon   TEXT,
        skin_dice   TEXT,     -- 装備中の演出の変更品（チンチロ用）
        skin_table  TEXT,     -- 装備中の演出の変更品（パチンコ・ブラックジャック用）
        daily_boost INTEGER NOT NULL DEFAULT 0,
        insurance   INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    )"""

NICK_PREFS_DDL = """
    CREATE TABLE IF NOT EXISTS nick_prefs (
        guild_id      INTEGER NOT NULL,
        user_id       INTEGER NOT NULL,
        original_nick TEXT,          -- 称号を付ける前のニックネーム（NULL はニックネームなし）
        applied_nick  TEXT,          -- Bot が設定したニックネーム（NULL は未適用）
        enabled       INTEGER NOT NULL DEFAULT 1,   -- 1: 称号の表示を、自分で選んだ人。0: 身分の【地下労働者】だけを、Bot が付けている人
        PRIMARY KEY (guild_id, user_id)
    )"""

STATEMENTS: tuple[str, ...] = (
    INVENTORY_DDL,
    SHOP_STOCK_DDL,
    PROFILE_DDL,
    NICK_PREFS_DDL,
)
