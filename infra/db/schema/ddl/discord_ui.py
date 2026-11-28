"""サーバー許可・機能制御・ランキング板・チャート投稿・週間統計・イベント状態の DDL。"""

GUILD_STATUS_DDL = """
    CREATE TABLE IF NOT EXISTS guild_status (
        guild_id    INTEGER PRIMARY KEY,
        status      TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'denied')),       -- pending / approved / denied
        name        TEXT,
        decided_by  INTEGER,
        updated_at  REAL NOT NULL,
        notified_at REAL
    )"""

FEATURES_OFF_DDL = """
    CREATE TABLE IF NOT EXISTS features_off (       -- 管理者が止めた機能（行があるあいだ止まる。domain/features.py）
        guild_id INTEGER NOT NULL,
        feature  TEXT    NOT NULL,
        PRIMARY KEY (guild_id, feature)
    )"""

LEADERBOARDS_DDL = """
    CREATE TABLE IF NOT EXISTS leaderboards (
        guild_id   INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL
    )"""

CHART_POSTS_DDL = """
    CREATE TABLE IF NOT EXISTS chart_posts (       -- /証券 市況 で出したチャート（1チャンネルに1つ。最後に使ってから一定時間だけ自動で更新）
        channel_id INTEGER PRIMARY KEY,
        message_id INTEGER NOT NULL,
        guild_id   INTEGER NOT NULL DEFAULT 0,
        period     TEXT    NOT NULL DEFAULT '24h',
        ts         REAL    NOT NULL DEFAULT 0,     -- 出した時刻（ここから一定時間、更新する）
        live       INTEGER NOT NULL DEFAULT 1,     -- 1: 更新中 / 0: 更新は終了
        symbol     TEXT    NOT NULL DEFAULT 'cs'       -- 出した銘柄のチャート
    )"""

CHART_BOARDS_DDL = """
    CREATE TABLE IF NOT EXISTS chart_boards (
        guild_id   INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL
    )"""

WEEKLY_STATS_DDL = """
    CREATE TABLE IF NOT EXISTS weekly_stats (      -- 週ごとのゲームの稼ぎ（週間ランキング用）
        guild_id INTEGER NOT NULL,
        week     TEXT    NOT NULL,             -- その週の月曜日（2026-10-26 の形）
        user_id  INTEGER NOT NULL,
        net      INTEGER NOT NULL DEFAULT 0,
        plays    INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, week, user_id)
    )"""

WEEKLY_CHAMPIONS_DDL = """
    CREATE TABLE IF NOT EXISTS weekly_champions (
        guild_id INTEGER NOT NULL,
        week     TEXT    NOT NULL,
        user_id  INTEGER NOT NULL,
        net      INTEGER NOT NULL,
        PRIMARY KEY (guild_id, week)
    )"""

WEEKLY_STATE_DDL = """
    CREATE TABLE IF NOT EXISTS weekly_state (      -- サーバーごとの、最後に発表した週
        guild_id  INTEGER PRIMARY KEY CHECK (guild_id >= 0),
        last_week TEXT NOT NULL
    )"""

SCHEMA_FLAGS_DDL = """
    CREATE TABLE IF NOT EXISTS schema_flags (      -- アプリ・移行フラグ（例: weekly_backfilled）
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )"""

EVENT_STATE_DDL = """
    CREATE TABLE IF NOT EXISTS event_state (       -- 突発イベントの告知の進み具合（0=まだ 1=開始を告知 2=終了を告知）。再起動しても二重に告知しない
        guild_id INTEGER NOT NULL,
        day      TEXT NOT NULL,
        phase    INTEGER NOT NULL DEFAULT 0 CHECK (phase >= 0),
        PRIMARY KEY (guild_id, day)
    )"""

MANUAL_EVENT_V1_DDL = """
    CREATE TABLE IF NOT EXISTS manual_event (      -- v1 互換（v6 で channel_id を discord_posts へ分離）
        guild_id   INTEGER PRIMARY KEY,
        game       TEXT    NOT NULL,
        boost      INTEGER NOT NULL,             -- 勝ち分に上乗せする割合（％）
        starts     REAL    NOT NULL,
        ends       REAL    NOT NULL,
        ended      INTEGER NOT NULL DEFAULT 0,   -- 終了を告知したか（時間切れ・手動終了のどちらでも 1）
        channel_id INTEGER NOT NULL DEFAULT 0  -- 開始を告知したチャンネル（終了の告知も同じ所へ。0 なら告知チャンネル）
    )"""

MANUAL_EVENT_DDL = """
    CREATE TABLE IF NOT EXISTS manual_event (      -- 持ち主が開いた特別イベント（1サーバーに1つ。新しく開くと置き換わる）
        guild_id INTEGER PRIMARY KEY,
        game     TEXT    NOT NULL,
        boost    INTEGER NOT NULL,               -- 勝ち分に上乗せする割合（％）
        starts   REAL    NOT NULL,
        ends     REAL    NOT NULL,
        ended    INTEGER NOT NULL DEFAULT 0 CHECK (ended IN (0, 1))
    )"""

DISCORD_POSTS_DDL = """
    CREATE TABLE IF NOT EXISTS discord_posts (     -- 各ドメインエンティティに紐づく Discord 投稿先の記録
        entity_type TEXT    NOT NULL CHECK (entity_type IN (
            'stream', 'horse_race', 'market_listing', 'kyosai_motion',
            'quiz', 'raffle', 'poll', 'loan', 'manual_event'
        )),
        entity_id   INTEGER NOT NULL,
        channel_id  INTEGER NOT NULL DEFAULT 0,
        message_id  INTEGER NOT NULL DEFAULT 0,
        post_done   INTEGER NOT NULL DEFAULT 0 CHECK (post_done IN (0, 1)),
        PRIMARY KEY (entity_type, entity_id)
    )"""

STREAM_VOICE_CHANNELS_DDL = """
    CREATE TABLE IF NOT EXISTS stream_voice_channels ( -- 配信に紐づく Discord ボイスチャンネル
        stream_id        INTEGER PRIMARY KEY,
        voice_channel_id INTEGER NOT NULL CHECK (voice_channel_id > 0),
        created_vc       INTEGER NOT NULL DEFAULT 0 CHECK (created_vc IN (0, 1)),
        FOREIGN KEY (stream_id) REFERENCES streams (id) ON DELETE CASCADE
    )"""

STATEMENTS: tuple[str, ...] = (
    GUILD_STATUS_DDL,
    FEATURES_OFF_DDL,
    LEADERBOARDS_DDL,
    CHART_POSTS_DDL,
    CHART_BOARDS_DDL,
    WEEKLY_STATS_DDL,
    WEEKLY_CHAMPIONS_DDL,
    WEEKLY_STATE_DDL,
    SCHEMA_FLAGS_DDL,
    EVENT_STATE_DDL,
    MANUAL_EVENT_DDL,
    DISCORD_POSTS_DDL,
    STREAM_VOICE_CHANNELS_DDL,
)
