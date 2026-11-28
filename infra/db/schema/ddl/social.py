"""個人間融資・P2P市場・クイズ・抽選・投票・配信・地下闘技場の DDL。"""

import random


def new_quiz_order(count: int = 4) -> list[int]:
    """クイズの選択肢の表示順（添字を並べ替えたもの）。登録時に1回だけ決めて固定する。"""
    order = list(range(count))
    random.SystemRandom().shuffle(order)
    return order


LOANS_V1_DDL = """
    CREATE TABLE IF NOT EXISTS loans (              -- 貸し借り（v1 互換: v6 で約定条件・UI 列を分離）
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id      INTEGER NOT NULL,
        lender_id     INTEGER NOT NULL,
        borrower_id   INTEGER NOT NULL,
        principal     INTEGER NOT NULL,
        rate_tenths   INTEGER NOT NULL,           -- 日利（0.1%を1とした整数。20 なら 2.0%/日）
        term_days     INTEGER NOT NULL,
        status        TEXT    NOT NULL,           -- offer / active / overdue / repaid / declined / cancelled / expired
        created       REAL    NOT NULL,
        offer_expires REAL    NOT NULL,           -- 承諾の期限
        accepted_at   REAL,
        due           REAL,
        paid          INTEGER NOT NULL DEFAULT 0, -- これまでの返済の合計
        reminded      INTEGER NOT NULL DEFAULT 0, -- 期限前の知らせを出したか
        channel_id    INTEGER NOT NULL DEFAULT 0,
        message_id    INTEGER NOT NULL DEFAULT 0
    )"""

LOANS_DDL = """
    CREATE TABLE IF NOT EXISTS loans (              -- 個人間融資（募集時は lender_id が NULL）
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id      INTEGER NOT NULL,
        lender_id     INTEGER CHECK (lender_id IS NULL OR lender_id > 0),
        borrower_id   INTEGER NOT NULL CHECK (borrower_id > 0),
        principal     INTEGER NOT NULL CHECK (principal > 0),
        rate_tenths   INTEGER NOT NULL CHECK (rate_tenths >= 0),
        term_days     INTEGER NOT NULL CHECK (term_days > 0),
        status        TEXT    NOT NULL CHECK (
            status IN ('request', 'offer', 'active', 'overdue', 'repaid', 'declined', 'cancelled', 'expired', 'discharged')
        ),
        created       REAL    NOT NULL,
        offer_expires REAL    NOT NULL
    )"""

LOAN_TERMS_DDL = """
    CREATE TABLE IF NOT EXISTS loan_terms (         -- 成立済み貸付の約定期間・返済額・通知状態
        loan_id     INTEGER PRIMARY KEY,
        accepted_at REAL    NOT NULL,
        due         REAL    NOT NULL,
        paid        INTEGER NOT NULL DEFAULT 0 CHECK (paid >= 0),
        reminded    INTEGER NOT NULL DEFAULT 0 CHECK (reminded IN (0, 1)),
        FOREIGN KEY (loan_id) REFERENCES loans (id) ON DELETE CASCADE
    )"""

