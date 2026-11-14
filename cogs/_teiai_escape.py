"""海外逃亡チャンス！！: 船の ASCII アートが進んで、数秒後に「成功！」か「失敗！」が出る（cogs/_teiai_panel.py から使う）。

当たり外れは、押した時点で core/eco_debt.py の escape_attempt が決める。ここは見せ方だけ（読み込まれる Cog ではない）。
"""

import asyncio

import discord

from core import catalogs, teiai, ui
from core.confirm import confirm_action

_CFG = catalogs.teiai()
FRAME_SECONDS = _CFG.escape_frame_seconds
SHIP = list(_CFG.escape_ship)
WAVES = list(_CFG.escape_waves)
PROGRESS = list(_CFG.escape_progress)


def ship_frame(step: int) -> str:
    indent = " " * (1 + step * 2)
    lines = [indent + row for row in SHIP]
    return "\n".join(lines + [WAVES[step % 2], WAVES[(step + 1) % 2]])


SUCCESS_ART = _CFG.escape_success_art
FAIL_ART = _CFG.escape_fail_art


def block(text: str) -> str:
    return f"```\n{text.strip(chr(10))}\n```"


async def run_escape(interaction: discord.Interaction, eco, after_success) -> None:
    """確認 → 当たり外れを決める → 船のアニメーション → 結果。公開メッセージで見せる。"""
    uid = interaction.user.id
    debt = eco.debt(uid)
    ok = await confirm_action(
        interaction,
        "海外逃亡を試しますか？",
        f"成功率は **{teiai.ESCAPE_CHANCE:.0%}**。チャンスは通算 **{teiai.ESCAPE_MAX_TRIES}回** までで、失敗も1回に数えます"
        f"（あと **{eco.escape_left(uid)}回**）。\n"
        "・**成功**: 全財産とショップの全品を**置いていき**、帝愛の借金が**全額ゼロ**になります"
        f"（貸付は免責・貸付は{teiai.BLACKLIST_DAYS}日のブラックリスト）。\n"
        f"・**失敗**: 罰金（借金の**{teiai.ESCAPE_FINE_RATE:.0%}**・最低{teiai.ESCAPE_FINE_MIN:,}ペリカ）が借金に加わり、"
        f"**{teiai.ESCAPE_COOLDOWN_HOURS}時間**は、もう試せません。\n"
        f"いまの借金: {ui.pelica(debt)}",
        "逃亡する！",
    )
    if ok is not True:
        return
    done, result = eco.escape_attempt(uid)
    if not done:
        return await interaction.followup.send(embed=ui.error_embed("逃亡できません", result), ephemeral=True)
    name = interaction.user.display_name
    title = f"🚢 {name} が、海外逃亡を試みる！！"
    message = await interaction.followup.send(
        embed=ui.embed(title, f"{PROGRESS[0]}\n{block(ship_frame(0))}", ui.INFO), wait=True
    )
    for step in range(1, len(PROGRESS)):
        await asyncio.sleep(FRAME_SECONDS)
        await message.edit(embed=ui.embed(title, f"{PROGRESS[step]}\n{block(ship_frame(step))}", ui.INFO))
    await asyncio.sleep(FRAME_SECONDS)
    if result["escaped"]:
        b = result["bankrupt"]
        text = (
            f"{block(SUCCESS_ART)}\n**成功！！** 帝愛の借金は、**ゼロ**になりました。\n"
            f"置いてきた財産: {ui.pelica(b['seized'])}（帝愛への配当 {ui.pelica(b['senior_paid'])}・"
            f"貸付への配当 {ui.pelica(b['junior_paid'])}・免責になった貸付 {ui.pelica(b['forgiven'])}）"
        )
        embed = ui.embed(f"🎉 {name} の海外逃亡、成功！！", text, ui.OK)
    else:
        text = (
            f"{block(FAIL_ART)}\n**失敗…！** 沿岸警備隊に捕まりました。\n"
            f"罰金 {ui.pelica(result['fine'])} が借金に加わり、借金は {ui.pelica(result['debt'])} になりました。"
            f"次に試せるのは、{teiai.ESCAPE_COOLDOWN_HOURS}時間後です。"
        )
        embed = ui.embed(f"🚨 {name} の海外逃亡、失敗…", text, ui.ERROR)
    await message.edit(embed=embed)
    eco.take_notices(uid)
    if result["escaped"]:
        await after_success(interaction)
