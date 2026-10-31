"""すぐ結果が出るゲーム: コイン、スロット、ルーレット（短い演出つき）。

結果は最初に決めて **先に精算** し、そのあとで演出を見せる。演出の途中でメッセージが消されたり
通信が失敗したりしても、結果（ペリカの増減）は変わらない。
"""

import asyncio
import random

import discord

from core import ui
from games import roulette, slot
from play.common import ReplayView, game_title, result_color, result_lines, signed, start_game

RNG = random.SystemRandom()      # OS の乱数。結果を予測・再現できない
COIN_MULT = 1.95                 # 当たりで 1.95 倍（還元率 97.5%）。端数は切り捨て
STEP = 0.9                       # 演出のコマ送り間隔（秒）


async def show(interaction: discord.Interaction, first: discord.Embed, frames: list, final: discord.Embed,
               view: discord.ui.View = None) -> None:
    """演出を見せる。表示の失敗（メッセージ削除・通信エラー）は無視する（精算は済んでいる）。"""
    try:
        await interaction.response.send_message(embed=first)
        for frame in frames:
            await asyncio.sleep(STEP)
            await interaction.edit_original_response(embed=frame)
        await asyncio.sleep(STEP)
        if view is not None:
            view.origin = interaction
            await interaction.edit_original_response(embed=final, view=view)
        else:
            await interaction.edit_original_response(embed=final)
    except discord.HTTPException:
        pass


# ----------------------------- コイン ----------------------------- #
async def play_coin(interaction: discord.Interaction, bot, bet: int, side: str):
    ctx = await start_game(interaction, bot, bet, "コイン")
    if ctx is None:
        return
    try:
        result = RNG.choice(["表", "裏"])
        won = result == side
        payout = int(bet * COIN_MULT) if won else 0
        net = ctx.settle(payout, keep_session=True)
        head = f"あなたの予想: **{side}**\n"
        final = ui.embed((f"🪙 {result}！ 当たり🎉" if won else f"🪙 {result}… ハズレ") + f"（{ctx.name}）" * bool(ctx.name) + f" — {signed(net)}ペリカ",
                         result_lines(ctx, payout, net, head.rstrip()), result_color(bet, net, ui.OK if won else ui.ERROR))
        await show(interaction,
                   ui.embed(game_title("🪙 コインを投げた…", ctx), head + "くるくる回っている…", ui.GOLD),
                   [ui.embed(game_title("🪙 コインを投げた…", ctx), head + "まだ回っている…", ui.GOLD)], final,
                   ReplayView(bot, ctx.user_id, bet, lambda it, b: play_coin(it, bot, b, side)))
    except Exception:
        ctx.abort()          # 精算前の失敗だけ返金（精算後なら何もしない）
        raise
    finally:
        ctx.release()


# ---------------------------- スロット ---------------------------- #
async def play_slot(interaction: discord.Interaction, bot, bet: int):
    ctx = await start_game(interaction, bot, bet, "スロット")
    if ctx is None:
        return
    try:
        reels = slot.spin(RNG)
        payout = int(bet * slot.multiplier(reels))        # 端数は切り捨て
        net = ctx.settle(payout, keep_session=True)
        spin, title = "🌀", game_title("🎰 スロット", ctx)
        line = f"┃ {reels[0]} ┃ {reels[1]} ┃ {reels[2]} ┃\n**{slot.label(reels)}**"
        final = ui.embed(f"{title} — {signed(net)}ペリカ", result_lines(ctx, payout, net, line),
                         result_color(bet, net, ui.OK if net > 0 else (ui.INFO if net == 0 else ui.ERROR)))
        await show(interaction,
                   ui.embed(title, f"┃ {spin} ┃ {spin} ┃ {spin} ┃", ui.GOLD),
                   [ui.embed(title, f"┃ {reels[0]} ┃ {spin} ┃ {spin} ┃", ui.GOLD),
                    ui.embed(title, f"┃ {reels[0]} ┃ {reels[1]} ┃ {spin} ┃", ui.GOLD)], final,
                   ReplayView(bot, ctx.user_id, bet, lambda it, b: play_slot(it, bot, b)))
    except Exception:
        ctx.abort()
        raise
    finally:
        ctx.release()


# --------------------------- ルーレット --------------------------- #
async def play_roulette(interaction: discord.Interaction, bot, bet: int, kind: str, number: int = None):
    # 賭け方の指定ミスは、賭け金を預かる前に弾く
    if kind == "number" and number is None:
        return await interaction.response.send_message(embed=ui.error_embed("数字が必要です", "「ストレート（数字）」を選んだときは、0〜36 の数字も指定してください。"), ephemeral=True)
    if kind != "number" and number is not None:
        return await interaction.response.send_message(embed=ui.error_embed("数字は不要です", "数字を指定するのは「ストレート（数字）」のときだけです。"), ephemeral=True)
    ctx = await start_game(interaction, bot, bet, "ルーレット")
    if ctx is None:
        return
    try:
        name = roulette.BETS[kind][0] + (f" {number}" if kind == "number" else "")
        result = roulette.spin(RNG)
        payout = roulette.payout(kind, bet, result, number)
        net = ctx.settle(payout, keep_session=True)
        title, head = game_title("🎡 ルーレット", ctx), f"あなたの賭け: **{name}**\n"
        verdict = "当たり🎉" if payout else "ハズレ"
        final = ui.embed(f"{title} — {roulette.color_emoji(result)} **{result}**（{roulette.color_of(result)}） {verdict}　{signed(net)}ペリカ",
                         result_lines(ctx, payout, net, head.rstrip()), result_color(bet, net, ui.OK if payout else ui.ERROR))
        await show(interaction,
                   ui.embed(title, head + "ボールが回り出した…", ui.GOLD),
                   [ui.embed(title, head + f"ボールが跳ねている… `{RNG.randint(0, 36)}`", ui.GOLD),
                    ui.embed(title, head + f"もうすぐ止まる… `{RNG.randint(0, 36)}`", ui.GOLD)], final,
                   ReplayView(bot, ctx.user_id, bet, lambda it, b: play_roulette(it, bot, b, kind, number)))
    except Exception:
        ctx.abort()
        raise
    finally:
        ctx.release()