LOANS_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_loans ON loans (guild_id, borrower_id, status)"""

MARKET_LISTINGS_V1_DDL = """
    CREATE TABLE IF NOT EXISTS market_listings (   -- P2P 市場（v1 互換: v6 で入札・成約・UI 列を分離）
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL,
        seller_id  INTEGER NOT NULL,
        kind       TEXT NOT NULL,                  -- item / horse
        ref        TEXT NOT NULL,                  -- 品のID、または馬の番号
        qty        INTEGER NOT NULL DEFAULT 1,
        mode       TEXT NOT NULL,                  -- fixed（定価）/ auction（競り）
        price      INTEGER NOT NULL,               -- 定価、または競りの開始価格（まとめて1つの値段）
        bid        INTEGER NOT NULL DEFAULT 0,     -- いまの最高入札額（預かり中）
        bidder_id  INTEGER NOT NULL DEFAULT 0,
        created    REAL NOT NULL,
        end_at     REAL NOT NULL,                  -- 競りの締切 / 定価の有効期限
        status     TEXT NOT NULL DEFAULT 'active', -- active / sold / cancelled / expired
        buyer_id   INTEGER NOT NULL DEFAULT 0,
        sold_price INTEGER NOT NULL DEFAULT 0,
        closed_at  REAL,
        channel_id INTEGER NOT NULL DEFAULT 0,     -- 告知を投稿したチャンネル（0 は、投稿していない）
        message_id INTEGER NOT NULL DEFAULT 0,     -- 告知の投稿（入札・売れたときに書き換える）
        post_done  INTEGER NOT NULL DEFAULT 0      -- 終わった出品の、告知の書き換えが済んでいるか（1 = 済み）
    )"""

MARKET_LISTINGS_DDL = """
    CREATE TABLE IF NOT EXISTS market_listings (   -- P2P 市場の出品コア（帝愛出品時は seller_id が NULL）
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id  INTEGER NOT NULL,
        seller_id INTEGER CHECK (seller_id IS NULL OR seller_id > 0),
        kind      TEXT    NOT NULL CHECK (kind IN ('item', 'horse')),
        ref       TEXT    NOT NULL,
        qty       INTEGER NOT NULL DEFAULT 1 CHECK (qty > 0),
        mode      TEXT    NOT NULL CHECK (mode IN ('fixed', 'auction')),
        price     INTEGER NOT NULL CHECK (price > 0),
        created   REAL    NOT NULL,
        end_at    REAL    NOT NULL,
        status    TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'sold', 'cancelled', 'expired'))
    )"""

MARKET_AUCTION_BIDS_DDL = """
    CREATE TABLE IF NOT EXISTS market_auction_bids ( -- 進行中・終了直前の競りにおける最高入札（預かり中）
        listing_id INTEGER PRIMARY KEY,
        bidder_id  INTEGER NOT NULL CHECK (bidder_id > 0),
        amount     INTEGER NOT NULL CHECK (amount > 0),
        FOREIGN KEY (listing_id) REFERENCES market_listings (id) ON DELETE CASCADE
    )"""

MARKET_AUCTION_BIDS_BIDDER_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_market_auction_bids_bidder ON market_auction_bids (bidder_id)"""

MARKET_CLOSURES_DDL = """
    CREATE TABLE IF NOT EXISTS market_closures (   -- 終了した出品の決済・終了時刻（キャンセル・期限切れは buyer_id NULL）
        listing_id INTEGER PRIMARY KEY,
        closed_at  REAL    NOT NULL,
        buyer_id   INTEGER CHECK (buyer_id IS NULL OR buyer_id > 0),
        sold_price INTEGER NOT NULL DEFAULT 0 CHECK (sold_price >= 0),
        FOREIGN KEY (listing_id) REFERENCES market_listings (id) ON DELETE CASCADE
    )"""

