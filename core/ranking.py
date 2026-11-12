"""ランキングの表示（`/ペリカ ランキング` と、自動更新のランキング掲示板で共通）。"""

from typing import Optional

import discord

from core import teiai, ui

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
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
