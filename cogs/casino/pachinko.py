"""パチンコ（地下パチンコ風・打ち出し型）。100発を打ち出し、スタートに入った数だけ回転し、当たれば連チャンする。

賭けた時点で結果をすべて決めて先に精算し、演出はあとから見せる（途中でメッセージが消されても結果は変わらない）。
`回数` を指定すると、同じ賭け金で何セットも連続で打つ（合計が上限以内）。演出は短くまとめ、結果は1つの要約で返す。
"""

import asyncio
import logging
import random
from dataclasses import dataclass

import discord

from cogs.casino.common import (
    CasinoDeps,
    ReplayView,
    game_title,
    log_display_failure,
    result_lines,
    signed,
    start_game,
)
from cogs.casino.texts import (
    PACHINKO_BANNER as BANNER,
)
from cogs.casino.texts import (
    PACHINKO_CHAIN_WAIT as CHAIN_WAIT,
)
from cogs.casino.texts import (
    PACHINKO_CUTIN as CUTIN,
)
from cogs.casino.texts import (
    PACHINKO_EXTRA_SETS as EXTRA_SETS,
)
from cogs.casino.texts import (
    PACHINKO_FLAVOR as FLAVOR,
)
from cogs.casino.texts import (
    PACHINKO_FRAME_BUDGET as FRAME_BUDGET,
)
from cogs.casino.texts import (
    PACHINKO_GRADES as GRADES,
)
from cogs.casino.texts import (
    PACHINKO_MULTI_SHOWN_MISSES as MULTI_SHOWN_MISSES,
)
from cogs.casino.texts import (
    PACHINKO_NORMAL_WHISPERS as NORMAL_WHISPERS,
)
from cogs.casino.texts import (
    PACHINKO_OMEN_TEXT as OMEN_TEXT,
)
from cogs.casino.texts import (
    PACHINKO_RANKS as RANKS,
)
from cogs.casino.texts import (
    PACHINKO_REVIVE_TEXT as REVIVE_TEXT,
)
from cogs.casino.texts import (
    PACHINKO_SHOWN_MISSES as SHOWN_MISSES,
)
from cogs.casino.texts import (
    PACHINKO_SHOWN_ROUNDS as SHOWN_ROUNDS,
)
from cogs.casino.texts import (
    PACHINKO_SPINNING as SPINNING,
)
from cogs.casino.texts import (
    PACHINKO_STEP_ICON as STEP_ICON,
)
from cogs.casino.texts import (
    PACHINKO_STEP_SECONDS as STEP,
)
from cogs.casino.texts import (
    PACHINKO_STEP_TEXT as STEP_TEXT,
)
from cogs.casino.texts import (
    PACHINKO_STEPS_OF as STEPS_OF,
)
from cogs.casino.texts import (
    PACHINKO_STORIES as STORIES,
)
from cogs.casino.texts import (
    PACHINKO_TITLE as TITLE,
)
from cogs.shared import ui
from domain.games import pachinko, rtplevel

_log = logging.getLogger(__name__)

RNG = random.SystemRandom()
MOOD_COLOR = {"normal": ui.DARK, "blue": ui.INFO, "red": ui.ERROR, "gold": ui.GOLD}  # 演出の色合い → 色


@dataclass(frozen=True)
class Frame:
    text: str
    mood: str = "normal"  # 表示の色合い（normal / blue / red / gold）


def banner_for(top: int, skin=None) -> str:
    """当たりの見出し。演出の変更品が見出しを持っていれば、大当たり以上（10倍〜）で、そちらを使う。"""
    if skin is not None and skin.banner and top >= 10:
        return skin.banner
    return BANNER.get(top, "")


NUMBER = "①②③④⑤⑥⑦⑧⑨⑩"


def round_text(rounds: tuple) -> str:
    return "・".join(f"{pachinko.ROUND_NAME[r]}{r}倍" for r in rounds)


def rank_of(mult: int) -> str:
    return next(name for floor, name in RANKS if mult >= floor)


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


def hold_row(icon: str) -> str:
    """保留（次に回る回転の予約）の表示。いま変化している玉を icon にする。"""
    return "保留: ⚪⚪" + icon + "⚪"


