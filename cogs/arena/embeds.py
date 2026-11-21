"""地下闘技場の掲示（出場表・結果）を組み立てる。"""

import datetime

import discord

from cogs.shared import ui
from domain import arena as arena_mod
from domain.ports.contracts import ArenaFinalResult, ArenaFighterRow, ArenaSemiResult, ArenaTournamentDetailRow

FORMAT_NAME = {"duel": "🥊 1on1", "royale": "⚔️ バトルロイヤル"}
STAGE_LABEL = {
    "semi_betting": "準決勝・受付中",
    "semi_done": "準決勝・結果待ち",
    "final_betting": "決勝・受付中",
    "done": "終了",
}


def when(ts: float, now: float) -> str:
    if now >= ts:
        return f"<t:{int(ts)}:t>"
    return f"<t:{int(ts)}:t>（<t:{int(ts)}:R>）"


BETTORS_SHOWN = 5  # 1戦士ごとに表示する、賭け額が多い順の人数（それ以上は人数だけ出す）


def bettor_text(uid: int, amount: int) -> str:
    who = "観客" if uid == arena_mod.NPC_SPECTATOR else f"<@{uid}>"
    return f"{who} {ui.pelica(amount)}"


def fighter_line(f: ArenaFighterRow, bettors: list, odds: float) -> str:
    bonus = arena_mod.weapon_bonus(f["bet_total"])
    mark = {"semi": "⏳", "final": "🏅", "out": "💀"}[f["stage"]]
    weapon = f" 🗡️+{bonus:.0%}" if bonus > 0 else ""
    line = (
        f"{mark} **{f['name']}**　強さ {f['base_power']}{weapon}　単勝 {odds:.1f}倍　"
        f"賭け総額 {ui.pelica(f['bet_total'])}"
    )
    if bettors:
        shown = "／".join(bettor_text(b["user_id"], b["amount"]) for b in bettors[:BETTORS_SHOWN])
        rest = len(bettors) - BETTORS_SHOWN
        line += f"\n　└ {shown}" + (f"　他{rest}人" if rest > 0 else "")
    return line


def hp_bar(hp: int, width: int = 10) -> str:
    filled = round(width * max(0, min(100, hp)) / 100)
    return "🟥" * filled + "⬛" * (width - filled)


def battle_embed(fighters: list[ArenaFighterRow], hp: list, title: str) -> discord.Embed:
    """試合演出（HPバー）の1コマ。勝敗はすでに決まっており、見た目の攻防だけを表示する。"""
    lines = [f"**{f['name']}**　{hp_bar(h)} {h}%" for f, h in zip(fighters, hp)]
    return ui.embed(title, "\n".join(lines), ui.GOLD)


def _schedule_line(t: ArenaTournamentDetailRow, semi_hour: int, final_hour: int, now: float, tz_hours: float) -> str:
    today = datetime.date.fromisoformat(t["day"])
    tz = datetime.timezone(datetime.timedelta(hours=tz_hours))
    semi_at = datetime.datetime.combine(today, datetime.time(semi_hour), tzinfo=tz).timestamp()
    final_at = datetime.datetime.combine(today, datetime.time(final_hour), tzinfo=tz).timestamp()
    if t["status"] == "semi_betting":
        return f"準決勝: {when(semi_at, now)}"
    if t["status"] == "final_betting":
        return f"決勝: {when(final_at, now)}"
    return ""


def tournament_embed(
    t: ArenaTournamentDetailRow, semi_hour: int, final_hour: int, breakdown: dict, now: float, tz_hours: float
) -> discord.Embed:
    fmt_label = FORMAT_NAME.get(t["format"], t["format"])
    status_label = STAGE_LABEL.get(t["status"], t["status"])
    schedule = _schedule_line(t, semi_hour, final_hour, now, tz_hours)
    lines = [f"今日の形式: {fmt_label}　状態: **{status_label}**" + (f"　{schedule}" if schedule else ""), ""]
    groups: dict[int, list[ArenaFighterRow]] = {}
    finalists: list[ArenaFighterRow] = []
    for f in t["fighters"]:
        if f["stage"] == "final":
            finalists.append(f)
        else:
            groups.setdefault(f["group_no"], []).append(f)
    for group_no in sorted(groups):
        lines.append(f"__準決勝 第{group_no}組__")
        odds = arena_mod.match_odds(groups[group_no])
        lines.extend(fighter_line(f, breakdown.get(f["id"], []), odds[f["name"]]) for f in groups[group_no])
        lines.append("")
    if finalists or t["status"] in ("final_betting", "done"):
        lines.append("__決勝__")
        if finalists:
            odds = arena_mod.match_odds(finalists, boost=arena_mod.FINAL_ODDS_BOOST)  # 精算と同じ決勝のオッズ
            lines.extend(fighter_line(f, breakdown.get(f["id"], []), odds[f["name"]]) for f in finalists)
        else:
            lines.append("（準決勝の結果で決まります）")
    title = "🏛️ 地下闘技場"
    return ui.embed(title, "\n".join(lines), ui.GOLD)


def semis_result_embed(result: ArenaSemiResult) -> discord.Embed:
    """準決勝の決着（組ごとの勝者と、決勝の顔ぶれ）。"""
    lines = [f"第{g}組: **{r['winner']}** が勝利（敗: {'・'.join(r['losers'])}）" for g, r in result["groups"].items()]
    return ui.embed(
        "🏛️ 準決勝、決着！",
        "\n".join(lines) + f"\n\n決勝は **{' vs '.join(result['finalists'])}**",
        ui.GOLD,
    )


def final_result_embed(result: ArenaFinalResult) -> discord.Embed:
    return ui.embed(
        "👑 決勝、決着！",
        f"優勝: **{result['champion']}**（他: {'・'.join(result['runner_up'])}）",
        ui.GOLD,
    )
