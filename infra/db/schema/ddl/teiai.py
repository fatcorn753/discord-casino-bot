"""帝愛銀行・帝愛物産・預金・CDS・債券・帝愛共済・差押競売・闇市の DDL。"""

TEIAI_BANK_V1_DDL = """
    CREATE TABLE IF NOT EXISTS teiai_bank (        -- 帝愛銀行の手元（v1 互換: v3 で 4 表へ分離）
        guild_id        INTEGER PRIMARY KEY,
        cash            REAL NOT NULL DEFAULT 0,   -- すぐ払い出せる現金
        loaned          REAL NOT NULL DEFAULT 0,   -- 地下融資に回している額（時間がたつと現金に戻る）
        ts              REAL NOT NULL DEFAULT 0,   -- 融資の回収を、ここまで反映した時刻
        thermo_load     REAL NOT NULL DEFAULT 0,   -- サーモスタットの短期熱量 L
        thermo_capacity REAL NOT NULL DEFAULT 0,   -- サーモスタットの長期キャパシティ E（0 は未初期化。下限から始める）
        thermo_ts       REAL NOT NULL DEFAULT 0,   -- L・E を、ここまで冷ました時刻
        thermo_burned   REAL NOT NULL DEFAULT 0,   -- 熱浴へ消えた（観測中は、消えるはずだった）額の累計
        thermo_bailed   REAL NOT NULL DEFAULT 0,   -- 熱浴が肩代わりした（観測中は、するはずだった）額の累計
        trust_ts        REAL NOT NULL DEFAULT 0,   -- 帝愛信託の利息を、ここまで払った時刻（0 は未開始）
        trust_accrued   REAL NOT NULL DEFAULT 0,   -- まだ払っていない利息の端数
        trust_paid      REAL NOT NULL DEFAULT 0,   -- これまでに払った信託の利息の累計
        excess_unpaid   REAL NOT NULL DEFAULT 0,   -- 信託でも熱浴・共済でも払えなかった超過損失の累計
        rf              REAL NOT NULL DEFAULT 0,   -- 無リスク金利 r_f（1日あたり。0 は未開始で、基準の金利。#18）
        rf_ts           REAL NOT NULL DEFAULT 0,   -- r_f を、ここまで進めた時刻
        kyosai_div_ts   REAL NOT NULL DEFAULT 0,   -- 帝愛共済の配当を、最後に配った時刻（0 は未開始。#34）
        kyosai_paid     REAL NOT NULL DEFAULT 0,   -- これまでに配った共済の配当の累計
        kyosai_charged  REAL NOT NULL DEFAULT 0,   -- これまでに共済へ請求した超過損失の累計
        trust_premium   REAL                       -- 動議で決まった信託の上乗せ金利（NULL は設定の既定値）
    )"""

TEIAI_BANK_DDL = """
    CREATE TABLE IF NOT EXISTS teiai_bank (        -- 帝愛銀行の手元（サーバーごとに1行）
        guild_id INTEGER PRIMARY KEY,
        cash     REAL NOT NULL DEFAULT 0,          -- すぐ払い出せる現金
        loaned   REAL NOT NULL DEFAULT 0,          -- 地下融資に回している額（時間がたつと現金に戻る）
        ts       REAL NOT NULL DEFAULT 0           -- 融資の回収を、ここまで反映した時刻
    )"""

TEIAI_THERMOSTAT_DDL = """
    CREATE TABLE IF NOT EXISTS teiai_thermostat (  -- サーモスタット（熱浴）の状態（サーバーごとに1行）
        guild_id INTEGER PRIMARY KEY,
        load     REAL NOT NULL DEFAULT 0,
        capacity REAL NOT NULL DEFAULT 0,
        ts       REAL NOT NULL DEFAULT 0,
        burned   REAL NOT NULL DEFAULT 0,
        bailed   REAL NOT NULL DEFAULT 0,
        FOREIGN KEY (guild_id) REFERENCES teiai_bank (guild_id) ON DELETE CASCADE
    )"""

TEIAI_TRUST_DDL = """
    CREATE TABLE IF NOT EXISTS teiai_trust (       -- 帝愛信託の金利・利息・超過損失（サーバーごとに1行）
        guild_id      INTEGER PRIMARY KEY,
        rf            REAL NOT NULL DEFAULT 0,
        rf_ts         REAL NOT NULL DEFAULT 0,
        trust_ts      REAL NOT NULL DEFAULT 0,
        accrued       REAL NOT NULL DEFAULT 0,
        paid          REAL NOT NULL DEFAULT 0,
        excess_unpaid REAL NOT NULL DEFAULT 0,
        premium       REAL,
        FOREIGN KEY (guild_id) REFERENCES teiai_bank (guild_id) ON DELETE CASCADE
    )"""

