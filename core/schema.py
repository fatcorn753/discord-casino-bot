"""データベースの表の定義（作成）と、古い版の表への列の追加（移行）。

Economy が起動するときに、create_tables → add_missing_columns の順で呼ぶ。サーバー別になる前の記録の移行は Economy 側にある。
"""

import json
import random
import sqlite3

USERS_DDL = """
    CREATE TABLE IF NOT EXISTS users (
        guild_id     INTEGER NOT NULL,
        user_id      INTEGER NOT NULL,
        balance      INTEGER NOT NULL,
        daily_streak INTEGER NOT NULL DEFAULT 0,
        last_daily   TEXT,
        games_played INTEGER NOT NULL DEFAULT 0,
        total_wagered INTEGER NOT NULL DEFAULT 0,
        total_won    INTEGER NOT NULL DEFAULT 0,
        biggest_win  INTEGER NOT NULL DEFAULT 0,
        created_at   REAL NOT NULL,
        last_slot    INTEGER,                 -- 最後にボーナスを受け取った「時間帯」の番号（6時間ごとの受け取り用）
        debt         INTEGER NOT NULL DEFAULT 0,   -- 借金（チンチロの倍払いが残高を超えた分）。入金のたびに強制で返済する
        PRIMARY KEY (guild_id, user_id)
    )"""
INVENTORY_DDL = """
    CREATE TABLE IF NOT EXISTS inventory (
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        item_id  TEXT    NOT NULL,
        qty      INTEGER NOT NULL,
        PRIMARY KEY (guild_id, user_id, item_id)
    )"""
PROFILE_DDL = """
    CREATE TABLE IF NOT EXISTS profile (
        guild_id    INTEGER NOT NULL,
        user_id     INTEGER NOT NULL,
        title       TEXT,
        card_color  TEXT,
        card_icon   TEXT,
        daily_boost INTEGER NOT NULL DEFAULT 0,
        insurance   INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    )"""


def new_quiz_order() -> list:
    """クイズの選択肢の表示順（添字を並べ替えたもの）。登録時に1回だけ決めて固定する。"""
    order = [0, 1, 2, 3]
    random.SystemRandom().shuffle(order)
    return order


