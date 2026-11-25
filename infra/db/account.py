"""台帳・口座残高プリミティブ（全リポジトリがコンポジションで共有する口座台帳）。"""

from domain.ports.bridges import IDebtLifecycleHook, NoOpDebtLifecycleHook
from domain.types import INITIAL_BALANCE
from infra.db.base import EcoDb, fetch_scalar


class AccountCore:
    """口座の作成・残高操作・台帳記録を担う共有ストア（Mixin 継承に依存しない）。"""

    def __init__(self, db: EcoDb, debt_hook: IDebtLifecycleHook | None = None) -> None:
        self._db = db
        self._conn = db.conn
        self._lock = db.lock
        self._debt_hook: IDebtLifecycleHook = debt_hook or NoOpDebtLifecycleHook()

    def ensure(self, guild_id: int, user_id: int) -> None:
        """未登録なら初期残高つきで作る（呼び出し側のトランザクション内で使う）。"""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO accounts (guild_id, user_id, balance, created_at) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, INITIAL_BALANCE, self._db.now_ts()),
        )
        if cur.rowcount:
            self.ledger(guild_id, user_id, INITIAL_BALANCE, INITIAL_BALANCE, "初期支給", kind="initial_grant")
            # 帝愛共済の口は配らない（総口数は固定。新しい人は、譲渡か帝愛の競りで手に入れる。#93）

    def ledger(
        self,
        guild_id: int,
        user_id: int,
        delta: int,
        balance_after: int,
        reason: str,
        *,
        kind: str = "",
        counterparty_id: int | None = None,
        ref_id: int | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO ledger (guild_id, user_id, delta, balance_after, reason, ts, kind, counterparty_id, ref_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, delta, balance_after, reason, self._db.now_ts(), kind, counterparty_id, ref_id),
        )

    def raw_balance(self, guild_id: int, user_id: int) -> int:
        return fetch_scalar(self._conn, "SELECT balance FROM accounts WHERE guild_id=? AND user_id=?", (guild_id, user_id))

    def debt(self, guild_id: int, user_id: int) -> int:
        self._debt_hook.accrue_interest(guild_id, user_id)
        v = fetch_scalar(self._conn, "SELECT debt FROM teiai_debts WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        return int(v) if v is not None else 0

    def debt_readonly(self, guild_id: int, user_id: int) -> int:
        """トランザクション内で蓄積利息を反映したうえの借金額（口座がなければ 0）。"""
        with self._db.tx():
            self._debt_hook.accrue_interest(guild_id, user_id)
            v = fetch_scalar(
                self._conn, "SELECT debt FROM teiai_debts WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            )
            return int(v) if v is not None else 0

    def after_debt_change(self, guild_id: int, user_id: int) -> None:
        self._debt_hook.after_debt_change(guild_id, user_id)

    def set_debt(
        self,
        guild_id: int,
        user_id: int,
        debt: int,
        principal: int | None = None,
        interest_at: float = 0.0,
    ) -> None:
        """借金・元本・利息時計を設定する（0 以下なら teiai_debts 行を削除する）。"""
        if debt <= 0:
            self._conn.execute("DELETE FROM teiai_debts WHERE guild_id=? AND user_id=?", (guild_id, user_id))
            return
        eff_principal = max(0, debt if principal is None else principal)
        self._conn.execute(
            "INSERT INTO teiai_debts (guild_id, user_id, debt, principal, interest_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "debt=excluded.debt, principal=excluded.principal, interest_at=excluded.interest_at",
            (guild_id, user_id, debt, eff_principal, interest_at),
        )

    def add_debt_principal(self, guild_id: int, user_id: int, amount: int) -> None:
        """帝愛の借金と元本を amount だけ増やす（既存の利息は元本に組み込まない）。"""
        if amount <= 0:
            return
        self._conn.execute(
            "INSERT INTO teiai_debts (guild_id, user_id, debt, principal, interest_at) VALUES (?, ?, ?, ?, 0.0) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "debt = teiai_debts.debt + excluded.debt, "
            "principal = CASE WHEN teiai_debts.debt <= 0 THEN excluded.principal "
            "WHEN teiai_debts.principal <= 0 THEN teiai_debts.debt + excluded.principal "
            "ELSE teiai_debts.principal + excluded.principal END",
            (guild_id, user_id, amount, amount),
        )

    def pay_debt_from_balance(self, guild_id: int, user_id: int, pay: int) -> None:
        """手元の残高から借金を pay だけ返済し、元本と完済時の利息時計を整合させる。"""
        if pay <= 0:
            return
        self._conn.execute(
            "UPDATE accounts SET balance = balance - ? WHERE guild_id=? AND user_id=?",
            (pay, guild_id, user_id),
        )
        self._conn.execute(
            "UPDATE teiai_debts SET debt = debt - ?, "
            "principal = CASE WHEN debt - ? <= 0 THEN 0 "
            "ELSE MIN(CASE WHEN principal <= 0 THEN debt ELSE principal END, debt - ?) END, "
            "interest_at = CASE WHEN debt - ? <= 0 THEN 0 ELSE interest_at END "
            "WHERE guild_id=? AND user_id=?",
            (pay, pay, pay, pay, guild_id, user_id),
        )
        self._conn.execute("DELETE FROM teiai_debts WHERE guild_id=? AND user_id=? AND debt <= 0", (guild_id, user_id))

    def apply(
        self,
        guild_id: int,
        user_id: int,
        delta: int,
        reason: str,
        income: int = 0,
        *,
        kind: str = "",
        counterparty_id: int | None = None,
        ref_id: int | None = None,
    ) -> int:
        """残高に delta を足す（0 未満にはならない）。実際に変化した量を返す。"""
        before = self.raw_balance(guild_id, user_id)
        after = max(0, before + delta)
        self._conn.execute("UPDATE accounts SET balance=? WHERE guild_id=? AND user_id=?", (after, guild_id, user_id))
        if after != before:
            self.ledger(guild_id, user_id, after - before, after, reason, kind=kind, counterparty_id=counterparty_id, ref_id=ref_id)
        if delta > 0 and income > 0:
            self._debt_hook.withhold_income(guild_id, user_id, min(income, delta))
        return self.raw_balance(guild_id, user_id) - before

    def reduce_debt(self, guild_id: int, user_id: int, amount: int) -> None:
        """シニア債権（帝愛への借金）を amount だけ減らす（呼び出し側のトランザクション内で使う）。"""
        if amount <= 0:
            return
        self._conn.execute(
            "UPDATE teiai_debts SET debt=debt-? WHERE guild_id=? AND user_id=?", (amount, guild_id, user_id)
        )

    def record_wager(self, guild_id: int, user_id: int, amount: int) -> None:
        """賭け金の累計を amount だけ増やす（呼び出し側のトランザクション内で使う）。"""
        if amount <= 0:
            return
        self._conn.execute(
            "INSERT INTO user_play_stats (guild_id, user_id, total_wagered) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "total_wagered = user_play_stats.total_wagered + excluded.total_wagered",
            (guild_id, user_id, amount),
        )

    def record_game_result(self, guild_id: int, user_id: int, won: int, net: int) -> None:
        """対局結果を記録する（呼び出し側のトランザクション内で使う）。biggest_win には max(0, net) を入れる。"""
        self._conn.execute(
            "INSERT INTO user_play_stats (guild_id, user_id, games_played, total_won, biggest_win) VALUES (?, ?, 1, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "games_played = user_play_stats.games_played + 1, "
            "total_won = user_play_stats.total_won + excluded.total_won, "
            "biggest_win = MAX(user_play_stats.biggest_win, excluded.biggest_win)",
            (guild_id, user_id, won, max(0, net)),
        )
