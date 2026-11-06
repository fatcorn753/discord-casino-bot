"""パチンコ（演出つき）。演出（予告の色・リーチの格）を先に抽選し、その演出で当たる確率を足して、当たりかどうかを決める。

流れ: ①予告の色を抽選 → ②リーチの格を抽選 → ③当たる確率 = 基本 + 予告ぶん + リーチぶん で当落を決める
      → ④当たりなら、リーチの格に応じた配当（2/5/20/100倍）を抽選。
見た目と結果のつながりが分かりやすく、数字はすべてこのファイルの表に集めてある（調整はここだけ）。
還元率は 100% を超えないこと（超えると、連打するだけでペリカが増えてしまう）。

演出は Discord のメッセージを何度も編集して見せる。ここでは「何コマ目に何を表示するか」だけを作る。
"""

import random
from dataclasses import dataclass

REEL_SYMBOLS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "🔥"]
SMALL = ["1️⃣", "2️⃣", "3️⃣"]
MEDIUM = ["4️⃣", "5️⃣", "6️⃣"]

NORMAL, SUPER, PREMIUM = "normal", "super", "premium"

BASE_HIT = 0.03          # 演出に関係なく足される、当たる確率の土台
# 演出ごとの（出る確率, 当たる確率に足す分）。足した合計が 1 を超えたら 1（確定）
OMEN_TABLE = {           # 予告の色（リーチの前に出る）。"" は予告なし
    "": (0.45, 0.00),
    "blue": (0.30, 0.05),
    "red": (0.18, 0.15),
    "gold": (0.07, 0.40),
}
TIER_TABLE = {           # リーチの格。"" はリーチなし（当たっても、リーチ演出なしで突然揃う「サプライズ当たり」になる）
    "": (0.45, 0.00),
    NORMAL: (0.38, 0.12),
    SUPER: (0.13, 0.35),
    PREMIUM: (0.04, 0.60),
}
# 当たりのときの配当（名前, 倍率）と、リーチの格ごとの出やすさ（格が高いほど大きい配当が出やすい）
PRIZES = [("小当たり", 2), ("当たり", 5), ("大当たり", 20), ("激アツ確変", 100)]
PRIZE_WEIGHTS = {
    "": [0.88, 0.10, 0.018, 0.002],
    NORMAL: [0.771, 0.199, 0.027, 0.003],
    SUPER: [0.66, 0.27, 0.06, 0.01],
    PREMIUM: [0.45, 0.33, 0.17, 0.05],
}
OMEN_TEXT = {"blue": "（青）ざわ…", "red": "（赤）ゾワッ…！ 予告だ…！", "gold": "（金）⚡ 激アツ予告 ⚡ これは来る…！"}


@dataclass(frozen=True)
class Outcome:
    name: str
    multiplier: int


MISS = Outcome("ハズレ", 0)


@dataclass(frozen=True)
class Frame:
    text: str
    mood: str = "normal"      # 表示の色合い（normal / blue / red / gold）


@dataclass(frozen=True)
class Spin:
    outcome: Outcome
    reels: tuple
    reach: bool
    tier: str            # リーチの格（リーチなしなら ""）
    omen: str            # 予告の色（予告なしなら ""）
    hit_prob: float      # この演出での、当たる確率（統計用）
    frames: tuple        # 1コマごとの表示（Frame）


def hit_probability(omen: str, tier: str) -> float:
    return min(1.0, BASE_HIT + OMEN_TABLE[omen][1] + TIER_TABLE[tier][1])


def _weighted(weights: dict, rng: random.Random):
    return rng.choices(list(weights), weights=list(weights.values()), k=1)[0]


def roll(rng: random.Random) -> tuple:
    """演出を先に抽選し、その演出での当落と配当を決める。(結果, 予告の色, リーチの格) を返す。"""
    omen = _weighted({k: p for k, (p, _) in OMEN_TABLE.items()}, rng)
    tier = _weighted({k: p for k, (p, _) in TIER_TABLE.items()}, rng)
    if rng.random() >= hit_probability(omen, tier):
        return MISS, omen, tier
    name, mult = rng.choices(PRIZES, weights=PRIZE_WEIGHTS[tier], k=1)[0]
    return Outcome(name, mult), omen, tier


def draw(rng: random.Random) -> Outcome:
    return roll(rng)[0]


def rtp() -> float:
    """設計上の還元率（演出の組み合わせすべてについて、期待される払い戻しを足したもの）。"""
    total = 0.0
    for omen, (po, _) in OMEN_TABLE.items():
        for tier, (pt, _) in TIER_TABLE.items():
            expected = sum(m * w for (_, m), w in zip(PRIZES, PRIZE_WEIGHTS[tier]))
            total += po * pt * hit_probability(omen, tier) * expected
    return total


def hit_rate() -> float:
    return sum(po * pt * hit_probability(o, t) for o, (po, _) in OMEN_TABLE.items() for t, (pt, _) in TIER_TABLE.items())


def hit_rate_by_omen() -> dict:
    return {o: sum(pt * hit_probability(o, t) for t, (pt, _) in TIER_TABLE.items()) for o in OMEN_TABLE}


def hit_rate_by_tier() -> dict:
    return {t: sum(po * hit_probability(o, t) for o, (po, _) in OMEN_TABLE.items()) for t in TIER_TABLE}


