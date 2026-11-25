"""会社・出資の DDL。"""

COMPANIES_V1_DDL = """
    CREATE TABLE IF NOT EXISTS companies (         -- 会社（v1 互換: v7 で accounts FK と CHECK を追加）
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
    )"""

COMPANIES_DDL = """
    CREATE TABLE IF NOT EXISTS companies (         -- 会社（1人1社。事業所は1社に1つ）
        guild_id      INTEGER NOT NULL,
        user_id       INTEGER NOT NULL,
        name          TEXT NOT NULL,
        kind          TEXT NOT NULL,                 -- 事業所の種類（catalogs/company.yaml の kinds）
        level         INTEGER NOT NULL DEFAULT 1 CHECK (level >= 1),    -- 事業所のレベル
        funds         INTEGER NOT NULL DEFAULT 0 CHECK (funds >= 0),    -- 会社資金（個人の残高とは別）
        stock         REAL NOT NULL DEFAULT 0 CHECK (stock >= 0),       -- 倉庫の在庫（買取価格 1.0 倍のときの額）
        stock_ts      REAL NOT NULL,                 -- 在庫を、ここまで積んだ時刻
        upkeep_day    TEXT NOT NULL,                 -- 維持費を、この日（現地の日付）まで精算した
        unpaid_days   INTEGER NOT NULL DEFAULT 0 CHECK (unpaid_days >= 0),    -- 維持費を払えなかった日が、続いている日数
        sold_total    INTEGER NOT NULL DEFAULT 0 CHECK (sold_total >= 0),    -- これまでの出荷で、会社に入った額の合計
        created       REAL NOT NULL,
        dividend_rate REAL NOT NULL DEFAULT 0 CHECK (dividend_rate >= 0),      -- 出荷の買取代金のうち、出資者へ配る割合（#71）
        PRIMARY KEY (guild_id, user_id),
        FOREIGN KEY (guild_id, user_id) REFERENCES accounts (guild_id, user_id) ON DELETE CASCADE
    )"""

COMPANY_STAKES_V1_DDL = """
    CREATE TABLE IF NOT EXISTS company_stakes (    -- 会社への出資（v1 互換: v7 で FK と CHECK を追加）
        guild_id    INTEGER NOT NULL,
        owner_id    INTEGER NOT NULL,
        investor_id INTEGER NOT NULL,
        amount      INTEGER NOT NULL,
        dividends   INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, owner_id, investor_id)
    )"""

COMPANY_STAKES_DDL = """
    CREATE TABLE IF NOT EXISTS company_stakes (    -- ほかの人の会社への出資（#71）
        guild_id    INTEGER NOT NULL,
        owner_id    INTEGER NOT NULL,              -- 会社の社長
        investor_id INTEGER NOT NULL,
        amount      INTEGER NOT NULL CHECK (amount > 0),              -- 出資額の合計（払い戻しはできない）
        dividends   INTEGER NOT NULL DEFAULT 0 CHECK (dividends >= 0),    -- これまでに受け取った配当の合計
        PRIMARY KEY (guild_id, owner_id, investor_id),
        CHECK (owner_id != investor_id),
        FOREIGN KEY (guild_id, owner_id) REFERENCES companies (guild_id, user_id) ON DELETE CASCADE,
        FOREIGN KEY (guild_id, investor_id) REFERENCES accounts (guild_id, user_id) ON DELETE CASCADE
    )"""

STATEMENTS: tuple[str, ...] = (
    COMPANIES_DDL,
    COMPANY_STAKES_DDL,
)
