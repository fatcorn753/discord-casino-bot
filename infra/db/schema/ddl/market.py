"""証券市場・暗号資産・信用取引・オプション・イベント・決算の DDL。"""

MARKET_PRICES_DDL = """
    CREATE TABLE IF NOT EXISTS market_prices (     -- 証券の価格（銘柄ごと）。全サーバー共通
        symbol TEXT    NOT NULL DEFAULT 'cs',
        ts     REAL    NOT NULL,
        price  INTEGER NOT NULL,
        event  INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (symbol, ts)
    )"""

MARKET_FAIR_DDL = """
    CREATE TABLE IF NOT EXISTS market_fair (       -- 裏の理論価格（値幅制限に関係なく動く）と、ストップ高・安の張り付き。全サーバー共通
        symbol TEXT PRIMARY KEY,
        fair   REAL NOT NULL,
        pin    INTEGER NOT NULL DEFAULT 0,          -- 1 ストップ高に張り付き / -1 ストップ安に張り付き / 0 なし
        ts     REAL NOT NULL
    )"""

CRYPTO_DDL = """
    CREATE TABLE IF NOT EXISTS crypto (            -- 銘柄ごとの現物の保有
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        symbol   TEXT    NOT NULL DEFAULT 'cs',
        units    INTEGER NOT NULL CHECK (units >= 0),        -- 保有する口数（1000口 = 1枚）
        cost     INTEGER NOT NULL CHECK (cost >= 0),        -- 買うのにかかった総額（損益の表示用）
        PRIMARY KEY (guild_id, user_id, symbol)
    )"""

CRYPTO_SHORT_DDL = """
    CREATE TABLE IF NOT EXISTS crypto_short (      -- v1 互換定義（v5 で margin_positions へ統合）
        guild_id    INTEGER NOT NULL,
        user_id     INTEGER NOT NULL,
        symbol      TEXT    NOT NULL DEFAULT 'cs',
        units       INTEGER NOT NULL,         -- 売り建てた口数
        entry_value REAL NOT NULL,            -- 建てたときの総額（口数×価格）。損益 = これ − いまの総額
        margin      INTEGER NOT NULL,         -- 預けた担保
        interest_at REAL NOT NULL DEFAULT 0,  -- 信用金利（貸株料）を、ここまで数えた時刻
        accrued     REAL NOT NULL DEFAULT 0,  -- 数えたが、まだ担保から引いていない金利（小数）
        PRIMARY KEY (guild_id, user_id, symbol)
    )"""

CRYPTO_LONG_DDL = """
    CREATE TABLE IF NOT EXISTS crypto_long (       -- v1 互換定義（v5 で margin_positions へ統合）
        guild_id    INTEGER NOT NULL,
        user_id     INTEGER NOT NULL,
        symbol      TEXT    NOT NULL,
        units       INTEGER NOT NULL,         -- 買い建てた口数
        entry_value REAL NOT NULL,            -- 建てたときの総額（口数×価格）。損益 = いまの総額 − これ
        margin      INTEGER NOT NULL,         -- 預けた担保
        interest_at REAL NOT NULL DEFAULT 0,  -- 信用金利（借入金利）を、ここまで数えた時刻
        accrued     REAL NOT NULL DEFAULT 0,  -- 数えたが、まだ担保から引いていない金利（小数）
        PRIMARY KEY (guild_id, user_id, symbol)
    )"""

MARGIN_POSITIONS_DDL = """
    CREATE TABLE IF NOT EXISTS margin_positions (  -- 銘柄・売買方向ごとの信用建玉（空売り・信用買い統合）
        guild_id    INTEGER NOT NULL,
        user_id     INTEGER NOT NULL,
        symbol      TEXT    NOT NULL,
        side        TEXT    NOT NULL CHECK (side IN ('long', 'short')),
        units       INTEGER NOT NULL CHECK (units > 0),
        entry_value REAL    NOT NULL,
        margin      INTEGER NOT NULL CHECK (margin >= 0),
        interest_at REAL    NOT NULL DEFAULT 0,
        accrued     REAL    NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id, symbol, side)
    )"""

OPTIONS_V1_DDL = """
    CREATE TABLE IF NOT EXISTS options (           -- オプション（v1 互換: v6 でペア・裸売り証拠金の子表を分離）
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        kind     TEXT    NOT NULL,           -- call / put
        symbol   TEXT    NOT NULL,
        strike   REAL    NOT NULL,           -- 権利行使価格（買った時点の価格の ±5%）
        units    INTEGER NOT NULL,           -- 口数（1000口 = 1枚）
        premium  INTEGER NOT NULL,           -- 払った掛け金（最大の損失）
        expiry   REAL    NOT NULL,           -- 満期（epoch秒）
        spot     REAL    NOT NULL,           -- 買った時点の価格
        ts       REAL    NOT NULL,
        status   TEXT    NOT NULL DEFAULT 'active',   -- active / exercised / expired / sold
        paid     INTEGER NOT NULL DEFAULT 0, -- 満期・売却で受け取った額
        final    REAL,                       -- 満期の価格（ペア・ワラントは A の価格）
        symbol2  TEXT,                       -- ペア・ワラントの、比べる相手の銘柄（なければ NULL）
        spot2    REAL,                       -- 買った時点の、相手の価格
        side     TEXT    NOT NULL DEFAULT 'long',   -- long（買い）/ short（裸売り。帝愛に売って、プレミアムを先にもらう）
        margin   INTEGER NOT NULL DEFAULT 0  -- 裸売りで預けた証拠金（買いは 0）
    )"""

