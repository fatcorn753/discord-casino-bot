"""パチンコ（打ち出し型）。1回の遊びで玉を何十発か打ち、スタートに入った数だけ回転し、当たれば連チャンする。

流れ（1セット）:
  ① 100発を打ち出す → 1発ごとに、約7%がスタートに入る（残りは釘やハズレの穴に落ちる）。入った数だけ回転する。
  ② 回転ごとに、予告の色とリーチの格を抽選し、当たる確率 = 基本 + 予告ぶん + リーチぶん で当落を決める。
  ③ 当たると大当たり。出玉は賭け金の2倍（70%）・10倍（25%）・30倍（5%）のどれかで、確変の連チャンが50%で続く。
  ④ セット全体の払い戻しは、出玉の合計（賭け金の倍数）。上限は CAP_MULT 倍。
還元率は 100% を超えないこと（超えると、連打するだけでペリカが増えてしまう）。数字はすべてこのファイルの表に集めてある。

演出は Discord のメッセージを何度も編集して見せる。ここでは「何コマ目に何を表示するか」だけを作る。
"""

import random
from dataclasses import dataclass
from functools import lru_cache

REEL_SYMBOLS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "🔥"]
SMALL = ["1️⃣", "2️⃣", "3️⃣"]
MEDIUM = ["4️⃣", "5️⃣", "6️⃣"]

NORMAL, SUPER, PREMIUM = "normal", "super", "premium"

BALLS_PER_SET = 100          # 1セットで打ち出す玉の数（賭け金 ÷ この数 が、玉1発の値段）
P_START = 0.07               # 玉1発が、スタートに入る確率
MAX_SETS = 10                # 連続投入できるセット数
CAP_MULT = 200               # 1セットの払い戻しの上限（賭け金の倍数）

# 大当たり1回の出玉（名前, 賭け金の倍数, 確率）と、確変が続く確率
ROUNDS = [("小当たり", 2, 0.695), ("大当たり", 10, 0.265), ("激アツ確変", 30, 0.04)]      # 還元率は約97%（当たりやすさは変えず、出玉の配分で調整）
CONTINUE = 0.50
ROUND_NAME = {m: name for name, m, _ in ROUNDS}

# 演出ごとの（出る確率, 当たる確率に足す分）。足した合計が 1 を超えたら 1（確定）
OMEN_TABLE = {           # 予告の色（リーチの前に出る）。"" は予告なし
    "": (0.75, 0.00),
    "blue": (0.15, 0.002),
    "red": (0.08, 0.01),
    "gold": (0.02, 0.08),
}
TIER_TABLE = {           # リーチの格。"" はリーチなし（当たっても、リーチ演出なしで突然揃う「サプライズ当たり」になる）
    "": (0.85, 0.00),
    NORMAL: (0.10, 0.01),
    SUPER: (0.04, 0.12),
    PREMIUM: (0.01, 0.45),
}
HIT_TARGET = 0.01322         # 1回転あたりの、当たる確率の平均。基本の確率は、これに合うよう下で決める（還元率が100%を超えない値）
OMEN_TEXT = {"blue": "（青）ざわ…", "red": "（赤）ゾワッ…！ 予告だ…！", "gold": "（金）⚡ 激アツ予告 ⚡ これは来る…！"}


def _base_hit() -> float:
    extra = sum(po * b for po, b in OMEN_TABLE.values()) + sum(pt * b for pt, b in TIER_TABLE.values())
    return HIT_TARGET - extra


BASE_HIT = _base_hit()       # 演出に関係なく足される、当たる確率の土台


@dataclass(frozen=True)
class Frame:
    text: str
    mood: str = "normal"      # 表示の色合い（normal / blue / red / gold）


@dataclass(frozen=True)
class SpinInfo:
    """1回転。"""
    omen: str                # 予告の色（予告なしなら ""）
    tier: str                # リーチの格（リーチなしなら ""）
    hit: bool
    rounds: tuple            # 当たったときの、ラウンドごとの出玉（賭け金の倍数）。連チャンの数だけ並ぶ
    hit_prob: float          # この演出での、当たる確率（統計用）

    @property
    def raw(self) -> int:
        return sum(self.rounds)


