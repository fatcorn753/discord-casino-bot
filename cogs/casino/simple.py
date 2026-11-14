"""すぐ結果が出るゲーム: スロット、ルーレット（短い演出つき）。

結果は最初に決めて **先に精算** し、そのあとで演出を見せる。演出の途中でメッセージが消されたり
通信が失敗したりしても、結果（ペリカの増減）は変わらない。
"""

import asyncio
import random

import discord

from cogs.casino.common import (
    ReplayView,
    game_title,
    log_display_failure,
    result_color,
    result_lines,
    signed,
    start_game,
)
from cogs.shared import ui
from domain.games import roulette, slot
from infra import catalogs

RNG = random.SystemRandom()  # OS の乱数。結果を予測・再現できない
STEP = catalogs.games().common.simple_step_seconds  # 演出のコマ送り間隔（秒）


async def show(
    interaction: discord.Interaction,
    first: discord.Embed,
    frames: list[discord.Embed],
    final: discord.Embed,
    view: ReplayView | None = None,
    game: str = "",
) -> None:
    """演出を見せる。表示の失敗（メッセージ削除・通信エラー）は結果に影響しない（精算は済んでいる）ので続行するが、記録は残す。"""
    step = "最初の表示"
    try:
        await interaction.response.send_message(embed=first)
        for i, frame in enumerate(frames, start=1):
            await asyncio.sleep(STEP)
            step = f"途中のコマ {i}/{len(frames)}"
            await interaction.edit_original_response(embed=frame)
        await asyncio.sleep(STEP)
        step = "結果の表示"
        if view is not None:
            view.origin = interaction
            await interaction.edit_original_response(embed=final, view=view)
        else:
            await interaction.edit_original_response(embed=final)
    except discord.HTTPException as exc:
        log_display_failure(game or "?", step, exc)


# ---------------------------- スロット ---------------------------- #
async def play_slot(interaction: discord.Interaction, bot, bet: int):
    ctx = await start_game(interaction, bot, bet, "スロット")
    if ctx is None:
        return
    try:
        reels = slot.spin(RNG)
        payout = int(bet * slot.multiplier(reels))  # 端数は切り捨て
        net = ctx.settle(payout, keep_session=True)
        spin, title = "🌀", game_title("🎰 スロット", ctx)
        line = f"┃ {reels[0]} ┃ {reels[1]} ┃ {reels[2]} ┃\n**{slot.label(reels)}**"
        final = ui.embed(
            f"{title} — {signed(net)}ペリカ",
            result_lines(ctx, payout, net, line),
            result_color(bet, net, ui.OK if net > 0 else (ui.INFO if net == 0 else ui.ERROR)),
        )
        await show(
            interaction,
            ui.embed(title, f"┃ {spin} ┃ {spin} ┃ {spin} ┃", ui.GOLD),
            [
                ui.embed(title, f"┃ {reels[0]} ┃ {spin} ┃ {spin} ┃", ui.GOLD),
                ui.embed(title, f"┃ {reels[0]} ┃ {reels[1]} ┃ {spin} ┃", ui.GOLD),
            ],
            final,
            ReplayView(bot, ctx.user_id, bet, lambda it, b: play_slot(it, bot, b)),
            game="スロット",
        )
    except Exception:
        ctx.abort()
        raise
    finally:
        ctx.release()


# --------------------------- ルーレット --------------------------- #
async def play_roulette(interaction: discord.Interaction, bot, bet: int, kind: str, number: int | None = None) -> None:
    # 賭け方の指定ミスは、賭け金を預かる前に弾く
    if kind == "number" and number is None:
        await interaction.response.send_message(
            embed=ui.error_embed(
                "数字が必要です", "「ストレート（数字）」を選んだときは、0〜36 の数字も指定してください。"
            ),
            ephemeral=True,
        )
        return
    if kind != "number" and number is not None:
        await interaction.response.send_message(
            embed=ui.error_embed("数字は不要です", "数字を指定するのは「ストレート（数字）」のときだけです。"),
            ephemeral=True,
        )
        return
    ctx = await start_game(interaction, bot, bet, "ルーレット")
    if ctx is None:
        return
    try:
        name = roulette.BETS[kind][0] + (f" {number}" if kind == "number" else "")
        result = roulette.spin(RNG)
        payout = roulette.payout(kind, bet, result, number)
        net = ctx.settle(payout, keep_session=True, rtp_kind=kind)
        title, head = game_title("🎡 ルーレット", ctx), f"あなたの賭け: **{name}**\n"
        verdict = "当たり🎉" if payout else "ハズレ"
        final = ui.embed(
            f"{title} — {roulette.color_emoji(result)} **{result}**（{roulette.color_of(result)}） {verdict}　{signed(net)}ペリカ",
            result_lines(ctx, payout, net, head.rstrip()),
            result_color(bet, net, ui.OK if payout else ui.ERROR),
        )
        await show(
            interaction,
            ui.embed(title, head + "ボールが回り出した…", ui.GOLD),
            [
                ui.embed(title, head + f"ボールが跳ねている… `{RNG.randint(0, 36)}`", ui.GOLD),
                ui.embed(title, head + f"もうすぐ止まる… `{RNG.randint(0, 36)}`", ui.GOLD),
            ],
            final,
            ReplayView(bot, ctx.user_id, bet, lambda it, b: play_roulette(it, bot, b, kind, number)),
            game="ルーレット",
        )
    except Exception:
        ctx.abort()
        raise
    finally:
        ctx.release()
