"""債券の種類と計算。利率・期間・上限はここの数字だけで決まる（調整はここを書き換える）。

通常債: 元本保証。途中解約もできる（違約金つき）。
ジャンク債: 利息が高い代わりに、満期のときに一定の確率で倒産して元本を失う。解約はできない。
期待値は 通常債 104%、ジャンク債 (1-0.20)×1.20 = 96%。
"""

from dataclasses import dataclass

MIN_AMOUNT = 1_000


@dataclass(frozen=True)
class Kind:
    key: str
    name: str
    emoji: str
    hours: int  # 満期までの時間
    rate: int  # 利息（元本に対する％）
    default_pct: int  # 倒産する確率（％）
    max_total: int  # 1人が持てる元本の合計の上限（保有中のもの）
    cancel_penalty: int  # 途中解約で引かれる元本の％。None なら解約できない


NORMAL = Kind("normal", "通常債", "🏦", 72, 4, 0, 30_000, 10)
JUNK = Kind("junk", "ジャンク債", "🗑️", 24, 20, 20, 10_000, None)
KINDS = {k.key: k for k in (NORMAL, JUNK)}


def interest(kind: Kind, principal: int) -> int:
    return principal * kind.rate // 100


def maturity_payout(kind: Kind, principal: int) -> int:
    return principal + interest(kind, principal)


def cancel_refund(kind: Kind, principal: int) -> int:
    return principal - principal * kind.cancel_penalty // 100
