"""
ペリカの経済 Facade（各ドメイン別の永続化 Mixin を統合し、IEconomy 契約を提供する）。

標準ライブラリの sqlite3 だけを使う。Bot 全体で 1 つの Economy を共有する。

残高・成績・デイリー・持ち物・履歴は **サーバーごと** に別々。呼び出し側は `economy.for_guild(サーバーID)` で
そのサーバー専用の見え方を取り、そこから各メソッドを呼ぶ（別のサーバーの記録とは混ざらない）。
"""

import time
from typing import Optional, Self

from domain.ports import IEconomy
from domain.types import (  # noqa: F401  (他のモジュールが infra.db.economy から読む)
    CONSUMABLE_EFFECTS,
    DAILY_BASE,
    DAILY_STREAK_BONUS,
    DAILY_STREAK_CAP,
    INITIAL_BALANCE,
    INSURANCE_CAP,
    CryptoResult,
    DailyResult,
    PurchaseResult,
    TransferResult,
)
from infra.db import schema
from infra.db.assets import AssetMixin
from infra.db.auction import AuctionMixin
from infra.db.bank import BankMixin
from infra.db.base import EcoDb
from infra.db.bonds import BondMixin
from infra.db.crypto import CryptoMixin
from infra.db.debt import DebtMixin
from infra.db.earnings import EarningsMixin
from infra.db.escrow import EscrowMixin
from infra.db.events import EventMixin
from infra.db.guild import GuildMixin
from infra.db.horse import HorseMixin
from infra.db.labor import LaborMixin
from infra.db.loans import LoanMixin
from infra.db.margin import MarginMixin
from infra.db.market import MarketMixin
from infra.db.market_events import MarketEventMixin
from infra.db.options import OptionMixin
from infra.db.p2p import P2PMixin
from infra.db.pachinko import PachinkoMixin
from infra.db.quiz import QuizMixin
from infra.db.race import RaceMixin
from infra.db.raffle import RaffleMixin
from infra.db.records import RecordMixin
from infra.db.rtp import RtpMixin
from infra.db.shop import ShopMixin
from infra.db.wallet import WalletMixin


class Economy(
    WalletMixin,
    EscrowMixin,
    AssetMixin,
    QuizMixin,
    BondMixin,
    RaffleMixin,
    EventMixin,
    PachinkoMixin,
    RtpMixin,
    LoanMixin,
    RecordMixin,
    MarginMixin,
    CryptoMixin,
    MarketMixin,
    ShopMixin,
    GuildMixin,
    DebtMixin,
    BankMixin,
    AuctionMixin,
    RaceMixin,
    HorseMixin,
    LaborMixin,
    OptionMixin,
    MarketEventMixin,
    EarningsMixin,
    P2PMixin,
    IEconomy,
):
    DAILY_BASE = DAILY_BASE
    DAILY_STREAK_BONUS = DAILY_STREAK_BONUS
    DAILY_STREAK_CAP = DAILY_STREAK_CAP

    def __init__(self, db_path: str, legacy_guild_id: Optional[int] = None, strict: bool = False):
        """legacy_guild_id: サーバー別になる前の古い記録を引き継ぐサーバー。古い記録があるのに未指定ならエラー。
        strict: True なら、for_guild を通さずに使うとエラーにする（Bot 本体で、指定漏れを防ぐ）。
        """
        db = EcoDb(db_path)
        self._bind_db(db, 0, strict)
        schema.migrate_to_guild_scope(self._conn, self._db_path, legacy_guild_id)
        self._init_schema()
        with self._lock, self._conn:
            schema.retire_autoplay(self._conn)

    # ------------------------------------------------------------------ #
    # サーバー単位の見え方                                                #
    # ------------------------------------------------------------------ #
    def for_guild(self, guild_id: int) -> Self:
        """そのサーバー専用の見え方を返す（接続とロックは共有する）。"""
        gid = int(guild_id)
        if gid <= 0:
            raise ValueError(f"サーバーIDが不正です: {guild_id!r}")
        view = type(self).__new__(type(self))
        view._bind_db(self._db, gid, False)
        return view

    def _g(self) -> int:
        if self._strict and self.guild_id == 0:
            raise RuntimeError("サーバーが指定されていません。economy.for_guild(サーバーID) を通して使ってください。")
        return self.guild_id

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            schema.create_tables(self._conn)
            schema.add_missing_columns(self._conn)
            self._purge_unlistable_auctions()  # 競売に出せなくなった品（称号）が、古い出品に残っていたら消す

    # ------------------------------------------------------------------ #
    # 台帳・口座プリミティブ                                              #
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
        self._accrue_debt_interest(user_id)  # 帝愛の利息（トイチ）。経過した日数ぶんがあれば、借金に加える
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
        with self._lock, self._conn:
            self._accrue_debt_interest(user_id)  # 帝愛の利息（トイチ）が溜まっていれば、先に加える
            row = self._conn.execute(
                "SELECT debt FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
            return row["debt"] if row else 0

    def close(self) -> None:
        self._db.close()