def pick_reels(outcome: Outcome, tier: str, rng: random.Random) -> tuple:
    """結果に合う出目を決める。当たりは3つ揃い。ハズレは、リーチのときは「あと一歩」、そうでなければバラバラ。"""
    if outcome.name == "小当たり":
        s = rng.choice(SMALL)
        return (s, s, s)
    if outcome.name == "当たり":
        s = rng.choice(MEDIUM)
        return (s, s, s)
    if outcome.name == "大当たり":
        return ("7️⃣",) * 3
    if outcome.name == "激アツ確変":
        return ("🔥",) * 3
    if tier:
        s = rng.choice(REEL_SYMBOLS)
        other = rng.choice([x for x in REEL_SYMBOLS if x != s])
        return (s, s, other)
    return tuple(rng.sample(REEL_SYMBOLS, 3))       # 3つとも違う


def ball_frames(rng: random.Random, rows: int = 3, width: int = 9) -> list:
    """玉がクギの間を落ちていく様子（等幅フォントのコードブロック）。"""
    pegs = ["  ".join("." if (r + c) % 2 == 0 else " " for c in range(width)) for r in range(rows)]
    col = width // 2
    out = []
    for r in range(rows):
        lines = []
        for rr in range(rows):
            cells = [("." if (rr + c) % 2 == 0 else " ") for c in range(width)]
            if rr == r:
                cells[col] = "O"
            lines.append(" ".join(cells))
        out.append("```\n" + "\n".join(lines) + "\n         ▼ START\n```")
        col = max(1, min(width - 2, col + rng.choice([-1, 0, 1])))
    del pegs
    return out


def reel_line(a: str, b: str, c: str) -> str:
    return f"┃ {a} ┃ {b} ┃ {c} ┃"


def build_spin(rng: random.Random) -> Spin:
    """演出の抽選から、結果、演出のコマまでを作る。予告→リーチ（格つき）→結果の順。"""
    outcome, omen, tier = roll(rng)
    reels = pick_reels(outcome, tier, rng)
    reach = bool(tier)
    spinning = lambda: tuple(rng.choice(REEL_SYMBOLS) for _ in range(3))     # noqa: E731
    frames = [Frame(f, "normal") for f in ball_frames(rng)]
    frames.append(Frame("玉が **START** に入った…！\nリールが回り出す…"))
    if omen:
        frames.append(Frame("**【予告】** " + OMEN_TEXT[omen], omen))
    frames.append(Frame(reel_line("🌀", "🌀", "🌀")))
    frames.append(Frame(reel_line(reels[0], "🌀", "🌀")))
    if not reach:
        frames.append(Frame(reel_line(reels[0], reels[1], "🌀") + ("\n…！？" if outcome.multiplier else ""), omen or "normal"))
    elif tier == NORMAL:
        frames += [Frame(reel_line(reels[0], reels[1], "🌀") + "\n**リーチ！** ざわ… ざわ…", "blue"),
                   Frame(reel_line(reels[0], reels[1], spinning()[2]) + "\nざわ… ざわ… ざわ…", "blue")]
    elif tier == SUPER:
        frames += [Frame(reel_line(reels[0], reels[1], "🌀") + "\n**リーチ！**", "blue"),
                   Frame("⚡ **スーパーリーチ** ⚡\n" + reel_line(reels[0], reels[1], "🌀") + "\n背景が変わった…！", "red"),
                   Frame("⚡⚡ **チャンス！！** ⚡⚡\n" + reel_line(reels[0], reels[1], spinning()[2]) + "\nざわ… ざわ… ざわ…", "red"),
                   Frame("⚡ スローになった… ⚡\n" + reel_line(reels[0], reels[1], "🌀") + "\n…ここで決まる…！", "red")]
    else:
        frames += [Frame(reel_line(reels[0], reels[1], "🌀") + "\n**リーチ！**", "blue"),
                   Frame("🔥 **プレミアムリーチ** 🔥\n" + reel_line(reels[0], reels[1], "🌀") + "\n画面が燃えている…！！", "red"),
                   Frame("🔥🔥 **全回転！！** 🔥🔥\n" + reel_line(*spinning()) + "\nどよめき…！", "gold"),
                   Frame("💥 **擬似連！！** 💥\n" + reel_line(reels[0], reels[1], spinning()[2]) + "\nもう1回…！ もう1回転する…！", "gold"),
                   Frame("🔥🔥🔥 **運命の瞬間** 🔥🔥🔥\n" + reel_line(reels[0], reels[1], "🌀") + "\n…頼む…！！", "gold")]
    frames.append(Frame(reel_line(*reels), {"gold": "gold", "red": "red"}.get(omen, "normal") if outcome.multiplier else "normal"))
    return Spin(outcome, reels, reach, tier, omen, hit_probability(omen, tier), tuple(frames))


FLAVOR = {
    "ハズレ": "…沈黙。ペリカが静かに溶けていく…",
    "小当たり": "ッ…！ 拾った…少しだけ…",
    "当たり": "来た…！ 流れは悪くない…",
    "大当たり": "ざわ…ざわ…ざわ…！ 大当たり…！ 見ろ、この出玉…！",
    "激アツ確変": "どよめき…！ 確変突入…！ 地下が揺れる…！！",
}