KYOSAI_STATE_V3_DDL = """
    CREATE TABLE IF NOT EXISTS kyosai_state (      -- 帝愛共済の状態（v3 互換: v6 で house_units を追加）
        guild_id INTEGER PRIMARY KEY,
        div_ts   REAL NOT NULL DEFAULT 0,
        paid     REAL NOT NULL DEFAULT 0,
        charged  REAL NOT NULL DEFAULT 0
    )"""

KYOSAI_STATE_DDL = """
    CREATE TABLE IF NOT EXISTS kyosai_state (      -- 帝愛共済の配当・超過損失請求・帝愛持ち口の状態（サーバーごとに1行）
        guild_id    INTEGER PRIMARY KEY,
        div_ts      REAL    NOT NULL DEFAULT 0,
        paid        REAL    NOT NULL DEFAULT 0,
        charged     REAL    NOT NULL DEFAULT 0,
        house_units INTEGER NOT NULL DEFAULT 0 CHECK (house_units >= 0)
    )"""

TEIAI_HOUSE_V1_DDL = """
    CREATE TABLE IF NOT EXISTS teiai_house (       -- 帝愛物産の買取プール（v1 互換: v3 で 2 表へ分離）
        guild_id   INTEGER PRIMARY KEY,
        balance    INTEGER NOT NULL DEFAULT 0,     -- いまの残高
        skimmed    INTEGER NOT NULL DEFAULT 0,     -- 後方互換用の累計列（現在は未使用）
        grant_day  TEXT NOT NULL DEFAULT '',       -- 会社の買取枠（新しく出す分）を使った日
        grant_used INTEGER NOT NULL DEFAULT 0      -- その日に、買取枠から出した額
    )"""

TEIAI_HOUSE_DDL = """
    CREATE TABLE IF NOT EXISTS teiai_house (       -- 帝愛物産の買取プール（サーバーごとに1行）。/会社 の資金が出入りする
        guild_id INTEGER PRIMARY KEY,
        balance  INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
        skimmed  INTEGER NOT NULL DEFAULT 0 CHECK (skimmed >= 0)
    )"""

TEIAI_HOUSE_GRANTS_DDL = """
    CREATE TABLE IF NOT EXISTS teiai_house_grants ( -- 帝愛物産の日次買取枠の使用状況
        guild_id INTEGER NOT NULL,
        day      TEXT NOT NULL,
        used     INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0),
        PRIMARY KEY (guild_id, day),
        FOREIGN KEY (guild_id) REFERENCES teiai_house (guild_id) ON DELETE CASCADE
    )"""

TEIAI_DEPOSITS_V1_DDL = """
    CREATE TABLE IF NOT EXISTS teiai_deposits (    -- 帝愛への預金（v1 互換: v7 で accounts FK と CHECK を追加）
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        amount   INTEGER NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    )"""

TEIAI_DEPOSITS_DDL = """
    CREATE TABLE IF NOT EXISTS teiai_deposits (    -- 帝愛への預金（人ごと）
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        amount   INTEGER NOT NULL CHECK (amount >= 0),
        PRIMARY KEY (guild_id, user_id),
        FOREIGN KEY (guild_id, user_id) REFERENCES accounts (guild_id, user_id) ON DELETE CASCADE
    )"""

CDS_V1_DDL = """
    CREATE TABLE IF NOT EXISTS cds (               -- CDS（v1 互換: v7 で accounts FK と CHECK を追加）
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id  INTEGER NOT NULL,
        buyer_id  INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        stake     INTEGER NOT NULL,
        odds      REAL    NOT NULL,
        ts        REAL    NOT NULL,
        expires   REAL    NOT NULL,
        status    TEXT    NOT NULL DEFAULT 'active',
        paid      INTEGER NOT NULL DEFAULT 0
    )"""

