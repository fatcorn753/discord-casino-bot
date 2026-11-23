"""ランキングの表示（`/ペリカ ランキング` と、自動更新のランキング掲示板で共通）。"""

import discord

from cogs.shared import ui
from cogs.shared.texts import PART_LABELS
from domain import teiai
from services.ranking import RankingService

MEDALS = ui.MEDALS
TOP_N = 10


def _groups_text(g: dict[str, int]) -> str:
    """流動資産・固定資産・負債の小計（0 の区分は省く）。内訳を開かなくても、区分が分かるように。"""
    parts = [("流動", g["liquid"]), ("固定", g["fixed"]), ("負債", g["liabilities"])]
    shown = [f"{name} {value:,}" for name, value in parts if value]
    return f"（{'・'.join(shown)}）" if shown else ""


def build_embed(
    repos: RankingService,
    guild_ids: set | None,
    title: str,
    viewer_id: int | None = None,
    header: str = "",
    footer: str | None = None,
    measure: str = "net",
) -> discord.Embed:
    """上位 TOP_N 人のランキングの埋め込みを作る。guild_ids を渡すと、その人たちの中での順位。

    measure は "net"（純資産 = 総資産 − 負債）か "gross"（総資産 = 流動資産 + 固定資産）。
    viewer_id を渡すと、その人が圏外のときに、自分の順位を最後に足す。
    """
    ranked = repos.ranked_assets(measure, guild_ids)
    if not ranked:
        e = ui.embed(title, f"{header}まだ誰も遊んでいません。`/ペリカ デイリー` から始めましょう。", ui.INFO)
    else:
        wealthy = repos.wealthy_ids()  # カタギでない人・富豪は、名前の横に身分の印

        def row(rank: int, uid: int, total: int, g: dict[str, int]) -> str:
            mark = teiai.STATUS_MARK.get(repos.display_status(uid, wealthy), "")
            return f"{MEDALS.get(rank, f'`{rank:>2}`')} <@{uid}>{mark}\u3000**{ui.pelica(total)}**{_groups_text(g)}"

        lines = [row(r, uid, total, g) for r, (uid, total, g) in enumerate(ranked[:TOP_N], 1)]
        if viewer_id:  # 閲覧者が圏外なら、その順位の行も末尾に足す
            me = next(((r, t) for r, t in enumerate(ranked, 1) if t[0] == viewer_id), None)
            if me is not None and me[0] > TOP_N:
                lines.append("…\n" + row(me[0], *me[1]))
        e = ui.embed(title, header + "\n".join(lines), ui.GOLD)
    if footer:
        e.set_footer(text=footer)
    return e


def build_breakdown_embed(repos: RankingService, user_id: int, name: str) -> discord.Embed:
    """その人の総資産の、おおまかな内訳（貸借対照表の区分: 流動資産／固定資産／負債／純資産）。"""
    from domain.types import FIXED_KEYS, LIABILITY_KEYS, LIQUID_KEYS, asset_groups

    found = next(((total, p) for uid, total, p in repos.net_assets() if uid == user_id), None)
    if found is None:
        return ui.embed(f"🧾 {name} の資産の内訳", "まだ口座がありません。", ui.INFO)
    _total, parts = found
    g = asset_groups(parts)

    def section(keys, negate=False):
        return [f"{PART_LABELS[k]}: **{ui.pelica(-parts[k] if negate else parts[k])}**" for k in keys if parts.get(k)]

    lines = ["**💧 流動資産**"] + (section(LIQUID_KEYS) or ["（なし）"])
    lines.append("\n**🏛️ 固定資産**")
    lines += section(FIXED_KEYS) or ["（なし）"]
    lines.append(f"\n**総資産（流動＋固定）: {ui.pelica(g['gross'])}**")
    lines.append("\n**📉 負債**")
    lines += section(LIABILITY_KEYS, negate=True) or ["（なし）"]
    lines.append(f"\n**負債合計: {ui.pelica(g['liabilities'])}**")
    lines.append(f"\n**🧾 純資産（総資産 − 負債）: {ui.pelica(g['net'])}**")
    e = ui.embed(f"🧾 {name} の資産の内訳", "\n".join(lines), ui.GOLD)
    e.set_footer(
        text="着せ替え・コレクションは買値、馬は購入価格＋引退金の見込み、発掘品は目安の価値、地下作業の工具・寮はこれまでの強化費用で数えます"
    )
    return e