MARKET_LISTINGS_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS market_listings_active ON market_listings (guild_id, status, end_at)"""

QUIZ_QUESTIONS_V1_DDL = """
    CREATE TABLE IF NOT EXISTS quiz_questions (     -- クイズの問題（v1 互換: v6 で選択肢・ユーザー出題・UI 列を分離）
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        question     TEXT    NOT NULL,
        choices      TEXT    NOT NULL,         -- JSON の配列。先頭が正解（表示のたびにシャッフルする）
        explanation  TEXT    NOT NULL DEFAULT '',
        shown_order  TEXT    NOT NULL DEFAULT '',   -- 表示する並び（choices の添字を並べ替えた JSON。登録時に1回だけシャッフルして固定）
        author_id    INTEGER NOT NULL DEFAULT 0,    -- 出題者。0 なら持ち主の問題（全サーバー共通・システムが報酬を払う）
        guild_id     INTEGER NOT NULL DEFAULT 0,    -- ユーザーの問題は、出題したサーバーの中だけ（出題者のペリカがサーバー別なので）
        channel_id   INTEGER NOT NULL DEFAULT 0,
        message_id   INTEGER NOT NULL DEFAULT 0,
        settled      INTEGER NOT NULL DEFAULT 0,    -- 出題者への報酬を決着させたか
        earned       INTEGER NOT NULL DEFAULT 0,    -- 出題者が、回答のたびにここまで受け取った累計
        creator_name TEXT    NOT NULL DEFAULT '',   -- 制作者の表示名（登録したときの名前。問題に出す）
        msg_right    TEXT    NOT NULL DEFAULT '',   -- 正解のときの見出しの文言（出題者が決める。空なら既定の「正解！」）
        msg_wrong    TEXT    NOT NULL DEFAULT '',   -- 不正解のときの見出しの文言（空なら既定の「不正解…」）
        reward       INTEGER NOT NULL,
        expires      REAL,                     -- 期限（epoch秒）。NULL なら無期限
        created      REAL    NOT NULL
    )"""

QUIZ_QUESTIONS_DDL = """
    CREATE TABLE IF NOT EXISTS quiz_questions (     -- クイズ問題本体（選択肢は quiz_choices、ユーザー出題情報は quiz_user_submissions）
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        question     TEXT    NOT NULL,
        explanation  TEXT    NOT NULL DEFAULT '',
        creator_name TEXT    NOT NULL DEFAULT '',
        msg_right    TEXT    NOT NULL DEFAULT '',
        msg_wrong    TEXT    NOT NULL DEFAULT '',
        reward       INTEGER NOT NULL,
        expires      REAL,
        created      REAL    NOT NULL
    )"""

QUIZ_CHOICES_DDL = """
    CREATE TABLE IF NOT EXISTS quiz_choices (       -- クイズ選択肢（choice_idx=0 が正解、display_pos が固定表示順）
        question_id INTEGER NOT NULL,
        choice_idx  INTEGER NOT NULL CHECK (choice_idx >= 0),
        label       TEXT    NOT NULL,
        display_pos INTEGER NOT NULL CHECK (display_pos >= 0),
        PRIMARY KEY (question_id, choice_idx),
        UNIQUE (question_id, display_pos),
        FOREIGN KEY (question_id) REFERENCES quiz_questions (id) ON DELETE CASCADE
    )"""

QUIZ_USER_SUBMISSIONS_DDL = """
    CREATE TABLE IF NOT EXISTS quiz_user_submissions ( -- ユーザー投稿クイズのサーバー・出題者・精算状態
        question_id INTEGER PRIMARY KEY,
        guild_id    INTEGER NOT NULL DEFAULT 0,
        author_id   INTEGER NOT NULL CHECK (author_id > 0),
        settled     INTEGER NOT NULL DEFAULT 0 CHECK (settled IN (0, 1)),
        earned      INTEGER NOT NULL DEFAULT 0 CHECK (earned >= 0),
        FOREIGN KEY (question_id) REFERENCES quiz_questions (id) ON DELETE CASCADE
    )"""

QUIZ_USER_SUBMISSIONS_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_quiz_user_submissions_guild_author ON quiz_user_submissions (guild_id, author_id, settled)"""

QUIZ_ANSWERS_DDL = """
    CREATE TABLE IF NOT EXISTS quiz_answers (       -- 回答（1問につき1人1回。サーバーごとに別の口座なので guild_id も持つ）
        question_id INTEGER NOT NULL,
        guild_id    INTEGER NOT NULL,
        user_id     INTEGER NOT NULL,
        correct     INTEGER NOT NULL CHECK (correct IN (0, 1)),
        ts          REAL    NOT NULL,
        counted     INTEGER NOT NULL DEFAULT 1 CHECK (counted IN (0, 1)),   -- 出題者の報酬に数えるか。ゲームを3回遊んでいない人の回答は 0（複数アカウントで稼ぐのを防ぐ）
        PRIMARY KEY (question_id, guild_id, user_id),
        FOREIGN KEY (question_id) REFERENCES quiz_questions (id) ON DELETE CASCADE
    )"""