def _text_frame(head: str, reels: tuple, text: str = "", mood: str = "normal") -> Frame:
    return Frame(head + reel_line(*reels) + (f"\n{text}" if text else ""), mood)


@dataclass(frozen=True)
class Staging:
    """見せる演出の格。抽選の omen / tier（当たる確率と統計用）とは別に、表示のためだけに決める。"""

    omen: str  # 予告の色（なしなら ""）
    tier: str  # リーチの格（なしなら ""）


# 外れるリーチ（ガセ）では、プレミアムリーチを出さない（実機の全回転と同じ確定演出）
_CAP_TIER = {pachinko.PREMIUM: pachinko.SUPER}
REVIVE_MIN = 10  # 復活演出を見せる、今回の当たり（1ラウンド目）の出玉の下限


def grade_of(mult: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """今回の当たりの出玉に合う、予告の色とリーチの格の候補。"""
    return next((omens, tiers) for floor, omens, tiers in GRADES if mult >= floor)


def staging_for(rng: random.Random, spin: pachinko.SpinInfo) -> Staging:
    """当たりは今回の当たり（1ラウンド目）の出玉に比例した格で、外れるリーチは抽選どおりの格（プレミアムは使わない）で見せる。
    連チャンの継続はこのあとの抽選として見せるので、リーチの時点の演出には混ぜない（まだ起きていないことを先に匂わせない）。"""
    if not spin.hit:
        return Staging(spin.omen, _CAP_TIER.get(spin.tier, spin.tier))
    omens, tiers = grade_of(spin.rounds[0])
    return Staging(rng.choice(omens), rng.choice(tiers))


def _pseudo_cycles(rng: random.Random, count: int, top_mood: str = "red") -> list:
    """擬似連: 止まったように見えて、揺れて、再び回り出す。count 回ぶん。止まったように見えるコマは、3つとも違う絵柄にする。
    合計の回数は先に出さず、「擬似連 2 → 擬似連 3 …」と1段ずつ上げる。2段目からの色は、リーチの格の色（top_mood）にする。"""
    frames = []
    for k in range(1, count + 1):
        mood = "red" if k == 1 else top_mood
        frames.append(
            _text_frame(
                "",
                tuple(rng.sample(pachinko.REEL_SYMBOLS, 3)),
                f"…止まった…？ いや、揺れている…！ **擬似連 {k + 1}**",
                mood,
            )
        )
        frames.append(_text_frame("", (SPINNING,) * 3, "再回転！", mood))
    return frames


def _revive_frames(rng: random.Random, sym: str, full: bool = False) -> list:
    """復活演出のコマ: 左右が揃ったまま、3つ目がずれて止まる（外れ）→ セリフ → 再回転（full なら全回転と名乗る）→ （呼び出し側が）出目。"""
    other = rng.choice([x for x in pachinko.REEL_SYMBOLS if x != sym])
    line, sub = rng.choice(REVIVE_TEXT)
    return [
        _text_frame("", (sym, sym, other), "…**外れた**…", "blue"),
        _text_frame("", (sym, sym, other), f"**{line}**", "red"),
        _text_frame(
            "🔥🔥 **復活！全回転！！** 🔥🔥\n" if full else "💥 **復活！！** 💥\n", (SPINNING,) * 3, sub, "gold"
        ),
    ]


def spin_frames(rng: random.Random, spin: pachinko.SpinInfo, label: str = "", staging: Staging | None = None) -> list:
    """1回転の演出のコマ。予告 → 回転 →（擬似連）→ 左右が止まってリーチ → 発展 → 出目 の順。
    当たりなら3つ揃い、外れるリーチなら最後に3つ目がずれて止まる。どちらも、出目のコマまでは同じ流れで見せる。"""
    staging = staging or staging_for(rng, spin)
    first = spin.rounds[0] if spin.rounds else 0
    omen, tier = staging.omen, staging.tier
    reels = pachinko.pick_reels(first, tier, rng)
    sym = reels[0]
    head = f"**{label}**\n" if label else ""
    frames = []
    for k in range(STEPS_OF.get(omen, 0)):  # ① 予告のステップアップ: 色が高いほど、段が多い
        mood = ("blue", "red", "gold")[k]
        text = f"**【予告】** {OMEN_TEXT[omen]}" if k + 1 == STEPS_OF[omen] else f"**【予告】** {STEP_TEXT[k]}"
        frames.append(Frame((head if k == 0 else "") + hold_row(STEP_ICON[k]) + "\n" + text, mood))
    frames.append(_text_frame(head if not omen else "", (SPINNING,) * 3, "回転中…"))  # ② 回転
    # リーチなしで、突然3つ揃う「サプライズ当たり」（出玉の小さい当たりだけ）。揃う直前まで、3つとも回っている
    if not tier:
        frames.append(_text_frame("", (SPINNING,) * 3, "…！？ **まさか…**", omen or "normal"))
        frames.append(_text_frame(head, reels))
        return frames
    mood = {pachinko.NORMAL: "blue", pachinko.SUPER: "red", pachinko.PREMIUM: "gold"}[tier]
    pseudo = rng.choice(pachinko.PSEUDO_COUNTS.get(tier, [0]))
    frames += _pseudo_cycles(rng, pseudo, mood)  # ③ 擬似連（あれば）。そのあと、あらためて左右が止まる
    frames.append(_text_frame("", (sym, SPINNING, SPINNING)))
    frames.append(
        _text_frame("", (sym, sym, SPINNING), "**リーチ！**", "blue")
    )  # ④ リーチ。ここから出目が決まるまで、左右は変えない
    # 復活は派手なので、今回の当たりが大当たり（10倍）以上のときだけ
    revive = spin.hit and spin.rounds[0] >= REVIVE_MIN and rng.random() < pachinko.REVIVE_CHANCE[tier]
    title, sub = rng.choice(STORIES[tier])
    if tier == pachinko.NORMAL:
        frames.append(_text_frame(f"{title}\n", (sym, sym, SPINNING), sub, mood))
        if rng.random() < 0.5:
            frames.append(_text_frame("", (sym, sym, SPINNING), rng.choice(NORMAL_WHISPERS), mood))
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
            frames += _revive_frames(rng, sym, tier == pachinko.PREMIUM)
        elif tier == pachinko.PREMIUM:
            frames.append(
                _text_frame("🔥🔥 **全回転！！** 🔥🔥\n", (SPINNING,) * 3, "すべての絵柄が光る…！！", "gold")
            )  # 全回転のあとは、すぐ出目
        else:
            frames.append(_text_frame("⚡ スローになった… ⚡\n", (sym, sym, SPINNING), "…ここで決まる…！", mood))
    if not spin.hit:  # ⑤ 外れ: 3つ目がずれて止まる
        frames.append(_text_frame(head, reels, "…**ハズレ**…"))
        return frames
    frames.append(_text_frame(head, reels))  # ⑤ 出目（3つ揃い）。揃ったら、演出はここで終わる
    return frames


def _size_mood(mult: int) -> str:
    """出玉の大きさに合わせた色合い（小さい当たりを、大きい当たりと同じ金色で飾らない）。"""
    return "gold" if mult >= 30 else ("red" if mult >= 10 else "blue")


def reveal_frames(
    spin: pachinko.SpinInfo, limit: int = SHOWN_ROUNDS, skin=None, rng: random.Random | None = None
) -> list:
    """演出が終わったあとに出す、当たりの宣言 → 連チャン → 終了のまとめ。
    見ている人はいまのコマしか知らない前提で作る: 続くかどうか・何連になるかは、そのときが来るまで出さない。1ラウンドごとに「継続…？」を挟み、続いたか終わったかを、そのあとで見せる。"""
    rng = rng or RNG
    rounds = spin.rounds
    n, first = len(rounds), rounds[0]
    frames = [Frame(banner_for(first, skin) or "# 🎉 当たり！！ 🎉", _size_mood(first))]
    total = 0
    for k, r in enumerate(rounds[:limit], start=1):
        total += r
        head = "# 🔥 1連目 🔥" if k == 1 else f"# 🔥 継続！ {k}連目 🔥"
        text = f"{head}\n{pachinko.ROUND_NAME[r]}！ 出玉 **+{r}倍**\n累計 **+{total}倍**"
        if k == 5:
            text += "\n🎆 **5連 達成！！** 🎆"
        frames.append(Frame(text, _size_mood(r)))
        frames.append(
            Frame(f"累計 **+{total}倍**\n{rng.choice(CHAIN_WAIT)}", "red")
        )  # 毎回待つ（待ちの有無で、続くかを先に教えない）
    if n > limit:
        title = "20連 突破！！！" if n >= 20 else ("10連 突破！！" if n >= 10 else f"{n}連チャン！！")
        frames.append(Frame(f"# 🎆🎆 {title} 🎆🎆\n合計 **{n}連**　累計 **+{spin.raw}倍**", "gold"))
    frames.append(
        Frame(
            f"✨ **連チャン終了** ✨\n**{n}連**　出玉 **+{spin.raw}倍**\n{rank_of(spin.raw)}",
            _size_mood(spin.raw),
        )
    )
    return frames


def hit_scene(rng: random.Random, spin: pachinko.SpinInfo, label: str, skin=None) -> list:
    """当たりの1場面: 演出をじっくり → そのあとに当たりを宣言。"""
    return spin_frames(rng, spin, label) + reveal_frames(spin, skin=skin, rng=rng)


def reach_misses(rng: random.Random, spins, limit: int) -> list[tuple[int, pachinko.SpinInfo]]:
    """外れたリーチ（ガセ）のうち、見せるものを limit 回まで選び、(何回転目か, 回転) を回転の順に返す。"""
    misses = [(i, s) for i, s in enumerate(spins, start=1) if s.tier and not s.hit]
    return sorted(rng.sample(misses, min(limit, len(misses))), key=lambda x: x[0])


def single_frames(rng: random.Random, result: pachinko.SetResult, skin=None) -> list:
    """1セットの演出のコマ。当たった回転と、外れたリーチの一部（ガセ）を、予告・リーチ・リールまで同じ流れで見せる。
    リーチが出ても、当たりとは限らない。当たりの宣言は演出のあと。ほかのハズレの回転は数字でまとめる。"""
    frames = [Frame(f, "normal") for f in ball_frames(rng, rows=2, balls=pachinko.BALLS_PER_SET)]
    frames.append(
        Frame(
            f"**{pachinko.BALLS_PER_SET}発** のうち **{result.starts}発** がスタートに入った！\n"
            + ("リールが回り出す…" if result.starts else "…今回は1回も入らなかった…")
        )
    )
    if not result.starts:
        return frames
    hits = pachinko.hit_spins(result.spins)
    scenes: dict[int, list] = {}
    used = len(frames) + 1  # 最後のまとめの分
    for index, spin in hits:  # 当たりを先に、回転の順でコマの上限まで確保する
        scene = hit_scene(rng, spin, f"{index}回転目", skin)
        if scenes and used + len(scene) > FRAME_BUDGET:
            break
        scenes[index] = scene
        used += len(scene)
    shown_hits = len(scenes)
    # 省いた当たりより後ろのガセは、順番が崩れるので見せない
    last_hit = max(scenes) if shown_hits < len(hits) else result.starts
    for index, spin in reach_misses(rng, result.spins, SHOWN_MISSES):  # 残りのコマで、ガセを混ぜる
        scene = spin_frames(rng, spin, f"{index}回転目")
        if index > last_hit or used + len(scene) > FRAME_BUDGET:
            continue
        scenes[index] = scene
        used += len(scene)
    previous = 0
    for index in sorted(scenes):
        gap = index - 1 - previous
        if gap > 0:
            frames.append(Frame(f"…{gap}回転 経過…"))
        frames += scenes[index]
        previous = index
    if not hits:
        reach = sum(1 for s in result.spins if s.tier)
        frames.append(
            Frame(
                f"**{result.starts}回転**、すべてハズレ…"
                + (f"\n（リーチが **{reach}回** あったが、外れた）" if reach else "")
            )
        )
        return frames
    more = len(hits) - shown_hits
    if more > 0:
        frames.append(Frame(f"さらに、大当たりが **{more}回**！ 合計 **+{result.mult}倍**", "gold"))
    else:
        frames.append(Frame(f"合計 **+{result.mult}倍**！", _size_mood(result.mult)))
    return frames


def set_line(index: int, result: pachinko.SetResult) -> str:
    mark = NUMBER[index] if index < len(NUMBER) else str(index + 1)
    if not result.mult:
        return f"{mark} 入賞 {result.starts:>2} … ハズレ"
    return f"{mark} 入賞 {result.starts:>2} … 🎉 **{result.chains}連** **+{result.mult}倍**"


def _set_label(i: int, index: int) -> str:
    return f"{NUMBER[i] if i < len(NUMBER) else i + 1} セットの {index}回転目"


def multi_frames(rng: random.Random, results: list, skin=None) -> list:
    """連続投入の演出のコマ。1セットずつ表を伸ばす。**当たったセット**は、表に載せる前に、その当たりの演出をじっくり見せる
    （出玉の大きい順に、コマの上限まで。残りの当たりは表にそのまま載せる）。
    残ったコマで、ハズレのセットの外れたリーチ（ガセ）も、いくつか同じ流れで見せる。"""
    frames = [Frame(f, "normal") for f in ball_frames(rng, rows=2, balls=pachinko.BALLS_PER_SET * len(results))]
    scenes = {}
    used = len(frames) + len(results)  # 表のコマ分を先に確保
    for i in sorted((i for i, r in enumerate(results) if r.mult), key=lambda i: -results[i].mult):
        hit_index, spin = pachinko.hit_spins(results[i].spins)[0]
        scene = hit_scene(rng, spin, _set_label(i, hit_index), skin)
        if scenes and used + len(scene) > FRAME_BUDGET:
            continue
        scenes[i] = scene
        used += len(scene)
    candidates = [(i, m) for i, r in enumerate(results) if not r.mult for m in reach_misses(rng, r.spins, 1)]
    for i, (index, spin) in sorted(rng.sample(candidates, min(MULTI_SHOWN_MISSES, len(candidates)))):
        scene = spin_frames(rng, spin, _set_label(i, index))
        if used + len(scene) > FRAME_BUDGET:
            continue
        scenes[i] = scene
        used += len(scene)
    lines = []
    for i, r in enumerate(results):
        if i in scenes:
            frames += scenes[i]
        lines.append(set_line(i, r))
        table = "\n".join(lines) + (f"\n… あと {len(results) - i - 1} セット" if i + 1 < len(results) else "")
        frames.append(Frame(table, "gold" if r.mult >= 10 else "normal"))
    return frames


def top_round(results: list) -> int:
    """当たったラウンドのうち、いちばん大きい出玉の倍数（当たりがなければ 0）。"""
    return max((x for r in results for s in r.spins for x in s.rounds), default=0)


def flavor_text(top: int) -> str:
    """結果の一言。いちばん大きいラウンドで決まる。"""
    key = "激アツ確変" if top >= 30 else ("大当たり" if top >= 10 else ("小当たり" if top else "ハズレ"))
    return FLAVOR[key]


def result_body(ctx, results: list, payout: int, net: int) -> str:
    best = max(results, key=lambda r: r.mult)
    top = top_round(results)
    lines = []
    if len(results) == 1:
        r = results[0]
        rounds = tuple(x for s in r.spins for x in s.rounds)
        hit_line = (
            f"大当たり **{r.chains}連**（{round_text(rounds)}）　出玉 **+{r.mult}倍**" if r.mult else "**ハズレ**"
        )
        lines += [
            banner_for(top, ctx.skin),
            f"スタート入賞 **{r.starts} / {pachinko.BALLS_PER_SET}発**",
            hit_line,
            flavor_text(top),
        ]
    else:
        lines += [banner_for(top, ctx.skin)]
        lines += [set_line(i, r) for i, r in enumerate(results)]
        wins = sum(1 for r in results if r.mult)
        lines += [
            "",
            f"{len(results)}セット（{pachinko.BALLS_PER_SET * len(results)}発）で 当たり **{wins}回**　最大 **+{best.mult}倍**",
        ]
    lines += ["", result_lines(ctx, payout, net)]
    return "\n".join(x for x in lines if x is not None)


class PachinkoView(ReplayView):
    """結果の下のボタン: もう一度・倍にして・×5連続（または×10連続）。"""

    def __init__(self, deps: CasinoDeps, user_id: int, per_bet: int, sets: int, replay, timeout: float = 90):
        super().__init__(deps, user_id, per_bet * sets, replay, timeout)
        self.per_bet, self.sets = per_bet, sets
        extra = next((n for n in EXTRA_SETS if n > sets), None)
        if extra is not None:
            button = discord.ui.Button(
                label=f"🎰 ×{extra}連続（{per_bet * extra:,}）", style=discord.ButtonStyle.success
            )
            button.callback = self._make_extra(extra)
            self.add_item(button)

    def _make_extra(self, n: int):
        async def callback(interaction: discord.Interaction):
            await play_pachinko(interaction, self.deps, self.per_bet, n)

        return callback


async def play_pachinko(interaction: discord.Interaction, deps: CasinoDeps, bet: int, sets: int = 1):
    """bet は1セットあたりの賭け金。合計（bet × sets）が、そのまま賭け金になる（残高が足りなければ始まらない）。"""
    sets = max(1, min(int(sets), pachinko.MAX_SETS))
    total = bet * sets
    ctx = await start_game(interaction, deps, total, "パチンコ")
    if ctx is None:
        return
    try:
        results = [pachinko.roll_set(RNG) for _ in range(sets)]
        level = ctx.casino.rtp_level(ctx.guild_id, "パチンコ")  # 還元率レベルは、セットごとに勝ち分へかける
        raw = [bet * r.mult for r in results]
        adjusted = [rtplevel.adjust("パチンコ", level, bet, p) for p in raw]
        ctx.rtp_shift = sum(adjusted) - sum(raw)
        net = ctx.settle(sum(adjusted), keep_session=True, rtp_done=True)
        for r in results:
            try:  # 統計用の記録。失敗しても、結果には影響させない
                ctx.casino.record_pachinko_set(ctx.guild_id, ctx.user_id, bet, pachinko.BALLS_PER_SET, r)
            except Exception as exc:
                _log.warning(f"パチンコの記録に失敗: {exc!r}")
        top = top_round(results)
        body = result_body(ctx, results, sum(raw), net)
        color = ui.GOLD if top >= 10 else (ui.OK if net > 0 else ui.ERROR)
        head = f"賭け金: **{ui.pelica(bet)}**" + (f" × {sets}セット" if sets > 1 else "") + "\n"
        frames = single_frames(RNG, results[0], ctx.skin) if sets == 1 else multi_frames(RNG, results, ctx.skin)
        step = "最初の表示"
        try:
            await interaction.response.send_message(
                embed=ui.embed(
                    game_title(TITLE, ctx),
                    head + "玉を弾く…ざわ… ざわ…" + (f"\n{ctx.skin.start_line}" if ctx.skin else ""),
                    ui.DARK,
                )
            )
            for i, frame in enumerate(frames, start=1):
                await asyncio.sleep(STEP)
                step = f"途中のコマ {i}/{len(frames)}"
                await interaction.edit_original_response(
                    embed=ui.embed(game_title(TITLE, ctx), head + frame.text, MOOD_COLOR[frame.mood])
                )
            await asyncio.sleep(STEP)
            step = "結果の表示"
            view = PachinkoView(deps, ctx.user_id, bet, sets, lambda it, b: play_pachinko(it, deps, b // sets, sets))
            view.origin = interaction
            await interaction.edit_original_response(
                embed=ui.embed(f"{game_title(TITLE, ctx)} — {signed(net)}ペリカ", body, color), view=view
            )
        except discord.HTTPException as exc:
            log_display_failure("パチンコ", step, exc)  # 表示に失敗しても、精算は済んでいる
    except Exception:
        ctx.abort()
        raise
    finally:
        ctx.release()