CDS_DDL = """
    CREATE TABLE IF NOT EXISTS cds (               -- CDS（破綻保証）。買い手が掛け金を払い、対象が強制破産すると配当（自分への CDS は借金を肩代わり）
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id  INTEGER NOT NULL,
        buyer_id  INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        stake     INTEGER NOT NULL CHECK (stake > 0),               -- 掛け金
        odds      REAL    NOT NULL,               -- 買ったときのオッズ（配当の見込み = 掛け金 × オッズ）
        ts        REAL    NOT NULL,
        expires   REAL    NOT NULL,
        status    TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paid', 'expired', 'void')),   -- active / paid / expired / void
        paid      INTEGER NOT NULL DEFAULT 0 CHECK (paid >= 0),
        FOREIGN KEY (guild_id, buyer_id) REFERENCES accounts (guild_id, user_id) ON DELETE CASCADE,
        FOREIGN KEY (guild_id, target_id) REFERENCES accounts (guild_id, user_id) ON DELETE CASCADE
    )"""

CDS_ACTIVE_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_cds_active ON cds (guild_id, target_id, expires) WHERE status = 'active'"""

BONDS_V1_DDL = """
    CREATE TABLE IF NOT EXISTS bonds (              -- 債券（v1 互換: v6 で bond_default_checks へ checked を分離）
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id  INTEGER NOT NULL,
        user_id   INTEGER NOT NULL,
        kind      TEXT    NOT NULL,           -- normal / junk
        principal INTEGER NOT NULL,
        due       REAL    NOT NULL,           -- 満期（epoch秒）
        status    TEXT    NOT NULL DEFAULT 'active',   -- active / paid / defaulted / cancelled
        paid      INTEGER NOT NULL DEFAULT 0, -- 満期・売却で戻した額
        ts        REAL    NOT NULL,           -- 買った時刻
        checked   REAL,                       -- ジャンク債の倒産抽選を、ここまで済ませた時刻（NULL なら買った時刻）
        rate      REAL                        -- 買ったときに決まった利率（満期までの ％。#18。NULL は種類の基準の利率）
    )"""

BONDS_V6_DDL = """
    CREATE TABLE IF NOT EXISTS bonds (              -- 債券（v6 互換: v7 で accounts FK を追加）
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id  INTEGER NOT NULL,
        user_id   INTEGER NOT NULL,
        kind      TEXT    NOT NULL,
        principal INTEGER NOT NULL CHECK (principal > 0),
        due       REAL    NOT NULL,
        status    TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paid', 'defaulted', 'cancelled')),
        paid      INTEGER NOT NULL DEFAULT 0 CHECK (paid >= 0),
        ts        REAL    NOT NULL,
        rate      REAL
    )"""

BONDS_DDL = """
    CREATE TABLE IF NOT EXISTS bonds (              -- 債券（買った分ごとに1行）。元本は買った時点で残高から預かる
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id  INTEGER NOT NULL,
        user_id   INTEGER NOT NULL,
        kind      TEXT    NOT NULL,
        principal INTEGER NOT NULL CHECK (principal > 0),
        due       REAL    NOT NULL,
        status    TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paid', 'defaulted', 'cancelled')),
        paid      INTEGER NOT NULL DEFAULT 0 CHECK (paid >= 0),
        ts        REAL    NOT NULL,
        rate      REAL,
        FOREIGN KEY (guild_id, user_id) REFERENCES accounts (guild_id, user_id) ON DELETE CASCADE
    )"""

BONDS_ACTIVE_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_bonds_active ON bonds (guild_id, user_id, due) WHERE status = 'active'"""

BOND_DEFAULT_CHECKS_DDL = """
    CREATE TABLE IF NOT EXISTS bond_default_checks ( -- ジャンク債の最終倒産抽選時刻
        bond_id    INTEGER PRIMARY KEY,
        checked_at REAL    NOT NULL,
        FOREIGN KEY (bond_id) REFERENCES bonds (id) ON DELETE CASCADE
    )"""

KYOSAI_UNITS_V1_DDL = """
    CREATE TABLE IF NOT EXISTS kyosai_units (      -- 帝愛共済の口（v1 互換: v6 で user_id=0 を kyosai_state.house_units へ分離）
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        units    INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    )"""

KYOSAI_UNITS_DDL = """
    CREATE TABLE IF NOT EXISTS kyosai_units (      -- 帝愛共済のプレイヤー保有口（帝愛持ち口は kyosai_state.house_units）
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL CHECK (user_id > 0),
        units    INTEGER NOT NULL DEFAULT 0 CHECK (units >= 0),
        PRIMARY KEY (guild_id, user_id)
    )"""

