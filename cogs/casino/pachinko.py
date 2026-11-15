"""パチンコ（地下パチンコ風・打ち出し型）。100発を打ち出し、スタートに入った数だけ回転し、当たれば連チャンする。

賭けた時点で結果をすべて決めて先に精算し、演出はあとから見せる（途中でメッセージが消されても結果は変わらない）。
`回数` を指定すると、同じ賭け金で何セットも連続で打つ（合計が上限以内）。演出は短くまとめ、結果は1つの要約で返す。
"""

import asyncio
import random
from dataclasses import dataclass

import discord

from cogs.casino.common import ReplayView, game_title, log_display_failure, result_lines, signed, start_game
from cogs.casino.texts import (
    PACHINKO_BANNER as BANNER,
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


def _pseudo_cycles(rng: random.Random, count: int) -> list:
    """擬似連: 止まったように見えて、揺れて、再び回り出す。count 回ぶん。止まったように見えるコマは、3つとも違う絵柄にする。"""
    frames = []
    for k in range(1, count + 1):
        label = "**擬似連！**" if count == 1 else f"**擬似連 {k}/{count}**"
        mood = "gold" if count > 1 and k == count else "red"
        frames.append(
            _text_frame(
                "", tuple(rng.sample(pachinko.REEL_SYMBOLS, 3)), f"…止まった…？ いや、揺れている…！ {label}", mood
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


def spin_frames(rng: random.Random, spin: pachinko.SpinInfo, label: str = "") -> list:
    """当たった1回転の演出のコマ。予告 → 回転 →（擬似連）→ 左右が止まってリーチ → 発展 → 出目（3つ揃い）の順。"""
    first = spin.rounds[0] if spin.rounds else 0
    reels = pachinko.pick_reels(first, spin.tier, rng)
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
    mood = {pachinko.NORMAL: "blue", pachinko.SUPER: "red", pachinko.PREMIUM: "gold"}[tier]
    pseudo = rng.choice(pachinko.PSEUDO_COUNTS.get(tier, [0]))
    frames += _pseudo_cycles(rng, pseudo)  # ③ 擬似連（あれば）。そのあと、あらためて左右が止まる
    frames.append(_text_frame("", (sym, SPINNING, SPINNING)))
    frames.append(
        _text_frame("", (sym, sym, SPINNING), "**リーチ！**", "blue")
    )  # ④ リーチ。ここから出目が決まるまで、左右は変えない
    revive = rng.random() < pachinko.REVIVE_CHANCE[tier]
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
    frames.append(_text_frame(head, reels))  # ⑤ 出目（3つ揃い）。揃ったら、演出はここで終わる
    return frames


def reveal_frames(spin: pachinko.SpinInfo, limit: int = SHOWN_ROUNDS, skin=None) -> list:
    """演出が終わったあとに出す、当たりの宣言 → 連チャン（数を大きく、5連で節目）→ 終了のまとめ。"""
    n, top = len(spin.rounds), max(spin.rounds)
    frames, total = (
        [Frame(f"{banner_for(top, skin) or '# 🎉 当たり！！ 🎉'}\n**{n}連** スタート！", "gold")],
        0,
    )
    for k, r in enumerate(spin.rounds[:limit], start=1):
        total += r
        frames.append(
            Frame(f"# 🔥 {k}連目 🔥\n{pachinko.ROUND_NAME[r]}！ 出玉 **+{r}倍**\n累計 **+{total}倍**", "gold")
        )
        if k == 5 and n > 5:
            frames.append(Frame(f"# 🎆 5連 達成！！ 🎆\n累計 **+{total}倍**　まだ止まらない…！", "gold"))
    if n > limit:
        title = "20連 突破！！！" if n >= 20 else ("10連 突破！！" if n >= 10 else f"{n}連チャン！！")
        frames.append(Frame(f"# 🎆🎆 {title} 🎆🎆\n合計 **{n}連**　累計 **+{spin.raw}倍**", "gold"))
    frames.append(
        Frame(
            f"✨ **連チャン終了** ✨\n**{n}連**　出玉 **+{spin.raw}倍**\n{rank_of(spin.raw)}",
            "gold",
        )
    )
    return frames


def hit_scene(rng: random.Random, spin: pachinko.SpinInfo, label: str, skin=None) -> list:
    """当たりの1場面: 演出をじっくり → そのあとに当たりを宣言。"""
    return spin_frames(rng, spin, label) + reveal_frames(spin, skin=skin)


def single_frames(rng: random.Random, result: pachinko.SetResult, skin=None) -> list:
    """1セットの演出のコマ。**当たった回転だけ**を、予告・リーチ・リール・連チャンまでじっくり見せる。
    当たりの宣言は演出のあと。ハズレの回転は数字でまとめる。連続する当たりも、上限まではそのまま見せる。"""
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
    if not hits:
        reach = sum(1 for s in result.spins if s.tier)
        frames.append(
            Frame(
                f"**{result.starts}回転**、すべてハズレ…"
                + (f"\n（リーチが **{reach}回** あったが、外れた）" if reach else "")
            )
        )
        return frames
    previous, shown = 0, 0
    for index, spin in hits:
        scene = hit_scene(rng, spin, f"{index}回転目", skin)
        if shown and len(frames) + len(scene) > FRAME_BUDGET:
            break
        gap = index - 1 - previous
        if gap > 0:
            frames.append(Frame(f"…{gap}回転 経過…"))
        frames += scene
        previous, shown = index, shown + 1
    more = len(hits) - shown
    if more > 0:
        frames.append(Frame(f"さらに、大当たりが **{more}回**！ 合計 **+{result.mult}倍**", "gold"))
    else:
        frames.append(Frame(f"合計 **+{result.mult}倍**！", "gold"))
    return frames


def set_line(index: int, result: pachinko.SetResult) -> str:
    mark = NUMBER[index] if index < len(NUMBER) else str(index + 1)
    if not result.mult:
        return f"{mark} 入賞 {result.starts:>2} … ハズレ"
    return f"{mark} 入賞 {result.starts:>2} … 🎉 **{result.chains}連** **+{result.mult}倍**"


def multi_frames(rng: random.Random, results: list, skin=None) -> list:
    """連続投入の演出のコマ。1セットずつ表を伸ばす。**当たったセット**は、表に載せる前に、その当たりの演出をじっくり見せる
    （出玉の大きい順に、コマの上限まで。残りの当たりは表にそのまま載せる）。"""
    frames = [Frame(f, "normal") for f in ball_frames(rng, rows=2, balls=pachinko.BALLS_PER_SET * len(results))]
    scenes = {}
    used = len(frames) + len(results)  # 表のコマ分を先に確保
    for i in sorted((i for i, r in enumerate(results) if r.mult), key=lambda i: -results[i].mult):
        hit_index, spin = pachinko.hit_spins(results[i].spins)[0]
        label = f"{NUMBER[i] if i < len(NUMBER) else i + 1} セットの {hit_index}回転目"
        scene = hit_scene(rng, spin, label, skin)
        if scenes and used + len(scene) > FRAME_BUDGET:
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

    def __init__(self, bot, user_id: int, per_bet: int, sets: int, replay, timeout: float = 90):
        super().__init__(bot, user_id, per_bet * sets, replay, timeout)
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
            await play_pachinko(interaction, self.bot, self.per_bet, n)

        return callback


async def play_pachinko(interaction: discord.Interaction, bot, bet: int, sets: int = 1):
    """bet は1セットあたりの賭け金。合計（bet × sets）が、そのまま賭け金になる（残高が足りなければ始まらない）。"""
    sets = max(1, min(int(sets), pachinko.MAX_SETS))
    total = bet * sets
    ctx = await start_game(interaction, bot, total, "パチンコ")
    if ctx is None:
        return
    try:
        results = [pachinko.roll_set(RNG) for _ in range(sets)]
        level = ctx.economy.rtp_level_get("パチンコ")  # 還元率レベルは、セットごとに勝ち分へかける
        raw = [bet * r.mult for r in results]
        adjusted = [rtplevel.adjust("パチンコ", level, bet, p) for p in raw]
        ctx.rtp_shift = sum(adjusted) - sum(raw)
        net = ctx.settle(sum(adjusted), keep_session=True, rtp_done=True)
        for r in results:
            try:  # 統計用の記録。失敗しても、結果には影響させない
                ctx.economy.pachinko_record_set(ctx.user_id, bet, pachinko.BALLS_PER_SET, r)
            except Exception as exc:
                print(f"パチンコの記録に失敗: {exc!r}")
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
            view = PachinkoView(bot, ctx.user_id, bet, sets, lambda it, b: play_pachinko(it, bot, b // sets, sets))
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