RAFFLE_V1_DDL = """
    CREATE TABLE IF NOT EXISTS raffle (             -- 抽選（v1 互換: v6 で UI 列を discord_posts へ分離）
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL,
        host_id    INTEGER NOT NULL,
        fee        INTEGER NOT NULL,          -- 参加費（全員同じ額）
        closes     REAL    NOT NULL,          -- 締切（epoch秒）
        channel_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL DEFAULT 0,
        status     TEXT    NOT NULL DEFAULT 'open'   -- open / done / cancelled
    )"""

RAFFLE_DDL = """
    CREATE TABLE IF NOT EXISTS raffle (             -- 抽選（持ち主が開催。1サーバーに募集中は1つだけ）
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        host_id  INTEGER NOT NULL,
        fee      INTEGER NOT NULL,
        closes   REAL    NOT NULL,
        status   TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'cancelled'))
    )"""

RAFFLE_ENTRIES_DDL = """
    CREATE TABLE IF NOT EXISTS raffle_entries (     -- 抽選の参加者。参加費は締切まで預かる
        raffle_id INTEGER NOT NULL,
        user_id   INTEGER NOT NULL,
        PRIMARY KEY (raffle_id, user_id),
        FOREIGN KEY (raffle_id) REFERENCES raffle (id) ON DELETE CASCADE
    )"""

POLLS_V1_DDL = """
    CREATE TABLE IF NOT EXISTS polls (             -- 投票（v1 互換: v6 で UI 列を discord_posts へ分離）
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL,
        creator    INTEGER NOT NULL,
        title      TEXT    NOT NULL,
        closes     REAL    NOT NULL,          -- 締切（epoch秒）
        channel_id INTEGER NOT NULL DEFAULT 0,
        message_id INTEGER NOT NULL DEFAULT 0,
        adj_yes    INTEGER NOT NULL DEFAULT 0, -- 票の書き換えで足した数（表示 = 本物の票 + これ）
        adj_no     INTEGER NOT NULL DEFAULT 0,
        rigs       INTEGER NOT NULL DEFAULT 0, -- 書き換えられた回数
        status     TEXT    NOT NULL DEFAULT 'open'   -- open / closed
    )"""

POLLS_DDL = """
    CREATE TABLE IF NOT EXISTS polls (             -- 投票（/投票。誰でも作れる賛成・反対の二択）
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        creator  INTEGER NOT NULL,
        title    TEXT    NOT NULL,
        closes   REAL    NOT NULL,
        adj_yes  INTEGER NOT NULL DEFAULT 0,
        adj_no   INTEGER NOT NULL DEFAULT 0,
        rigs     INTEGER NOT NULL DEFAULT 0 CHECK (rigs >= 0),
        status   TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed'))
    )"""

POLL_VOTES_DDL = """
    CREATE TABLE IF NOT EXISTS poll_votes (        -- 投票の票（1人1票。あとから変えられる）
        poll_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        yes     INTEGER NOT NULL CHECK (yes IN (0, 1)),
        PRIMARY KEY (poll_id, user_id),
        FOREIGN KEY (poll_id) REFERENCES polls (id) ON DELETE CASCADE
    )"""

STREAMS_V1_DDL = """
    CREATE TABLE IF NOT EXISTS streams (           -- 配信（v1 互換: v6 で UI/VC 列を分離）
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id         INTEGER NOT NULL,
        streamer         INTEGER NOT NULL,
        title            TEXT    NOT NULL,
        url              TEXT    NOT NULL DEFAULT '',
        channel_id       INTEGER NOT NULL DEFAULT 0,
        message_id       INTEGER NOT NULL DEFAULT 0,
        started          REAL    NOT NULL,
        ends             REAL    NOT NULL,          -- これを過ぎたら見回りで自動で閉じる（epoch秒）
        tips             INTEGER NOT NULL DEFAULT 0, -- スパチャの合計（配信者が受け取った額）
        tip_count        INTEGER NOT NULL DEFAULT 0,
        status           TEXT    NOT NULL DEFAULT 'live',  -- live / ended
        voice_channel_id INTEGER NOT NULL DEFAULT 0, -- 配信している場所（Discord のボイスチャンネル。なければ 0）
        created_vc       INTEGER NOT NULL DEFAULT 0  -- 1 なら Bot が作った VC（配信を閉じたら消す）
    )"""