@dataclass(frozen=True)
class SetResult:
    """1セット（100発）の結果。"""
    starts: int              # スタートに入った玉の数 = 回転数
    spins: tuple             # SpinInfo の並び
    raw: int                 # 出玉の合計（上限をかける前）
    mult: int                # 払い戻しの倍数（上限をかけたあと。0 ならハズレ）

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
    return SetResult(starts, spins, raw, min(raw, CAP_MULT))


# ----------------------------- 設計上の数字（厳密な計算） ----------------------------- #
def mean_hit_prob() -> float:
    return sum(po * pt * hit_probability(o, t) for o, (po, _) in OMEN_TABLE.items() for t, (pt, _) in TIER_TABLE.items())


def hit_rate_by_omen() -> dict:
    return {o: sum(pt * hit_probability(o, t) for t, (pt, _) in TIER_TABLE.items()) for o in OMEN_TABLE}


def hit_rate_by_tier() -> dict:
    return {t: sum(po * hit_probability(o, t) for o, (po, _) in OMEN_TABLE.items()) for t in TIER_TABLE}


def _convolve(a: list, b: list) -> list:
    """合計の分布（0〜CAP_MULT。CAP_MULT の位置には「それ以上」をまとめる）。"""
    out = [0.0] * (CAP_MULT + 1)
    for i, pa in enumerate(a):
        if pa:
            for j, pb in enumerate(b):
                if pb:
                    out[min(i + j, CAP_MULT)] += pa * pb
    return out


@lru_cache(maxsize=None)
def _set_distribution() -> tuple:
    """1セットの払い戻しの倍数の分布 [0..CAP_MULT]（上限は CAP_MULT にまとめる）。"""
    one = [0.0] * (CAP_MULT + 1)
    for _, m, p in ROUNDS:
        one[m] += p
    chain = list(one)                                    # 連チャン1回ぶんの合計の分布: f = (1-c)R + c (R * f)
    for _ in range(CAP_MULT):
        step = _convolve(one, chain)
        new = [(1 - CONTINUE) * one[a] + CONTINUE * step[a] for a in range(CAP_MULT + 1)]
        if max(abs(x - y) for x, y in zip(new, chain)) < 1e-15:
            break
        chain = new
    q = P_START * mean_hit_prob()                        # 玉1発が、大当たりの始まりになる確率
    total = [1.0] + [0.0] * CAP_MULT
    result = [0.0] * (CAP_MULT + 1)
    comb = 1.0                                           # 二項分布 P(M = m) を、漸化式で求める
    for m in range(0, 31):
        p_m = comb * q ** m * (1 - q) ** (BALLS_PER_SET - m) if m <= BALLS_PER_SET else 0.0
        result = [r + p_m * t for r, t in zip(result, total)]
        total = _convolve(total, chain)
        comb = comb * (BALLS_PER_SET - m) / (m + 1) if m < BALLS_PER_SET else 0.0
    return tuple(result)


def rtp() -> float:
    """設計上の還元率（セットの払い戻しの倍数の期待値。上限を含む）。"""
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
    return tuple(rng.sample(REEL_SYMBOLS, 3))       # 3つとも違う


def ball_frames(rng: random.Random, rows: int = 3, width: int = 9, balls: int = 100) -> list:
    """玉がクギの間を落ちていく様子（等幅フォントのコードブロック）。"""
    col = width // 2
    out = []
    for r in range(rows):
        lines = []
        for rr in range(rows):
            cells = [("." if (rr + c) % 2 == 0 else " ") for c in range(width)]
            if rr == r:
                cells[col] = "O"
            lines.append(" ".join(cells))
        out.append("```\n" + "\n".join(lines) + f"\n         ▼ START  （{balls}発 打ち出し）\n```")
        col = max(1, min(width - 2, col + rng.choice([-1, 0, 1])))
    return out


