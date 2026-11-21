"""競馬のレースカード・出馬表 Embed・公開ボタン（再起動耐性 DynamicItem）。"""

import datetime
import re
from typing import TYPE_CHECKING, Any, cast

import discord

from cogs.shared import ui
from cogs.shared.bridges import IHorseButtonBridge, NoOpHorseButtonBridge
from domain import horse as horse_mod
from domain import skins
from services.horse import HorseDesk

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot

MEDAL = ui.MEDALS


def record_text(e: dict) -> str:
    """成績（オッズの元）。初出走は、そう書く。"""
    if not e.get("races"):
        return "初出走"
    return f"{e['races']}戦{e['wins']}勝（3着内{e['places']}）"


def stat_short(e: dict) -> str:
    return f"速{e['speed']} ス{e['stamina']} パ{e['power']} 根{e['guts']}"


def race_title(desk: HorseDesk, race: dict) -> str:
    tag = "（臨時）" if race["kind"] == "special" else ("🏆重賞" if desk.is_major_race(race) else "")
    return f"🏇 第{desk.race_number(race['id'])}競走{tag}"


async def refresh_entry_card(
    interaction: discord.Interaction,
    desk: HorseDesk,
    race_id: int,
) -> None:
    """申し込み・取り下げのあと、チャンネルの出馬表（申し込み中）を、いまの申し込みに書き換える。"""
    race = desk.race_get(race_id)
    if race is None or race["status"] != "entry" or not race["message_id"] or not race["channel_id"]:
        return
    channel = interaction.guild.get_channel(race["channel_id"]) if interaction.guild else None
    if channel is None:
        return
    try:
        await (
            cast(discord.TextChannel, channel)
            .get_partial_message(race["message_id"])
            .edit(embed=card_embed(desk, race))
        )
    except discord.HTTPException:  # 消されていても、進行には関係ない
        pass


def house_mark(e: dict, now: datetime.datetime) -> str:
    """名前の前の印。季節演出の期間中は、帝愛の馬（馬主なし）だけに季節の絵を付ける（見た目だけ）。"""
    icon = "" if e["owner_id"] else skins.house_horse_icon(now)
    return f"{icon} " if icon else ""


def runner_icon(e: dict, now: datetime.datetime) -> str:
    """走路を走る絵。帝愛の馬は、季節演出の期間中は季節の絵（例: 🦌）になる。"""
    return ("" if e["owner_id"] else skins.house_horse_icon(now)) or "🐎"


def when(ts: float, now: float) -> str:
    """時刻と「◯分後」。過ぎた時刻は、「◯秒前」が増え続けて紛らわしいので、時刻だけにする。"""
    if now >= ts:
        return f"<t:{int(ts)}:t>"
    return f"<t:{int(ts)}:t>（<t:{int(ts)}:R>）"


def card_embed(desk: HorseDesk, race: dict, closed: bool = False) -> discord.Embed:
    """出馬表。申し込み中は申し込みの馬、発売中は枠・オッズ・調子、終わったら着順。"""
    title = race_title(desk, race)
    now = desk.clock.now_ts()
    today = desk.clock.now()
    entries = desk.race_entries(race["id"])
    if race["status"] == "entry":
        reg = [e for e in entries if e["lane"] < 0]
        if closed:
            lines = [f"出走の申し込みは、<t:{int(race['entry_close'])}:t> に締め切りました。"]
        else:
            lines = [
                f"出走の申し込み: **{when(race['entry_close'], now)}** まで（出走料 {ui.pelica(horse_mod.ENTRY_FEE)}）"
            ]
            lines.append(f"発走: {when(race['start_at'], now)}")
            lines.append("`/競馬 厩舎` から、馬を申し込めます。足りない分は、帝愛の馬が走ります。")
        lines += ["", f"**申し込み {len(reg)} / {horse_mod.FIELD_SIZE}頭**"] + [
            f"{e['silks']} {e['name']}（<@{e['owner_id']}>）" for e in reg
        ]
        return ui.embed(f"{title}　{'受付終了' if closed else '出走受付中'}", "\n".join(lines), ui.INFO)
    track = horse_mod.Track(race["distance"], race["going"])
    if race["status"] == "betting":
        if closed:
            head = [
                f"コース: **{track.label}**",
                f"馬券の発売は、<t:{int(race['bet_close'])}:t> に締め切りました。",
            ]
        else:
            head = [
                f"コース: **{track.label}**　発走: {when(race['start_at'], now)}",
                f"馬券の発売: **{when(race['bet_close'], now)}** まで（単勝=1着・複勝=3着以内。**オッズは成績で決まり、賭けた時点で確定**）",
            ]

        def entry_style(e: dict) -> str:
            return e["style"] or horse_mod.style_of({k: e[k] for k in horse_mod.STAT_KEYS})

        rows = [
            f"`{e['lane']}` {e['silks']} {house_mark(e, today)}**{e['name']}** {horse_mod.FORM_MARK[e['form']]} "
            f"{horse_mod.STYLE_MARK[entry_style(e)]}　"
            f"単勝 **{e['odds_win']:.1f}** 複勝 **{e['odds_place']:.1f}**\n　└ {record_text(e)}　{stat_short(e)}"
            + (f"　馬主 <@{e['owner_id']}>" if e["owner_id"] else "")
            for e in entries
        ]
        e = ui.embed(f"{title}　{'発売終了' if closed else '発売中'}", "\n".join(head + [""] + rows), ui.GOLD)
        e.set_footer(
            text=f"調子 ◎好調 ○普通 △不調　控除 {horse_mod.TAKEOUT:.0%}　スピードは短距離、スタミナは長距離、パワーは重い馬場に効く"
        )
        return e
    order = sorted(entries, key=lambda x: x["finish"])

    def place(x: dict) -> str:
        return MEDAL.get(x["finish"], str(x["finish"]) + "着")

    rows = [
        f"{place(x)} {x['silks']} {house_mark(x, today)}{x['name']}　単勝 {x['odds_win']:.1f}"
        + (f"（<@{x['owner_id']}>）" if x["owner_id"] else "")
        for x in order
    ]
    return ui.embed(f"{title}　結果", f"コース: **{track.label}**\n" + "\n".join(rows), ui.OK)


class RaceButton(discord.ui.DynamicItem[discord.ui.Button], template=r"horse:(?P<act>bet|card):(?P<id>\d+)"):
    LABEL = {
        "bet": ("🎫 馬券を買う", discord.ButtonStyle.success),
        "card": ("📋 出馬表", discord.ButtonStyle.secondary),
    }

    def __init__(self, act: str, race_id: int):
        label, style = self.LABEL[act]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"horse:{act}:{race_id}"))
        self.act, self.race_id = act, race_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str]):
        return cls(match["act"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        bot = cast("CasinoBot", interaction.client)
        bridge = bot.bridges.resolve_or(IHorseButtonBridge, NoOpHorseButtonBridge())
        await bridge.handle_button(interaction, self.act, self.race_id)


def card_view(race_id: int, betting: bool) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(RaceButton("bet" if betting else "card", race_id))
    return view
