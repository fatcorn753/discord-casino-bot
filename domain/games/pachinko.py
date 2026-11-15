"""パチンコ（打ち出し型）。1回の遊びで玉を何十発か打ち、スタートに入った数だけ回転し、当たれば連チャンする。

流れ（1セット）:
  ① 100発を打ち出す → 1発ごとに、約7%がスタートに入る（残りは釘やハズレの穴に落ちる）。入った数だけ回転する。
  ② 回転ごとに、予告の色とリーチの格を抽選し、当たる確率 = 基本 + 予告ぶん + リーチぶん で当落を決める。
  ③ 当たると大当たり。出玉は ROUNDS の表のどれか（2倍・10倍・30倍。割合は表のとおり）で、確変の連チャンが CONTINUE の確率で続く。
  ④ セット全体の払い戻しは、出玉の合計（賭け金の倍数）。上限はない。
還元率は 100% を超えないこと（超えると、連打するだけでペリカが増えてしまう）。いまは約97%。数字はすべてこのファイルの表に集めてある。

演出は Discord のメッセージを何度も編集して見せる。ここでは「何コマ目に何を表示するか」だけを作る。
"""

import random
from dataclasses import dataclass
from functools import lru_cache

from domain.ports import get_catalogs

_CFG = get_catalogs().games().pachinko

REEL_SYMBOLS = list(_CFG.reel_symbols)
SMALL = list(_CFG.small)
MEDIUM = list(_CFG.medium)

NORMAL, SUPER, PREMIUM = "normal", "super", "premium"

BALLS_PER_SET = _CFG.balls_per_set  # 1セットで打ち出す玉の数（賭け金 ÷ この数 が、玉1発の値段）
P_START = _CFG.p_start  # 玉1発が、スタートに入る確率
MAX_SETS = _CFG.max_sets  # 連続投入できるセット数
ANALYSIS_MAX = _CFG.analysis_max  # 還元率などの計算で、これ以上の出玉は「それ以上」にまとめる（遊びには上限がない。確率は 1e-9 以下で、計算への影響は無視できる）

# 大当たり1回の出玉（名前, 賭け金の倍数, 確率）と、確変が続く確率
ROUNDS = [(r.name, r.mult, r.prob) for r in _CFG.rounds]  # 還元率は約97%（当たりやすさは変えず、出玉の配分で調整）
CONTINUE = _CFG.continue_prob
ROUND_NAME = {m: name for name, m, _ in ROUNDS}

# 演出ごとの（出る確率, 当たる確率に足す分）。足した合計が 1 を超えたら 1（確定）
OMEN_TABLE = {k: (v.prob, v.bonus) for k, v in _CFG.omen_table.items()}
TIER_TABLE = {k: (v.prob, v.bonus) for k, v in _CFG.tier_table.items()}
HIT_TARGET = (
    _CFG.hit_target  # 1回転あたりの、当たる確率の平均。基本の確率は、これに合うよう下で決める（還元率が100%を超えない値）
)
REVIVE_CHANCE = dict(_CFG.revive_chance)
PSEUDO_COUNTS = {k: list(v) for k, v in _CFG.pseudo_counts.items()}


def _base_hit() -> float:
    extra = sum(po * b for po, b in OMEN_TABLE.values()) + sum(pt * b for pt, b in TIER_TABLE.values())
    return HIT_TARGET - extra


BASE_HIT = _base_hit()  # 演出に関係なく足される、当たる確率の土台


@dataclass(frozen=True)
class SpinInfo:
    """1回転。"""

    omen: str  # 予告の色（予告なしなら ""）
    tier: str  # リーチの格（リーチなしなら ""）
    hit: bool
    rounds: tuple  # 当たったときの、ラウンドごとの出玉（賭け金の倍数）。連チャンの数だけ並ぶ
    hit_prob: float  # この演出での、当たる確率（統計用）

    @property
    def raw(self) -> int:
        return sum(self.rounds)


@dataclass(frozen=True)
class SetResult:
    """1セット（100発）の結果。"""

    starts: int  # スタートに入った玉の数 = 回転数
    spins: tuple  # SpinInfo の並び
    raw: int  # 出玉の合計 = 払い戻しの倍数（0 ならハズレ）

    @property
    def mult(self) -> int:
        """払い戻しの倍数。出玉の合計と同じ（上限はない）。"""
        return self.raw

    @property
    def hits(self) -> int:
        return sum(1 for s in self.spins if s.hit)

    @property
    def chains(self) -> int:
        """連チャンの数（大当たりのラウンドの数）。"""
        return sum(len(s.rounds) for s in self.spins)

    @property
    def max_chain(self) -> int:
        return max((len(s.rounds) for s in self.spins), default=0)


def hit_probability(omen: str, tier: str) -> float:
    return min(1.0, BASE_HIT + OMEN_TABLE[omen][1] + TIER_TABLE[tier][1])


def _weighted(weights: dict, rng: random.Random):
    return rng.choices(list(weights), weights=list(weights.values()), k=1)[0]