def reel_line(a: str, b: str, c: str) -> str:
    return f"┃ {a} ┃ {b} ┃ {c} ┃"


STEP_TEXT = ["ざわ…", "ゾワッ…！", "⚡ 激アツ ⚡"]            # 予告のステップアップ（1段目→2段目→3段目）
STEP_ICON = ["🔵", "🔴", "🟡"]
STEPS_OF = {"blue": 1, "red": 2, "gold": 3}                # 予告の色ごとの、ステップの数（色が高いほど長い）


def hold_row(icon: str) -> str:
    """保留（次に回る回転の予約）の表示。いま変化している玉を icon にする。"""
    return "保留: ⚪⚪" + icon + "⚪"


CUTIN = {SUPER: ["「チャンスだ！！」", "「いけるぞ！！」", "「ここからが本番だ！」", "「見せてやる！！」"],
         PREMIUM: ["「これで終わりだ！！」", "「全部まとめて持っていけ！！」", "「運命を変えろ！！」", "「勝負だ！！」"]}


def _pseudo(rng, r0, r1, sp, n: int) -> list:
    """擬似連: n 回の回転コマ。回数は当たりとは無関係に選ぶ（回数で結果が分からないように）。最後の回は金色。"""
    tail = {1: "もう1回転する…！", 2: "さらにもう1回…！！"}
    frames = []
    for k in range(1, n + 1):
        label = "💥 **擬似連！** 💥" if n == 1 else f"💥 **擬似連 {k}/{n}** 💥"
        text = tail.get(k, "もう1回…！") if k < n else ("ついに最後の回転…！！" if n > 1 else tail[1])
        frames.append(Frame(label + "\n" + reel_line(*sp()) + "\n" + text, "gold" if n > 1 and k == n else "red"))
    return frames


def _normal_a(rng, r0, r1, sp):
    return [Frame(reel_line(r0, r1, "🌀") + "\n**リーチ！** ざわ… ざわ…", "blue"),
            Frame("🎞️ **ロングリーチ** 🎞️\n" + reel_line(r0, r1, sp()[2]) + "\nゆっくり回っている…", "blue"),
            Frame(reel_line(r0, r1, "🌀") + "\nざわ… ざわ… ざわ…", "blue")]


def _normal_b(rng, r0, r1, sp):
    return [Frame(reel_line(r0, r1, "🌀") + "\n**リーチ！**", "blue"),
            Frame("🌊 **波の予告** 🌊\n" + reel_line(r0, r1, sp()[2]) + "\n画面が揺れている…", "blue"),
            Frame(reel_line(r0, r1, "🌀") + "\nじわ… じわ…", "blue")]


def _normal_c(rng, r0, r1, sp):
    return [Frame(reel_line(r0, r1, "🌀") + "\n**リーチ！**", "blue"),
            Frame("🐟 **魚群が横切った** 🐟\n" + reel_line(r0, r1, sp()[2]) + "\nなにか起きる…？", "blue"),
            Frame(reel_line(r0, r1, "🌀") + "\n…どっちだ…", "blue")]


def _super_a(rng, r0, r1, sp):
    return [Frame(reel_line(r0, r1, "🌀") + "\n**リーチ！**", "blue"),
            Frame("⚡ **スーパーリーチ** ⚡\n" + reel_line(r0, r1, "🌀") + "\n背景が変わった…！", "red"),
            Frame("🗯️ **カットイン！** 🗯️\n" + rng.choice(CUTIN[SUPER]) + "\n" + reel_line(r0, r1, sp()[2]), "red"),
            *_pseudo(rng, r0, r1, sp, rng.choice([1, 2, 3])),
            Frame("⚡⚡ **チャンス！！** ⚡⚡\n" + reel_line(r0, r1, sp()[2]) + "\nざわ… ざわ… ざわ…", "red"),
            Frame("⚡ スローになった… ⚡\n" + reel_line(r0, r1, "🌀") + "\n…ここで決まる…！", "red")]


