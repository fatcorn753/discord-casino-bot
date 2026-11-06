"""パチンコ（地下パチンコ風）。玉が落ちて、リールが回り、リーチを経て結果が出るまでをコマ送りで見せる。

抽選の結果は最初に決めて先に精算する。演出は見せるだけなので、途中でメッセージが消されても結果は変わらない。
"""

import asyncio
import random

import discord

from core import ui
from games import pachinko
from play.common import ReplayView, game_title, log_display_failure, result_lines, signed, start_game

RNG = random.SystemRandom()
TITLE = "🎰 地下パチンコ"
STEP = 1.1          # メッセージ編集のレート制限に収まる間隔（秒）
MOOD_COLOR = {"normal": ui.DARK, "blue": ui.INFO, "red": ui.ERROR, "gold": ui.GOLD}     # 演出の色合い → 色
BANNER = {"大当たり": "# 🎊 大当たり！！ 🎊", "激アツ確変": "# 🔥🔥 激アツ確変！！！ 🔥🔥"}


async def play_pachinko(interaction: discord.Interaction, bot, bet: int):
    ctx = await start_game(interaction, bot, bet, "パチンコ")
    if ctx is None:
        return
    try:
        spin = pachinko.build_spin(RNG)
        payout = bet * spin.outcome.multiplier
        net = ctx.settle(payout, keep_session=True)
        try:       # 統計用の記録。失敗しても、結果には影響させない
            ctx.economy.pachinko_record(ctx.user_id, spin.omen, spin.tier, spin.outcome.multiplier, bet)
        except Exception as exc:
            print(f"パチンコの記録に失敗: {exc!r}")
        won = spin.outcome.multiplier > 0
        body = "\n".join([
            BANNER.get(spin.outcome.name, ""),
            pachinko.reel_line(*spin.reels),
            f"**{spin.outcome.name}**" + (f"（{spin.outcome.multiplier}倍）" if won else ""),
            pachinko.FLAVOR[spin.outcome.name],
            "",
            result_lines(ctx, payout, net),
        ])
        color = ui.GOLD if spin.outcome.multiplier >= 10 else (ui.OK if won else ui.ERROR)
        head = f"賭け金: **{ui.pelica(bet)}**\n"
        step = "最初の表示"
        try:
            await interaction.response.send_message(embed=ui.embed(game_title(TITLE, ctx), head + "玉を弾く…ざわ… ざわ…", ui.DARK))
            for i, frame in enumerate(spin.frames, start=1):
                await asyncio.sleep(STEP)
                step = f"途中のコマ {i}/{len(spin.frames)}"
                await interaction.edit_original_response(embed=ui.embed(game_title(TITLE, ctx), head + frame.text, MOOD_COLOR[frame.mood]))
            await asyncio.sleep(STEP)
            step = "結果の表示"
            view = ReplayView(bot, ctx.user_id, bet, lambda it, b: play_pachinko(it, bot, b))
            view.origin = interaction
            await interaction.edit_original_response(embed=ui.embed(f"{game_title(TITLE, ctx)} — {signed(net)}ペリカ", body, color), view=view)
        except discord.HTTPException as exc:
            log_display_failure("パチンコ", step, exc)      # 表示に失敗しても、精算は済んでいる
    except Exception:
        ctx.abort()
        raise
    finally:
        ctx.release()