KYOSAI_AUCTIONS_DDL = """
    CREATE TABLE IF NOT EXISTS kyosai_auctions (   -- 帝愛の持ち口の競り（入札で値段が決まる。#93）
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL,
        units      INTEGER NOT NULL CHECK (units > 0),                -- 出した口数（帝愛の持ち口から、落札のときに移す）
        top_bid    INTEGER NOT NULL DEFAULT 0 CHECK (top_bid >= 0),      -- いまの最高値（入札した人の手元から預かっている）
        top_bidder INTEGER,
        ends       REAL    NOT NULL,
        status     TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'sold', 'unsold'))  -- open / sold / unsold
    )"""

KYOSAI_OFFERS_DDL = """
    CREATE TABLE IF NOT EXISTS kyosai_offers (     -- 口の譲渡の申し出（売り手 → 買い手。買い手が受けると成立）
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        seller   INTEGER NOT NULL,
        buyer    INTEGER NOT NULL,
        units    INTEGER NOT NULL CHECK (units > 0),
        price    INTEGER NOT NULL CHECK (price >= 0),                  -- 全部でいくら
        expires  REAL    NOT NULL,
        status   TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'cancelled')),   -- open / done / cancelled
        done_at  REAL    NOT NULL DEFAULT 0         -- 成立した時刻（同じ2人の、24時間の取引額の上限に使う）
    )"""

KYOSAI_MOTIONS_V1_DDL = """
    CREATE TABLE IF NOT EXISTS kyosai_motions (    -- 動議（v1 互換: v6 で UI 列を discord_posts へ分離）
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL,
        proposer   INTEGER NOT NULL,
        premium    REAL    NOT NULL,
        created    REAL    NOT NULL DEFAULT 0,      -- 出した時刻（同じ人のクールダウンに使う）
        closes     REAL    NOT NULL,
        status     TEXT    NOT NULL DEFAULT 'open', -- open / passed / rejected
        yes        INTEGER NOT NULL DEFAULT 0,      -- 締めたときの賛成の口数
        no         INTEGER NOT NULL DEFAULT 0,
        channel_id INTEGER NOT NULL DEFAULT 0,      -- 告知の投稿（締めたときに結果へ書き換え、ボタンを外す）
        message_id INTEGER NOT NULL DEFAULT 0
    )"""

KYOSAI_MOTIONS_DDL = """
    CREATE TABLE IF NOT EXISTS kyosai_motions (    -- 動議（信託の上乗せ金利を premium にする）。いくつも並行できる（#94）
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        proposer INTEGER NOT NULL,
        premium  REAL    NOT NULL,
        created  REAL    NOT NULL DEFAULT 0,
        closes   REAL    NOT NULL,
        status   TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'passed', 'rejected')),
        yes      INTEGER NOT NULL DEFAULT 0,
        no       INTEGER NOT NULL DEFAULT 0
    )"""

KYOSAI_VOTES_DDL = """
    CREATE TABLE IF NOT EXISTS kyosai_votes (       -- 動議への投票（締めるときに、そのときの口数で数える）
        motion_id INTEGER NOT NULL,
        user_id   INTEGER NOT NULL,
        yes       INTEGER NOT NULL CHECK (yes IN (0, 1)),
        PRIMARY KEY (motion_id, user_id),
        FOREIGN KEY (motion_id) REFERENCES kyosai_motions (id) ON DELETE CASCADE
    )"""

SEIZED_ITEMS_DDL = """
    CREATE TABLE IF NOT EXISTS seized_items (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id  INTEGER NOT NULL,
        item_id   TEXT NOT NULL,
        qty       INTEGER NOT NULL CHECK (qty >= 0),
        listed_at REAL NOT NULL
    )"""

SEIZED_ITEMS_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS seized_items_item ON seized_items (guild_id, item_id, listed_at)"""

TEIAI_MARKET_DDL = """
    CREATE TABLE IF NOT EXISTS teiai_market (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id    INTEGER NOT NULL,
        day         REAL NOT NULL,
        item_id     TEXT NOT NULL,
        mode        TEXT NOT NULL,
        qty         INTEGER NOT NULL CHECK (qty >= 0),
        start_price INTEGER NOT NULL CHECK (start_price > 0),
        end_price   INTEGER NOT NULL CHECK (end_price > 0),
        black       INTEGER NOT NULL CHECK (black IN (0, 1)),
        listed_at   REAL NOT NULL,
        expires_at  REAL NOT NULL,
        FOREIGN KEY (guild_id, day) REFERENCES teiai_market_days (guild_id, day) ON DELETE CASCADE
    )"""

TEIAI_MARKET_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS teiai_market_day ON teiai_market (guild_id, day)"""

