"""マイグレーション前後の会計・資産不変条件の検証。"""

import math
import sqlite3

InvariantValue = int | float

# (キー名, テーブル名, 必須列, 集計SQL式)
_INVARIANT_SPECS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("users_count", "users", (), "COUNT(*)"),
    ("users_balance_sum", "users", ("balance",), "COALESCE(SUM(balance), 0)"),
    ("users_debt_sum", "users", ("debt",), "COALESCE(SUM(debt), 0)"),
    ("users_debt_principal_sum", "users", ("debt_principal",), "COALESCE(SUM(debt_principal), 0)"),
    ("inventory_qty_sum", "inventory", ("qty",), "COALESCE(SUM(qty), 0)"),
    ("escrow_sum", "escrow", ("amount",), "COALESCE(SUM(amount), 0)"),
    ("teiai_deposits_sum", "teiai_deposits", ("amount",), "COALESCE(SUM(amount), 0)"),
    ("teiai_bank_cash_sum", "teiai_bank", ("cash",), "COALESCE(SUM(cash), 0.0)"),
    ("teiai_bank_loaned_sum", "teiai_bank", ("loaned",), "COALESCE(SUM(loaned), 0.0)"),
    ("teiai_house_balance_sum", "teiai_house", ("balance",), "COALESCE(SUM(balance), 0)"),
    ("kyosai_units_sum", "kyosai_units", ("units",), "COALESCE(SUM(units), 0)"),
    ("companies_funds_sum", "companies", ("funds",), "COALESCE(SUM(funds), 0)"),
    ("company_stakes_sum", "company_stakes", ("amount",), "COALESCE(SUM(amount), 0)"),
    ("crypto_units_sum", "crypto", ("units",), "COALESCE(SUM(units), 0)"),
    ("crypto_short_margin_sum", "crypto_short", ("margin",), "COALESCE(SUM(margin), 0)"),
    ("crypto_long_margin_sum", "crypto_long", ("margin",), "COALESCE(SUM(margin), 0)"),
    ("bonds_principal_sum", "bonds", ("principal",), "COALESCE(SUM(principal), 0)"),
    ("options_margin_sum", "options", ("margin",), "COALESCE(SUM(margin), 0)"),
    ("loans_principal_sum", "loans", ("principal",), "COALESCE(SUM(principal), 0)"),
    ("jackpot_pool_sum", "jackpot", ("pool",), "COALESCE(SUM(pool), 0)"),
    ("labor_pot_sum", "labor", ("pot",), "COALESCE(SUM(pot), 0)"),
    (
        "seized_items_positive_qty_sum",
        "seized_items",
        ("qty",),
        "COALESCE(SUM(CASE WHEN qty > 0 THEN qty ELSE 0 END), 0)",
    ),
    ("horse_bets_count", "horse_bets", (), "COUNT(*)"),
    ("market_listings_count", "market_listings", (), "COUNT(*)"),
)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def _query_spec(
    conn: sqlite3.Connection,
    table: str,
    required_cols: tuple[str, ...],
    expr: str,
) -> InvariantValue | None:
    cols = _table_columns(conn, table)
    if not cols or not set(required_cols).issubset(cols):
        return None
    row = conn.execute(f"SELECT {expr} FROM {table}").fetchone()
    val = row[0] if row is not None else 0
    return float(val) if isinstance(val, float) else int(val)


def capture_invariants(conn: sqlite3.Connection) -> dict[str, InvariantValue]:
    """マイグレーション前後に保つべき合計値・件数のスナップショットを取得する。"""
    result: dict[str, InvariantValue] = {}
    for key, table, required_cols, expr in _INVARIANT_SPECS:
        val = _query_spec(conn, table, required_cols, expr)
        if val is not None:
            result[key] = val
    return result


def _values_match(before: InvariantValue, after: InvariantValue) -> bool:
    if isinstance(before, float) or isinstance(after, float):
        return math.isclose(float(before), float(after), rel_tol=1e-9, abs_tol=1e-6)
    return before == after


def diff_invariants(
    before: dict[str, InvariantValue],
    after: dict[str, InvariantValue],
) -> list[str]:
    """前後の不変条件を比較し、不一致があればエラー内容の一覧を返す。"""
    diffs: list[str] = []
    for key, bval in before.items():
        if key not in after:
            diffs.append(f"{key}: 移行後に欠落（移行前={bval}）")
            continue
        aval = after[key]
        if not _values_match(bval, aval):
            diffs.append(f"{key}: 移行前={bval} != 移行後={aval}")
    return diffs


def verify_invariants(
    before: dict[str, InvariantValue],
    after: dict[str, InvariantValue],
) -> None:
    """不変条件の差分が1つでもあれば RuntimeError を送出する。"""
    diffs = diff_invariants(before, after)
    if diffs:
        raise RuntimeError("マイグレーションの不変条件検証に失敗しました:\n" + "\n".join(diffs))