OPTIONS_V6_DDL = """
    CREATE TABLE IF NOT EXISTS options (           -- オプション（v6 互換: v7 で accounts FK を追加）
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        kind     TEXT    NOT NULL,
        symbol   TEXT    NOT NULL,
        strike   REAL    NOT NULL,
        units    INTEGER NOT NULL CHECK (units > 0),
        premium  INTEGER NOT NULL CHECK (premium >= 0),
        expiry   REAL    NOT NULL,
        spot     REAL    NOT NULL,
        ts       REAL    NOT NULL,
        status   TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'exercised', 'expired', 'sold')),
        paid     INTEGER NOT NULL DEFAULT 0 CHECK (paid >= 0),
        final    REAL,
        side     TEXT    NOT NULL DEFAULT 'long' CHECK (side IN ('long', 'short'))
    )"""

OPTIONS_DDL = """
    CREATE TABLE IF NOT EXISTS options (           -- オプション（爆騰コール・暴落プット・ペア・裸売りの共通ヘッダ）
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        kind     TEXT    NOT NULL,
        symbol   TEXT    NOT NULL,
        strike   REAL    NOT NULL,
        units    INTEGER NOT NULL CHECK (units > 0),
        premium  INTEGER NOT NULL CHECK (premium >= 0),
        expiry   REAL    NOT NULL,
        spot     REAL    NOT NULL,
        ts       REAL    NOT NULL,
        status   TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'exercised', 'expired', 'sold')),
        paid     INTEGER NOT NULL DEFAULT 0 CHECK (paid >= 0),
        final    REAL,
        side     TEXT    NOT NULL DEFAULT 'long' CHECK (side IN ('long', 'short')),
        FOREIGN KEY (guild_id, user_id) REFERENCES accounts (guild_id, user_id) ON DELETE CASCADE
    )"""

OPTIONS_ACTIVE_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_options_active ON options (guild_id, expiry) WHERE status = 'active'"""

OPTION_PAIRS_DDL = """
    CREATE TABLE IF NOT EXISTS option_pairs (      -- ペア・ワラント用の対比銘柄・購入時点価格
        option_id INTEGER PRIMARY KEY,
        symbol2   TEXT    NOT NULL,
        spot2     REAL    NOT NULL,
        FOREIGN KEY (option_id) REFERENCES options (id) ON DELETE CASCADE
    )"""

OPTION_SHORT_MARGINS_DDL = """
    CREATE TABLE IF NOT EXISTS option_short_margins ( -- 裸売りオプションの預託証拠金
        option_id INTEGER PRIMARY KEY,
        margin    INTEGER NOT NULL CHECK (margin > 0),
        FOREIGN KEY (option_id) REFERENCES options (id) ON DELETE CASCADE
    )"""

MARKET_EVENTS_DDL = """
    CREATE TABLE IF NOT EXISTS market_events (     -- 市場イベント（ボラティリティ予告）。市場は全サーバー共通なので、サーバーに属さない
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol      TEXT NOT NULL,
        announce_ts REAL NOT NULL,                -- 予告した時刻
        start_ts    REAL NOT NULL,                -- 値動きが大きくなり始める時刻
        end_ts      REAL NOT NULL,
        factor      REAL NOT NULL                 -- 値動きの大きさ σ の倍率
    )"""

EARNINGS_DDL = """
    CREATE TABLE IF NOT EXISTS earnings (           -- 定期決算（毎週火曜13:30）。市場は全サーバー共通なので、サーバーに属さない
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol      TEXT NOT NULL,
        announce_at REAL NOT NULL,                -- 予告の時刻（月曜9:00）
        release_at  REAL NOT NULL,                -- 発表の時刻
        direction   INTEGER NOT NULL CHECK (direction IN (-1, 1)),             -- 1 好決算 / -1 悪決算（発表まで、だれにも分からない）
        size        REAL NOT NULL,                -- 理論価格の動きの大きさ
        announced   INTEGER NOT NULL DEFAULT 0 CHECK (announced IN (0, 1)),
        released    INTEGER NOT NULL DEFAULT 0 CHECK (released IN (0, 1))
    )"""

EARNINGS_MEMOS_DDL = """
    CREATE TABLE IF NOT EXISTS earnings_memos (     -- 届けた裏情報メモ（1つの決算に、1人1回まで）
        event_id INTEGER NOT NULL,
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        hint     INTEGER NOT NULL CHECK (hint IN (-1, 1)),                -- 噂の方向（本当とは限らない）
        PRIMARY KEY (event_id, guild_id, user_id),
        FOREIGN KEY (event_id) REFERENCES earnings (id) ON DELETE CASCADE
    )"""

EARNINGS_SHOP_DDL = """
    CREATE TABLE IF NOT EXISTS earnings_shop (      -- 帝愛市で売った裏情報メモ（1つの決算に、1人1枚・サーバーごとに数が限られる）
        event_id INTEGER NOT NULL,
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        hint     INTEGER NOT NULL CHECK (hint IN (-1, 1)),
        price    INTEGER NOT NULL CHECK (price >= 0),
        ts       REAL NOT NULL,
        PRIMARY KEY (event_id, guild_id, user_id),
        FOREIGN KEY (event_id) REFERENCES earnings (id) ON DELETE CASCADE
    )"""

STATEMENTS: tuple[str, ...] = (
    MARKET_PRICES_DDL,
    MARKET_FAIR_DDL,
    CRYPTO_DDL,
    MARGIN_POSITIONS_DDL,
    OPTIONS_DDL,
    OPTIONS_ACTIVE_INDEX_DDL,
    OPTION_PAIRS_DDL,
    OPTION_SHORT_MARGINS_DDL,
    MARKET_EVENTS_DDL,
    EARNINGS_DDL,
    EARNINGS_MEMOS_DDL,
    EARNINGS_SHOP_DDL,
)
