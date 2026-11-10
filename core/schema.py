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
        debt         INTEGER NOT NULL DEFAULT 0,   -- 帝愛からの借金（シニア債権）。収入から天引きで返済する（core/teiai.py）
        bankrupt_at  REAL,                         -- 自己破産した時刻（NULL なら破産者ではない）
        blacklist_until REAL,                      -- 破産者の身分が終わる時刻（完済のときに、完済から7日後を入れる）
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
SHOP_STOCK_DDL = """
    CREATE TABLE IF NOT EXISTS shop_stock (        -- ショップの在庫（買える数）。1人ずつ別々。時間で補充される（core/shop.py の STOCK_RULES）
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


def new_quiz_order() -> list:
    """クイズの選択肢の表示順（添字を並べ替えたもの）。登録時に1回だけ決めて固定する。"""
    order = [0, 1, 2, 3]
    random.SystemRandom().shuffle(order)
    return order


def create_tables(conn: sqlite3.Connection) -> None:
    """表と索引を、無ければ作る（あれば何もしない）。"""
    conn.execute(USERS_DDL)
    conn.execute(INVENTORY_DDL)
    conn.execute(SHOP_STOCK_DDL)
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
        CREATE TABLE IF NOT EXISTS horses (          -- 馬主が持つ競走馬
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id  INTEGER NOT NULL,
            owner_id  INTEGER NOT NULL,
            name      TEXT NOT NULL,
            coat      TEXT NOT NULL,
            silks     TEXT NOT NULL,
            speed     INTEGER NOT NULL,
            stamina   INTEGER NOT NULL,
            power     INTEGER NOT NULL,
            guts      INTEGER NOT NULL,
            races     INTEGER NOT NULL DEFAULT 0,
            wins      INTEGER NOT NULL DEFAULT 0,
            places    INTEGER NOT NULL DEFAULT 0,   -- 3着以内
            earnings  INTEGER NOT NULL DEFAULT 0,   -- 賞金の合計
            bonus     INTEGER NOT NULL DEFAULT 0,   -- 金にんじんで増えた、ステータス合計の増加分
            created   REAL NOT NULL,
            listed_at REAL                          -- 破産で差し押さえられて、帝愛の競売に出た時刻（owner_id=-1 のとき）
        );
        CREATE INDEX IF NOT EXISTS horses_owner ON horses (guild_id, owner_id);
        CREATE TABLE IF NOT EXISTS horse_races (     -- レース（定時と臨時）
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER NOT NULL,
            kind        TEXT NOT NULL,              -- regular / special
            start_at    REAL NOT NULL,
            entry_close REAL NOT NULL,
            bet_close   REAL NOT NULL,
            status      TEXT NOT NULL,              -- entry / betting / done
            distance    INTEGER NOT NULL DEFAULT 0,
            going       TEXT NOT NULL DEFAULT '',
            channel_id  INTEGER NOT NULL DEFAULT 0,
            message_id  INTEGER NOT NULL DEFAULT 0,
            created     REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS horse_races_status ON horse_races (guild_id, status, start_at);
        CREATE TABLE IF NOT EXISTS horse_entries (   -- 出走馬（申し込みと、レース時点のステータスのコピー）
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id   INTEGER NOT NULL,
            lane      INTEGER NOT NULL DEFAULT -1,  -- 枠番（-1 は、締切前の申し込み）
            horse_id  INTEGER NOT NULL DEFAULT 0,   -- 0 は帝愛の馬（NPC）
            owner_id  INTEGER NOT NULL DEFAULT 0,
            name      TEXT NOT NULL,
            coat      TEXT NOT NULL DEFAULT '',
            silks     TEXT NOT NULL DEFAULT '',
            speed     INTEGER NOT NULL DEFAULT 50,
            stamina   INTEGER NOT NULL DEFAULT 50,
            power     INTEGER NOT NULL DEFAULT 50,
            guts      INTEGER NOT NULL DEFAULT 50,
            form      INTEGER NOT NULL DEFAULT 0,
            races     INTEGER NOT NULL DEFAULT 0,   -- 戦績のコピー（オッズに使う。帝愛の馬は作りもの）
            wins      INTEGER NOT NULL DEFAULT 0,
            places    INTEGER NOT NULL DEFAULT 0,
            odds_win  REAL NOT NULL DEFAULT 0,
            odds_place REAL NOT NULL DEFAULT 0,
            finish    INTEGER NOT NULL DEFAULT 0    -- 着順（0 は、まだ）
        );
        CREATE INDEX IF NOT EXISTS horse_entries_race ON horse_entries (race_id, lane);
        CREATE TABLE IF NOT EXISTS horse_bets (      -- 馬券
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            race_id  INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            kind     TEXT NOT NULL,                 -- win / place / quinella / wide / exacta / trio
            lane     INTEGER NOT NULL,              -- 単勝・複勝の枠（組み合わせの馬券では、最初の枠）
            picks    TEXT NOT NULL DEFAULT '',      -- 組み合わせの馬券の選び方（core/horse.py の combo_key。例 "2-5"・"5>2"）
            stake    INTEGER NOT NULL,
            odds     REAL NOT NULL,                 -- 賭けた時点のオッズ（確定）
            payout   INTEGER NOT NULL DEFAULT 0,
            settled  INTEGER NOT NULL DEFAULT 0,
            created  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS horse_bets_race ON horse_bets (race_id, user_id);
        CREATE TABLE IF NOT EXISTS horse_odds (      -- 組み合わせの馬券のオッズ（レースごと。単勝・複勝は horse_entries にある）
            race_id INTEGER NOT NULL,
            kind    TEXT NOT NULL,
            key     TEXT NOT NULL,
            odds    REAL NOT NULL,
            PRIMARY KEY (race_id, kind, key)
        );
        CREATE TABLE IF NOT EXISTS labor (           -- 地下作業（クリッカー）。工具・疲労・まだ受け取っていない稼ぎ
            guild_id     INTEGER NOT NULL,
            user_id      INTEGER NOT NULL,
            pick         INTEGER NOT NULL DEFAULT 0,
            glove        INTEGER NOT NULL DEFAULT 0,
            helmet       INTEGER NOT NULL DEFAULT 0,
            fatigue      REAL NOT NULL DEFAULT 0,
            fatigue_at   REAL NOT NULL DEFAULT 0,
            pot          INTEGER NOT NULL DEFAULT 0,   -- 受け取る前の稼ぎ
            day          TEXT NOT NULL DEFAULT '',
            day_earned   INTEGER NOT NULL DEFAULT 0,   -- 今日の稼ぎ（上限の判定）
            last_click   REAL NOT NULL DEFAULT 0,
            rest_until   REAL NOT NULL DEFAULT 0,
            clicks       INTEGER NOT NULL DEFAULT 0,
            earned_total INTEGER NOT NULL DEFAULT 0,
            apprentice   INTEGER NOT NULL DEFAULT 0,   -- 雇っている作業員の人数
            miner        INTEGER NOT NULL DEFAULT 0,
            craftsman    INTEGER NOT NULL DEFAULT 0,
            foreman      INTEGER NOT NULL DEFAULT 0,
            dorm         INTEGER NOT NULL DEFAULT 0,   -- 寮の段（1段ごとに、雇える人数 +1）
            wage         TEXT NOT NULL DEFAULT 'normal',
            idle_at      REAL NOT NULL DEFAULT 0,      -- 作業員の稼ぎを、最後に数えた時刻
            renew        INTEGER NOT NULL DEFAULT 1,   -- 作業員の契約の自動更新（1=オン）
            ores         TEXT NOT NULL DEFAULT '',     -- 受け取る前の稼ぎに含まれる鉱石（JSON {鉱石: 個数}）
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS labor_workers (    -- 雇っている作業員（1人1行。契約の期限つき）
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            kind       TEXT NOT NULL,
            expires_at REAL NOT NULL,
            legacy     INTEGER NOT NULL DEFAULT 0   -- 1 = 鉱石版より前の契約（起動時に、契約料を返して解約した）
        );
        CREATE INDEX IF NOT EXISTS labor_workers_owner ON labor_workers (guild_id, user_id);
        CREATE TABLE IF NOT EXISTS seized_items (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id  INTEGER NOT NULL,
            item_id   TEXT NOT NULL,
            qty       INTEGER NOT NULL,
            listed_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS seized_items_item ON seized_items (guild_id, item_id, listed_at);
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
        CREATE TABLE IF NOT EXISTS market_prices (     -- 証券の価格（銘柄ごと）。全サーバー共通
            symbol TEXT    NOT NULL DEFAULT 'cs',
            ts     REAL    NOT NULL,
            price  INTEGER NOT NULL,
            event  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (symbol, ts)
        );
        CREATE TABLE IF NOT EXISTS crypto (            -- 銘柄ごとの現物の保有
            guild_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            symbol   TEXT    NOT NULL DEFAULT 'cs',
            units    INTEGER NOT NULL,        -- 保有する口数（1000口 = 1枚）
            cost     INTEGER NOT NULL,        -- 買うのにかかった総額（損益の表示用）
            PRIMARY KEY (guild_id, user_id, symbol)
        );
        CREATE TABLE IF NOT EXISTS crypto_short (      -- 銘柄ごとの空売り（1人が、銘柄ごとに1つの建玉）
            guild_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            symbol      TEXT    NOT NULL DEFAULT 'cs',
            units       INTEGER NOT NULL,         -- 売り建てた口数
            entry_value REAL NOT NULL,            -- 建てたときの総額（口数×価格）。損益 = これ − いまの総額
            margin      INTEGER NOT NULL,         -- 預けた担保
            interest_at REAL NOT NULL DEFAULT 0,  -- 信用金利（貸株料）を、ここまで数えた時刻
            accrued     REAL NOT NULL DEFAULT 0,  -- 数えたが、まだ担保から引いていない金利（小数）
            PRIMARY KEY (guild_id, user_id, symbol)
        );
        CREATE TABLE IF NOT EXISTS crypto_long (       -- 銘柄ごとの信用買い（レバレッジをかけた買い建て。空売りと同じ形）
            guild_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            symbol      TEXT    NOT NULL,
            units       INTEGER NOT NULL,         -- 買い建てた口数
            entry_value REAL NOT NULL,            -- 建てたときの総額（口数×価格）。損益 = いまの総額 − これ
            margin      INTEGER NOT NULL,         -- 預けた担保
            interest_at REAL NOT NULL DEFAULT 0,  -- 信用金利（借入金利）を、ここまで数えた時刻
            accrued     REAL NOT NULL DEFAULT 0,  -- 数えたが、まだ担保から引いていない金利（小数）
            PRIMARY KEY (guild_id, user_id, symbol)
        );
        CREATE TABLE IF NOT EXISTS teiai_bank (        -- 帝愛銀行の手元（サーバーごとに1行）
            guild_id INTEGER PRIMARY KEY,
            cash     REAL NOT NULL DEFAULT 0,          -- すぐ払い出せる現金
            loaned   REAL NOT NULL DEFAULT 0,          -- 地下融資に回している額（時間がたつと現金に戻る）
            ts       REAL NOT NULL DEFAULT 0           -- 融資の回収を、ここまで反映した時刻
        );
        CREATE TABLE IF NOT EXISTS teiai_house (       -- 帝愛の胴元の口座（サーバーごとに1行）。ピンハネが入る。銀行の手元（預金の準備）とは別
            guild_id INTEGER PRIMARY KEY,
            balance  INTEGER NOT NULL DEFAULT 0,       -- いまの残高
            skimmed  INTEGER NOT NULL DEFAULT 0        -- これまでに、ピンハネで入った額の合計
        );
        CREATE TABLE IF NOT EXISTS teiai_deposits (    -- 帝愛への預金（人ごと）
            guild_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            amount   INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS cds (               -- CDS（破綻保証）。買い手が掛け金を払い、対象が強制破産すると配当（自分への CDS は借金を肩代わり）
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id  INTEGER NOT NULL,
            buyer_id  INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            stake     INTEGER NOT NULL,               -- 掛け金
            odds      REAL    NOT NULL,               -- 買ったときのオッズ（配当の見込み = 掛け金 × オッズ）
            ts        REAL    NOT NULL,
            expires   REAL    NOT NULL,
            status    TEXT    NOT NULL DEFAULT 'active',   -- active / paid / expired / void
            paid      INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS options (           -- オプション（爆騰コール・暴落プット）。買った分ごとに1行。相手は帝愛銀行
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id  INTEGER NOT NULL,
            user_id   INTEGER NOT NULL,
            kind      TEXT    NOT NULL,           -- call / put
            symbol    TEXT    NOT NULL,
            strike    REAL    NOT NULL,           -- 権利行使価格（買った時点の価格の ±5%）
            units     INTEGER NOT NULL,           -- 口数（1000口 = 1枚）
            premium   INTEGER NOT NULL,           -- 払った掛け金（最大の損失）
            expiry    REAL    NOT NULL,           -- 満期（epoch秒）
            spot      REAL    NOT NULL,           -- 買った時点の価格
            ts        REAL    NOT NULL,
            status    TEXT    NOT NULL DEFAULT 'active',   -- active / exercised / expired / sold
            paid      INTEGER NOT NULL DEFAULT 0, -- 満期・売却で受け取った額
            final     REAL,                       -- 満期の価格（ペア・ワラントは A の価格）
            symbol2   TEXT,                       -- ペア・ワラントの、比べる相手の銘柄（なければ NULL）
            spot2     REAL,                       -- 買った時点の、相手の価格
            side      TEXT    NOT NULL DEFAULT 'long',   -- long（買い）/ short（裸売り。帝愛に売って、プレミアムを先にもらう）
            margin    INTEGER NOT NULL DEFAULT 0  -- 裸売りで預けた証拠金（買いは 0）
        );
        CREATE TABLE IF NOT EXISTS market_events (     -- 市場イベント（ボラティリティ予告）。市場は全サーバー共通なので、サーバーに属さない
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT NOT NULL,
            announce_ts REAL NOT NULL,                -- 予告した時刻
            start_ts    REAL NOT NULL,                -- 値動きが大きくなり始める時刻
            end_ts      REAL NOT NULL,
            factor      REAL NOT NULL                 -- 値動きの大きさ σ の倍率
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
            counted     INTEGER NOT NULL DEFAULT 1,   -- 出題者の報酬に数えるか。ゲームを3回遊んでいない人の回答は 0（複数アカウントで稼ぐのを防ぐ）
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
            paid      INTEGER NOT NULL DEFAULT 0, -- 満期・売却で戻した額
            ts        REAL    NOT NULL,           -- 買った時刻
            checked   REAL                        -- ジャンク債の倒産抽選を、ここまで済ませた時刻（NULL なら買った時刻）
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
        CREATE TABLE IF NOT EXISTS chart_posts (       -- /証券 市況 で出したチャート（1チャンネルに1つ。最後に使ってから一定時間だけ自動で更新）
            channel_id INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL,
            guild_id   INTEGER NOT NULL DEFAULT 0,
            period     TEXT    NOT NULL DEFAULT '24h',
            ts         REAL    NOT NULL DEFAULT 0,     -- 出した時刻（ここから一定時間、更新する）
            live       INTEGER NOT NULL DEFAULT 1,     -- 1: 更新中 / 0: 更新は終了
            symbol     TEXT    NOT NULL DEFAULT 'cs'       -- 出した銘柄のチャート
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
            enabled INTEGER NOT NULL DEFAULT 1,   -- 1: 称号の表示を、自分で選んだ人。0: 身分の【地下労働者】だけを、Bot が付けている人
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
    hs = {r["name"] for r in conn.execute("PRAGMA table_info(horses)")}
    if "bonus" not in hs:
        conn.execute("ALTER TABLE horses ADD COLUMN bonus INTEGER NOT NULL DEFAULT 0")
    he = {r["name"] for r in conn.execute("PRAGMA table_info(horse_entries)")}
    for col in ("races", "wins", "places"):
        if col not in he:
            conn.execute(f"ALTER TABLE horse_entries ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
    us = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    if "last_slot" not in us:
        conn.execute("ALTER TABLE users ADD COLUMN last_slot INTEGER")
    if "debt" not in us:
        conn.execute("ALTER TABLE users ADD COLUMN debt INTEGER NOT NULL DEFAULT 0")
    if "checked" not in {r["name"] for r in conn.execute("PRAGMA table_info(bonds)")}:
        conn.execute("ALTER TABLE bonds ADD COLUMN checked REAL")
    opt_cols = {r["name"] for r in conn.execute("PRAGMA table_info(options)")}
    for col, ddl in (
        ("symbol2", "TEXT"),
        ("spot2", "REAL"),
        ("side", "TEXT NOT NULL DEFAULT 'long'"),
        ("margin", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if opt_cols and col not in opt_cols:
            conn.execute(f"ALTER TABLE options ADD COLUMN {col} {ddl}")
    ls = {r["name"] for r in conn.execute("PRAGMA table_info(labor)")}
    for col, ddl in (
        ("apprentice", "INTEGER NOT NULL DEFAULT 0"),
        ("miner", "INTEGER NOT NULL DEFAULT 0"),
        ("craftsman", "INTEGER NOT NULL DEFAULT 0"),
        ("foreman", "INTEGER NOT NULL DEFAULT 0"),
        ("dorm", "INTEGER NOT NULL DEFAULT 0"),
        ("wage", "TEXT NOT NULL DEFAULT 'normal'"),
        ("idle_at", "REAL NOT NULL DEFAULT 0"),
        ("renew", "INTEGER NOT NULL DEFAULT 1"),
        ("ores", "TEXT NOT NULL DEFAULT ''"),
    ):
        if col not in ls:
            conn.execute(f"ALTER TABLE labor ADD COLUMN {col} {ddl}")
    _migrate_labor_counts(conn)
    if "legacy" not in {r["name"] for r in conn.execute("PRAGMA table_info(labor_workers)")}:
        conn.execute("ALTER TABLE labor_workers ADD COLUMN legacy INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE labor_workers SET legacy=1")  # この列を足す前に雇われていた人は、すべて旧ルールの契約
        _refund_legacy_contracts(conn)
    for table in ("crypto_short", "crypto_long"):  # 信用金利の列（建玉の数え始めの時刻と、まだ引いていない端数）
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and "interest_at" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN interest_at REAL NOT NULL DEFAULT 0")
            conn.execute(f"ALTER TABLE {table} ADD COLUMN accrued REAL NOT NULL DEFAULT 0")
    if "debt_interest_at" not in us:
        conn.execute("ALTER TABLE users ADD COLUMN debt_interest_at REAL NOT NULL DEFAULT 0")
    if "grace_until" not in us:
        conn.execute("ALTER TABLE users ADD COLUMN grace_until REAL")
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
    if "enabled" not in cols:  # 既存の行は、称号の表示を自分で選んだ人
        conn.execute("ALTER TABLE nick_prefs ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
    answers = {r["name"] for r in conn.execute("PRAGMA table_info(quiz_answers)")}
    if "counted" not in answers:
        conn.execute("ALTER TABLE quiz_answers ADD COLUMN counted INTEGER NOT NULL DEFAULT 1")
    profile_cols = {r["name"] for r in conn.execute("PRAGMA table_info(profile)")}
    for col in ("skin_dice", "skin_table"):
        if col not in profile_cols:
            conn.execute(f"ALTER TABLE profile ADD COLUMN {col} TEXT")
    users_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    for col in ("bankrupt_at", "blacklist_until", "escape_at"):
        if col not in users_cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} REAL")
    for table, col, ddl in (
        ("horse_bets", "picks", "TEXT NOT NULL DEFAULT ''"),
        ("horses", "listed_at", "REAL"),
    ):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    _add_symbol_columns(conn)


# 銘柄を足す前の表（主キーに symbol がない）。CSトークンだけだった頃の記録は、すべて symbol='cs' として移す
_SYMBOL_TABLES = {
    "market_prices": (
        "CREATE TABLE market_prices (symbol TEXT NOT NULL DEFAULT 'cs', ts REAL NOT NULL, price INTEGER NOT NULL, "
        "event INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (symbol, ts))",
        "ts, price, event",
    ),
    "crypto": (
        "CREATE TABLE crypto (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, symbol TEXT NOT NULL DEFAULT 'cs', "
        "units INTEGER NOT NULL, cost INTEGER NOT NULL, PRIMARY KEY (guild_id, user_id, symbol))",
        "guild_id, user_id, units, cost",
    ),
    "crypto_short": (
        "CREATE TABLE crypto_short (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, symbol TEXT NOT NULL DEFAULT 'cs', "
        "units INTEGER NOT NULL, entry_value REAL NOT NULL, margin INTEGER NOT NULL, PRIMARY KEY (guild_id, user_id, symbol))",
        "guild_id, user_id, units, entry_value, margin",
    ),
}


def _migrate_labor_counts(conn: sqlite3.Connection, now: float = None) -> None:
    """作業員を「人数の列」で持っていた版から、契約つきの1人1行の表（labor_workers）へ移す。いまの人は、今から7日の契約にする（何度動かしても安全）。"""
    import time

    now = time.time() if now is None else now
    for r in conn.execute(
        "SELECT guild_id, user_id, apprentice, miner, craftsman, foreman FROM labor "
        "WHERE apprentice>0 OR miner>0 OR craftsman>0 OR foreman>0"
    ).fetchall():
        for kind in ("apprentice", "miner", "craftsman", "foreman"):
            for _ in range(r[kind]):
                conn.execute(
                    "INSERT INTO labor_workers (guild_id, user_id, kind, expires_at) VALUES (?, ?, ?, ?)",
                    (r["guild_id"], r["user_id"], kind, now + 7 * 86400.0),
                )
        conn.execute(
            "UPDATE labor SET apprentice=0, miner=0, craftsman=0, foreman=0 WHERE guild_id=? AND user_id=?",
            (r["guild_id"], r["user_id"]),
        )


def _refund_legacy_contracts(conn: sqlite3.Connection, now: float = None) -> None:
    """鉱石版より前に雇った作業員の契約料を、いまの値段で返して、契約を解約する（ルール変更の補償）。列を足したときに、1回だけ動く。"""
    import time

    from core import labor

    now = time.time() if now is None else now
    refunds: dict = {}
    for r in conn.execute("SELECT id, guild_id, user_id, kind FROM labor_workers WHERE legacy=1").fetchall():
        key = (r["guild_id"], r["user_id"])
        refunds[key] = refunds.get(key, 0) + labor.CONTRACT_FEE.get(r["kind"], 0)
    for (guild_id, user_id), amount in refunds.items():
        row = conn.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()
        if row is not None and amount > 0:
            after = row["balance"] + amount
            conn.execute("UPDATE users SET balance=? WHERE guild_id=? AND user_id=?", (after, guild_id, user_id))
            conn.execute(
                "INSERT INTO ledger (guild_id, user_id, delta, balance_after, reason, ts) VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, user_id, amount, after, "地下作業:契約料の返金（ルール変更で解約）", now),
            )
    conn.execute("DELETE FROM labor_workers WHERE legacy=1")


def _add_symbol_columns(conn: sqlite3.Connection) -> None:
    """市場・保有・空売りの表に、銘柄の列を足す（主キーが変わるので、表を作り直して、記録を写す）。チャートの記録は、列を足すだけ。"""
    for table, (ddl, columns) in _SYMBOL_TABLES.items():
        if "symbol" in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
            continue
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        conn.execute(ddl)
        conn.execute(f"INSERT INTO {table} (symbol, {columns}) SELECT 'cs', {columns} FROM {table}_old")
        conn.execute(f"DROP TABLE {table}_old")
    if "symbol" not in {r["name"] for r in conn.execute("PRAGMA table_info(chart_posts)")}:
        conn.execute("ALTER TABLE chart_posts ADD COLUMN symbol TEXT NOT NULL DEFAULT 'cs'")
