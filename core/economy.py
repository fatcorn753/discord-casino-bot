"""
ペリカの経済（残高・預かり金・デイリー・送金・ランキング）。

標準ライブラリの sqlite3 だけを使う。Bot 全体で 1 つの Economy を共有する。

残高・成績・デイリー・持ち物・履歴は **サーバーごと** に別々。呼び出し側は `economy.for_guild(サーバーID)` で
そのサーバー専用の見え方を取り、そこから各メソッドを呼ぶ（別のサーバーの記録とは混ざらない）。

設計の要点:
- 残高の増減は必ず 1 回のトランザクションで行い、台帳（ledger）に残す。連打や同時操作で残高が狂わない
- 賭け金は開始時に「預かり金（escrow）」として残高から引く。ゲームが終わったら払い戻し額を精算する。
  Bot が途中で落ちた場合は、起動時に預かり金を全額返す（賭け金が消えない）
- 残高はマイナスにならない（負けても 0 まで）
"""

import collections
import copy
import datetime
import math
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

from core import history as history_mod
from core import market as market_mod
from core import missions as missions_mod
from core import shop as shop_mod
from core import weekly as weekly_mod

INITIAL_BALANCE = 3000
INSURANCE_CAP = 1_000     # 保険券で戻る額の上限（core/shop.py の説明と同じ値）

# 消耗品 → 効果の列。呼び出し側の指定ではなく、ここで対応を固定する
CONSUMABLE_EFFECTS = {"daily_double": "daily_boost", "insurance": "insurance"}


@dataclass(frozen=True)
class DailyResult:
    ok: bool
    amount: int = 0
    streak: int = 0
    balance: int = 0
    boosted: bool = False        # デイリー2倍券を使った


@dataclass(frozen=True)
class PurchaseResult:
    ok: bool
    reason: str = ""
    balance: int = 0


@dataclass(frozen=True)
class CryptoResult:
    ok: bool
    reason: str = ""
    units: int = 0       # 売買した口数
    money: int = 0       # 買い: 支払った総額 / 売り: 受け取った額（どちらも手数料込み）
    fee: int = 0
    balance: int = 0
    profit: int = 0      # 売り: 買った額に対する損益


@dataclass(frozen=True)
class TransferResult:
    ok: bool
    reason: str = ""
    received: int = 0
    fee: int = 0
    sender_balance: int = 0


