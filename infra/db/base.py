"""Economy と各部品（eco_*.py）が共有する DB 接続状態と型付き基底契約。

abc.ABC を使わず通常の基底クラス（NotImplementedError スタブ）で契約を明示し、
各 Mixin の DB 操作・ドメイン間参照を静的型チェック可能にする。
"""

import os
import sqlite3
import threading
from typing import Any, Optional, Self

from domain.errors import Failure, Notice
from domain.market import DEFAULT as DEFAULT_SYMBOL
from domain.types import CryptoResult


class EcoDb:
    """1 つの SQLite データベースに対して共有される接続・ロック・一時通知ストア。"""

    def __init__(self, db_path: str) -> None:
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self.db_path = db_path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.notices: dict[tuple[int, int], list[Notice]] = {}

    def close(self) -> None:
        with self.lock:
            self.conn.close()


class EcoBase:
    """すべての eco_*.py Mixin が継承する型付き基底クラス。"""

    _db: EcoDb
    _db_path: str
    _conn: sqlite3.Connection
    _lock: threading.RLock
    guild_id: int
    _strict: bool
    _notices: dict[tuple[int, int], list[Notice]]

    HORSE_PRICE: int
    SIDE_NAME: dict[str, str]

    def _bind_db(self, db: EcoDb, guild_id: int, strict: bool) -> None:
        self._db = db
        self._db_path = db.db_path
        self._conn = db.conn
        self._lock = db.lock
        self._notices = db.notices
        self.guild_id = guild_id
        self._strict = strict

    def for_guild(self, guild_id: int) -> Self:
        raise NotImplementedError

    def _g(self) -> int:
        raise NotImplementedError

    def _ensure(self, user_id: int) -> None:
        raise NotImplementedError

    def _ledger(self, user_id: int, delta: int, balance_after: int, reason: str) -> None:
        raise NotImplementedError

    def _balance(self, user_id: int) -> int:
        raise NotImplementedError

    def _debt(self, user_id: int) -> int:
        raise NotImplementedError

    def _apply(self, user_id: int, delta: int, reason: str, income: int = 0) -> int:
        raise NotImplementedError

    def debt(self, user_id: int) -> int:
        raise NotImplementedError

    def debit_up_to(self, user_id: int, amount: int, reason: str) -> int:
        raise NotImplementedError

    def crypto_values(self) -> dict[int, int]:
        raise NotImplementedError

    def wealthy_ids(self) -> set[int]:
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # ドメイン部品（eco_*.py）間で参照されるメソッド群                    #
    # ------------------------------------------------------------------ #
    def _accrue_debt_interest(self, user_id: int, now: Optional[float] = None) -> int:
        raise NotImplementedError

    def _after_debt_change(self, user_id: int) -> None:
        raise NotImplementedError

    def _note(self, user_id: int, code: str, **params: Any) -> None:
        raise NotImplementedError

    def _owe(self, user_id: int, shortfall: int, reason: str) -> int:
        raise NotImplementedError

    def _repay_all_of(self, user_id: int, amount: int, label: str) -> int:
        raise NotImplementedError

    def _seed(self, user_id: int, amount: int) -> None:
        raise NotImplementedError

    def _topup_seed(self, user_id: int) -> int:
        raise NotImplementedError

    def _withhold(self, user_id: int, income: int) -> int:
        raise NotImplementedError

    def status(self, user_id: int, now: Optional[float] = None) -> str:
        raise NotImplementedError

    def bankrupt(self, user_id: int, forced: bool = False, escape: bool = False) -> tuple[Any, ...]:
        raise NotImplementedError

    def can_borrow_for_bet(self, user_id: int, need: int) -> tuple[Any, ...]:
        raise NotImplementedError

    def loan_blacklisted(self, user_id: int, now: Optional[float] = None) -> bool:
        raise NotImplementedError

    def _bank_income(
        self, amount: int, share: float, now: Optional[float] = None, needs_depositors: bool = False
    ) -> None:
        raise NotImplementedError

    def _bank_locks(self, now: float) -> float:
        raise NotImplementedError

    def _bank_pay(self, amount: int, now: Optional[float] = None) -> int:
        raise NotImplementedError

    def _bank_set(self, cash: float, loaned: float) -> None:
        raise NotImplementedError

    def _bank_sync(self, now: float) -> dict[str, Any]:
        raise NotImplementedError

    def _cds_cover_self(self, user_id: int, shortfall: int, now: Optional[float] = None) -> int:
        raise NotImplementedError

    def _cds_settle_default(self, target_id: int, debt_before: int, now: Optional[float] = None) -> int:
        raise NotImplementedError

    def _cds_void(self, target_id: int) -> None:
        raise NotImplementedError

    def _deposit_take(self, user_id: int, need: int, now: Optional[float] = None) -> int:
        raise NotImplementedError

    def _grace_update(self, user_id: int, now: Optional[float] = None) -> None:
        raise NotImplementedError

    def _house_income(self, amount: int) -> None:
        raise NotImplementedError

    def bank_values(self) -> dict[int, int]:
        raise NotImplementedError

    def deposit_of(self, user_id: int) -> int:
        raise NotImplementedError

    def grace_until(self, user_id: int) -> Optional[float]:
        raise NotImplementedError

    def _auction_list_locked(self, item_id: str, qty: int, now: float) -> None:
        raise NotImplementedError

    def _purge_unlistable_auctions(self) -> int:
        raise NotImplementedError

    def _bond_cancel_locked(self, user_id: int, bond_id: int, now: Optional[float] = None) -> tuple[Any, ...]:
        raise NotImplementedError

    def bond_values(self) -> dict[int, int]:
        raise NotImplementedError

    def _crypto_sell_locked(
        self,
        user_id: int,
        units: Optional[int],
        price: float,
        symbol: str = DEFAULT_SYMBOL,
        taxable: bool = False,
    ) -> CryptoResult:
        raise NotImplementedError

    def _margin_close_locked(
        self,
        user_id: int,
        side: str,
        units: Optional[int],
        price: float,
        label: str,
        symbol: str = DEFAULT_SYMBOL,
        owe: bool = False,
        taxable: bool = False,
    ) -> CryptoResult:
        raise NotImplementedError

    def _margin_rows(self, user_id: int) -> list[Any]:
        raise NotImplementedError

    def _margin_status_locked(
        self,
        user_id: int,
        prices: dict[str, Any],
        add: Optional[tuple[Any, ...]] = None,
        drop_collateral: float = 0.0,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _pin_reason(self, symbol: str, action: str) -> Optional[Failure]:
        raise NotImplementedError

    def holdings(self, user_id: int) -> list[dict[str, Any]]:
        raise NotImplementedError

    def latest_prices(self) -> dict[str, Any]:
        raise NotImplementedError

    def margin_equity(self) -> dict[int, int]:
        raise NotImplementedError

    def margin_positions(self, user_id: int) -> list[dict[str, Any]]:
        raise NotImplementedError

    def margin_status(self, user_id: int, prices: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        raise NotImplementedError

    def _option_close_locked(self, user_id: int, option_id: int, now: float) -> tuple[Any, ...]:
        raise NotImplementedError

    def option_values(self) -> dict[int, int]:
        raise NotImplementedError

    def _collect_overdue_for(self, user_id: int, budget: int) -> int:
        raise NotImplementedError

    def loan_remaining(self, loan: dict[str, Any], now: float) -> int:
        raise NotImplementedError

    def _consume(self, user_id: int, item_id: str) -> bool:
        raise NotImplementedError

    def _seize_horses(self, user_id: int, now: float) -> int:
        raise NotImplementedError

    def horse_get(self, horse_id: int) -> Optional[dict[str, Any]]:
        raise NotImplementedError

    def _relic_buffs(self, user_id: int) -> dict[str, float]:
        raise NotImplementedError

    def _horse_count(self, user_id: int) -> int:
        raise NotImplementedError

    def p2p_pull_back(self, user_id: int, now: Optional[float] = None) -> dict[str, Any]:
        raise NotImplementedError

    def earnings_jumps(self, now: float, seconds: float) -> list[dict[str, Any]]:
        raise NotImplementedError

    def market_events(self, now: Optional[float] = None) -> list[dict[str, Any]]:
        raise NotImplementedError
