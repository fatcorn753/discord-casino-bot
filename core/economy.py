"""
ペリカの経済（残高・預かり金・デイリー・送金・ランキング）。

標準ライブラリの sqlite3 だけを使う。Bot 全体で 1 つの Economy を共有する。

設計の要点:
- 残高の増減は必ず 1 回のトランザクションで行い、台帳（ledger）に残す。連打や同時操作で残高が狂わない
- 賭け金は開始時に「預かり金（escrow）」として残高から引く。ゲームが終わったら払い戻し額を精算する。
  Bot が途中で落ちた場合は、起動時に預かり金を全額返す（賭け金が消えない）
- 残高はマイナスにならない（負けても 0 まで）
"""

import datetime
import math
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

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

    def __init__(self, db_path: str):
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self):
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id      INTEGER PRIMARY KEY,
                    balance      INTEGER NOT NULL,
                    daily_streak INTEGER NOT NULL DEFAULT 0,
                    last_daily   TEXT,
                    games_played INTEGER NOT NULL DEFAULT 0,
                    total_wagered INTEGER NOT NULL DEFAULT 0,
                    total_won    INTEGER NOT NULL DEFAULT 0,
                    biggest_win  INTEGER NOT NULL DEFAULT 0,
                    created_at   REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ledger (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id       INTEGER NOT NULL,
                    delta         INTEGER NOT NULL,
                    balance_after INTEGER NOT NULL,
                    reason        TEXT NOT NULL,
                    ts            REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ledger_user ON ledger (user_id, id);
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
                CREATE TABLE IF NOT EXISTS inventory (
                    user_id INTEGER NOT NULL,
                    item_id TEXT    NOT NULL,
                    qty     INTEGER NOT NULL,
                    PRIMARY KEY (user_id, item_id)
                );
                CREATE TABLE IF NOT EXISTS nick_prefs (
                    guild_id INTEGER NOT NULL,
                    user_id  INTEGER NOT NULL,
                    original_nick TEXT,          -- 称号を付ける前のニックネーム（NULL はニックネームなし）
                    applied_nick  TEXT,          -- Bot が設定したニックネーム（NULL は未適用）
                    PRIMARY KEY (guild_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS profile (
                    user_id     INTEGER PRIMARY KEY,
                    title       TEXT,
                    card_color  TEXT,
                    card_icon   TEXT,
                    daily_boost INTEGER NOT NULL DEFAULT 0,
                    insurance   INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS escrow (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER NOT NULL,
                    amount     INTEGER NOT NULL,
                    game       TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            # 作成済みの古い表に、列を足す（初回の版には元のニックネームの記録がなかった）
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
            "INSERT OR IGNORE INTO users (user_id, balance, created_at) VALUES (?, ?, ?)",
            (user_id, INITIAL_BALANCE, time.time()),
        )
        if cur.rowcount:
            self._ledger(user_id, INITIAL_BALANCE, INITIAL_BALANCE, "初期支給")

    def _ledger(self, user_id: int, delta: int, balance_after: int, reason: str) -> None:
        self._conn.execute(
            "INSERT INTO ledger (user_id, delta, balance_after, reason, ts) VALUES (?, ?, ?, ?, ?)",
            (user_id, delta, balance_after, reason, time.time()),
        )

    def _balance(self, user_id: int) -> int:
        return self._conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()["balance"]

    def _apply(self, user_id: int, delta: int, reason: str) -> int:
        """残高に delta を足す（0 未満にはならない）。実際に変化した量を返す。"""
        before = self._balance(user_id)
        after = max(0, before + delta)
        self._conn.execute("UPDATE users SET balance=? WHERE user_id=?", (after, user_id))
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
                "INSERT INTO escrow (user_id, amount, game, created_at) VALUES (?, ?, ?, ?)",
                (user_id, amount, game, time.time()),
            )
            self._conn.execute("UPDATE users SET total_wagered = total_wagered + ? WHERE user_id=?", (amount, user_id))
            return cur.lastrowid

    def add_to_escrow(self, escrow_id: int, extra: int) -> bool:
        """預かり金を増やす（ブラックジャックのダブルダウン）。残高不足なら False。"""
        with self._lock, self._conn:
            row = self._conn.execute("SELECT user_id, game FROM escrow WHERE id=?", (escrow_id,)).fetchone()
            if row is None or extra <= 0 or self._balance(row["user_id"]) < extra:
                return False
            self._apply(row["user_id"], -extra, f"追加の賭け:{row['game']}")
            self._conn.execute("UPDATE escrow SET amount = amount + ? WHERE id=?", (extra, escrow_id))
            self._conn.execute("UPDATE users SET total_wagered = total_wagered + ? WHERE user_id=?", (extra, row["user_id"]))
            return True

    def escrow_amount(self, escrow_id: int) -> int:
        with self._lock:
            row = self._conn.execute("SELECT amount FROM escrow WHERE id=?", (escrow_id,)).fetchone()
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
            row = self._conn.execute("SELECT user_id, amount FROM escrow WHERE id=?", (escrow_id,)).fetchone()
            if row is None:
                return None
            user_id, amount = row["user_id"], row["amount"]
            payout = max(0, int(payout))
            insured = 0
            if payout == 0:
                prof = self._conn.execute("SELECT insurance FROM profile WHERE user_id=?", (user_id,)).fetchone()
                if prof and prof["insurance"] > 0:
                    insured = min(amount // 2, INSURANCE_CAP)
                    self._conn.execute("UPDATE profile SET insurance = insurance - 1 WHERE user_id=?", (user_id,))
            total = payout + insured
            if total:
                self._apply(user_id, total, f"払い戻し:{game}" if payout else f"保険券で半額返金:{game}")
            self._conn.execute("DELETE FROM escrow WHERE id=?", (escrow_id,))
            taken = -self._apply(user_id, -penalty, f"追加の支払い:{game}") if penalty > 0 else 0
            net = total - amount - taken
            self._conn.execute(
                "UPDATE users SET games_played = games_played + 1, total_won = total_won + ?, "
                "biggest_win = MAX(biggest_win, ?) WHERE user_id=?",
                (total, net, user_id),
            )
            return net, taken, insured

    def refund(self, escrow_id: int) -> bool:
        """エラー時の返金。賭け金（追加分を含む）を全額返す。成績（遊んだ回数・勝ち額）には数えない。"""
        with self._lock, self._conn:
            row = self._conn.execute("SELECT user_id, amount, game FROM escrow WHERE id=?", (escrow_id,)).fetchone()
            if row is None:
                return False
            self._apply(row["user_id"], row["amount"], f"返金:{row['game']}")
            self._conn.execute("DELETE FROM escrow WHERE id=?", (escrow_id,))
            return True

    def refund_all_escrows(self) -> int:
        """起動時に呼ぶ。Bot が落ちて残った預かり金を、すべて持ち主に返す。返した件数を返す。"""
        with self._lock, self._conn:
            rows = self._conn.execute("SELECT id, user_id, amount, game FROM escrow").fetchall()
            for r in rows:
                self._apply(r["user_id"], r["amount"], f"返金（中断したゲーム）:{r['game']}")
                self._conn.execute("DELETE FROM escrow WHERE id=?", (r["id"],))
            return len(rows)

    # ------------------------------------------------------------------ #
    # デイリー                                                            #
    # ------------------------------------------------------------------ #
    def claim_daily(self, user_id: int, today: datetime.date) -> DailyResult:
        """1日1回のボーナス。昨日も受け取っていれば連続日数が伸び、金額が増える。"""
        with self._lock, self._conn:
            self._ensure(user_id)
            row = self._conn.execute("SELECT daily_streak, last_daily FROM users WHERE user_id=?", (user_id,)).fetchone()
            last = datetime.date.fromisoformat(row["last_daily"]) if row["last_daily"] else None
            if last is not None and last >= today:     # 時差設定や時計が戻っても二重に受け取れない
                return DailyResult(False, streak=row["daily_streak"], balance=self._balance(user_id))
            streak = row["daily_streak"] + 1 if last == today - datetime.timedelta(days=1) else 1
            amount = self.DAILY_BASE + self.DAILY_STREAK_BONUS * min(streak - 1, self.DAILY_STREAK_CAP)
            prof = self._conn.execute("SELECT daily_boost FROM profile WHERE user_id=?", (user_id,)).fetchone()
            boosted = bool(prof and prof["daily_boost"] > 0)
            if boosted:
                amount *= 2
                self._conn.execute("UPDATE profile SET daily_boost = daily_boost - 1 WHERE user_id=?", (user_id,))
            self._apply(user_id, amount, f"デイリー（{streak}日連続{'・2倍券' if boosted else ''}）")
            self._conn.execute("UPDATE users SET daily_streak=?, last_daily=? WHERE user_id=?",
                               (streak, today.isoformat(), user_id))
            return DailyResult(True, amount, streak, self._balance(user_id), boosted)

    # ------------------------------------------------------------------ #
    # ショップ（持ち物・装備・消耗品）                                    #
    # ------------------------------------------------------------------ #
    SLOTS = ("title", "card_color", "card_icon")

    def _ensure_profile(self, user_id: int) -> None:
        self._conn.execute("INSERT OR IGNORE INTO profile (user_id) VALUES (?)", (user_id,))

    def purchase(self, user_id: int, item_id: str, label: str, price: int, unique: bool, qty: int = 1) -> PurchaseResult:
        """購入。支払い・持ち物の追加を1回の処理で行う。持っているもの（消耗品以外）は買えない。"""
        if qty < 1 or price <= 0:
            return PurchaseResult(False, "個数と値段は1以上にしてください。")
        total = price * qty
        with self._lock, self._conn:
            self._ensure(user_id)
            have = self._conn.execute("SELECT qty FROM inventory WHERE user_id=? AND item_id=?", (user_id, item_id)).fetchone()
            if unique and have:
                return PurchaseResult(False, "すでに持っています。", self._balance(user_id))
            if self._balance(user_id) < total:
                return PurchaseResult(False, f"ペリカが足りません（{total:,}ペリカ必要・残高 {self._balance(user_id):,}ペリカ）。", self._balance(user_id))
            self._apply(user_id, -total, f"購入:{label}" + (f"×{qty}" if qty > 1 else ""))
            self._conn.execute(
                "INSERT INTO inventory (user_id, item_id, qty) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, item_id) DO UPDATE SET qty = qty + excluded.qty", (user_id, item_id, qty))
            return PurchaseResult(True, balance=self._balance(user_id))

    def inventory(self, user_id: int) -> dict:
        with self._lock:
            return {r["item_id"]: r["qty"] for r in self._conn.execute(
                "SELECT item_id, qty FROM inventory WHERE user_id=? AND qty>0", (user_id,))}

    def get_profile(self, user_id: int) -> dict:
        """装備中の称号・カード、消耗品の効果。読み取り専用（口座は作らない）。"""
        with self._lock:
            row = self._conn.execute("SELECT * FROM profile WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else {"user_id": user_id, "title": None, "card_color": None, "card_icon": None,
                                      "daily_boost": 0, "insurance": 0}

    def equip(self, user_id: int, slot: str, item_id: Optional[str]) -> bool:
        """称号・カードの色・アイコンを装備（None で外す）。持っていないものは装備できない。"""
        if slot not in self.SLOTS:
            raise ValueError(slot)
        with self._lock, self._conn:
            if item_id is not None and not self._conn.execute(
                    "SELECT 1 FROM inventory WHERE user_id=? AND item_id=? AND qty>0", (user_id, item_id)).fetchone():
                return False
            self._ensure_profile(user_id)
            self._conn.execute(f"UPDATE profile SET {slot}=? WHERE user_id=?", (item_id, user_id))
            return True

    def arm(self, user_id: int, item_id: str) -> bool:
        """消耗品を1つ使って、その効果を有効にする。持っていない・消耗品でないものは False。"""
        effect = CONSUMABLE_EFFECTS.get(item_id)
        if effect is None:
            return False
        with self._lock, self._conn:
            cur = self._conn.execute("UPDATE inventory SET qty = qty - 1 WHERE user_id=? AND item_id=? AND qty>0", (user_id, item_id))
            if not cur.rowcount:
                return False
            self._ensure_profile(user_id)
            self._conn.execute(f"UPDATE profile SET {effect} = {effect} + 1 WHERE user_id=?", (user_id,))
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
            return {r["user_id"]: dict(r) for r in self._conn.execute(f"SELECT * FROM profile WHERE user_id IN ({marks})", ids)}

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
    def top(self, limit: int = 10, user_ids: Optional[set] = None) -> list:
        """[(順位, ユーザーID, 残高), ...]。user_ids を渡すと、その人たちの中での順位（サーバー別ランキング用）。"""
        with self._lock:
            rows = self._conn.execute("SELECT user_id, balance FROM users ORDER BY balance DESC, user_id").fetchall()
        ranked = [(r["user_id"], r["balance"]) for r in rows if user_ids is None or r["user_id"] in user_ids]
        return [(i + 1, uid, bal) for i, (uid, bal) in enumerate(ranked[:limit])]

    def rank_of(self, user_id: int, user_ids: Optional[set] = None) -> Optional[tuple]:
        """(順位, 残高, 対象人数)。登録がなければ None。"""
        with self._lock:
            rows = self._conn.execute("SELECT user_id, balance FROM users ORDER BY balance DESC, user_id").fetchall()
        ranked = [(r["user_id"], r["balance"]) for r in rows if user_ids is None or r["user_id"] in user_ids]
        for i, (uid, bal) in enumerate(ranked):
            if uid == user_id:
                return i + 1, bal, len(ranked)
        return None

    def peek_stats(self, user_id: int) -> Optional[dict]:
        """読み取り専用。未登録の人を作らずに、成績を返す（未登録なら None）。"""
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            return dict(row) if row else None

    def stats(self, user_id: int) -> dict:
        with self._lock, self._conn:
            self._ensure(user_id)
            return dict(self._conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone())

    def history(self, user_id: int, limit: int = 10) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT delta, balance_after, reason, ts FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
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
                "SELECT COALESCE(SUM(delta), 0) AS s FROM ledger WHERE user_id=? AND reason LIKE '送金（送）%' AND ts>=?",
                (sender, day_start_ts)).fetchone()["s"])
            if self._conn.execute("SELECT games_played FROM users WHERE user_id=?", (sender,)).fetchone()["games_played"] < min_games:
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
