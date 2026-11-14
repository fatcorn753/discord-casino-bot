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
OMEN_TEXT = dict(_CFG.omen_text)


def _base_hit() -> float:
    extra = sum(po * b for po, b in OMEN_TABLE.values()) + sum(pt * b for pt, b in TIER_TABLE.values())
    return HIT_TARGET - extra


BASE_HIT = _base_hit()  # 演出に関係なく足される、当たる確率の土台


@dataclass(frozen=True)
class Frame:
    text: str
    mood: str = "normal"  # 表示の色合い（normal / blue / red / gold）


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


STEP_TEXT = list(_CFG.step_text)  # 予告のステップアップ（1段目→2段目→3段目）
STEP_ICON = list(_CFG.step_icon)
STEPS_OF = dict(_CFG.steps_of)  # 予告の色ごとの、ステップの数（色が高いほど長い）
SPINNING = _CFG.spinning  # 回っているリール


def hold_row(icon: str) -> str:
    """保留（次に回る回転の予約）の表示。いま変化している玉を icon にする。"""
    return "保留: ⚪⚪" + icon + "⚪"


# ----------------------------- 演出の台本 ----------------------------- #
# 演出は「台本」を1本選んで流す。リールは「回っている／止まっている」の状態を持ち、次のきまりを守る。
#   ① 左右が同じ絵柄で止まったら「リーチ」。そのあと出目が決まるまで、左右の絵柄は変えない（真ん中は回り続ける）
#   ② 3つ揃って止まるのは、最後の1コマだけ。揃ったら演出は終わり、すぐ当たりの宣言になる
#   ③ 途中のコマの絵柄は乱数で出さない（たまたま揃って見えるのを防ぐ）。例外は、擬似連の「止まったように見えるコマ」で、
#      3つとも違う絵柄にする（リーチにも揃いにも見えない）
#   ④ 左右が戻るのは、再回転する演出（擬似連・全回転）のときだけ
CUTIN = {k: list(v) for k, v in _CFG.cutin.items()}
# 発展の演出の種類。1回の当たりで、この中から1つだけ選ぶ（毎回、全部が流れるわけではない）
STORIES = {k: [(s.title, s.sub) for s in v] for k, v in _CFG.stories.items()}
# 復活演出: いったん「外れた」ように見せて、そこから当たりに変わる（当たったときだけの演出。当落には関係しない）。格が高いほど出やすい
REVIVE_CHANCE = dict(_CFG.revive_chance)
REVIVE_TEXT = [(r.line, r.sub) for r in _CFG.revive_text]
PSEUDO_COUNTS = {
    k: list(v) for k, v in _CFG.pseudo_counts.items()
}  # 擬似連の回数の候補（格ごと。当たり外れとは無関係に選ぶ）


def _text_frame(head: str, reels: tuple, text: str = "", mood: str = "normal") -> Frame:
    return Frame(head + reel_line(*reels) + (f"\n{text}" if text else ""), mood)


def _pseudo_cycles(rng: random.Random, count: int) -> list:
    """擬似連: 止まったように見えて、揺れて、再び回り出す。count 回ぶん。止まったように見えるコマは、3つとも違う絵柄にする。"""
    frames = []
    for k in range(1, count + 1):
        label = "**擬似連！**" if count == 1 else f"**擬似連 {k}/{count}**"
        mood = "gold" if count > 1 and k == count else "red"
        frames.append(
            _text_frame("", tuple(rng.sample(REEL_SYMBOLS, 3)), f"…止まった…？ いや、揺れている…！ {label}", mood)
        )
        frames.append(_text_frame("", (SPINNING,) * 3, "再回転！", mood))
    return frames


def _revive_frames(rng: random.Random, sym: str, full: bool = False) -> list:
    """復活演出のコマ: 左右が揃ったまま、3つ目がずれて止まる（外れ）→ セリフ → 再回転（full なら全回転と名乗る）→ （呼び出し側が）出目。"""
    other = rng.choice([x for x in REEL_SYMBOLS if x != sym])
    line, sub = rng.choice(REVIVE_TEXT)
    return [
        _text_frame("", (sym, sym, other), "…**外れた**…", "blue"),
        _text_frame("", (sym, sym, other), f"**{line}**", "red"),
        _text_frame(
            "🔥🔥 **復活！全回転！！** 🔥🔥\n" if full else "💥 **復活！！** 💥\n", (SPINNING,) * 3, sub, "gold"
        ),
    ]