STREAMS_DDL = """
    CREATE TABLE IF NOT EXISTS streams (           -- 配信（/配信。UI・VC 情報は子表へ分離）
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id  INTEGER NOT NULL,
        streamer  INTEGER NOT NULL,
        title     TEXT    NOT NULL,
        url       TEXT    NOT NULL DEFAULT '',
        started   REAL    NOT NULL,
        ends      REAL    NOT NULL,
        tips      INTEGER NOT NULL DEFAULT 0 CHECK (tips >= 0),
        tip_count INTEGER NOT NULL DEFAULT 0 CHECK (tip_count >= 0),
        status    TEXT    NOT NULL DEFAULT 'live' CHECK (status IN ('live', 'ended'))
    )"""

STREAM_ROUNDS_DDL = """
    CREATE TABLE IF NOT EXISTS stream_rounds (     -- 配信の勝負（勝敗の賭けの1回分）
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        stream_id INTEGER NOT NULL,
        guild_id  INTEGER NOT NULL,
        label     TEXT    NOT NULL,
        status    TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'locked', 'win', 'lose', 'void')), -- open / locked / win / lose / void
        created   REAL    NOT NULL,
        settled   REAL    NOT NULL DEFAULT 0,
        FOREIGN KEY (stream_id) REFERENCES streams (id) ON DELETE CASCADE
    )"""

STREAM_BETS_DDL = """
    CREATE TABLE IF NOT EXISTS stream_bets (       -- 配信の勝負への賭け（1人1つの側。同じ側なら足せる）。賭け金は精算まで預かる
        round_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        side     TEXT    NOT NULL CHECK (side IN ('win', 'lose')),             -- win / lose
        amount   INTEGER NOT NULL CHECK (amount > 0),
        PRIMARY KEY (round_id, user_id),
        FOREIGN KEY (round_id) REFERENCES stream_rounds (id) ON DELETE CASCADE
    )"""

ARENA_TOURNAMENTS_DDL = """
    CREATE TABLE IF NOT EXISTS arena_tournaments (  -- 地下闘技場の、1日1回の大会（形式ごとに別の大会として、毎日両方開く）
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        day     TEXT    NOT NULL,
        format  TEXT    NOT NULL,              -- duel（1on1）/ royale（バトロワ）
        status  TEXT    NOT NULL DEFAULT 'semi_betting',  -- semi_betting -> semi_done -> final_betting -> done
        created REAL    NOT NULL
    )"""

ARENA_TOURNAMENTS_INDEX_DDL = """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_arena_tournaments_day ON arena_tournaments (guild_id, day, format)"""

ARENA_FIGHTERS_DDL = """
    CREATE TABLE IF NOT EXISTS arena_fighters (     -- 戦士（NPC。大会ごとに生成）
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id INTEGER NOT NULL,
        name          TEXT    NOT NULL,
        base_power    INTEGER NOT NULL,
        group_no      INTEGER NOT NULL,          -- 準決勝の組（1on1は1〜2・バトロワは1〜4）
        stage         TEXT    NOT NULL DEFAULT 'semi' CHECK (stage IN ('semi', 'final', 'out')),  -- semi / final / out（脱落）
        bet_total     INTEGER NOT NULL DEFAULT 0 CHECK (bet_total >= 0), -- 大会を通じた、この戦士への賭け総額（武器の強さに使う）
        result        TEXT CHECK (result IS NULL OR result IN ('won', 'lost')),                       -- いまの段の結果（NULL 未決着 / won / lost）
        FOREIGN KEY (tournament_id) REFERENCES arena_tournaments (id) ON DELETE CASCADE
    )"""

