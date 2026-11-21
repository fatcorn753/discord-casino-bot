"""週間ランキングの発表の画面。"""

import discord

from cogs.shared import ui
from cogs.shared.messages import format_standings_lines
from domain import shop, weekly
from domain.ports.contracts import WeeklyAward

KING = shop.BY_ID[shop.WEEKLY_KING]


def weekly_embed(week: str, result: WeeklyAward) -> discord.Embed:
    lines = format_standings_lines(result["standings"], ui.MEDALS)
    champ = result["champion"]
    if champ:
        lines += [
            "",
            f"👑 **<@{champ[0]}>** が **{KING.name}** を獲得しました！（通算{result['wins']}回目）",
            f"今週の稼ぎ **{champ[1]:+,}ペリカ**（{champ[2]}戦）。`/ショップ` の「持ち物」の画面で身につけられます（次の発表まで）。",
        ]
    else:
        lines += [
            "",
            f"今週は、{weekly.MIN_PLAYS}ゲーム以上遊んでプラスだった人がいませんでした。王者はお休みです。",
        ]
    if result["previous"]:
        lines.append("前の王者 " + "・".join(f"<@{u}>" for u in result["previous"]) + " の称号は外れました。")
    e = ui.embed(f"🏆 週間ランキング 発表！（{weekly.week_label(week)}）", "\n".join(lines), ui.GOLD)
    e.set_footer(text="毎週月曜0時に発表 ｜ 稼ぎ = その週のゲームの損益の合計")
    return e