def spin_frames(rng: random.Random, spin: SpinInfo, label: str = "") -> list:
    """当たった1回転の演出のコマ。予告 → 回転 →（擬似連）→ 左右が止まってリーチ → 発展 → 出目（3つ揃い）の順。

    label は先頭に付ける見出し（例: 「3回転目」）。当たりの宣言は、この演出が終わったあとに、呼び出し側が出す。
    台本は格（リーチなし・ノーマル・スーパー・プレミアム）ごとに違い、擬似連・カットイン・発展の種類は、回ごとに選ぶ。
    """
    first = spin.rounds[0] if spin.rounds else 0
    reels = pick_reels(first, spin.tier, rng)
    sym = reels[0]
    omen, tier = spin.omen, spin.tier
    head = f"**{label}**\n" if label else ""
    frames = []
    for k in range(STEPS_OF.get(omen, 0)):  # ① 予告のステップアップ: 色が高いほど、段が多い
        mood = ("blue", "red", "gold")[k]
        text = f"**【予告】** {OMEN_TEXT[omen]}" if k + 1 == STEPS_OF[omen] else f"**【予告】** {STEP_TEXT[k]}"
        frames.append(Frame((head if k == 0 else "") + hold_row(STEP_ICON[k]) + "\n" + text, mood))
    frames.append(_text_frame(head if not omen else "", (SPINNING,) * 3, "回転中…"))  # ② 回転
    if not tier:  # リーチなしで、突然3つ揃う「サプライズ当たり」。揃う直前まで、3つとも回っている
        frames.append(_text_frame("", (SPINNING,) * 3, "…！？ **まさか…**", omen or "normal"))
        frames.append(_text_frame("💫 **フリーズ！！** 💫\n", (SPINNING,) * 3, "画面が固まった…！！", "gold"))
        frames.append(_text_frame(head, reels))
        return frames
    mood = {NORMAL: "blue", SUPER: "red", PREMIUM: "gold"}[tier]
    pseudo = rng.choice(PSEUDO_COUNTS.get(tier, [0]))
    frames += _pseudo_cycles(rng, pseudo)  # ③ 擬似連（あれば）。そのあと、あらためて左右が止まる
    frames.append(_text_frame("", (sym, SPINNING, SPINNING)))
    frames.append(
        _text_frame("", (sym, sym, SPINNING), "**リーチ！**", "blue")
    )  # ④ リーチ。ここから出目が決まるまで、左右は変えない
    revive = rng.random() < REVIVE_CHANCE[tier]
    title, sub = rng.choice(STORIES[tier])
    if tier == NORMAL:
        frames.append(_text_frame(f"{title}\n", (sym, sym, SPINNING), sub, mood))
        if rng.random() < 0.5:
            frames.append(_text_frame("", (sym, sym, SPINNING), rng.choice(list(_CFG.normal_whispers)), mood))
        if revive:
            frames += _revive_frames(rng, sym)
    else:
        frames.append(_text_frame(f"{title}\n", (sym, sym, SPINNING), sub, "red"))  # 発展
        if rng.random() < 0.7:  # カットインは、ある回とない回がある
            frames.append(
                _text_frame("🗯️ **カットイン！** 🗯️\n" + rng.choice(CUTIN[tier]) + "\n", (sym, sym, SPINNING), "", mood)
            )
        if revive:  # スローのあと、いったん外れて、復活する
            frames.append(_text_frame("⚡ スローになった… ⚡\n", (sym, sym, SPINNING), "…ここで決まる…！", mood))
            frames += _revive_frames(rng, sym, tier == PREMIUM)
        elif tier == PREMIUM:
            frames.append(
                _text_frame("🔥🔥 **全回転！！** 🔥🔥\n", (SPINNING,) * 3, "すべての絵柄が光る…！！", "gold")
            )  # 全回転のあとは、すぐ出目
        else:
            frames.append(_text_frame("⚡ スローになった… ⚡\n", (sym, sym, SPINNING), "…ここで決まる…！", mood))
    frames.append(_text_frame(head, reels))  # ⑤ 出目（3つ揃い）。揃ったら、演出はここで終わる
    return frames


def hit_spins(spins) -> list:
    """当たった回転だけを、(何回転目か, 回転) の並びで返す。演出は、ここだけを見せる。"""
    return [(i, s) for i, s in enumerate(spins, start=1) if s.hit]


FLAVOR = dict(_CFG.flavor)