def create_tables(conn: sqlite3.Connection) -> None:
    """表と索引を、無ければ作る（あれば何もしない）。"""
    conn.execute(USERS_DDL)
    conn.execute(INVENTORY_DDL)
    conn.execute(PROFILE_DDL)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ledger (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id      INTEGER NOT NULL DEFAULT 0,
            user_id       INTEGER NOT NULL,
            delta         INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            reason        TEXT NOT NULL,
            ts            REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ledger_user ON ledger (guild_id, user_id, id);
        CREATE TABLE IF NOT EXISTS escrow (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id   INTEGER NOT NULL DEFAULT 0,
            user_id    INTEGER NOT NULL,
            amount     INTEGER NOT NULL,
            game       TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS guild_users (
            guild_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS guild_status (
            guild_id    INTEGER PRIMARY KEY,
            status      TEXT NOT NULL,       -- pending / approved / denied
            name        TEXT,
            decided_by  INTEGER,
            updated_at  REAL NOT NULL,
            notified_at REAL
        );
        CREATE TABLE IF NOT EXISTS leaderboards (
            guild_id   INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS play_state (
            guild_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            win_streak INTEGER NOT NULL DEFAULT 0,
            best_streak INTEGER NOT NULL DEFAULT 0,
            day_streak INTEGER NOT NULL DEFAULT 0,      -- ミッション用: その日の、賭け金が最低額以上の連勝
            streak_day TEXT,
            plays      INTEGER NOT NULL DEFAULT 0,       -- 成績の集計（勝率・損益）は、この記録を付け始めてからの分
            wins       INTEGER NOT NULL DEFAULT 0,
            net_total  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS missions (
            guild_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            day        TEXT    NOT NULL,
            mission_id TEXT    NOT NULL,
            progress   INTEGER NOT NULL DEFAULT 0,
            done       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, day, mission_id)
        );
        CREATE TABLE IF NOT EXISTS market_prices (     -- CSトークンの価格。全サーバー共通
            ts    REAL PRIMARY KEY,
            price INTEGER NOT NULL,
            event INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS crypto (
            guild_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            units    INTEGER NOT NULL,        -- 保有する口数（1000口 = 1枚）
            cost     INTEGER NOT NULL,        -- 買うのにかかった総額（損益の表示用）
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS crypto_short (      -- CSトークンの空売り（1人1つの建玉）
            guild_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            units       INTEGER NOT NULL,         -- 売り建てた口数
            entry_value REAL NOT NULL,            -- 建てたときの総額（口数×価格）。損益 = これ − いまの総額
            margin      INTEGER NOT NULL,         -- 預けた担保
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS quiz_questions (     -- クイズの問題（全サーバー共通。持ち主が登録する）
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            question    TEXT    NOT NULL,
            choices     TEXT    NOT NULL,         -- JSON の配列。先頭が正解（表示のたびにシャッフルする）
            explanation TEXT    NOT NULL DEFAULT '',
            shown_order TEXT    NOT NULL DEFAULT '',   -- 表示する並び（choices の添字を並べ替えた JSON。登録時に1回だけシャッフルして固定）
            author_id   INTEGER NOT NULL DEFAULT 0,    -- 出題者。0 なら持ち主の問題（全サーバー共通・システムが報酬を払う）
            guild_id    INTEGER NOT NULL DEFAULT 0,    -- ユーザーの問題は、出題したサーバーの中だけ（出題者のペリカがサーバー別なので）
            channel_id  INTEGER NOT NULL DEFAULT 0,
            message_id  INTEGER NOT NULL DEFAULT 0,
            settled     INTEGER NOT NULL DEFAULT 0,    -- 出題者への報酬を決着させたか
            earned      INTEGER NOT NULL DEFAULT 0,    -- 出題者が、回答のたびにここまで受け取った累計
            creator_name TEXT   NOT NULL DEFAULT '',   -- 制作者の表示名（登録したときの名前。問題に出す）
            msg_right   TEXT    NOT NULL DEFAULT '',   -- 正解のときの見出しの文言（出題者が決める。空なら既定の「正解！」）
            msg_wrong   TEXT    NOT NULL DEFAULT '',   -- 不正解のときの見出しの文言（空なら既定の「不正解…」）
            reward      INTEGER NOT NULL,
            expires     REAL,                     -- 期限（epoch秒）。NULL なら無期限
            created     REAL    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS quiz_answers (       -- 回答（1問につき1人1回。サーバーごとに別の口座なので guild_id も持つ）
            question_id INTEGER NOT NULL,
            guild_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            correct     INTEGER NOT NULL,
            ts          REAL    NOT NULL,
            PRIMARY KEY (question_id, guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS bonds (              -- 債券（買った分ごとに1行）。元本は買った時点で残高から預かる
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id  INTEGER NOT NULL,
            user_id   INTEGER NOT NULL,
            kind      TEXT    NOT NULL,           -- normal / junk
            principal INTEGER NOT NULL,
            due       REAL    NOT NULL,           -- 満期（epoch秒）
            status    TEXT    NOT NULL DEFAULT 'active',   -- active / paid / defaulted / cancelled
            paid      INTEGER NOT NULL DEFAULT 0, -- 満期・解約で戻した額
            ts        REAL    NOT NULL            -- 買った時刻
        );
        CREATE TABLE IF NOT EXISTS raffle (             -- 抽選（持ち主が開催。1サーバーに募集中は1つだけ）
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id   INTEGER NOT NULL,
            host_id    INTEGER NOT NULL,
            fee        INTEGER NOT NULL,          -- 参加費（全員同じ額）
            closes     REAL    NOT NULL,          -- 締切（epoch秒）
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL DEFAULT 0,
            status     TEXT    NOT NULL DEFAULT 'open'   -- open / done / cancelled
        );
        CREATE TABLE IF NOT EXISTS raffle_entries (     -- 抽選の参加者。参加費は締切まで預かる
            raffle_id INTEGER NOT NULL,
            user_id   INTEGER NOT NULL,
            PRIMARY KEY (raffle_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS chart_posts (       -- /暗号 チャート で出したチャート（1チャンネルに1つ。最後に使ってから一定時間だけ自動で更新）
            channel_id INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL,
            guild_id   INTEGER NOT NULL DEFAULT 0,
            period     TEXT    NOT NULL DEFAULT '24h',
            ts         REAL    NOT NULL DEFAULT 0,     -- 出した時刻（ここから一定時間、更新する）
            live       INTEGER NOT NULL DEFAULT 1      -- 1: 更新中 / 0: 更新は終了
        );
        CREATE TABLE IF NOT EXISTS chart_boards (
            guild_id   INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS weekly_stats (      -- 週ごとのゲームの稼ぎ（週間ランキング用）
            guild_id INTEGER NOT NULL,
            week     TEXT    NOT NULL,             -- その週の月曜日（2026-10-26 の形）
            user_id  INTEGER NOT NULL,
            net      INTEGER NOT NULL DEFAULT 0,
            plays    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, week, user_id)
        );
        CREATE TABLE IF NOT EXISTS weekly_champions (
            guild_id INTEGER NOT NULL,
            week     TEXT    NOT NULL,
            user_id  INTEGER NOT NULL,
            net      INTEGER NOT NULL,
            PRIMARY KEY (guild_id, week)
        );
        CREATE TABLE IF NOT EXISTS event_state (       -- 突発イベントの告知の進み具合（0=まだ 1=開始を告知 2=終了を告知）。再起動しても二重に告知しない
            guild_id INTEGER NOT NULL, day TEXT NOT NULL, phase INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, day)
        );
        CREATE TABLE IF NOT EXISTS manual_event (      -- 持ち主が開いた特別イベント（1サーバーに1つ。新しく開くと置き換わる）
            guild_id INTEGER PRIMARY KEY,
            game     TEXT    NOT NULL,
            boost    INTEGER NOT NULL,             -- 勝ち分に上乗せする割合（％）
            starts   REAL    NOT NULL,
            ends     REAL    NOT NULL,
            ended    INTEGER NOT NULL DEFAULT 0,   -- 終了を告知したか（時間切れ・手動終了のどちらでも 1）
            channel_id INTEGER NOT NULL DEFAULT 0  -- 開始を告知したチャンネル（終了の告知も同じ所へ。0 なら告知チャンネル）
        );
        CREATE TABLE IF NOT EXISTS pachinko_spins (     -- パチンコの1回転ごとの記録（設計した当たり率と実績を比べる統計用）
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            ts       REAL    NOT NULL,
            omen     TEXT    NOT NULL,            -- 予告の色（'' は予告なし）
            tier     TEXT    NOT NULL,            -- リーチの格（'' はリーチなし）
            hit      INTEGER NOT NULL             -- 当たったら 1
        );
        CREATE TABLE IF NOT EXISTS pachinko_sets (      -- パチンコの1セット（100発）ごとの記録
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id  INTEGER NOT NULL,
            user_id   INTEGER NOT NULL,
            ts        REAL    NOT NULL,
            bet       INTEGER NOT NULL,           -- そのセットの賭け金
            balls     INTEGER NOT NULL,           -- 打った玉の数
            starts    INTEGER NOT NULL,           -- スタートに入った玉の数（回転数）
            hits      INTEGER NOT NULL,           -- 大当たりの始まりの数
            chains    INTEGER NOT NULL,           -- 大当たりのラウンドの合計（連チャンを含む）
            chain_max INTEGER NOT NULL,           -- 1回の大当たりでの最大連チャン
            mult      INTEGER NOT NULL            -- 払い戻しの倍数（上限をかけたあと。還元率レベルをかける前）
        );
        CREATE INDEX IF NOT EXISTS idx_pachinko_spins ON pachinko_spins (guild_id, ts);
        CREATE INDEX IF NOT EXISTS idx_pachinko_sets ON pachinko_sets (guild_id, ts);
        CREATE TABLE IF NOT EXISTS rtp_level (          -- ゲームごとの還元率レベル（1〜6。3は標準＝保存しない）
            guild_id INTEGER NOT NULL,
            game     TEXT    NOT NULL,
            level    INTEGER NOT NULL,
            PRIMARY KEY (guild_id, game)
        );
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
        );
        CREATE INDEX IF NOT EXISTS idx_loans ON loans (guild_id, borrower_id, status);
        CREATE TABLE IF NOT EXISTS weekly_state (      -- サーバーごとの、最後に発表した週（guild_id=0 は、過去分の補完を済ませた印）
            guild_id  INTEGER PRIMARY KEY,
            last_week TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS nick_prefs (
            guild_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            original_nick TEXT,          -- 称号を付ける前のニックネーム（NULL はニックネームなし）
            applied_nick  TEXT,          -- Bot が設定したニックネーム（NULL は未適用）
            PRIMARY KEY (guild_id, user_id)
        );
        """
    )


def add_missing_columns(conn: sqlite3.Connection) -> None:
    """作成済みの古い表に、あとから増えた列を足す。"""
    # 作成済みの古い表に、列を足す（初回の版には元のニックネームの記録がなかった）
    cp = {r["name"] for r in conn.execute("PRAGMA table_info(chart_posts)")}
    for col, ddl in (
        ("guild_id", "INTEGER NOT NULL DEFAULT 0"),
        ("period", "TEXT NOT NULL DEFAULT '24h'"),
        ("ts", "REAL NOT NULL DEFAULT 0"),
        ("live", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if col not in cp:
            conn.execute(f"ALTER TABLE chart_posts ADD COLUMN {col} {ddl}")
    me = {r["name"] for r in conn.execute("PRAGMA table_info(manual_event)")}
    if "channel_id" not in me:
        conn.execute("ALTER TABLE manual_event ADD COLUMN channel_id INTEGER NOT NULL DEFAULT 0")
    qq = {r["name"] for r in conn.execute("PRAGMA table_info(quiz_questions)")}
    if "shown_order" not in qq:
        conn.execute("ALTER TABLE quiz_questions ADD COLUMN shown_order TEXT NOT NULL DEFAULT ''")
    for col in ("author_id", "guild_id", "channel_id", "message_id", "settled", "earned"):
        if col not in qq:
            conn.execute(f"ALTER TABLE quiz_questions ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
    for col in ("creator_name", "msg_right", "msg_wrong"):
        if col not in qq:
            conn.execute(f"ALTER TABLE quiz_questions ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    for r in conn.execute(
        "SELECT id FROM quiz_questions WHERE shown_order=''"
    ).fetchall():  # 並びが未設定の古い問題に、1回だけ決めて固定する
        conn.execute("UPDATE quiz_questions SET shown_order=? WHERE id=?", (json.dumps(new_quiz_order()), r["id"]))
    us = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    if "last_slot" not in us:
        conn.execute("ALTER TABLE users ADD COLUMN last_slot INTEGER")
    if "debt" not in us:
        conn.execute("ALTER TABLE users ADD COLUMN debt INTEGER NOT NULL DEFAULT 0")
    ps = {r["name"] for r in conn.execute("PRAGMA table_info(play_state)")}
    for col, ddl in (
        ("day_streak", "INTEGER NOT NULL DEFAULT 0"),
        ("streak_day", "TEXT"),
        ("plays", "INTEGER NOT NULL DEFAULT 0"),
        ("wins", "INTEGER NOT NULL DEFAULT 0"),
        ("net_total", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if col not in ps:
            conn.execute(f"ALTER TABLE play_state ADD COLUMN {col} {ddl}")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nick_prefs)")}
    for col in ("original_nick", "applied_nick"):
        if col not in cols:
            conn.execute(f"ALTER TABLE nick_prefs ADD COLUMN {col} TEXT")