ARENA_FIGHTERS_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_arena_fighters ON arena_fighters (tournament_id)"""

ARENA_BETS_V1_DDL = """
    CREATE TABLE IF NOT EXISTS arena_bets (         -- 戦士への賭け（v1 互換: v6 で NPC 観客賭けを arena_npc_bets へ分離）
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id INTEGER NOT NULL,
        stage         TEXT    NOT NULL,           -- semi / final（どの段への賭けか）
        fighter_id    INTEGER NOT NULL,
        user_id       INTEGER NOT NULL,
        amount        INTEGER NOT NULL,
        payout        INTEGER NOT NULL DEFAULT 0,
        settled       INTEGER NOT NULL DEFAULT 0,
        created       REAL    NOT NULL
    )"""

ARENA_BETS_DDL = """
    CREATE TABLE IF NOT EXISTS arena_bets (         -- プレイヤーの戦士への賭け（user_id > 0 のみ）
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id INTEGER NOT NULL,
        stage         TEXT    NOT NULL CHECK (stage IN ('semi', 'final')),
        fighter_id    INTEGER NOT NULL,
        user_id       INTEGER NOT NULL CHECK (user_id > 0),
        amount        INTEGER NOT NULL CHECK (amount > 0),
        payout        INTEGER NOT NULL DEFAULT 0 CHECK (payout >= 0),
        settled       INTEGER NOT NULL DEFAULT 0 CHECK (settled IN (0, 1)),
        created       REAL    NOT NULL,
        FOREIGN KEY (tournament_id) REFERENCES arena_tournaments (id) ON DELETE CASCADE,
        FOREIGN KEY (fighter_id) REFERENCES arena_fighters (id) ON DELETE CASCADE
    )"""

ARENA_BETS_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_arena_bets ON arena_bets (tournament_id, stage, fighter_id)"""

ARENA_BETS_USER_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_arena_bets_user ON arena_bets (tournament_id, user_id)"""

ARENA_NPC_BETS_DDL = """
    CREATE TABLE IF NOT EXISTS arena_npc_bets (     -- NPC 観客による初期賭け
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id INTEGER NOT NULL,
        stage         TEXT    NOT NULL CHECK (stage IN ('semi', 'final')),
        fighter_id    INTEGER NOT NULL,
        amount        INTEGER NOT NULL CHECK (amount > 0),
        settled       INTEGER NOT NULL DEFAULT 0 CHECK (settled IN (0, 1)),
        created       REAL    NOT NULL,
        FOREIGN KEY (tournament_id) REFERENCES arena_tournaments (id) ON DELETE CASCADE,
        FOREIGN KEY (fighter_id) REFERENCES arena_fighters (id) ON DELETE CASCADE
    )"""

ARENA_NPC_BETS_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_arena_npc_bets ON arena_npc_bets (tournament_id, stage, fighter_id)"""

STATEMENTS: tuple[str, ...] = (
    LOANS_DDL,
    LOANS_INDEX_DDL,
    LOAN_TERMS_DDL,
    MARKET_LISTINGS_DDL,
    MARKET_LISTINGS_INDEX_DDL,
    MARKET_AUCTION_BIDS_DDL,
    MARKET_AUCTION_BIDS_BIDDER_INDEX_DDL,
    MARKET_CLOSURES_DDL,
    QUIZ_QUESTIONS_DDL,
    QUIZ_CHOICES_DDL,
    QUIZ_USER_SUBMISSIONS_DDL,
    QUIZ_USER_SUBMISSIONS_INDEX_DDL,
    QUIZ_ANSWERS_DDL,
    RAFFLE_DDL,
    RAFFLE_ENTRIES_DDL,
    POLLS_DDL,
    POLL_VOTES_DDL,
    STREAMS_DDL,
    STREAM_ROUNDS_DDL,
    STREAM_BETS_DDL,
    ARENA_TOURNAMENTS_DDL,
    ARENA_TOURNAMENTS_INDEX_DDL,
    ARENA_FIGHTERS_DDL,
    ARENA_FIGHTERS_INDEX_DDL,
    ARENA_BETS_DDL,
    ARENA_BETS_INDEX_DDL,
    ARENA_BETS_USER_INDEX_DDL,
    ARENA_NPC_BETS_DDL,
    ARENA_NPC_BETS_INDEX_DDL,
)
