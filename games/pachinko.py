"""パチンコ（演出つき）。抽選は先に決め、その結果に合わせてコマ送りの演出を作る。

演出は Discord のメッセージを何度も編集して見せる。ここでは「何コマ目に何を表示するか」だけを作る。
"""

import random
from dataclasses import dataclass

REEL_SYMBOLS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "🔥"]
SMALL = ["1️⃣", "2️⃣", "3️⃣"]
MEDIUM = ["4️⃣", "5️⃣", "6️⃣"]

# (名前, 払い戻し倍率, 確率)
OUTCOMES = [
    ("ハズレ", 0, 0.80),
    ("小当たり", 2, 0.12),
    ("当たり", 5, 0.05),
    ("大当たり", 10, 0.025),
    ("激アツ確変", 40, 0.005),
]
REACH_ON_LOSE = 0.30     # ハズレでも、リーチ（あと一歩）を見せる割合


@dataclass(frozen=True)
class Outcome:
    name: str
    multiplier: int


@dataclass(frozen=True)
class Spin:
    outcome: Outcome
    reels: tuple
    reach: bool
    frames: tuple        # 1コマごとの表示（説明文）


def draw(rng: random.Random) -> Outcome:
    names = [(n, m) for n, m, _ in OUTCOMES]
    n, m = rng.choices(names, weights=[p for _, _, p in OUTCOMES], k=1)[0]
    return Outcome(n, m)


def rtp() -> float:
    return sum(m * p for _, m, p in OUTCOMES)


def pick_reels(outcome: Outcome, rng: random.Random) -> tuple:
    """抽選結果に合う出目を決める。当たりは3つ揃い、ハズレはリーチ風か、バラバラ。"""
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
    if rng.random() < REACH_ON_LOSE:
        s = rng.choice(REEL_SYMBOLS)
        other = rng.choice([x for x in REEL_SYMBOLS if x != s])
        return (s, s, other)
    reels = rng.sample(REEL_SYMBOLS, 3)       # 3つとも違う
    return tuple(reels)


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
    """抽選から演出のコマまでを作る。"""
    outcome = draw(rng)
    reels = pick_reels(outcome, rng)
    reach = reels[0] == reels[1]       # 左2つが揃っていれば、最後の1つを引っ張る演出にする
    spinning = lambda: tuple(rng.choice(REEL_SYMBOLS) for _ in range(3))     # noqa: E731
    frames = list(ball_frames(rng))
    frames.append("玉が **START** に入った…！\nリールが回り出す…")
    s = spinning()
    frames.append(reel_line("🌀", "🌀", "🌀"))
    frames.append(reel_line(reels[0], "🌀", "🌀"))
    frames.append(reel_line(reels[0], reels[1], "🌀") + ("\n**リーチ！** ざわ… ざわ…" if reach else ""))
    if reach:
        frames.append(reel_line(reels[0], reels[1], s[2]) + "\nざわ… ざわ… ざわ…")
        frames.append(reel_line(reels[0], reels[1], "🌀") + "\n…ここで決まる…！")
    frames.append(reel_line(*reels))
    return Spin(outcome, reels, reach, tuple(frames))


FLAVOR = {
    "ハズレ": "…沈黙。ペリカが静かに溶けていく…",
    "小当たり": "ッ…！ 拾った…少しだけ…",
    "当たり": "来た…！ 流れは悪くない…",
    "大当たり": "ざわ…ざわ…ざわ…！ 大当たり…！ 見ろ、この出玉…！",
    "激アツ確変": "どよめき…！ 確変突入…！ 地下が揺れる…！！",
}