def _super_b(rng, r0, r1, sp):
    return [Frame(reel_line(r0, r1, "🌀") + "\n**リーチ！**", "blue"),
            Frame("🎬 **ストーリーリーチ** 🎬\n" + reel_line(r0, r1, "🌀") + "\n主人公が立ち上がる…！", "red"),
            Frame("🗯️ **セリフ** 🗯️\n" + rng.choice(CUTIN[SUPER]) + "\n" + reel_line(r0, r1, sp()[2]), "red"),
            *_pseudo(rng, r0, r1, sp, rng.choice([1, 2, 3])),
            Frame("🔥 **逆転のチャンス！** 🔥\n" + reel_line(r0, r1, sp()[2]) + "\n音楽が盛り上がる…！", "red"),
            Frame("…… **溜め** ……\n" + reel_line(r0, r1, "🌀") + "\n…頼む…！", "red")]


def _super_c(rng, r0, r1, sp):
    return [Frame(reel_line(r0, r1, "🌀") + "\n**リーチ！**", "blue"),
            Frame("🚃 **ダッシュリーチ** 🚃\n" + reel_line(r0, r1, sp()[2]) + "\n絵柄が走り出した…！", "red"),
            Frame("🗯️ **カットイン！** 🗯️\n" + rng.choice(CUTIN[SUPER]) + "\n" + reel_line(r0, r1, sp()[2]), "red"),
            *_pseudo(rng, r0, r1, sp, rng.choice([1, 2, 3])),
            Frame("⚡⚡ **加速！！** ⚡⚡\n" + reel_line(r0, r1, sp()[2]) + "\n止まらない…！", "red"),
            Frame("⚡ ブレーキ… ⚡\n" + reel_line(r0, r1, "🌀") + "\n…止まるか…！", "red")]


def _premium_a(rng, r0, r1, sp):
    return [Frame(reel_line(r0, r1, "🌀") + "\n**リーチ！**", "blue"),
            *_pseudo(rng, r0, r1, sp, rng.choice([2, 3, 4])),
            Frame("🔥 **プレミアムリーチ** 🔥\n" + reel_line(r0, r1, "🌀") + "\n画面が燃えている…！！", "red"),
            Frame("🔥🔥 **全回転！！** 🔥🔥\n" + reel_line(*sp()) + "\nどよめき…！", "gold"),
            Frame("🗯️ **プレミアムカットイン** 🗯️\n" + rng.choice(CUTIN[PREMIUM]) + "\n" + reel_line(r0, r1, sp()[2]), "gold"),
            Frame("🔥🔥🔥 **運命の瞬間** 🔥🔥🔥\n" + reel_line(r0, r1, "🌀") + "\n…頼む…！！", "gold")]


def _premium_b(rng, r0, r1, sp):
    return [Frame(reel_line(r0, r1, "🌀") + "\n**リーチ！**", "blue"),
            Frame("🌌 **空間が歪む** 🌌\n" + reel_line(*sp()) + "\n何かが来る…！", "red"),
            Frame("⚡ **ボタン連打チャンス** ⚡\n" + reel_line(r0, r1, sp()[2]) + "\n連打連打連打…！", "red"),
            Frame("🐉 **龍が現れた！！** 🐉\n" + reel_line(*sp()) + "\n地鳴りがする…！！", "gold"),
            Frame("🔥 **プレミアムリーチ** 🔥\n" + reel_line(r0, r1, "🌀") + "\n画面が燃えている…！！", "red"),
            Frame("🔥🔥 **全回転！！** 🔥🔥\n" + reel_line(*sp()) + "\nすべての絵柄が光る…！", "gold"),
            Frame("🗯️ **プレミアムカットイン** 🗯️\n" + rng.choice(CUTIN[PREMIUM]) + "\n" + reel_line(r0, r1, sp()[2]), "gold"),
            Frame("🔥🔥🔥 **運命の瞬間** 🔥🔥🔥\n" + reel_line(r0, r1, "🌀") + "\n…頼む…！！", "gold")]