TEIAI_DEBTS_DDL = """
    CREATE TABLE IF NOT EXISTS teiai_debts (       -- 帝愛からの借金（シニア債権）
        guild_id    INTEGER NOT NULL,
        user_id     INTEGER NOT NULL,
        debt        INTEGER NOT NULL DEFAULT 0 CHECK (debt >= 0),
        principal   INTEGER NOT NULL DEFAULT 0 CHECK (principal >= 0),
        interest_at REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id),
        FOREIGN KEY (guild_id, user_id) REFERENCES accounts (guild_id, user_id) ON DELETE CASCADE
    )"""

TEIAI_GRACE_DDL = """
    CREATE TABLE IF NOT EXISTS teiai_grace (       -- 破産猶予とペナルティ状態
        guild_id    INTEGER NOT NULL,
        user_id     INTEGER NOT NULL,
        grace_until REAL,
        penalty     REAL NOT NULL DEFAULT 0 CHECK (penalty >= 0),
        penalty_at  REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id),
        FOREIGN KEY (guild_id, user_id) REFERENCES accounts (guild_id, user_id) ON DELETE CASCADE
    )"""

TEIAI_GRACE_ACTIVE_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_teiai_grace_active ON teiai_grace (guild_id, grace_until) WHERE grace_until IS NOT NULL"""

TEIAI_BANKRUPTCIES_DDL = """
    CREATE TABLE IF NOT EXISTS teiai_bankruptcies (  -- 自己破産・ブラックリスト状態
        guild_id        INTEGER NOT NULL,
        user_id         INTEGER NOT NULL,
        bankrupt_at     REAL NOT NULL,
        blacklist_until REAL,
        PRIMARY KEY (guild_id, user_id),
        FOREIGN KEY (guild_id, user_id) REFERENCES accounts (guild_id, user_id) ON DELETE CASCADE
    )"""

TEIAI_ESCAPES_DDL = """
    CREATE TABLE IF NOT EXISTS teiai_escapes (     -- 海外逃亡の試行記録
        guild_id        INTEGER NOT NULL,
        user_id         INTEGER NOT NULL,
        last_attempt_at REAL,
        tries           INTEGER NOT NULL DEFAULT 0 CHECK (tries >= 0),
        PRIMARY KEY (guild_id, user_id),
        FOREIGN KEY (guild_id, user_id) REFERENCES accounts (guild_id, user_id) ON DELETE CASCADE
    )"""

TEIAI_MARKET_DAYS_DDL = """
    CREATE TABLE IF NOT EXISTS teiai_market_days (
        guild_id INTEGER NOT NULL,
        day      REAL NOT NULL,
        PRIMARY KEY (guild_id, day)
    )"""

STATEMENTS: tuple[str, ...] = (
    TEIAI_DEBTS_DDL,
    TEIAI_GRACE_DDL,
    TEIAI_GRACE_ACTIVE_INDEX_DDL,
    TEIAI_BANKRUPTCIES_DDL,
    TEIAI_ESCAPES_DDL,
    TEIAI_BANK_DDL,
    TEIAI_THERMOSTAT_DDL,
    TEIAI_TRUST_DDL,
    KYOSAI_STATE_DDL,
    TEIAI_HOUSE_DDL,
    TEIAI_HOUSE_GRANTS_DDL,
    TEIAI_DEPOSITS_DDL,
    CDS_DDL,
    CDS_ACTIVE_INDEX_DDL,
    BONDS_DDL,
    BONDS_ACTIVE_INDEX_DDL,
    BOND_DEFAULT_CHECKS_DDL,
    KYOSAI_UNITS_DDL,
    KYOSAI_AUCTIONS_DDL,
    KYOSAI_OFFERS_DDL,
    KYOSAI_MOTIONS_DDL,
    KYOSAI_VOTES_DDL,
    SEIZED_ITEMS_DDL,
    SEIZED_ITEMS_INDEX_DDL,
    TEIAI_MARKET_DAYS_DDL,
    TEIAI_MARKET_DDL,
    TEIAI_MARKET_INDEX_DDL,
)
