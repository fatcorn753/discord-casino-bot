"""債券の種類と計算。利率・期間・上限はここの数字だけで決まる（調整はここを書き換える）。

通常債: 元本保証。満期の前でも、割引現在価値（DCF）の値段でいつでも売れる（待つほど値段が満期の額に近づく）。
ジャンク債: 利息が高い代わりに、満期までのどの瞬間にも倒産しうる（毎分の倒産抽選。ポアソン過程）。売れない。
倒産の強さ（ハザード率）λ は、満期までに倒産する確率が default_pct になるように決める: 1-exp(-λ×期間) = default_pct。
期待値は 通常債 104%、ジャンク債 (1-0.20)×1.20 = 96%（倒産の時期が満期でも途中でも変わらない）。
"""

import math
from dataclasses import dataclass
from typing import Optional

from domain.ports import get_catalogs

_CFG = get_catalogs().bonds()
MIN_AMOUNT = _CFG.min_amount


@dataclass(frozen=True)
class Kind:
    key: str
    name: str
    emoji: str
    hours: int  # 満期までの時間
    rate: int  # 利息（元本に対する％）
    default_pct: int  # 倒産する確率（％）
    max_total: int  # 1人が持てる元本の合計の上限（保有中のもの）
    sellable: bool  # 満期の前に、DCF の値段で売れるか


KINDS = {
    k.key: Kind(k.key, k.name, k.emoji, k.hours, k.rate, k.default_pct, k.max_total, k.sellable) for k in _CFG.kinds
}
NORMAL = KINDS["normal"]
JUNK = KINDS["junk"]


def interest(kind: Kind, principal: int, rate_pct: Optional[float] = None) -> int:
    """満期の利息。rate_pct は買ったときに決まった利率（％。#18）。None なら種類の基準の利率。"""
    rate = kind.rate if rate_pct is None else rate_pct
    return int(principal * rate / 100)


def maturity_payout(kind: Kind, principal: int, rate_pct: Optional[float] = None) -> int:
    return principal + interest(kind, principal, rate_pct)


SALE_DISCOUNT_PER_HOUR = (
    _CFG.sale_discount_per_hour
)  # 満期前に売るときの割引率（1時間あたり。連続複利）。市場で売る手間賃で、満期まで待つよりは必ず少し安い


def hazard_per_hour(kind: Kind) -> float:
    """ジャンク債の倒産の強さ λ（1時間あたり）。満期までの倒産確率が default_pct になる値。倒産しない債券は 0。"""
    if kind.default_pct <= 0:
        return 0.0
    return -math.log(1 - kind.default_pct / 100) / kind.hours


def default_probability(kind: Kind, hours: float) -> float:
    """hours 時間のあいだに倒産する確率 1-exp(-λ×hours)。"""
    return 1 - math.exp(-hazard_per_hour(kind) * max(0.0, hours))


def sale_value(
    kind: Kind,
    principal: int,
    now: float,
    due: float,
    rate_pct: Optional[float] = None,
    discount_per_hour: float = SALE_DISCOUNT_PER_HOUR,
) -> int:
    """満期前に売ったときの手取り（DCF）。満期の受取額を、残り時間 τ だけ割り引く: payout × exp(-割引率 × τ)。切り捨て。

    割引率は、いまの無リスク金利 + 売却スプレッド（#18。domain/rates.sale_discount_per_hour）。金利が上がると値段が下がる。
    満期に近づくほど満期の受取額に近づく。同じ金利のままなら、満期まで待つほうが必ず多い。
    """
    remaining = max(0.0, (due - now) / 3600)
    return int(maturity_payout(kind, principal, rate_pct) * math.exp(-discount_per_hour * remaining))
