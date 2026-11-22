"""抽選の募集・結果・中止の画面。"""

import discord

from cogs.shared import ui
from domain.ports import get_catalogs
from domain.ports.contracts import RaffleResult, RaffleView

SHOWN_ENTRANTS = get_catalogs().economy().raffle.shown_entrants  # 掲示に並べる参加者の数（それ以上は人数だけ）


def raffle_embed(raffle: RaffleView, now: float | None = None) -> discord.Embed:
    entries = raffle["entries"]
    prize = raffle["fee"] * len(entries)
    shown = " ".join(f"<@{u}>" for u in entries[:SHOWN_ENTRANTS]) + (
        f" ほか{len(entries) - SHOWN_ENTRANTS}人" if len(entries) > SHOWN_ENTRANTS else ""
    )
    lines = [
        f"参加費: **{ui.pelica(raffle['fee'])}**（全員同じ額。押した時点で先に引かれます）",
        f"締切: <t:{int(raffle['closes'])}:R>（<t:{int(raffle['closes'])}:t>）",
        f"参加者: **{len(entries)}人**" + (f"\n{shown}" if entries else ""),
        f"いまの賞金: **{ui.pelica(prize)}**（集めた参加費の全額。当選者は1人）",
        "",
        "参加者が2人に満たないときは、全員に全額を返します。",
    ]
    return ui.embed("🎟️ 抽選 参加者募集中！", "\n".join(lines), ui.GOLD)


def result_embed(raffle: RaffleResult) -> discord.Embed:
    n = len(raffle["entries"])
    if raffle["winner"] is not None:
        lines = [
            f"🎉 当選: <@{raffle['winner']}>",
            f"賞金: **{ui.pelica(raffle['prize'])}**（参加 {n}人 × {ui.pelica(raffle['fee'])}）",
        ]
        return ui.embed("🎟️ 抽選 結果発表！", "\n".join(lines), ui.GOLD)
    if n == 0:
        return ui.embed("🎟️ 抽選 終了", "参加者がいなかったので、抽選は行いませんでした。", ui.INFO)
    return ui.embed(
        "🎟️ 抽選 終了", f"参加者が{n}人だったので、抽選は行わず、全員に{ui.pelica(raffle['fee'])}を返しました。", ui.INFO
    )


def cancelled_embed(raffle: RaffleResult) -> discord.Embed:
    return ui.embed(
        "🎟️ 抽選 中止",
        f"抽選は中止されました。参加した{len(raffle['entries'])}人に参加費を返しました。",
        ui.WARN,
    )
