"""ルーレット（ヨーロピアン: 0〜36）。賭け方と払い戻し。"""

import random

RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}

# 賭け方 → (表示名, 払い戻し倍率: 当たったときの受け取り総額/賭け金)
BETS = {
    "red": ("赤", 2), "black": ("黒", 2),
    "even": ("偶数", 2), "odd": ("奇数", 2),
    "low": ("1〜18", 2), "high": ("19〜36", 2),
    "dozen1": ("1〜12", 3), "dozen2": ("13〜24", 3), "dozen3": ("25〜36", 3),
    "number": ("ストレート（数字）", 36),
}


def spin(rng: random.Random) -> int:
    return rng.randint(0, 36)


def color_of(n: int) -> str:
    if n == 0:
        return "緑"
    return "赤" if n in RED else "黒"


def color_emoji(n: int) -> str:
    return {"緑": "🟢", "赤": "🔴", "黒": "⚫"}[color_of(n)]


def wins(kind: str, result: int, number: int = None) -> bool:
    """その賭け方が当たりか。0 は、数字の賭け以外はすべてハズレ。"""
    if kind == "number":
        return number == result
    if result == 0:
        return False
    return {
        "red": result in RED, "black": result not in RED,
        "even": result % 2 == 0, "odd": result % 2 == 1,
        "low": result <= 18, "high": result >= 19,
        "dozen1": result <= 12, "dozen2": 13 <= result <= 24, "dozen3": result >= 25,
    }[kind]


def payout(kind: str, bet: int, result: int, number: int = None) -> int:
    """受け取る総額（ハズレは 0）。"""
    return bet * BETS[kind][1] if wins(kind, result, number) else 0


def rtp(kind: str) -> float:
    """その賭け方の還元率。"""
    hit = sum(1 for n in range(37) if wins(kind, n, 17))
    return hit / 37 * BETS[kind][1]
