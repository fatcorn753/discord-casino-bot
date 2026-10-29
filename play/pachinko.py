"""パチンコ（地下パチンコ風）。玉が落ちて、リールが回り、リーチを経て結果が出るまでをコマ送りで見せる。

抽選の結果は最初に決めて先に精算する。演出は見せるだけなので、途中でメッセージが消されても結果は変わらない。
"""

import asyncio
import random

import discord

from core import ui
from games import pachinko
from play.common import ReplayView, result_lines, signed, start_game

RNG = random.SystemRandom()
TITLE = "🎰 地下パチンコ"
STEP = 1.1          # メッセージ編集のレート制限に収まる間隔（秒）


async def play_pachinko(interaction: discord.Interaction, bot, bet: int):
    ctx = await start_game(interaction, bot, bet, "パチンコ")
    if ctx is None:
        return
    try:
        spin = pachinko.build_spin(RNG)
        payout = bet * spin.outcome.multiplier
        net = ctx.settle(payout, keep_session=True)
        won = spin.outcome.multiplier > 0
        body = "\n".join([
            pachinko.reel_line(*spin.reels),
            f"**{spin.outcome.name}**" + (f"（{spin.outcome.multiplier}倍）" if won else ""),
            pachinko.FLAVOR[spin.outcome.name],
            "",
            result_lines(ctx, payout, net),
        ])
        color = ui.GOLD if spin.outcome.multiplier >= 10 else (ui.OK if won else ui.ERROR)
        head = f"賭け金: **{ui.pelica(bet)}**\n"
        try:
            await interaction.response.send_message(embed=ui.embed(TITLE, head + "玉を弾く…ざわ… ざわ…", ui.DARK))
            for frame in spin.frames:
                await asyncio.sleep(STEP)
                await interaction.edit_original_response(embed=ui.embed(TITLE, head + frame, ui.DARK))
            await asyncio.sleep(STEP)
            view = ReplayView(bot, ctx.user_id, bet, lambda it, b: play_pachinko(it, bot, b))
            view.origin = interaction
            await interaction.edit_original_response(embed=ui.embed(f"{TITLE} — {signed(net)}ペリカ", body, color), view=view)
        except discord.HTTPException:
            pass      # 表示に失敗しても、精算は済んでいる
    except Exception:
        ctx.abort()
        raise
    finally:
        ctx.release()
