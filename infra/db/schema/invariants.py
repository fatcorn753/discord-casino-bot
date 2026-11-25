"""マイグレーション前後の会計・資産不変条件の検証。"""

import math
import sqlite3

InvariantValue = int | float
CandidateSpec = tuple[str, tuple[str, ...], str]

# (論理キー名, 優先順に評価する (テーブル名, 必須列, 集計SQL式) 候補群)
_INVARIANT_SPECS: tuple[tuple[str, tuple[CandidateSpec, ...]], ...] = (
    (
        "accounts_count",
        (
            ("accounts", (), "COUNT(*)"),
            ("users", (), "COUNT(*)"),
        ),
    ),
    (
        "accounts_balance_sum",
        (
            ("accounts", ("balance",), "COALESCE(SUM(balance), 0)"),
            ("users", ("balance",), "COALESCE(SUM(balance), 0)"),
        ),
    ),
    (
        "debts_sum",
        (
            ("teiai_debts", ("debt",), "COALESCE(SUM(debt), 0)"),
            ("users", ("debt",), "COALESCE(SUM(debt), 0)"),
        ),
    ),
    (
        "debts_principal_sum",
        (
            ("teiai_debts", ("principal",), "COALESCE(SUM(principal), 0)"),
            ("users", ("debt_principal",), "COALESCE(SUM(debt_principal), 0)"),
        ),
    ),
    (
        "play_stats_games_sum",
        (
            ("user_play_stats", ("games_played",), "COALESCE(SUM(games_played), 0)"),
            ("users", ("games_played",), "COALESCE(SUM(games_played), 0)"),
        ),
    ),
    (
        "play_stats_wagered_sum",
        (
            ("user_play_stats", ("total_wagered",), "COALESCE(SUM(total_wagered), 0)"),
            ("users", ("total_wagered",), "COALESCE(SUM(total_wagered), 0)"),
        ),
    ),
    (
        "play_stats_won_sum",
        (
            ("user_play_stats", ("total_won",), "COALESCE(SUM(total_won), 0)"),
            ("users", ("total_won",), "COALESCE(SUM(total_won), 0)"),
        ),
    ),
    (
        "daily_streak_sum",
        (
            ("user_daily", ("streak",), "COALESCE(SUM(streak), 0)"),
            ("users", ("daily_streak",), "COALESCE(SUM(daily_streak), 0)"),
        ),
    ),
    (
        "grace_active_count",
        (
            ("teiai_grace", ("grace_until",), "COUNT(grace_until)"),
            ("users", ("grace_until",), "COUNT(grace_until)"),
        ),
    ),
    (
        "bankruptcies_count",
        (
            ("teiai_bankruptcies", ("bankrupt_at",), "COUNT(*)"),
            ("users", ("bankrupt_at",), "COUNT(bankrupt_at)"),
        ),
    ),
    (
        "escapes_tries_sum",
        (
            ("teiai_escapes", ("tries",), "COALESCE(SUM(tries), 0)"),
            ("users", ("escape_tries",), "COALESCE(SUM(escape_tries), 0)"),
        ),
    ),
    ("inventory_qty_sum", (("inventory", ("qty",), "COALESCE(SUM(qty), 0)"),)),
    ("escrow_sum", (("escrow", ("amount",), "COALESCE(SUM(amount), 0)"),)),
    ("teiai_deposits_sum", (("teiai_deposits", ("amount",), "COALESCE(SUM(amount), 0)"),)),
    ("teiai_bank_cash_sum", (("teiai_bank", ("cash",), "COALESCE(SUM(cash), 0.0)"),)),
    ("teiai_bank_loaned_sum", (("teiai_bank", ("loaned",), "COALESCE(SUM(loaned), 0.0)"),)),
    (
        "teiai_thermo_burned_sum",
        (
            ("teiai_thermostat", ("burned",), "COALESCE(SUM(burned), 0.0)"),
            ("teiai_bank", ("thermo_burned",), "COALESCE(SUM(thermo_burned), 0.0)"),
        ),
    ),
    (
        "teiai_thermo_bailed_sum",
        (
            ("teiai_thermostat", ("bailed",), "COALESCE(SUM(bailed), 0.0)"),
            ("teiai_bank", ("thermo_bailed",), "COALESCE(SUM(thermo_bailed), 0.0)"),
        ),
    ),
    (
        "teiai_trust_paid_sum",
        (
            ("teiai_trust", ("paid",), "COALESCE(SUM(paid), 0.0)"),
            ("teiai_bank", ("trust_paid",), "COALESCE(SUM(trust_paid), 0.0)"),
        ),
    ),
    (
        "teiai_excess_unpaid_sum",
        (
            ("teiai_trust", ("excess_unpaid",), "COALESCE(SUM(excess_unpaid), 0.0)"),
            ("teiai_bank", ("excess_unpaid",), "COALESCE(SUM(excess_unpaid), 0.0)"),
        ),
    ),
    (
        "kyosai_paid_sum",
        (
            ("kyosai_state", ("paid",), "COALESCE(SUM(paid), 0.0)"),
            ("teiai_bank", ("kyosai_paid",), "COALESCE(SUM(kyosai_paid), 0.0)"),
        ),
    ),
    (
        "kyosai_charged_sum",
        (
            ("kyosai_state", ("charged",), "COALESCE(SUM(charged), 0.0)"),
            ("teiai_bank", ("kyosai_charged",), "COALESCE(SUM(kyosai_charged), 0.0)"),
        ),
    ),
    ("teiai_house_balance_sum", (("teiai_house", ("balance",), "COALESCE(SUM(balance), 0)"),)),
    ("teiai_house_skimmed_sum", (("teiai_house", ("skimmed",), "COALESCE(SUM(skimmed), 0)"),)),
    (
        "teiai_house_grant_used_sum",
        (
            ("teiai_house_grants", ("used",), "COALESCE(SUM(used), 0)"),
            (
                "teiai_house",
                ("grant_day", "grant_used"),
                "COALESCE(SUM(CASE WHEN grant_day != '' AND grant_used > 0 THEN grant_used ELSE 0 END), 0)",
            ),
        ),
    ),
    ("kyosai_units_sum", (("kyosai_units", ("units",), "COALESCE(SUM(units), 0)"),)),
    ("companies_funds_sum", (("companies", ("funds",), "COALESCE(SUM(funds), 0)"),)),
    ("company_stakes_sum", (("company_stakes", ("amount",), "COALESCE(SUM(amount), 0)"),)),
    ("crypto_units_sum", (("crypto", ("units",), "COALESCE(SUM(units), 0)"),)),
    ("crypto_short_margin_sum", (("crypto_short", ("margin",), "COALESCE(SUM(margin), 0)"),)),
    ("crypto_long_margin_sum", (("crypto_long", ("margin",), "COALESCE(SUM(margin), 0)"),)),
    ("bonds_principal_sum", (("bonds", ("principal",), "COALESCE(SUM(principal), 0)"),)),
    ("options_margin_sum", (("options", ("margin",), "COALESCE(SUM(margin), 0)"),)),
    ("loans_principal_sum", (("loans", ("principal",), "COALESCE(SUM(principal), 0)"),)),
    ("jackpot_pool_sum", (("jackpot", ("pool",), "COALESCE(SUM(pool), 0)"),)),
    ("labor_pot_sum", (("labor", ("pot",), "COALESCE(SUM(pot), 0)"),)),
    (
        "seized_items_positive_qty_sum",
        (("seized_items", ("qty",), "COALESCE(SUM(CASE WHEN qty > 0 THEN qty ELSE 0 END), 0)"),),
    ),
    ("horse_bets_count", (("horse_bets", (), "COUNT(*)"),)),
    ("market_listings_count", (("market_listings", (), "COUNT(*)"),)),
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


def _eval_candidates(conn: sqlite3.Connection, candidates: tuple[CandidateSpec, ...]) -> InvariantValue | None:
    for table, required_cols, expr in candidates:
        val = _query_spec(conn, table, required_cols, expr)
        if val is not None:
            return val
    return None


def capture_invariants(conn: sqlite3.Connection) -> dict[str, InvariantValue]:
    """マイグレーション前後に保つべき合計値・件数のスナップショットを取得する。"""
    result: dict[str, InvariantValue] = {}
    for key, candidates in _INVARIANT_SPECS:
        val = _eval_candidates(conn, candidates)
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
