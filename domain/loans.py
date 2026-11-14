"""貸し借りの数字と計算（Discord に依存しない）。利息は日割りの単利。

- 利息 = 元本 × 日利 × 経過日数。経過日数は、貸した時刻から数えて切り上げ（最低1日）、期間（日数）が上限。
- 期限を過ぎると、**遅延損害金**が付く（延滞の借り得をなくすため）。元本 × 遅延日利 × 延滞日数（切り上げ）。
  遅延日利は、約定日利の1.5倍と LATE_MIN_RATE_TENTHS の大きいほう。遅延損害金の合計は、元本の LATE_CAP 倍が上限。
- 返済は、まず元本、次に利息に充てる。
- 日利は 0.1% 刻みで、tenths（0.1%を1とした整数）で持つ。金額と日利に上限はない（貸す人の残高と、借りる人の承諾が歯止め）。
"""

import math

from domain.ports import get_catalogs

_CFG = get_catalogs().economy().loans
USER_MIN_PRINCIPAL = _CFG.user_min_principal
MIN_TERM_DAYS = _CFG.min_term_days
MAX_TERM_DAYS = _CFG.max_term_days
OFFER_HOURS = _CFG.offer_hours  # 承諾を待つ時間
MAX_BORROWER_OPEN = _CFG.max_borrower_open  # 同時に出せる募集（借りたい人が出す）の数
MAX_LENDER_OPEN = _CFG.max_lender_open  # 同時に出せる提案（承諾待ち）の数
MAX_LENDER_ACTIVE = _CFG.max_lender_active  # 同時に貸せる件数
REMIND_HOURS = _CFG.remind_hours  # 期限のこの時間前に知らせる
LATE_MIN_RATE_TENTHS = (
    _CFG.late_min_rate_tenths
)  # 遅延日利の下限（2.0%/日）。約定日利の1.5倍がこれより小さいときは、これを使う
LATE_CAP = _CFG.late_cap  # 遅延損害金の合計の上限（元本の何倍まで）


def rate_text(rate_tenths: int) -> str:
    return f"{rate_tenths / 10:g}%"


def elapsed_days(accepted_at: float, now: float, term_days: int) -> int:
    return min(term_days, max(1, math.ceil((now - accepted_at) / 86400)))


def interest(principal: int, rate_tenths: int, days: int) -> int:
    return principal * rate_tenths * days // 1000


def late_rate_tenths(rate_tenths: int) -> int:
    """遅延日利（tenths）。約定日利の1.5倍と、下限の大きいほう。"""
    return max(rate_tenths * 3 // 2, LATE_MIN_RATE_TENTHS)


def late_interest(principal: int, rate_tenths: int, term_days: int, accepted_at: float, now: float) -> int:
    """期限（貸した時刻 + 期間）を過ぎたぶんの遅延損害金。期限内なら 0。延滞日数は切り上げ、合計は元本の LATE_CAP 倍まで。"""
    due = accepted_at + term_days * 86400.0
    if now <= due:
        return 0
    days = math.ceil((now - due) / 86400)
    return min(principal * LATE_CAP, interest(principal, late_rate_tenths(rate_tenths), days))


def total_owed(principal: int, rate_tenths: int, term_days: int, accepted_at: float, now: float) -> int:
    """いま返すとしたら、全部でいくらか（元本＋日割りの利息＋期限を過ぎていれば遅延損害金）。"""
    return (
        principal
        + interest(principal, rate_tenths, elapsed_days(accepted_at, now, term_days))
        + late_interest(principal, rate_tenths, term_days, accepted_at, now)
    )


def full_term_total(principal: int, rate_tenths: int, term_days: int) -> int:
    """期間いっぱい借りた場合の返済総額。"""
    return principal + interest(principal, rate_tenths, term_days)
