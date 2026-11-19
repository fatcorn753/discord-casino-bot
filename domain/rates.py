"""金利の共通の計算（#33・#18）。金利はすべて「1日あたり・連続複利」で表す。

#18: r_f は帝愛銀行の準備率 θ に連動して動く。θ が低い（現金が足りない）ほど金利を上げて、債券で現金を集める。

無リスク金利 r_f は、元本保証の通常債（帝愛社債）の利率が基準。帝愛信託の利率は r_f に上乗せをしたもの。
"""

import math
from typing import Optional

from domain import bonds, teiai


def per_day(rate_pct: float, hours: float) -> float:
    """hours 時間で rate_pct ％増える利率を、1日あたりの連続複利に直す。"""
    return math.log(1 + rate_pct / 100) / (hours / 24)


BASE_RISK_FREE_PER_DAY = per_day(bonds.NORMAL.rate, bonds.NORMAL.hours)  # 通常債（72時間で 4%）≒ 1.31%/日


def growth(rate_per_day: float, days: float) -> float:
    """rate_per_day で days 日運用したときの増え方（元本に対する割合。exp(r·t) − 1）。"""
    return math.expm1(rate_per_day * max(0.0, days))


RF_MIN = per_day(teiai.RF_MIN_PCT, bonds.NORMAL.hours)
RF_MAX = per_day(teiai.RF_MAX_PCT, bonds.NORMAL.hours)
_NOISE_MAX_DAYS = 1 / 24  # 止まっていたあとの1回の揺らぎは、1時間ぶんまで（長い停止で金利が飛ばないように）
# 売却の割引率のうち、金利でない分（手間賃）。基準の金利のとき、これまでの割引率（SALE_DISCOUNT_PER_HOUR）と同じになる
SALE_SPREAD_PER_HOUR = max(0.0, bonds.SALE_DISCOUNT_PER_HOUR - BASE_RISK_FREE_PER_DAY / 24)


def rf_target(theta: Optional[float]) -> float:
    """準備率 θ のときの r_f の目標 μ(θ)（1日あたり）。θ が None（信託も社債もない）なら基準の金利。"""
    if theta is None:
        return BASE_RISK_FREE_PER_DAY
    return RF_MIN + (RF_MAX - RF_MIN) * math.exp(-teiai.RF_ALPHA * max(0.0, theta))


def rf_step(r: float, theta: Optional[float], days: float, z: float) -> float:
    """r_f を days 日だけ進める（CIR 風）。z は標準正規乱数。

    平均回帰は厳密に（μ + (r − μ)·exp(−κ·dt)）、揺らぎは σ·√(r·dt)·z。結果は [下限, 上限] に収める。
    """
    if days <= 0:
        return r
    mu = rf_target(theta)
    drift = mu + (r - mu) * math.exp(-teiai.RF_KAPPA_PER_DAY * days)
    noise = teiai.RF_SIGMA * math.sqrt(max(r, 0.0) * min(days, _NOISE_MAX_DAYS)) * z
    return min(max(drift + noise, RF_MIN), RF_MAX)


def bond_rate_pct(kind: bonds.Kind, rf: float) -> float:
    """いまの r_f で買う債券の利率（満期までの ％。小数2桁）。

    通常債は r_f を満期までの利率に直したもの。ほかの種類は、基準からのずれ（通常債の利率 − 基準の利率）だけ動かす。
    """
    normal = math.expm1(rf * bonds.NORMAL.hours / 24) * 100
    return round(max(0.0, kind.rate + normal - bonds.NORMAL.rate), 2)


def sale_discount_per_hour(rf: float) -> float:
    """売却（DCF）の割引率（1時間あたり）= いまの r_f + 売却スプレッド。"""
    return rf / 24 + SALE_SPREAD_PER_HOUR
