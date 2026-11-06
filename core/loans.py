"""貸し借りの数字と計算（Discord に依存しない）。利息は日割りの単利。

- 利息 = 元本 × 日利 × 経過日数。経過日数は、貸した時刻から数えて切り上げ（最低1日）、期間（日数）が上限。
- 期限を過ぎても、利息は期間ぶんで止まる（延滞の利息は付かない）。
- 返済は、まず元本、次に利息に充てる。
"""

import math

USER_MIN_PRINCIPAL = 100
USER_MAX_PRINCIPAL = 100_000
HOUSE_MAX_PRINCIPAL = 1_000_000
MAX_RATE_TENTHS = 1000           # 日利の上限（100.0%）。日利は 0.1% 刻みで、tenths（0.1%を1とした整数）で持つ
MIN_TERM_DAYS = 1
MAX_TERM_DAYS = 30
OFFER_HOURS = 24                 # 承諾を待つ時間
MAX_BORROWER_ACTIVE = 3          # 同時に借りられる件数
MAX_LENDER_OPEN = 5              # 同時に出せる提案（承諾待ち）の数
MAX_LENDER_ACTIVE = 10           # 同時に貸せる件数
REMIND_HOURS = 24                # 期限のこの時間前に知らせる


def rate_text(rate_tenths: int) -> str:
    return f"{rate_tenths / 10:g}%"


def elapsed_days(accepted_at: float, now: float, term_days: int) -> int:
    return min(term_days, max(1, math.ceil((now - accepted_at) / 86400)))


def interest(principal: int, rate_tenths: int, days: int) -> int:
    return principal * rate_tenths * days // 1000


def total_owed(principal: int, rate_tenths: int, term_days: int, accepted_at: float, now: float) -> int:
    """いま返すとしたら、全部でいくらか（元本＋日割りの利息）。"""
    return principal + interest(principal, rate_tenths, elapsed_days(accepted_at, now, term_days))


def full_term_total(principal: int, rate_tenths: int, term_days: int) -> int:
    """期間いっぱい借りた場合の返済総額。"""
    return principal + interest(principal, rate_tenths, term_days)


def split_payment(principal: int, paid_before: int, amount: int) -> tuple:
    """返済 amount を、(元本に充てた額, 利息に充てた額) に分ける。先に元本。"""
    principal_part = min(paid_before + amount, principal) - min(paid_before, principal)
    return principal_part, amount - principal_part
