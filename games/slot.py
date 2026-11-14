"""スロット（3リール）。出目の重みと払い戻しの倍率、還元率（RTP）の計算。"""

import itertools
import random

from core import catalogs

_CFG = catalogs.games().slot

# 出目と重み（重いほど出やすい）
WEIGHTS = {s.symbol: s.weight for s in _CFG.symbols}
# 3つ揃いの払い戻し倍率（賭け金に対する総額）
THREE = {s.symbol: s.three_mult for s in _CFG.symbols}
CHERRY_TWO = _CFG.cherry_two  # 🍒が2つ
CHERRY_ONE = _CFG.cherry_one  # 🍒が1つ（少し戻る）

SYMBOLS = list(WEIGHTS)


def spin(rng: random.Random) -> tuple:
    return tuple(rng.choices(SYMBOLS, weights=[WEIGHTS[s] for s in SYMBOLS], k=3))


def multiplier(reels) -> float:
    """払い戻しの倍率（賭け金に対する受け取り総額。0 ならハズレ）。"""
    a, b, c = reels
    if a == b == c:
        return THREE[a]
    cherries = list(reels).count("🍒")
    return {2: CHERRY_TWO, 1: CHERRY_ONE}.get(cherries, 0)


def label(reels) -> str:
    a, b, c = reels
    if a == b == c:
        return "7️⃣ 大当たり！！" if a == "7️⃣" else f"{a} 3つ揃い！"
    cherries = list(reels).count("🍒")
    if cherries == 2:
        return "🍒🍒 チェリー2つ"
    if cherries == 1:
        return "🍒 チェリー（少し戻り）"
    return "ハズレ"


def rtp() -> float:
    """還元率（賭けた額に対して平均でいくら戻るか）を、全出目の組み合わせから正確に計算する。"""
    total = sum(WEIGHTS.values())
    result = 0.0
    for reels in itertools.product(SYMBOLS, repeat=3):
        p = 1.0
        for s in reels:
            p *= WEIGHTS[s] / total
        result += p * multiplier(reels)
    return result
