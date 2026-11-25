"""個人間融資・P2P市場・クイズ・抽選・投票・配信・地下闘技場の DDL。"""

import random


def new_quiz_order() -> list[int]:
    """クイズの選択肢の表示順（添字を並べ替えたもの）。登録時に1回だけ決めて固定する。"""
    order = [0, 1, 2, 3]
    random.SystemRandom().shuffle(order)
    return order


LOANS_DDL = """
    CREATE TABLE IF NOT EXISTS loans (              -- 貸し借り（ユーザー同士）
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

LOANS_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_loans ON loans (guild_id, borrower_id, status)"""

MARKET_LISTINGS_DDL = """
    CREATE TABLE IF NOT EXISTS market_listings (   -- プレイヤー間の市場の出品（定価と競り）。出した品・馬は、出品中は預かる
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

MARKET_LISTINGS_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS market_listings_active ON market_listings (guild_id, status, end_at)"""

QUIZ_QUESTIONS_DDL = """
    CREATE TABLE IF NOT EXISTS quiz_questions (     -- クイズの問題（全サーバー共通。持ち主が登録する）
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

QUIZ_ANSWERS_DDL = """
    CREATE TABLE IF NOT EXISTS quiz_answers (       -- 回答（1問につき1人1回。サーバーごとに別の口座なので guild_id も持つ）
        question_id INTEGER NOT NULL,
        guild_id    INTEGER NOT NULL,
        user_id     INTEGER NOT NULL,
        correct     INTEGER NOT NULL,
        ts          REAL    NOT NULL,
        counted     INTEGER NOT NULL DEFAULT 1,   -- 出題者の報酬に数えるか。ゲームを3回遊んでいない人の回答は 0（複数アカウントで稼ぐのを防ぐ）
        PRIMARY KEY (question_id, guild_id, user_id)
    )"""

RAFFLE_DDL = """
    CREATE TABLE IF NOT EXISTS raffle (             -- 抽選（持ち主が開催。1サーバーに募集中は1つだけ）
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL,
        host_id    INTEGER NOT NULL,
        fee        INTEGER NOT NULL,          -- 参加費（全員同じ額）
        closes     REAL    NOT NULL,          -- 締切（epoch秒）
        channel_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL DEFAULT 0,
        status     TEXT    NOT NULL DEFAULT 'open'   -- open / done / cancelled
    )"""

RAFFLE_ENTRIES_DDL = """
    CREATE TABLE IF NOT EXISTS raffle_entries (     -- 抽選の参加者。参加費は締切まで預かる
        raffle_id INTEGER NOT NULL,
        user_id   INTEGER NOT NULL,
        PRIMARY KEY (raffle_id, user_id)
    )"""

POLLS_DDL = """
    CREATE TABLE IF NOT EXISTS polls (             -- 投票（/投票。誰でも作れる賛成・反対の二択）
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

POLL_VOTES_DDL = """
    CREATE TABLE IF NOT EXISTS poll_votes (        -- 投票の票（1人1票。あとから変えられる）
        poll_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        yes     INTEGER NOT NULL,
        PRIMARY KEY (poll_id, user_id)
    )"""

STREAMS_DDL = """
    CREATE TABLE IF NOT EXISTS streams (           -- 配信（/配信。スパチャと勝敗の賭けを受ける配信パネル）
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

STREAM_ROUNDS_DDL = """
    CREATE TABLE IF NOT EXISTS stream_rounds (     -- 配信の勝負（勝敗の賭けの1回分）
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        stream_id INTEGER NOT NULL,
        guild_id  INTEGER NOT NULL,
        label     TEXT    NOT NULL,
        status    TEXT    NOT NULL DEFAULT 'open', -- open / locked / win / lose / void
        created   REAL    NOT NULL,
        settled   REAL    NOT NULL DEFAULT 0
    )"""

STREAM_BETS_DDL = """
    CREATE TABLE IF NOT EXISTS stream_bets (       -- 配信の勝負への賭け（1人1つの側。同じ側なら足せる）。賭け金は精算まで預かる
        round_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        side     TEXT    NOT NULL,             -- win / lose
        amount   INTEGER NOT NULL,
        PRIMARY KEY (round_id, user_id)
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
        stage         TEXT    NOT NULL DEFAULT 'semi',  -- semi / final / out（脱落）
        bet_total     INTEGER NOT NULL DEFAULT 0, -- 大会を通じた、この戦士への賭け総額（武器の強さに使う）
        result        TEXT                       -- いまの段の結果（NULL 未決着 / won / lost）
    )"""

ARENA_FIGHTERS_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_arena_fighters ON arena_fighters (tournament_id)"""

ARENA_BETS_DDL = """
    CREATE TABLE IF NOT EXISTS arena_bets (         -- 戦士への賭け（観客＝NPCの分は user_id が負の特定値）
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

ARENA_BETS_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_arena_bets ON arena_bets (tournament_id, stage, fighter_id)"""

ARENA_BETS_USER_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_arena_bets_user ON arena_bets (tournament_id, user_id)"""

STATEMENTS: tuple[str, ...] = (
    LOANS_DDL,
    LOANS_INDEX_DDL,
    MARKET_LISTINGS_DDL,
    MARKET_LISTINGS_INDEX_DDL,
    QUIZ_QUESTIONS_DDL,
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
)
