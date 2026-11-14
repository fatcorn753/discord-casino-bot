"""ルーレット（ヨーロピアン: 0〜36）。賭け方と払い戻し。"""

import random

from core import catalogs

_CFG = catalogs.games().roulette

RED = set(_CFG.red)

# 賭け方 → (表示名, 払い戻し倍率: 当たったときの受け取り総額/賭け金)
BETS = {k: (v.label, v.mult) for k, v in _CFG.bets.items()}
_COLOR_EMOJI = dict(_CFG.color_emoji)


def spin(rng: random.Random) -> int:
    return rng.randint(0, 36)


def color_of(n: int) -> str:
    if n == 0:
        return "緑"
    return "赤" if n in RED else "黒"


def color_emoji(n: int) -> str:
    return _COLOR_EMOJI[color_of(n)]


def wins(kind: str, result: int, number: int | None = None) -> bool:
    """その賭け方が当たりか。0 は、数字の賭け以外はすべてハズレ。"""
    if kind == "number":
        return number == result
    if result == 0:
        return False
    return {
        "red": result in RED,
        "black": result not in RED,
        "even": result % 2 == 0,
        "odd": result % 2 == 1,
        "low": result <= 18,
        "high": result >= 19,
        "dozen1": result <= 12,
        "dozen2": 13 <= result <= 24,
        "dozen3": result >= 25,
    }[kind]


def payout(kind: str, bet: int, result: int, number: int | None = None) -> int:
    """受け取る総額（ハズレは 0）。"""
    return bet * BETS[kind][1] if wins(kind, result, number) else 0


def rtp(kind: str) -> float:
    """その賭け方の還元率。"""
    hit = sum(1 for n in range(37) if wins(kind, n, 17))
    return hit / 37 * BETS[kind][1]