def _premium_c(rng, r0, r1, sp):
    return [Frame(reel_line(r0, r1, "🌀") + "\n**リーチ！**", "blue"),
            Frame("🌠 **流星群** 🌠\n" + reel_line(r0, r1, sp()[2]) + "\n夜空が光る…！", "red"),
            *_pseudo(rng, r0, r1, sp, rng.choice([2, 3, 4])),
            Frame("🔥 **プレミアムリーチ** 🔥\n" + reel_line(r0, r1, "🌀") + "\n金色に輝いている…！！", "gold"),
            Frame("👑 **覚醒！！** 👑\n" + reel_line(*sp()) + "\n全リールが金色に…！", "gold"),
            Frame("🗯️ **プレミアムカットイン** 🗯️\n" + rng.choice(CUTIN[PREMIUM]) + "\n" + reel_line(r0, r1, sp()[2]), "gold"),
            Frame("🔥🔥🔥 **運命の瞬間** 🔥🔥🔥\n" + reel_line(r0, r1, "🌀") + "\n…頼む…！！", "gold")]


REACH_PATTERNS = {NORMAL: [_normal_a, _normal_b, _normal_c], SUPER: [_super_a, _super_b, _super_c], PREMIUM: [_premium_a, _premium_b, _premium_c]}


def spin_frames(rng: random.Random, spin: SpinInfo, label: str = "") -> list:
    """1回転の演出のコマ。保留変化・予告（ステップアップ）→ 回転 → リーチ（格つき・豪華な演出）→ 出目の順。

    label は先頭に付ける見出し（例: 「3回転目」）。当たりの宣言は、この演出が終わったあとに、呼び出し側が出す。
    """
    first = spin.rounds[0] if spin.rounds else 0
    reels = pick_reels(first, spin.tier, rng)
    reach = bool(spin.tier)
    omen, tier = spin.omen, spin.tier
    head = f"**{label}**\n" if label else ""
    r0, r1 = reels[0], reels[1]
    spinning = lambda: tuple(rng.choice(REEL_SYMBOLS) for _ in range(3))     # noqa: E731
    frames = []
    for k in range(STEPS_OF.get(omen, 0)):                   # ① 予告のステップアップ: 色が高いほど、段が多い
        last = k + 1 == STEPS_OF[omen]
        mood = ("blue", "red", "gold")[k]
        text = f"**【予告】** {OMEN_TEXT[omen]}" if last else f"**【予告】** {STEP_TEXT[k]}"
        frames.append(Frame((head if k == 0 else "") + hold_row(STEP_ICON[k]) + "\n" + text, mood))
    frames.append(Frame((head if not omen else "") + reel_line("🌀", "🌀", "🌀") + "\n回転中…"))      # ② 回転
    frames.append(Frame(reel_line(r0, "🌀", "🌀")))
    if not reach:                                              # ③ リーチなし: 突然揃うサプライズ
        frames += [Frame(reel_line(r0, r1, "🌀") + "\n…！？ **まさか…**", omen or "normal"),
                   Frame("💫 **フリーズ！！** 💫\n" + reel_line(r0, r1, "🌀") + "\n画面が固まった…！！", "gold")]
    else:
        frames += rng.choice(REACH_PATTERNS[tier])(rng, r0, r1, spinning)
    frames.append(Frame(head + reel_line(*reels), "normal"))   # ④ 出目
    return frames


def hit_spins(spins) -> list:
    """当たった回転だけを、(何回転目か, 回転) の並びで返す。演出は、ここだけを見せる。"""
    return [(i, s) for i, s in enumerate(spins, start=1) if s.hit]


FLAVOR = {
    "ハズレ": "…沈黙。ペリカが静かに溶けていく…",
    "小当たり": "ッ…！ 拾った…少しだけ…",
    "大当たり": "ざわ…ざわ…ざわ…！ 大当たり…！ 見ろ、この出玉…！",
    "激アツ確変": "どよめき…！ 確変突入…！ 地下が揺れる…！！",
}