class Economy:
    DAILY_BASE = 1000
    DAILY_STREAK_BONUS = 100     # 連続1日ごとの加算
    DAILY_STREAK_CAP = 9         # 加算は9日分まで（最大 +900）

    def __init__(self, db_path: str, legacy_guild_id: Optional[int] = None, strict: bool = False):
        """legacy_guild_id: サーバー別になる前の古い記録を引き継ぐサーバー。古い記録があるのに未指定ならエラー。
        strict: True なら、for_guild を通さずに使うとエラーにする（Bot 本体で、指定漏れを防ぐ）。
        """
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self.guild_id = 0
        self._strict = strict
        self._migrate_to_guild_scope(legacy_guild_id)
        self._init_schema()

    # ------------------------------------------------------------------ #
    # サーバー単位の見え方                                                #
    # ------------------------------------------------------------------ #
    def for_guild(self, guild_id: int) -> "Economy":
        """そのサーバー専用の見え方を返す（接続とロックは共有する）。"""
        gid = int(guild_id)
        if gid <= 0:
            raise ValueError(f"サーバーIDが不正です: {guild_id!r}")
        view = copy.copy(self)
        view.guild_id = gid
        view._strict = False
        return view

    def _g(self) -> int:
        if self._strict and self.guild_id == 0:
            raise RuntimeError("サーバーが指定されていません。economy.for_guild(サーバーID) を通して使ってください。")
        return self.guild_id

    # ------------------------------------------------------------------ #
    # 記録の形（スキーマ）と、サーバー別になる前の記録の移行                #
    # ------------------------------------------------------------------ #
    def _migrate_to_guild_scope(self, legacy_guild_id: Optional[int]) -> None:
        """サーバー別になる前の記録（users に guild_id が無い）を、指定したサーバーのものとして引き継ぐ。"""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(users)")}
        if not cols or "guild_id" in cols:
            return                           # 新規、または移行済み
        if legacy_guild_id is None:
            raise RuntimeError(
                "古い形式の記録があります。引き継ぎ先のサーバーIDを、config.json の legacy_guild_id に設定してください。")
        gid = int(legacy_guild_id)
        if gid <= 0:
            raise ValueError(f"legacy_guild_id が不正です: {legacy_guild_id!r}")
        backup = self._db_path + ".before-guild-scope"
        if not os.path.exists(backup):       # 万一に備えて、移行の前に記録を丸ごと退避する
            tmp = backup + ".tmp"            # 途中で落ちた不完全なファイルを、退避済みと誤認しないよう最後に改名する
            dst = sqlite3.connect(tmp)
            self._conn.backup(dst)
            dst.close()
            os.replace(tmp, backup)
        c = self._conn
        have = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("ALTER TABLE users RENAME TO users_old")
            c.execute(self._USERS_DDL)
            c.execute("INSERT INTO users (guild_id, user_id, balance, daily_streak, last_daily, games_played, total_wagered, "
                      "total_won, biggest_win, created_at) SELECT ?, user_id, balance, daily_streak, last_daily, games_played, "
                      "total_wagered, total_won, biggest_win, created_at FROM users_old", (gid,))
            c.execute("DROP TABLE users_old")
            c.execute("DROP INDEX IF EXISTS ledger_user")
            for t in ("ledger", "escrow"):   # 無い表は、あとの _init_schema が新しい形で作る
                if t in have:
                    c.execute(f"ALTER TABLE {t} ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 0")
                    c.execute(f"UPDATE {t} SET guild_id=?", (gid,))
            if "inventory" in have:
                c.execute("ALTER TABLE inventory RENAME TO inventory_old")
                c.execute(self._INVENTORY_DDL)
                c.execute("INSERT INTO inventory (guild_id, user_id, item_id, qty) SELECT ?, user_id, item_id, qty FROM inventory_old", (gid,))
                c.execute("DROP TABLE inventory_old")
            if "profile" in have:
                c.execute("ALTER TABLE profile RENAME TO profile_old")
                c.execute(self._PROFILE_DDL)
                c.execute("INSERT INTO profile (guild_id, user_id, title, card_color, card_icon, daily_boost, insurance) "
                          "SELECT ?, user_id, title, card_color, card_icon, daily_boost, insurance FROM profile_old", (gid,))
                c.execute("DROP TABLE profile_old")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    _USERS_DDL = """
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
            PRIMARY KEY (guild_id, user_id)
        )"""
    _INVENTORY_DDL = """
        CREATE TABLE IF NOT EXISTS inventory (
            guild_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            item_id  TEXT    NOT NULL,
            qty      INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id, item_id)
        )"""
    _PROFILE_DDL = """
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

    def _init_schema(self):
        with self._lock, self._conn:
            self._conn.execute(self._USERS_DDL)
            self._conn.execute(self._INVENTORY_DDL)
            self._conn.execute(self._PROFILE_DDL)
            self._conn.executescript(
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
            # 作成済みの古い表に、列を足す（初回の版には元のニックネームの記録がなかった）
            us = {r["name"] for r in self._conn.execute("PRAGMA table_info(users)")}
            if "last_slot" not in us:
                self._conn.execute("ALTER TABLE users ADD COLUMN last_slot INTEGER")
            ps = {r["name"] for r in self._conn.execute("PRAGMA table_info(play_state)")}
            for col, ddl in (("day_streak", "INTEGER NOT NULL DEFAULT 0"), ("streak_day", "TEXT"),
                             ("plays", "INTEGER NOT NULL DEFAULT 0"), ("wins", "INTEGER NOT NULL DEFAULT 0"), ("net_total", "INTEGER NOT NULL DEFAULT 0")):
                if col not in ps:
                    self._conn.execute(f"ALTER TABLE play_state ADD COLUMN {col} {ddl}")
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(nick_prefs)")}
            for col in ("original_nick", "applied_nick"):
                if col not in cols:
                    self._conn.execute(f"ALTER TABLE nick_prefs ADD COLUMN {col} TEXT")

    # ------------------------------------------------------------------ #
    # 内部                                                                #
    # ------------------------------------------------------------------ #
    def _ensure(self, user_id: int) -> None:
        """未登録なら初期残高つきで作る（呼び出し側のトランザクション内で使う）。"""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO users (guild_id, user_id, balance, created_at) VALUES (?, ?, ?, ?)",
            (self._g(), user_id, INITIAL_BALANCE, time.time()),
        )
        if cur.rowcount:
            self._ledger(user_id, INITIAL_BALANCE, INITIAL_BALANCE, "初期支給")

    def _ledger(self, user_id: int, delta: int, balance_after: int, reason: str) -> None:
        self._conn.execute(
            "INSERT INTO ledger (guild_id, user_id, delta, balance_after, reason, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (self._g(), user_id, delta, balance_after, reason, time.time()),
        )

    def _balance(self, user_id: int) -> int:
        return self._conn.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)).fetchone()["balance"]

    def _apply(self, user_id: int, delta: int, reason: str) -> int:
        """残高に delta を足す（0 未満にはならない）。実際に変化した量を返す。"""
        before = self._balance(user_id)
        after = max(0, before + delta)
        self._conn.execute("UPDATE users SET balance=? WHERE guild_id=? AND user_id=?", (after, self._g(), user_id))
        if after != before:
            self._ledger(user_id, after - before, after, reason)
        return after - before

    # ------------------------------------------------------------------ #
    # 残高                                                                #
    # ------------------------------------------------------------------ #
    def balance(self, user_id: int) -> int:
        with self._lock, self._conn:
            self._ensure(user_id)
            return self._balance(user_id)

    def credit(self, user_id: int, amount: int, reason: str) -> int:
        """残高を増やす（amount > 0）。管理者の付与などに使う。"""
        if amount <= 0:
            raise ValueError("amount は正の数にしてください")
        with self._lock, self._conn:
            self._ensure(user_id)
            self._apply(user_id, amount, reason)
            return self._balance(user_id)

    def debit_up_to(self, user_id: int, amount: int, reason: str) -> int:
        """残高の範囲で amount を引く。実際に引けた額を返す（チンチロの倍払いなど）。"""
        if amount <= 0:
            return 0
        with self._lock, self._conn:
            self._ensure(user_id)
            return -self._apply(user_id, -amount, reason)

    # ------------------------------------------------------------------ #
    # 賭け金の預かりと精算                                                #
    # ------------------------------------------------------------------ #
    def place_bet(self, user_id: int, amount: int, game: str) -> Optional[int]:
        """賭け金を残高から預かり、預かり金IDを返す。残高不足なら None。"""
        if amount <= 0:
            return None
        with self._lock, self._conn:
            self._ensure(user_id)
            if self._balance(user_id) < amount:
                return None
            self._apply(user_id, -amount, f"賭け:{game}")
            cur = self._conn.execute(
                "INSERT INTO escrow (guild_id, user_id, amount, game, created_at) VALUES (?, ?, ?, ?, ?)",
                (self._g(), user_id, amount, game, time.time()),
            )
            self._conn.execute("UPDATE users SET total_wagered = total_wagered + ? WHERE guild_id=? AND user_id=?",
                               (amount, self._g(), user_id))
            return cur.lastrowid

    def add_to_escrow(self, escrow_id: int, extra: int) -> bool:
        """預かり金を増やす（ブラックジャックのダブルダウン）。残高不足なら False。"""
        with self._lock, self._conn:
            row = self._conn.execute("SELECT user_id, game FROM escrow WHERE id=? AND guild_id=?", (escrow_id, self._g())).fetchone()
            if row is None or extra <= 0 or self._balance(row["user_id"]) < extra:
                return False
            self._apply(row["user_id"], -extra, f"追加の賭け:{row['game']}")
            self._conn.execute("UPDATE escrow SET amount = amount + ? WHERE id=? AND guild_id=?", (extra, escrow_id, self._g()))
            self._conn.execute("UPDATE users SET total_wagered = total_wagered + ? WHERE guild_id=? AND user_id=?",
                               (extra, self._g(), row["user_id"]))
            return True

    def escrow_amount(self, escrow_id: int) -> int:
        with self._lock:
            row = self._conn.execute("SELECT amount FROM escrow WHERE id=? AND guild_id=?", (escrow_id, self._g())).fetchone()
            return row["amount"] if row else 0

    def settle(self, escrow_id: int, payout: int, game: str) -> Optional[int]:
        """預かり金を精算する。payout は「受け取る総額」（賭け金を含む。負けなら 0）。

        すでに精算済みなら何もせず None（二重払いを防ぐ）。成功時は損益（payout - 賭け金）を返す。
        """
        result = self.settle_with_penalty(escrow_id, payout, game, 0)
        return None if result is None else result[0]

    def settle_with_penalty(self, escrow_id: int, payout: int, game: str, penalty: int) -> Optional[tuple]:
        """精算と、倍払いなどの追加の支払いを、1回のトランザクションで行う。

        penalty は残高の範囲まで引く。負けて払い戻しが 0 のとき、保険券があれば賭け金の半分を返す。
        (損益（追加の支払い・保険を含む）, 実際に引いた追加額, 保険で戻った額) を返す。
        すでに精算済みなら何もせず None（二重に精算も追加の支払いもしない）。
        """
        with self._lock, self._conn:
            row = self._conn.execute("SELECT user_id, amount FROM escrow WHERE id=? AND guild_id=?", (escrow_id, self._g())).fetchone()
            if row is None:
                return None
            user_id, amount = row["user_id"], row["amount"]
            payout = max(0, int(payout))
            insured = 0
            if payout == 0:
                prof = self._conn.execute("SELECT insurance FROM profile WHERE guild_id=? AND user_id=?", (self._g(), user_id)).fetchone()
                if prof and prof["insurance"] > 0:
                    insured = min(amount // 2, INSURANCE_CAP)
                    self._conn.execute("UPDATE profile SET insurance = insurance - 1 WHERE guild_id=? AND user_id=?", (self._g(), user_id))
            total = payout + insured
            if total:
                self._apply(user_id, total, f"払い戻し:{game}" if payout else f"保険券で半額返金:{game}")
            self._conn.execute("DELETE FROM escrow WHERE id=? AND guild_id=?", (escrow_id, self._g()))
            taken = -self._apply(user_id, -penalty, f"追加の支払い:{game}") if penalty > 0 else 0
            net = total - amount - taken
            self._conn.execute(
                "UPDATE users SET games_played = games_played + 1, total_won = total_won + ?, "
                "biggest_win = MAX(biggest_win, ?) WHERE guild_id=? AND user_id=?",
                (total, net, self._g(), user_id),
            )
            return net, taken, insured

    def refund(self, escrow_id: int) -> bool:
        """エラー時の返金。賭け金（追加分を含む）を全額返す。成績（遊んだ回数・勝ち額）には数えない。"""
        with self._lock, self._conn:
            row = self._conn.execute("SELECT user_id, amount, game FROM escrow WHERE id=? AND guild_id=?", (escrow_id, self._g())).fetchone()
            if row is None:
                return False
            self._apply(row["user_id"], row["amount"], f"返金:{row['game']}")
            self._conn.execute("DELETE FROM escrow WHERE id=? AND guild_id=?", (escrow_id, self._g()))
            return True

    def refund_all_escrows(self) -> int:
        """起動時に呼ぶ。Bot が落ちて残った預かり金を、すべて持ち主に返す。返した件数を返す。"""
        with self._lock, self._conn:
            rows = self._conn.execute("SELECT id, guild_id, user_id, amount, game FROM escrow").fetchall()
            for r in rows:      # 全サーバー分。それぞれ、預かったサーバーの口座へ返す
                try:
                    view = self.for_guild(r["guild_id"])
                except ValueError:           # 出どころ不明の預かり金で、起動全体を止めない
                    self._conn.execute("DELETE FROM escrow WHERE id=?", (r["id"],))
                    continue
                view._ensure(r["user_id"])
                view._apply(r["user_id"], r["amount"], f"返金（中断したゲーム）:{r['game']}")
                self._conn.execute("DELETE FROM escrow WHERE id=?", (r["id"],))
            return len(rows)

    # ------------------------------------------------------------------ #
    # 連勝とデイリーミッション                                            #
    # ------------------------------------------------------------------ #
    def record_play(self, user_id: int, game: str, bet: int, net: int, day: datetime.date, tags=()) -> dict:
        """1回遊んだ結果を記録する。連勝を更新し、デイリーミッションを進めて、達成した分の報酬を渡す。

        {"streak", "new_best", "completed": [(ミッション, 報酬)], "bonus"} を返す。引き分け（損益0）は連勝を変えない。
        tags は、ゲームが付ける印（ブラックジャックのダブル勝ちなど）。
        """
        with self._lock, self._conn:
            self._ensure(user_id)
            g, d = self._g(), day.isoformat()
            row = self._conn.execute("SELECT win_streak, best_streak, day_streak, streak_day FROM play_state WHERE guild_id=? AND user_id=?", (g, user_id)).fetchone()
            streak, best = (row["win_streak"], row["best_streak"]) if row else (0, 0)
            day_streak = row["day_streak"] if row and row["streak_day"] == d else 0      # ミッション用の連勝は、日をまたいで持ち越さない
            if net > 0:
                streak += 1
            elif net < 0:
                streak = 0
            counts = bet >= missions_mod.MIN_BET            # 小さい賭けの量産でミッションを埋められないようにする
            if counts:
                day_streak = day_streak + 1 if net > 0 else (0 if net < 0 else day_streak)
            new_best = streak > best
            best = max(best, streak)
            self._conn.execute("INSERT INTO play_state (guild_id, user_id, win_streak, best_streak, day_streak, streak_day, plays, wins, net_total) "
                               "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?) "
                               "ON CONFLICT(guild_id, user_id) DO UPDATE SET win_streak=excluded.win_streak, best_streak=excluded.best_streak, "
                               "day_streak=excluded.day_streak, streak_day=excluded.streak_day, plays=plays+1, wins=wins+excluded.wins, "
                               "net_total=net_total+excluded.net_total",
                               (g, user_id, streak, best, day_streak, d if counts else (row["streak_day"] if row else None), int(net > 0), net))
            self._conn.execute("INSERT INTO weekly_stats (guild_id, week, user_id, net, plays) VALUES (?, ?, ?, ?, 1) "
                               "ON CONFLICT(guild_id, week, user_id) DO UPDATE SET net=net+excluded.net, plays=plays+1",
                               (g, weekly_mod.week_id(day), user_id, net))
            completed, bonus = [], 0
            missions = missions_mod.today_missions(day) if counts else []
            for m in missions:
                r = self._conn.execute("SELECT progress, done FROM missions WHERE guild_id=? AND user_id=? AND day=? AND mission_id=?",
                                       (g, user_id, d, m.id)).fetchone()
                if r and r["done"]:
                    continue
                progress = missions_mod.progress_after(m, r["progress"] if r else 0, game, bet, net, day_streak, tags)
                done = progress >= m.target
                self._conn.execute("INSERT INTO missions (guild_id, user_id, day, mission_id, progress, done) VALUES (?, ?, ?, ?, ?, ?) "
                                   "ON CONFLICT(guild_id, user_id, day, mission_id) DO UPDATE SET progress=excluded.progress, done=excluded.done",
                                   (g, user_id, d, m.id, min(progress, m.target), int(done)))
                if done:
                    self._apply(user_id, m.reward, f"ミッション報酬:{m.name}")
                    completed.append((m, m.reward))
            if completed:
                n = self._conn.execute("SELECT COUNT(*) FROM missions WHERE guild_id=? AND user_id=? AND day=? AND done=1", (g, user_id, d)).fetchone()[0]
                if n >= missions_mod.PER_DAY:
                    bonus = missions_mod.COMPLETE_BONUS
                    self._apply(user_id, bonus, "ミッション全達成ボーナス")
            return {"streak": streak, "new_best": new_best, "completed": completed, "bonus": bonus}

    def mission_status(self, user_id: int, day: datetime.date) -> list:
        """今日のミッションごとの (ミッション, 進み具合, 達成済みか)。口座は作らない。"""
        with self._lock:
            out = []
            for m in missions_mod.today_missions(day):
                r = self._conn.execute("SELECT progress, done FROM missions WHERE guild_id=? AND user_id=? AND day=? AND mission_id=?",
                                       (self._g(), user_id, day.isoformat(), m.id)).fetchone()
                out.append((m, r["progress"] if r else 0, bool(r and r["done"])))
            return out

    # ------------------------------------------------------------------ #
    # 週間ランキング（先週の稼ぎ1位に、限定称号）                           #
    # ------------------------------------------------------------------ #
    def weekly_top(self, week: str, limit: int = 10) -> list:
        """[(ユーザーID, 稼ぎ, ゲーム数), ...]。その週の稼ぎが多い順（同じなら、ゲーム数が少ない順）。"""
        with self._lock:
            return [(r["user_id"], r["net"], r["plays"]) for r in self._conn.execute(
                "SELECT user_id, net, plays FROM weekly_stats WHERE guild_id=? AND week=? ORDER BY net DESC, plays ASC, user_id LIMIT ?",
                (self._g(), week, limit))]

    def weekly_rank(self, week: str, user_id: int) -> Optional[tuple]:
        """(順位, 稼ぎ, ゲーム数, 人数)。その週に遊んでいなければ None。"""
        rows = self.weekly_top(week, 10_000)
        for i, (uid, net, plays) in enumerate(rows):
            if uid == user_id:
                return i + 1, net, plays, len(rows)
        return None

    def weekly_last_announced(self) -> Optional[str]:
        with self._lock:
            r = self._conn.execute("SELECT last_week FROM weekly_state WHERE guild_id=?", (self._g(),)).fetchone()
            return r["last_week"] if r else None

    def set_weekly_announced(self, week: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO weekly_state (guild_id, last_week) VALUES (?, ?) "
                               "ON CONFLICT(guild_id) DO UPDATE SET last_week=excluded.last_week", (self._g(), week))

    def weekly_champion_count(self, user_id: int) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM weekly_champions WHERE guild_id=? AND user_id=?", (self._g(), user_id)).fetchone()[0]

    def weekly_award(self, week: str) -> Optional[dict]:
        """その週の発表。王者（稼ぎがプラスで、ゲーム数が規定以上の1位）に限定称号を贈り、前の週の保持者からは外す。

        すでにその週以降を発表済みなら何もせず None。{"champion": (ユーザーID, 稼ぎ, ゲーム数) or None, "previous": [外れた人], "wins": 通算回数, "standings": 上位}
        """
        with self._lock, self._conn:
            last = self.weekly_last_announced()
            if last is not None and last >= week:
                return None
            g, king = self._g(), shop_mod.WEEKLY_KING
            previous = [r["user_id"] for r in self._conn.execute("SELECT user_id FROM inventory WHERE guild_id=? AND item_id=?", (g, king))]
            self._conn.execute("DELETE FROM inventory WHERE guild_id=? AND item_id=?", (g, king))
            self._conn.execute("UPDATE profile SET title=NULL WHERE guild_id=? AND title=?", (g, king))     # 装備していた人は、外す
            top = self._conn.execute(
                "SELECT user_id, net, plays FROM weekly_stats WHERE guild_id=? AND week=? AND net>0 AND plays>=? "
                "ORDER BY net DESC, plays ASC, user_id LIMIT 1", (g, week, weekly_mod.MIN_PLAYS)).fetchone()
            champion, wins = None, 0
            if top:
                champion = (top["user_id"], top["net"], top["plays"])
                self._conn.execute("INSERT OR REPLACE INTO weekly_champions (guild_id, week, user_id, net) VALUES (?, ?, ?, ?)", (g, week, top["user_id"], top["net"]))
                self._conn.execute("INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, 1) "
                                   "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty=1", (g, top["user_id"], king))
                wins = self.weekly_champion_count(top["user_id"])
            self.set_weekly_announced(week)
            return {"champion": champion, "previous": [u for u in previous if not champion or u != champion[0]],
                    "wins": wins, "standings": self.weekly_top(week, weekly_mod.ANNOUNCE_TOP)}

    def backfill_weekly(self, utc_offset_hours: float = 9) -> int:
        """この機能の前に遊んだ分を、台帳から週間の記録に入れる（初回だけ）。入れたゲーム数を返す。"""
        with self._lock, self._conn:
            if self._conn.execute("SELECT 1 FROM weekly_state WHERE guild_id=0").fetchone():
                return 0
            tz = datetime.timezone(datetime.timedelta(hours=float(utc_offset_hours)))
            by_user = collections.defaultdict(list)
            for r in self._conn.execute("SELECT guild_id, user_id, delta, balance_after, reason, ts FROM ledger ORDER BY id"):
                by_user[(r["guild_id"], r["user_id"])].append(dict(r))
            count = 0
            for (g, uid), rows in by_user.items():
                if g <= 0:
                    continue
                for ts, _game, net in history_mod.game_results(rows):
                    week = weekly_mod.week_id(datetime.datetime.fromtimestamp(ts, tz).date())
                    self._conn.execute("INSERT INTO weekly_stats (guild_id, week, user_id, net, plays) VALUES (?, ?, ?, ?, 1) "
                                       "ON CONFLICT(guild_id, week, user_id) DO UPDATE SET net=net+excluded.net, plays=plays+1", (g, week, uid, net))
                    count += 1
            self._conn.execute("INSERT INTO weekly_state (guild_id, last_week) VALUES (0, 'backfilled')")
            return count

    def play_summary(self, user_id: int) -> dict:
        """ゲームの成績（遊んだ回数・勝った回数・損益・最大連勝）。読み取り専用で、口座は作らない。"""
        with self._lock:
            r = self._conn.execute("SELECT plays, wins, net_total, best_streak FROM play_state WHERE guild_id=? AND user_id=?",
                                   (self._g(), user_id)).fetchone()
            return dict(r) if r else {"plays": 0, "wins": 0, "net_total": 0, "best_streak": 0}

    def win_streak(self, user_id: int) -> tuple:
        """(いまの連勝, 自己ベスト)。"""
        with self._lock:
            r = self._conn.execute("SELECT win_streak, best_streak FROM play_state WHERE guild_id=? AND user_id=?", (self._g(), user_id)).fetchone()
            return (r["win_streak"], r["best_streak"]) if r else (0, 0)

    # ------------------------------------------------------------------ #
    # CSトークン                                                          #
    # ------------------------------------------------------------------ #
    def market_latest(self) -> Optional[tuple]:
        """いまの価格 (時刻, 価格, 出来事)。まだ無ければ None。時計が戻っても、最後に記録した行を最新とする。"""
        with self._lock:
            r = self._conn.execute("SELECT ts, price, event FROM market_prices ORDER BY rowid DESC LIMIT 1").fetchone()
            return (r["ts"], r["price"], r["event"]) if r else None

    def market_set(self, ts: float, price: float, event: int = 0) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO market_prices (ts, price, event) VALUES (?, ?, ?)", (ts, price, event))

    def market_history(self, since: float) -> list:
        """since（UNIX時間）以降の価格の列 [(時刻, 価格, 出来事), ...]。古い順。"""
        with self._lock:
            return [(r["ts"], r["price"], r["event"]) for r in self._conn.execute(
                "SELECT ts, price, event FROM market_prices WHERE ts>=? ORDER BY ts", (since,))]

    def market_prune(self, before: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM market_prices WHERE ts<?", (before,))

    def crypto_holding(self, user_id: int) -> tuple:
        """(保有口数, 買うのにかかった総額)。読み取り専用。"""
        with self._lock:
            r = self._conn.execute("SELECT units, cost FROM crypto WHERE guild_id=? AND user_id=?", (self._g(), user_id)).fetchone()
            return (r["units"], r["cost"]) if r else (0, 0)

    def crypto_holders(self, limit: int = 10) -> list:
        """このサーバーの保有者 [(user_id, 口数, 買った総額), ...]。買った額の大きい順。"""
        with self._lock:
            return [(r["user_id"], r["units"], r["cost"]) for r in self._conn.execute(
                "SELECT user_id, units, cost FROM crypto WHERE guild_id=? AND units>0 ORDER BY cost DESC, user_id LIMIT ?", (self._g(), limit))]

    def crypto_buy(self, user_id: int, amount: int, price: float) -> "CryptoResult":
        """amount ペリカの範囲で、price の価格で買えるだけ買う（手数料込み）。"""
        with self._lock, self._conn:
            self._ensure(user_id)
            if amount <= 0 or price <= 0:
                return CryptoResult(False, "金額が正しくありません。")
            if self._balance(user_id) < amount:
                return CryptoResult(False, f"ペリカが足りません（残高 {self._balance(user_id):,}ペリカ）。", balance=self._balance(user_id))
            units, total, fee = market_mod.buy_quote(amount, price)
            if units < 1:
                return CryptoResult(False, "その金額では 0.001枚も買えません。金額を増やしてください。", balance=self._balance(user_id))
            self._apply(user_id, -total, f"{market_mod.NAME}購入:{market_mod.lots(units)}@{market_mod.fmt(price)}")
            self._conn.execute(
                "INSERT INTO crypto (guild_id, user_id, units, cost) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET units=units+excluded.units, cost=cost+excluded.cost",
                (self._g(), user_id, units, total))
            return CryptoResult(True, units=units, money=total, fee=fee, balance=self._balance(user_id))

    def crypto_sell(self, user_id: int, units: Optional[int], price: float) -> "CryptoResult":
        """units 口を売る（None なら全部）。受け取る額は手数料を引いたもの。"""
        with self._lock, self._conn:
            have, cost = self.crypto_holding(user_id)
            if have <= 0:
                return CryptoResult(False, f"{market_mod.NAME}を持っていません。")
            units = have if units is None else units
            if units < 1 or units > have:
                return CryptoResult(False, f"売れるのは 0.001〜{market_mod.lots(have)} です（持っている分）。")
            proceeds, fee = market_mod.sell_quote(units, price)
            if proceeds < 1 and units != have:
                return CryptoResult(False, "その数量では受け取れる額が 1ペリカ未満です。もう少し多く売ってください。")
            removed = cost if units == have else round(cost * units / have)       # 端数の按分は四捨五入（切り捨てだと取得額が少しずつ残る）
            self._ensure(user_id)
            self._apply(user_id, proceeds, f"{market_mod.NAME}売却:{market_mod.lots(units)}@{market_mod.fmt(price)}")
            if units == have:
                self._conn.execute("DELETE FROM crypto WHERE guild_id=? AND user_id=?", (self._g(), user_id))
            else:
                self._conn.execute("UPDATE crypto SET units=units-?, cost=cost-? WHERE guild_id=? AND user_id=?", (units, removed, self._g(), user_id))
            return CryptoResult(True, units=units, money=proceeds, fee=fee, balance=self._balance(user_id), profit=proceeds - removed)

    def set_chart_board(self, guild_id: int, channel_id: int, message_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO chart_boards (guild_id, channel_id, message_id) VALUES (?, ?, ?)", (guild_id, channel_id, message_id))

    def get_chart_board(self, guild_id: int) -> Optional[dict]:
        with self._lock:
            r = self._conn.execute("SELECT * FROM chart_boards WHERE guild_id=?", (guild_id,)).fetchone()
            return dict(r) if r else None

    def list_chart_boards(self) -> list:
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM chart_boards")]

    def delete_chart_board(self, guild_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM chart_boards WHERE guild_id=?", (guild_id,))

    # ------------------------------------------------------------------ #
    # デイリー                                                            #
    # ------------------------------------------------------------------ #
    def claim_daily(self, user_id: int, today: datetime.date, slot: Optional[int] = None) -> DailyResult:
        """時間帯（slot）ごとに1回のボーナス。slot が前回より進んでいれば受け取れる（省略時は1日1回）。

        連続日数は「日」で数える。昨日も受け取っていれば伸び、金額が増える。同じ日に何回受け取っても、連続日数は1日分。
        """
        slot = today.toordinal() if slot is None else slot
        with self._lock, self._conn:
            self._ensure(user_id)
            row = self._conn.execute("SELECT daily_streak, last_daily, last_slot FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)).fetchone()
            last = datetime.date.fromisoformat(row["last_daily"]) if row["last_daily"] else None
            last_slot = row["last_slot"]
            if last_slot is None and last is not None:
                last_slot = last.toordinal()            # 時間帯を記録する前の受け取りは、その日の1回として扱う
            if (last is not None and last > today) or (last_slot is not None and slot <= last_slot):
                return DailyResult(False, streak=row["daily_streak"], balance=self._balance(user_id))      # 時計が戻っても二重に受け取れない
            if last == today:
                streak = max(1, row["daily_streak"])
            else:
                streak = row["daily_streak"] + 1 if last == today - datetime.timedelta(days=1) else 1
            amount = self.DAILY_BASE + self.DAILY_STREAK_BONUS * min(streak - 1, self.DAILY_STREAK_CAP)
            prof = self._conn.execute("SELECT daily_boost FROM profile WHERE guild_id=? AND user_id=?", (self._g(), user_id)).fetchone()
            boosted = bool(prof and prof["daily_boost"] > 0)
            if boosted:
                amount *= 2
                self._conn.execute("UPDATE profile SET daily_boost = daily_boost - 1 WHERE guild_id=? AND user_id=?", (self._g(), user_id))
            self._apply(user_id, amount, f"デイリー（{streak}日連続{'・2倍券' if boosted else ''}）")
            self._conn.execute("UPDATE users SET daily_streak=?, last_daily=?, last_slot=? WHERE guild_id=? AND user_id=?",
                               (streak, today.isoformat(), slot, self._g(), user_id))
            return DailyResult(True, amount, streak, self._balance(user_id), boosted)

    # ------------------------------------------------------------------ #
    # ショップ（持ち物・装備・消耗品）                                    #
    # ------------------------------------------------------------------ #
    SLOTS = ("title", "card_color", "card_icon")

    def _ensure_profile(self, user_id: int) -> None:
        self._conn.execute("INSERT OR IGNORE INTO profile (guild_id, user_id) VALUES (?, ?)", (self._g(), user_id))

    def purchase(self, user_id: int, item_id: str, label: str, price: int, unique: bool, qty: int = 1) -> PurchaseResult:
        """購入。支払い・持ち物の追加を1回の処理で行う。持っているもの（消耗品以外）は買えない。"""
        if qty < 1 or price <= 0:
            return PurchaseResult(False, "個数と値段は1以上にしてください。")
        total = price * qty
        with self._lock, self._conn:
            self._ensure(user_id)
            have = self._conn.execute("SELECT qty FROM inventory WHERE guild_id=? AND user_id=? AND item_id=?", (self._g(), user_id, item_id)).fetchone()
            if unique and have:
                return PurchaseResult(False, "すでに持っています。", self._balance(user_id))
            if self._balance(user_id) < total:
                return PurchaseResult(False, f"ペリカが足りません（{total:,}ペリカ必要・残高 {self._balance(user_id):,}ペリカ）。", self._balance(user_id))
            self._apply(user_id, -total, f"購入:{label}" + (f"×{qty}" if qty > 1 else ""))
            self._conn.execute(
                "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty = qty + excluded.qty", (self._g(), user_id, item_id, qty))
            return PurchaseResult(True, balance=self._balance(user_id))

    def inventory(self, user_id: int) -> dict:
        with self._lock:
            return {r["item_id"]: r["qty"] for r in self._conn.execute(
                "SELECT item_id, qty FROM inventory WHERE guild_id=? AND user_id=? AND qty>0", (self._g(), user_id))}

    def get_profile(self, user_id: int) -> dict:
        """装備中の称号・カード、消耗品の効果。読み取り専用（口座は作らない）。"""
        with self._lock:
            row = self._conn.execute("SELECT * FROM profile WHERE guild_id=? AND user_id=?", (self._g(), user_id)).fetchone()
        return dict(row) if row else {"user_id": user_id, "title": None, "card_color": None, "card_icon": None,
                                      "daily_boost": 0, "insurance": 0}

    def equip(self, user_id: int, slot: str, item_id: Optional[str]) -> bool:
        """称号・カードの色・アイコンを装備（None で外す）。持っていないものは装備できない。"""
        if slot not in self.SLOTS:
            raise ValueError(slot)
        with self._lock, self._conn:
            if item_id is not None and not self._conn.execute(
                    "SELECT 1 FROM inventory WHERE guild_id=? AND user_id=? AND item_id=? AND qty>0", (self._g(), user_id, item_id)).fetchone():
                return False
            self._ensure_profile(user_id)
            self._conn.execute(f"UPDATE profile SET {slot}=? WHERE guild_id=? AND user_id=?", (item_id, self._g(), user_id))
            return True

    def arm(self, user_id: int, item_id: str) -> bool:
        """消耗品を1つ使って、その効果を有効にする。持っていない・消耗品でないものは False。"""
        effect = CONSUMABLE_EFFECTS.get(item_id)
        if effect is None:
            return False
        with self._lock, self._conn:
            cur = self._conn.execute("UPDATE inventory SET qty = qty - 1 WHERE guild_id=? AND user_id=? AND item_id=? AND qty>0", (self._g(), user_id, item_id))
            if not cur.rowcount:
                return False
            self._ensure_profile(user_id)
            self._conn.execute(f"UPDATE profile SET {effect} = {effect} + 1 WHERE guild_id=? AND user_id=?", (self._g(), user_id))
            return True

    # ------------------------------------------------------------------ #
    # 利用できるサーバーの承認                                            #
    # ------------------------------------------------------------------ #
    def set_guild_status(self, guild_id: int, status: str, name: Optional[str] = None, by: Optional[int] = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO guild_status (guild_id, status, name, decided_by, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET status=excluded.status, "
                "name=COALESCE(excluded.name, guild_status.name), decided_by=excluded.decided_by, updated_at=excluded.updated_at",
                (guild_id, status, name, by, time.time()))

    def get_guild_status(self, guild_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM guild_status WHERE guild_id=?", (guild_id,)).fetchone()
        return dict(row) if row else None

    def list_guild_status(self) -> list:
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM guild_status ORDER BY updated_at DESC")]

    def mark_guild_notified(self, guild_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE guild_status SET notified_at=? WHERE guild_id=?", (time.time(), guild_id))

    # ------------------------------------------------------------------ #
    # ランキング掲示板（自動で更新されるメッセージ。1サーバーに1つ）       #
    # ------------------------------------------------------------------ #
    def set_leaderboard(self, guild_id: int, channel_id: int, message_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO leaderboards (guild_id, channel_id, message_id) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, message_id=excluded.message_id",
                (guild_id, channel_id, message_id))

    def get_leaderboard(self, guild_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM leaderboards WHERE guild_id=?", (guild_id,)).fetchone()
        return dict(row) if row else None

    def list_leaderboards(self) -> list:
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM leaderboards")]

    def delete_leaderboard(self, guild_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM leaderboards WHERE guild_id=?", (guild_id,))

    def set_nick_pref(self, guild_id: int, user_id: int, enabled: bool) -> None:
        """称号をそのサーバーでのニックネームに付けるか（付けると選んだ人だけ）。"""
        with self._lock, self._conn:
            if enabled:
                self._conn.execute("INSERT OR IGNORE INTO nick_prefs (guild_id, user_id) VALUES (?, ?)", (guild_id, user_id))
            else:
                self._conn.execute("DELETE FROM nick_prefs WHERE guild_id=? AND user_id=?", (guild_id, user_id))

    def get_nick_state(self, guild_id: int, user_id: int):
        """称号を付けたときの記録 (元のニックネーム, 設定したニックネーム)。未適用なら None。"""
        with self._lock:
            row = self._conn.execute("SELECT original_nick, applied_nick FROM nick_prefs WHERE guild_id=? AND user_id=?",
                                     (guild_id, user_id)).fetchone()
        return None if row is None or row["applied_nick"] is None else (row["original_nick"], row["applied_nick"])

    def set_nick_state(self, guild_id: int, user_id: int, original, applied) -> None:
        """記録を保存する（applied=None で記録を消す）。設定が ON の人にだけ記録できる。"""
        with self._lock, self._conn:
            self._conn.execute("UPDATE nick_prefs SET original_nick=?, applied_nick=? WHERE guild_id=? AND user_id=?",
                               (original if applied is not None else None, applied, guild_id, user_id))

    def delete_guild_nick_prefs(self, guild_id: int) -> None:
        """Bot がサーバーから外れたときに、そのサーバーの設定を消す。"""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM nick_prefs WHERE guild_id=?", (guild_id,))

    def nick_pref_guilds(self, user_id: int) -> list:
        """称号をニックネームに付ける設定にしているサーバーのID一覧。"""
        with self._lock:
            return [r["guild_id"] for r in self._conn.execute("SELECT guild_id FROM nick_prefs WHERE user_id=?", (user_id,))]

    def nick_pref(self, guild_id: int, user_id: int) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM nick_prefs WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone() is not None

    def profiles_of(self, user_ids) -> dict:
        """{ユーザーID: profile} をまとめて取る（ランキングの称号表示用）。"""
        ids = list(user_ids)
        if not ids:
            return {}
        with self._lock:
            marks = ",".join("?" * len(ids))
            return {r["user_id"]: dict(r) for r in self._conn.execute(
                f"SELECT * FROM profile WHERE guild_id=? AND user_id IN ({marks})", [self._g(), *ids])}

    # ------------------------------------------------------------------ #
    # サーバー別ランキング用: どのサーバーで遊んだか                      #
    # ------------------------------------------------------------------ #
    def touch_guild_user(self, guild_id: int, user_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR IGNORE INTO guild_users (guild_id, user_id) VALUES (?, ?)", (guild_id, user_id))

    def guild_user_ids(self, guild_id: int) -> set:
        with self._lock:
            return {r["user_id"] for r in self._conn.execute("SELECT user_id FROM guild_users WHERE guild_id=?", (guild_id,))}

    # ------------------------------------------------------------------ #
    # ランキング・統計・履歴                                              #
    # ------------------------------------------------------------------ #
    def crypto_values(self) -> dict:
        """このサーバーの {ユーザーID: CSトークンの評価額}。売ったときの手取りで、いまの価格がまだ無ければ空。"""
        with self._lock:
            latest = self.market_latest()
            if latest is None:
                return {}
            return {r["user_id"]: market_mod.sell_quote(r["units"], latest[1])[0] for r in self._conn.execute(
                "SELECT user_id, units FROM crypto WHERE guild_id=? AND units>0", (self._g(),))}

    def _ranked(self, user_ids: Optional[set]) -> list:
        """[(ユーザーID, 総資産)] を多い順に。総資産は、残高にCSトークンの評価額を足したもの。"""
        with self._lock:
            rows = self._conn.execute("SELECT user_id, balance FROM users WHERE guild_id=?", (self._g(),)).fetchall()
            values = self.crypto_values()
        total = [(r["user_id"], r["balance"] + values.get(r["user_id"], 0)) for r in rows if user_ids is None or r["user_id"] in user_ids]
        return sorted(total, key=lambda t: (-t[1], t[0]))

    def top(self, limit: int = 10, user_ids: Optional[set] = None) -> list:
        """[(順位, ユーザーID, 総資産), ...]。このサーバーの口座だけで並べる。user_ids を渡すと、さらにその人たちに絞る。

        総資産は、残高（現金）にCSトークンの評価額を足したもの。トークンを買っても順位が下がらないようにしている。
        """
        return [(i + 1, uid, bal) for i, (uid, bal) in enumerate(self._ranked(user_ids)[:limit])]

    def rank_of(self, user_id: int, user_ids: Optional[set] = None) -> Optional[tuple]:
        """(順位, 総資産, 対象人数)。登録がなければ None。"""
        ranked = self._ranked(user_ids)
        for i, (uid, bal) in enumerate(ranked):
            if uid == user_id:
                return i + 1, bal, len(ranked)
        return None

    def peek_stats(self, user_id: int) -> Optional[dict]:
        """読み取り専用。未登録の人を作らずに、成績を返す（未登録なら None）。"""
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)).fetchone()
            return dict(row) if row else None

    def stats(self, user_id: int) -> dict:
        with self._lock, self._conn:
            self._ensure(user_id)
            return dict(self._conn.execute("SELECT * FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)).fetchone())

    def history(self, user_id: int, limit: int = 10) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT delta, balance_after, reason, ts FROM ledger WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
                (self._g(), user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 送金                                                                #
    # ------------------------------------------------------------------ #
    def transfer(self, sender: int, receiver: int, amount: int, fee_rate: float,
                 daily_limit: int, day_start_ts: float, min_games: int = 0) -> TransferResult:
        """sender から receiver へ送る。手数料は送る側が上乗せで払う。1日の送金額に上限がある。"""
        if sender == receiver:
            return TransferResult(False, "自分には送金できません。")
        if amount <= 0:
            return TransferResult(False, "金額は1以上にしてください。")
        fee = math.ceil(round(amount * fee_rate, 6)) if fee_rate else 0     # 切り上げ（少額でも手数料は最低1）
        with self._lock, self._conn:
            self._ensure(sender)
            self._ensure(receiver)
            sent_today = -(self._conn.execute(
                "SELECT COALESCE(SUM(delta), 0) AS s FROM ledger WHERE guild_id=? AND user_id=? AND reason LIKE '送金（送）%' AND ts>=?",
                (self._g(), sender, day_start_ts)).fetchone()["s"])
            if self._conn.execute("SELECT games_played FROM users WHERE guild_id=? AND user_id=?", (self._g(), sender)).fetchone()["games_played"] < min_games:
                return TransferResult(False, f"送金は、{min_games}回以上ゲームで遊んでからできます（複数アカウントでの稼ぎ対策です）。")
            if sent_today + amount + fee > daily_limit:
                return TransferResult(False, f"1日の送金は合計 {daily_limit:,} ペリカまでです（今日は {sent_today:,} ペリカ送金済み）。")
            if self._balance(sender) < amount + fee:
                return TransferResult(False, f"残高が足りません（送金額 {amount:,} + 手数料 {fee:,} が必要です）。")
            self._apply(sender, -(amount + fee), f"送金（送）→{receiver}")
            self._apply(receiver, amount, f"送金（受）←{sender}")
            return TransferResult(True, received=amount, fee=fee, sender_balance=self._balance(sender))

    def close(self):
        with self._lock:
            self._conn.close()
