"""ランキングの表示（`/ペリカ ランキング` と、自動更新のランキング掲示板で共通）。"""

from typing import Optional

import discord

from core import catalogs, teiai, ui

MEDALS = dict(catalogs.economy().wallet.medals)
TOP_N = 10


def build_embed(
    eco,
    guild_ids: Optional[set],
    title: str,
    viewer_id: Optional[int] = None,
    header: str = "",
    footer: Optional[str] = None,
) -> discord.Embed:
    """上位 TOP_N 人のランキングの埋め込みを作る。guild_ids を渡すと、その人たちの中での順位。

    viewer_id を渡すと、その人が圏外のときに、自分の順位を最後に足す。
    """
    top = eco.top(TOP_N, guild_ids)
    if not top:
        e = ui.embed(title, f"{header}まだ誰も遊んでいません。`/ペリカ デイリー` から始めましょう。", ui.INFO)
    else:
        crypto, bonds = eco.crypto_values(), eco.bond_values()

        def worth(uid, total):
            parts = []
            if crypto.get(uid, 0):
                parts.append(f"CS {crypto[uid]:,}")
            if bonds.get(uid, 0):
                parts.append(f"債券 {bonds[uid]:,}")
            return f"**{ui.pelica(total)}**" + (f"（うち{'・'.join(parts)}）" if parts else "")

        wealthy = eco.wealthy_ids()  # カタギでない人・富豪は、名前の横に身分の印
        lines = [
            f"{MEDALS.get(r, f'`{r:>2}`')} <@{uid}>{teiai.STATUS_MARK.get(eco.display_status(uid, wealthy), '')}　{worth(uid, bal)}"
            for r, uid, bal in top
        ]
        me = eco.rank_of(viewer_id, guild_ids) if viewer_id else None
        if me and me[0] > TOP_N:
            lines.append(f"…\n`{me[0]:>2}` <@{viewer_id}>　{worth(viewer_id, me[1])}")
        e = ui.embed(title, header + "\n".join(lines), ui.GOLD)
    if footer:
        e.set_footer(text=footer)
    return e


def build_final_embed(
    eco, guild_ids: Optional[set], title: str, viewer_id: Optional[int] = None, footer: Optional[str] = None
) -> discord.Embed:
    """総資産（馬・持ち物・発掘品なども、ペリカに換算しての総額）の上位 TOP_N 人。"""
    ranked = eco.final_assets(guild_ids)
    if not ranked:
        e = ui.embed(title, "まだ誰も遊んでいません。`/ペリカ デイリー` から始めましょう。", ui.INFO)
    else:
        wealthy = eco.wealthy_ids()
        lines = [
            f"{MEDALS.get(r, f'`{r:>2}`')} <@{uid}>{teiai.STATUS_MARK.get(eco.display_status(uid, wealthy), '')}"
            f"\u3000**{ui.pelica(total)}**"
            for r, (uid, total, _p) in enumerate(ranked[:TOP_N], 1)
        ]
        if viewer_id:
            for r, (uid, total, _p) in enumerate(ranked, 1):
                if uid == viewer_id and r > TOP_N:
                    lines.append(f"…\n`{r:>2}` <@{viewer_id}>\u3000**{ui.pelica(total)}**")
        e = ui.embed(title, "\n".join(lines), ui.GOLD)
    if footer:
        e.set_footer(text=footer)
    return e


def build_breakdown_embed(eco, user_id: int, name: str) -> discord.Embed:
    """その人の総資産の、おおまかな内訳（貸借対照表の区分: 流動資産／固定資産／負債／純資産）。"""
    from core import eco_assets
    from core.eco_assets import FIXED_KEYS, LIABILITY_KEYS, LIQUID_KEYS, PART_LABELS

    found = next(((total, p) for uid, total, p in eco.final_assets() if uid == user_id), None)
    if found is None:
        return ui.embed(f"🧾 {name} の資産の内訳", "まだ口座がありません。", ui.INFO)
    _total, parts = found
    g = eco_assets.AssetMixin.asset_groups(parts)

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
