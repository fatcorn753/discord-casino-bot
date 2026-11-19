"""金利の共通の計算（#33・#18）。金利はすべて「1日あたり・連続複利」で表す。

無リスク金利 r_f は、元本保証の通常債（帝愛社債）の利率が基準。帝愛信託の利率は r_f に上乗せをしたもの。
"""

import math

from domain import bonds


def per_day(rate_pct: float, hours: float) -> float:
    """hours 時間で rate_pct ％増える利率を、1日あたりの連続複利に直す。"""
    return math.log(1 + rate_pct / 100) / (hours / 24)


BASE_RISK_FREE_PER_DAY = per_day(bonds.NORMAL.rate, bonds.NORMAL.hours)  # 通常債（72時間で 4%）≒ 1.31%/日


def growth(rate_per_day: float, days: float) -> float:
    """rate_per_day で days 日運用したときの増え方（元本に対する割合。exp(r·t) − 1）。"""
    return math.expm1(rate_per_day * max(0.0, days))