def roll_rounds(rng: random.Random) -> tuple:
    """大当たりから始まる連チャンの、ラウンドごとの出玉（賭け金の倍数）。"""
    rounds = []
    while True:
        rounds.append(rng.choices([m for _, m, _ in ROUNDS], weights=[p for _, _, p in ROUNDS], k=1)[0])
        if rng.random() >= CONTINUE:
            return tuple(rounds)


def roll_spin(rng: random.Random) -> SpinInfo:
    """演出を先に抽選し、その演出での当落と連チャンを決める。"""
    omen = _weighted({k: p for k, (p, _) in OMEN_TABLE.items()}, rng)
    tier = _weighted({k: p for k, (p, _) in TIER_TABLE.items()}, rng)
    prob = hit_probability(omen, tier)
    hit = rng.random() < prob
    return SpinInfo(omen, tier, hit, roll_rounds(rng) if hit else (), prob)


def roll_set(rng: random.Random) -> SetResult:
    """100発を打ち出し、スタートに入った数だけ回転して、セット全体の払い戻しを決める。"""
    starts = sum(1 for _ in range(BALLS_PER_SET) if rng.random() < P_START)
    spins = tuple(roll_spin(rng) for _ in range(starts))
    raw = sum(s.raw for s in spins)
    return SetResult(starts, spins, raw)


# ----------------------------- 設計上の数字（厳密な計算） ----------------------------- #
def mean_hit_prob() -> float:
    return sum(
        po * pt * hit_probability(o, t) for o, (po, _) in OMEN_TABLE.items() for t, (pt, _) in TIER_TABLE.items()
    )


def hit_rate_by_omen() -> dict:
    return {o: sum(pt * hit_probability(o, t) for t, (pt, _) in TIER_TABLE.items()) for o in OMEN_TABLE}


def hit_rate_by_tier() -> dict:
    return {t: sum(po * hit_probability(o, t) for o, (po, _) in OMEN_TABLE.items()) for t in TIER_TABLE}


def _convolve(a: list, b: list) -> list:
    """合計の分布（0〜ANALYSIS_MAX。ANALYSIS_MAX の位置には「それ以上」をまとめる）。"""
    out = [0.0] * (ANALYSIS_MAX + 1)
    for i, pa in enumerate(a):
        if pa:
            for j, pb in enumerate(b):
                if pb:
                    out[min(i + j, ANALYSIS_MAX)] += pa * pb
    return out


@lru_cache(maxsize=None)
def _set_distribution() -> tuple:
    """1セットの払い戻しの倍数の分布 [0..ANALYSIS_MAX]（それ以上は ANALYSIS_MAX にまとめる。計算のためだけで、遊びに上限はない）。"""
    one = [0.0] * (ANALYSIS_MAX + 1)
    for _, m, p in ROUNDS:
        one[m] += p
    chain = list(one)  # 連チャン1回ぶんの合計の分布: f = (1-c)R + c (R * f)
    for _ in range(ANALYSIS_MAX):
        step = _convolve(one, chain)
        new = [(1 - CONTINUE) * one[a] + CONTINUE * step[a] for a in range(ANALYSIS_MAX + 1)]
        if max(abs(x - y) for x, y in zip(new, chain)) < 1e-15:
            break
        chain = new
    q = P_START * mean_hit_prob()  # 玉1発が、大当たりの始まりになる確率
    total = [1.0] + [0.0] * ANALYSIS_MAX
    result = [0.0] * (ANALYSIS_MAX + 1)
    comb = 1.0  # 二項分布 P(M = m) を、漸化式で求める
    for m in range(0, 31):
        p_m = comb * q**m * (1 - q) ** (BALLS_PER_SET - m) if m <= BALLS_PER_SET else 0.0
        result = [r + p_m * t for r, t in zip(result, total)]
        total = _convolve(total, chain)
        comb = comb * (BALLS_PER_SET - m) / (m + 1) if m < BALLS_PER_SET else 0.0
    return tuple(result)


def rtp() -> float:
    """設計上の還元率（セットの払い戻しの倍数の期待値）。"""
    return sum(a * p for a, p in enumerate(_set_distribution()))


def hit_rate() -> float:
    """1セットで、当たる（払い戻しが0でない）確率。"""
    return 1.0 - _set_distribution()[0]


def pick_reels(round_mult: int, tier: str, rng: random.Random) -> tuple:
    """回転の出目を決める。当たりは3つ揃い（出玉の大きさで絵柄が違う）。ハズレは、リーチなら「あと一歩」、そうでなければバラバラ。"""
    if round_mult == 2:
        s = rng.choice(SMALL)
        return (s, s, s)
    if round_mult == 10:
        return ("7️⃣",) * 3
    if round_mult >= 30:
        return ("🔥",) * 3
    if tier:
        s = rng.choice(REEL_SYMBOLS)
        other = rng.choice([x for x in REEL_SYMBOLS if x != s])
        return (s, s, other)
    return tuple(rng.sample(REEL_SYMBOLS, 3))  # 3つとも違う


def hit_spins(spins) -> list:
    """当たった回転だけを、(何回転目か, 回転) の並びで返す。演出は、ここだけを見せる。"""
    return [(i, s) for i, s in enumerate(spins, start=1) if s.hit]
