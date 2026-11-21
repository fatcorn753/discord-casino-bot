"""台帳・口座残高プリミティブ（全リポジトリがコンポジションで共有する口座台帳）。"""

from typing import Optional

from domain.ports.bridges import IDebtLifecycleHook, NoOpDebtLifecycleHook
from domain.types import INITIAL_BALANCE
from infra.db.base import EcoDb, fetch_scalar


class AccountCore:
    """口座の作成・残高操作・台帳記録を担う共有ストア（Mixin 継承に依存しない）。"""

    def __init__(self, db: EcoDb, debt_hook: Optional[IDebtLifecycleHook] = None) -> None:
        self._db = db
        self._conn = db.conn
        self._lock = db.lock
        self._debt_hook: IDebtLifecycleHook = debt_hook or NoOpDebtLifecycleHook()

    def ensure(self, guild_id: int, user_id: int) -> None:
        """未登録なら初期残高つきで作る（呼び出し側のトランザクション内で使う）。"""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO users (guild_id, user_id, balance, created_at) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, INITIAL_BALANCE, self._db.now_ts()),
        )
        if cur.rowcount:
            self.ledger(guild_id, user_id, INITIAL_BALANCE, INITIAL_BALANCE, "初期支給")
            # 帝愛共済の口は配らない（総口数は固定。新しい人は、譲渡か帝愛の競りで手に入れる。#93）

    def ledger(self, guild_id: int, user_id: int, delta: int, balance_after: int, reason: str) -> None:
        self._conn.execute(
            "INSERT INTO ledger (guild_id, user_id, delta, balance_after, reason, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, delta, balance_after, reason, self._db.now_ts()),
        )

    def raw_balance(self, guild_id: int, user_id: int) -> int:
        return fetch_scalar(self._conn, "SELECT balance FROM users WHERE guild_id=? AND user_id=?", (guild_id, user_id))

    def debt(self, guild_id: int, user_id: int) -> int:
        self._debt_hook.accrue_interest(guild_id, user_id)
        return fetch_scalar(self._conn, "SELECT debt FROM users WHERE guild_id=? AND user_id=?", (guild_id, user_id))

    def debt_readonly(self, guild_id: int, user_id: int) -> int:
        """トランザクション内で蓄積利息を反映したうえの借金額（口座がなければ 0）。"""
        with self._db.tx():
            self._debt_hook.accrue_interest(guild_id, user_id)
            v = fetch_scalar(self._conn, "SELECT debt FROM users WHERE guild_id=? AND user_id=?", (guild_id, user_id))
            return v if v is not None else 0

    def after_debt_change(self, guild_id: int, user_id: int) -> None:
        self._debt_hook.after_debt_change(guild_id, user_id)

    def apply(self, guild_id: int, user_id: int, delta: int, reason: str, income: int = 0) -> int:
        """残高に delta を足す（0 未満にはならない）。実際に変化した量を返す。"""
        before = self.raw_balance(guild_id, user_id)
        after = max(0, before + delta)
        self._conn.execute("UPDATE users SET balance=? WHERE guild_id=? AND user_id=?", (after, guild_id, user_id))
        if after != before:
            self.ledger(guild_id, user_id, after - before, after, reason)
        if delta > 0 and income > 0:
            self._debt_hook.withhold_income(guild_id, user_id, min(income, delta))
        return self.raw_balance(guild_id, user_id) - before
