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

import copy
import datetime
import math
import os
import sqlite3
import threading
import time
from typing import Optional

from core import market as market_mod
from core import schema, teiai
from core.eco_auction import AuctionMixin
from core.eco_bank import BankMixin
from core.eco_bonds import BondMixin
from core.eco_crypto import CryptoMixin
from core.eco_debt import DebtMixin
from core.eco_events import EventMixin
from core.eco_guild import GuildMixin
from core.eco_loans import LoanMixin
from core.eco_market_events import MarketEventMixin
from core.eco_options import OptionMixin
from core.eco_pachinko import PachinkoMixin
from core.eco_quiz import QuizMixin
from core.eco_raffle import RaffleMixin
from core.eco_records import RecordMixin
from core.eco_rtp import RtpMixin
from core.eco_shop import ShopMixin
from core.eco_types import (  # noqa: F401  (他のモジュールが core.economy から読む)
    CONSUMABLE_EFFECTS,
    INITIAL_BALANCE,
    INSURANCE_CAP,
    CryptoResult,
    DailyResult,
    PurchaseResult,
    TransferResult,
)
from core.shop import STREAK_GUARD


class Economy(
    QuizMixin,
    BondMixin,
    RaffleMixin,
    EventMixin,
    PachinkoMixin,
    RtpMixin,
    LoanMixin,
    RecordMixin,
    CryptoMixin,
    ShopMixin,
    GuildMixin,
    DebtMixin,
    BankMixin,
    AuctionMixin,
    OptionMixin,
    MarketEventMixin,
):
    DAILY_BASE = 1000
    DAILY_STREAK_BONUS = 100  # 連続1日ごとの加算
    DAILY_STREAK_CAP = 20  # 加算は20日分まで（最大 +2,000。1回の額は最大 3,000）

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
        self._notices = {}  # 結果の画面に出す一言（core/eco_debt.py）。サーバーごとの見え方（for_guild）の間で、共有する
        self._migrate_to_guild_scope(legacy_guild_id)
        self._init_schema()
        self._retire_autoplay()

    def _retire_autoplay(self) -> None:
        """廃止した自動プレイの表が残っていたら、預け中の分を残高に戻してから表を消す（預けた額を失わせない）。"""
        with self._lock, self._conn:
            if not self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='autoplay'").fetchone():
                return
            for row in self._conn.execute("SELECT guild_id, user_id, pot FROM autoplay").fetchall():
                if row["pot"] > 0:
                    view = copy.copy(self)
                    view.guild_id = row["guild_id"]
                    view._ensure(row["user_id"])
                    view._apply(row["user_id"], row["pot"], "自動プレイ精算")
            self._conn.execute("DROP TABLE autoplay")

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
            return  # 新規、または移行済み
        if legacy_guild_id is None:
            raise RuntimeError(
                "古い形式の記録があります。引き継ぎ先のサーバーIDを、config.json の legacy_guild_id に設定してください。"
            )
        gid = int(legacy_guild_id)
        if gid <= 0:
            raise ValueError(f"legacy_guild_id が不正です: {legacy_guild_id!r}")
        backup = self._db_path + ".before-guild-scope"
        if not os.path.exists(backup):  # 万一に備えて、移行の前に記録を丸ごと退避する
            tmp = backup + ".tmp"  # 途中で落ちた不完全なファイルを、退避済みと誤認しないよう最後に改名する
            dst = sqlite3.connect(tmp)
            self._conn.backup(dst)
            dst.close()
            os.replace(tmp, backup)
        c = self._conn
        have = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("ALTER TABLE users RENAME TO users_old")
            c.execute(schema.USERS_DDL)
            c.execute(
                "INSERT INTO users (guild_id, user_id, balance, daily_streak, last_daily, games_played, total_wagered, "
                "total_won, biggest_win, created_at) SELECT ?, user_id, balance, daily_streak, last_daily, games_played, "
                "total_wagered, total_won, biggest_win, created_at FROM users_old",
                (gid,),
            )
            c.execute("DROP TABLE users_old")
            c.execute("DROP INDEX IF EXISTS ledger_user")
            for t in ("ledger", "escrow"):  # 無い表は、あとの _init_schema が新しい形で作る
                if t in have:
                    c.execute(f"ALTER TABLE {t} ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 0")
                    c.execute(f"UPDATE {t} SET guild_id=?", (gid,))
            if "inventory" in have:
                c.execute("ALTER TABLE inventory RENAME TO inventory_old")
                c.execute(schema.INVENTORY_DDL)
                c.execute(
                    "INSERT INTO inventory (guild_id, user_id, item_id, qty) SELECT ?, user_id, item_id, qty FROM inventory_old",
                    (gid,),
                )
                c.execute("DROP TABLE inventory_old")
            if "profile" in have:
                c.execute("ALTER TABLE profile RENAME TO profile_old")
                c.execute(schema.PROFILE_DDL)
                c.execute(
                    "INSERT INTO profile (guild_id, user_id, title, card_color, card_icon, daily_boost, insurance) "
                    "SELECT ?, user_id, title, card_color, card_icon, daily_boost, insurance FROM profile_old",
                    (gid,),
                )
                c.execute("DROP TABLE profile_old")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    def _init_schema(self):
        with self._lock, self._conn:
            schema.create_tables(self._conn)
            schema.add_missing_columns(self._conn)

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
        return self._conn.execute(
            "SELECT balance FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)
        ).fetchone()["balance"]

    def _debt(self, user_id: int) -> int:
        return self._conn.execute(
            "SELECT debt FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)
        ).fetchone()["debt"]

    def _apply(self, user_id: int, delta: int, reason: str, income: int = 0) -> int:
        """残高に delta を足す（0 未満にはならない）。実際に変化した量を返す。

        income は、delta のうち「収入」の部分（ゲームの純利益・デイリー・ミッション報酬など）。
        借金があれば、その REPAY_RATE を、借金の返済へ天引きする（core/teiai.py）。賭け金の返金や、借りたお金などは、収入ではない。
        """
        before = self._balance(user_id)
        after = max(0, before + delta)
        self._conn.execute("UPDATE users SET balance=? WHERE guild_id=? AND user_id=?", (after, self._g(), user_id))
        if after != before:
            self._ledger(user_id, after - before, after, reason)
        if delta > 0 and income > 0:
            self._withhold(user_id, min(income, delta))
        return self._balance(user_id) - before

    def debt(self, user_id: int) -> int:
        """借金の残り。口座が無ければ 0（口座は作らない）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT debt FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
            return row["debt"] if row else 0

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
            self._conn.execute(
                "UPDATE users SET total_wagered = total_wagered + ? WHERE guild_id=? AND user_id=?",
                (amount, self._g(), user_id),
            )
            return cur.lastrowid

    def add_to_escrow(self, escrow_id: int, extra: int, credit: bool = False) -> bool:
        """預かり金を増やす（ブラックジャックのダブルダウン）。残高不足なら False。

        credit=True なら、足りない分を帝愛の信用で借りて（借金になる）追加する（can_borrow_for_bet で許される範囲だけ）。
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT user_id, game FROM escrow WHERE id=? AND guild_id=?", (escrow_id, self._g())
            ).fetchone()
            if row is None or extra <= 0:
                return False
            short = extra - self._balance(row["user_id"])
            if short > 0:
                if not credit or not self.can_borrow_for_bet(row["user_id"], short)[0]:
                    return False
                self._seed(row["user_id"], short)  # 手元に足りない分だけ、帝愛が貸す
            self._apply(row["user_id"], -extra, f"追加の賭け:{row['game']}")
            self._conn.execute(
                "UPDATE escrow SET amount = amount + ? WHERE id=? AND guild_id=?", (extra, escrow_id, self._g())
            )
            self._conn.execute(
                "UPDATE users SET total_wagered = total_wagered + ? WHERE guild_id=? AND user_id=?",
                (extra, self._g(), row["user_id"]),
            )
            return True

    def escrow_amount(self, escrow_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT amount FROM escrow WHERE id=? AND guild_id=?", (escrow_id, self._g())
            ).fetchone()
            return row["amount"] if row else 0

    def settle(self, escrow_id: int, payout: int, game: str) -> Optional[int]:
        """預かり金を精算する。payout は「受け取る総額」（賭け金を含む。負けなら 0）。

        すでに精算済みなら何もせず None（二重払いを防ぐ）。成功時は損益（payout - 賭け金）を返す。
        """
        result = self.settle_with_penalty(escrow_id, payout, game, 0)
        return None if result is None else result[0]

    def settle_with_penalty(self, escrow_id: int, payout: int, game: str, penalty: int) -> Optional[tuple]:
        """精算と、倍払いなどの追加の支払いを、1回のトランザクションで行う。

        penalty はまず残高から引き、足りない分は借金にする。負けて払い戻しが 0 のとき、保険券があれば賭け金の半分を返す。
        (損益（追加の支払い・保険を含む）, 追加の支払い（借金にした分を含む）, 保険で戻った額) を返す。
        すでに精算済みなら何もせず None（二重に精算も追加の支払いもしない）。
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT user_id, amount FROM escrow WHERE id=? AND guild_id=?", (escrow_id, self._g())
            ).fetchone()
            if row is None:
                return None
            user_id, amount = row["user_id"], row["amount"]
            payout = max(0, int(payout))
            insured = 0
            if payout == 0:
                prof = self._conn.execute(
                    "SELECT insurance FROM profile WHERE guild_id=? AND user_id=?", (self._g(), user_id)
                ).fetchone()
                if prof and prof["insurance"] > 0:
                    insured = min(amount // 2, INSURANCE_CAP)
                    self._conn.execute(
                        "UPDATE profile SET insurance = insurance - 1 WHERE guild_id=? AND user_id=?",
                        (self._g(), user_id),
                    )
            total = payout + insured
            if total:
                # 払い戻しのうち、賭け金を超えた分（純利益）だけが収入。賭け金の返金や、保険券の返金からは天引きしない
                self._apply(
                    user_id,
                    total,
                    f"払い戻し:{game}" if payout else f"保険券で半額返金:{game}",
                    income=max(0, payout - amount),
                )
            self._conn.execute("DELETE FROM escrow WHERE id=? AND guild_id=?", (escrow_id, self._g()))
            self._bank_income(
                int(max(0, amount - total) * teiai.LOSS_SHARE), 1.0, needs_depositors=True
            )  # 負けた額の一部が、預金者の利回りになる
            taken = -self._apply(user_id, -penalty, f"追加の支払い:{game}") if penalty > 0 else 0
            if penalty > taken:  # 残高で足りない分は、強制執行したうえで、残りを帝愛の借金にする
                self._owe(user_id, penalty - taken, f"{game}の倍払い")
            taken = max(penalty, 0)
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
            row = self._conn.execute(
                "SELECT user_id, amount, game FROM escrow WHERE id=? AND guild_id=?", (escrow_id, self._g())
            ).fetchone()
            if row is None:
                return False
            self._apply(row["user_id"], row["amount"], f"返金:{row['game']}")
            self._conn.execute("DELETE FROM escrow WHERE id=? AND guild_id=?", (escrow_id, self._g()))
            return True

    def refund_all_escrows(self) -> int:
        """起動時に呼ぶ。Bot が落ちて残った預かり金を、すべて持ち主に返す。返した件数を返す。"""
        with self._lock, self._conn:
            rows = self._conn.execute("SELECT id, guild_id, user_id, amount, game FROM escrow").fetchall()
            for r in rows:  # 全サーバー分。それぞれ、預かったサーバーの口座へ返す
                try:
                    view = self.for_guild(r["guild_id"])
                except ValueError:  # 出どころ不明の預かり金で、起動全体を止めない（返せないので、記録だけ残す）
                    print(
                        f"出どころ不明の預かり金を破棄します: escrow={r['id']} guild={r['guild_id']} user={r['user_id']} amount={r['amount']} game={r['game']}"
                    )
                    self._conn.execute("DELETE FROM escrow WHERE id=?", (r["id"],))
                    continue
                view._ensure(r["user_id"])
                view._apply(r["user_id"], r["amount"], f"返金（中断したゲーム）:{r['game']}")
                self._conn.execute("DELETE FROM escrow WHERE id=?", (r["id"],))
            return len(rows)

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
            row = self._conn.execute(
                "SELECT daily_streak, last_daily, last_slot FROM users WHERE guild_id=? AND user_id=?",
                (self._g(), user_id),
            ).fetchone()
            last = datetime.date.fromisoformat(row["last_daily"]) if row["last_daily"] else None
            last_slot = row["last_slot"]
            if last_slot is None and last is not None:
                last_slot = last.toordinal()  # 時間帯を記録する前の受け取りは、その日の1回として扱う
            if (last is not None and last > today) or (last_slot is not None and slot <= last_slot):
                return DailyResult(
                    False, streak=row["daily_streak"], balance=self._balance(user_id)
                )  # 時計が戻っても二重に受け取れない
            guarded = 0
            if last == today:
                streak = max(1, row["daily_streak"])
            elif last == today - datetime.timedelta(days=1):
                streak = row["daily_streak"] + 1
            else:
                streak = 1
                # 受け取れなかった日が、連続守り券の枚数以内なら、1枚で1日ぶん埋めて連続をつなぐ（足りなければ1枚も使わない）
                missed = (today - last).days - 1 if last is not None else 0
                if missed > 0 and row["daily_streak"] > 0:
                    cur = self._conn.execute(
                        "UPDATE inventory SET qty = qty - ? WHERE guild_id=? AND user_id=? AND item_id=? AND qty>=?",
                        (missed, self._g(), user_id, STREAK_GUARD, missed),
                    )
                    if cur.rowcount:
                        streak, guarded = row["daily_streak"] + 1, missed
            amount = self.DAILY_BASE + self.DAILY_STREAK_BONUS * min(streak - 1, self.DAILY_STREAK_CAP)
            prof = self._conn.execute(
                "SELECT daily_boost FROM profile WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
            boosted = bool(prof and prof["daily_boost"] > 0)
            if boosted:
                amount *= 2
                self._conn.execute(
                    "UPDATE profile SET daily_boost = daily_boost - 1 WHERE guild_id=? AND user_id=?",
                    (self._g(), user_id),
                )
            debt_before = self._debt(user_id)
            skimmed = teiai.skim(amount) if debt_before > 0 else 0  # 借金中は、帝愛がピンハネ（借金の元本は減らない）
            if skimmed:
                self._ledger(user_id, 0, self._balance(user_id), f"帝愛のピンハネ（デイリー {skimmed:,}ペリカ）")
                self._bank_income(skimmed, teiai.SKIM_YIELD_SHARE, needs_depositors=True)  # 預金者の利回りになる
            credit = amount - skimmed
            self._apply(
                user_id,
                credit,
                f"デイリー（{streak}日連続{'・2倍券' if boosted else ''}{'・連続守り券' + str(guarded) + '枚' if guarded else ''}）",
                income=credit,
            )
            repaid = debt_before - self._debt(user_id)
            seed = self._topup_seed(user_id)  # 借金中で、手元が種銭の枠に満たなければ、枠まで貸す
            self._conn.execute(
                "UPDATE users SET daily_streak=?, last_daily=?, last_slot=? WHERE guild_id=? AND user_id=?",
                (streak, today.isoformat(), slot, self._g(), user_id),
            )
            return DailyResult(True, amount, streak, self._balance(user_id), boosted, repaid, skimmed, seed, guarded)

    # ------------------------------------------------------------------ #
    # サーバー別ランキング用: どのサーバーで遊んだか                      #
    # ------------------------------------------------------------------ #
    def touch_guild_user(self, guild_id: int, user_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO guild_users (guild_id, user_id) VALUES (?, ?)", (guild_id, user_id)
            )

    def guild_user_ids(self, guild_id: int) -> set:
        with self._lock:
            return {
                r["user_id"]
                for r in self._conn.execute("SELECT user_id FROM guild_users WHERE guild_id=?", (guild_id,))
            }

    # ------------------------------------------------------------------ #
    # ランキング・統計・履歴                                              #
    # ------------------------------------------------------------------ #
    def crypto_values(self) -> dict:
        """このサーバーの {ユーザーID: 現物（全銘柄）の評価額}。売ったときの手取りで、価格がまだない銘柄は数えない。"""
        with self._lock:
            prices = self.latest_prices()
            values = {}
            for r in self._conn.execute(
                "SELECT user_id, symbol, units FROM crypto WHERE guild_id=? AND units>0", (self._g(),)
            ):
                if r["symbol"] in prices:
                    values[r["user_id"]] = (
                        values.get(r["user_id"], 0) + market_mod.sell_quote(r["units"], prices[r["symbol"]])[0]
                    )
            return values

    def _ranked(self, user_ids: Optional[set]) -> list:
        """[(ユーザーID, 総資産)] を多い順に。総資産は、残高にCSトークンの評価額を足し、借金を引いたもの。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, balance - debt AS balance FROM users WHERE guild_id=?", (self._g(),)
            ).fetchall()
            values = self.crypto_values()
            for uid, eq in self.margin_equity().items():
                values[uid] = values.get(uid, 0) + eq
            for uid, held in self.bond_values().items():
                values[uid] = values.get(uid, 0) + held  # 預けている債券の元本も総資産
            for uid, held in self.bank_values().items():
                values[uid] = values.get(uid, 0) + held  # 帝愛への預金も総資産
            for uid, held in self.option_values().items():
                values[uid] = values.get(uid, 0) + held  # オプションも、いま売ったときの手取りで総資産
        total = [
            (r["user_id"], r["balance"] + values.get(r["user_id"], 0))
            for r in rows
            if user_ids is None or r["user_id"] in user_ids
        ]
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
            row = self._conn.execute(
                "SELECT * FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
            return dict(row) if row else None

    def stats(self, user_id: int) -> dict:
        with self._lock, self._conn:
            self._ensure(user_id)
            return dict(
                self._conn.execute(
                    "SELECT * FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)
                ).fetchone()
            )

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
    def transfer(
        self,
        sender: int,
        receiver: int,
        amount: int,
        fee_rate: float,
        daily_limit: int,
        day_start_ts: float,
        min_games: int = 0,
    ) -> TransferResult:
        """sender から receiver へ送る。手数料は送る側が上乗せで払う。1日の送金額に上限がある。"""
        if sender == receiver:
            return TransferResult(False, "自分には送金できません。")
        if amount <= 0:
            return TransferResult(False, "金額は1以上にしてください。")
        fee = (
            math.ceil(round(amount * fee_rate, 6)) if fee_rate else 0
        )  # 切り上げ（少額でも手数料は最低1）。1日の上限には数えない
        with self._lock, self._conn:
            self._ensure(sender)
            self._ensure(receiver)
            sent_today = -(
                self._conn.execute(
                    "SELECT COALESCE(SUM(delta), 0) AS s FROM ledger WHERE guild_id=? AND user_id=? AND reason LIKE '送金（送）%' AND ts>=?",
                    (self._g(), sender, day_start_ts),
                ).fetchone()["s"]
            )
            if (
                self._conn.execute(
                    "SELECT games_played FROM users WHERE guild_id=? AND user_id=?", (self._g(), sender)
                ).fetchone()["games_played"]
                < min_games
            ):
                return TransferResult(
                    False, f"送金は、{min_games}回以上ゲームで遊んでからできます（複数アカウントでの稼ぎ対策です）。"
                )
            if sent_today + amount > daily_limit:
                return TransferResult(
                    False, f"1日の送金は合計 {daily_limit:,} ペリカまでです（今日は {sent_today:,} ペリカ送金済み）。"
                )
            if self._debt(sender) > 0:
                return TransferResult(False, f"借金が {self._debt(sender):,}ペリカ 残っているので、送金はできません。")
            if self._balance(sender) < amount + fee:
                return TransferResult(False, f"残高が足りません（送金額 {amount:,} + 手数料 {fee:,} が必要です）。")
            self._apply(
                sender, -amount, f"送金（送）→{receiver}"
            )  # 手数料は別の行にする（1日の上限は、送った額だけで数えるため）
            if fee:
                self._apply(sender, -fee, "送金手数料")
            self._apply(receiver, amount, f"送金（受）←{sender}", income=amount)
            return TransferResult(True, received=amount, fee=fee, sender_balance=self._balance(sender))

    def close(self):
        with self._lock:
            self._conn.close()
