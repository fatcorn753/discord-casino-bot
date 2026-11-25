"""ショップ・持ち物・プロフィール・ニックネーム設定の DDL。"""

INVENTORY_DDL = """
    CREATE TABLE IF NOT EXISTS inventory (
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        item_id  TEXT    NOT NULL,
        qty      INTEGER NOT NULL CHECK (qty >= 0),
        PRIMARY KEY (guild_id, user_id, item_id)
    )"""

SHOP_STOCK_DDL = """
    CREATE TABLE IF NOT EXISTS shop_stock (        -- ショップの在庫（買える数）。1人ずつ別々。時間で補充される（domain/shop.py の STOCK_RULES）
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        item_id  TEXT    NOT NULL,
        stock    REAL    NOT NULL CHECK (stock >= 0),                  -- updated の時点の在庫（小数）
        updated  REAL    NOT NULL,
        PRIMARY KEY (guild_id, user_id, item_id)
    )"""

PROFILE_DDL = """
    CREATE TABLE IF NOT EXISTS profile (         -- v1 互換定義（v4 で user_equipment・user_buffs へ縦持ち分離）
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

USER_EQUIPMENT_DDL = """
    CREATE TABLE IF NOT EXISTS user_equipment (  -- 装備中の装飾・演出（スロットごとに縦持ち 5NF）
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        slot     TEXT NOT NULL CHECK (slot IN ('title', 'card_color', 'card_icon', 'skin_dice', 'skin_table')),
        item_id  TEXT NOT NULL,
        PRIMARY KEY (guild_id, user_id, slot)
    )"""

USER_BUFFS_DDL = """
    CREATE TABLE IF NOT EXISTS user_buffs (      -- 消耗品の使用回数ストック（効果名ごとに縦持ち 5NF）
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        buff     TEXT NOT NULL CHECK (buff IN ('daily_boost', 'insurance')),
        charges  INTEGER NOT NULL CHECK (charges > 0),
        PRIMARY KEY (guild_id, user_id, buff)
    )"""

NICK_PREFS_DDL = """
    CREATE TABLE IF NOT EXISTS nick_prefs (
        guild_id      INTEGER NOT NULL,
        user_id       INTEGER NOT NULL,
        original_nick TEXT,          -- 称号を付ける前のニックネーム（NULL はニックネームなし）
        applied_nick  TEXT,          -- Bot が設定したニックネーム（NULL は未適用）
        enabled       INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),   -- 1: 称号の表示を、自分で選んだ人。0: 身分の【地下労働者】だけを、Bot が付けている人
        PRIMARY KEY (guild_id, user_id)
    )"""

STATEMENTS: tuple[str, ...] = (
    INVENTORY_DDL,
    SHOP_STOCK_DDL,
    USER_EQUIPMENT_DDL,
    USER_BUFFS_DDL,
    NICK_PREFS_DDL,
)
