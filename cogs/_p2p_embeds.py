"""プレイヤー間の市場と競りの埋め込み・表示文の組み立て。"""

import discord

from cogs._horse_views import stat_lines
from core import p2p, ui
from core import shop as shop_mod


def when(ts: float) -> str:
    return f"<t:{int(ts)}:R>（<t:{int(ts)}:t>）"


def price_text(r: dict) -> str:
    if r["mode"] == "fixed":
        return f"定価 **{ui.pelica(r['price'])}**"
    now = f"現在 **{ui.pelica(r['bid'])}**" if r["bid"] else f"開始 **{ui.pelica(r['price'])}**"
    return f"競り {now}"


def line_of(eco, r: dict) -> str:
    return f"`#{r['id']}` {eco.p2p_label(r)}　{price_text(r)}　{'締切' if r['mode'] == 'auction' else '期限'} {when(r['end_at'])}"


def entry_text(eco, r: dict) -> str:
    """一覧の1件（2行）。何を・いくらで・どの方式で・誰が・いつまでか。競りは、入札の有無も。"""
    if r["mode"] == "fixed":
        how = f"💵 定価 **{ui.pelica(r['price'])}**"
    elif r["bid"]:
        how = f"🔨 競り 現在 **{ui.pelica(r['bid'])}**（入札あり）"
    else:
        how = f"🔨 競り 開始 **{ui.pelica(r['price'])}**（入札なし）"
    left = "締切" if r["mode"] == "auction" else "期限"
    return f"`#{r['id']}` **{eco.p2p_label(r)}**　{how}\n　└ 出品 <@{r['seller_id']}>　{left} {when(r['end_at'])}"


def detail_embed(eco, r: dict) -> discord.Embed:
    lines = [
        f"出品者: <@{r['seller_id']}>",
        price_text(r),
        f"{'締切' if r['mode'] == 'auction' else '期限'}: {when(r['end_at'])}",
    ]
    if r["mode"] == "auction":
        need = p2p.min_next_bid(r["price"], r["bid"])
        lines.append(
            f"最高入札: {'<@' + str(r['bidder_id']) + '>' if r['bidder_id'] else 'まだありません'}　次の入札は **{ui.pelica(need)}** 以上"
        )
        lines.append(
            f"（締切の{p2p.SNIPE_SECONDS // 60}分前を切ってからの入札は、締切が{p2p.SNIPE_SECONDS // 60}分延びます）"
        )
    if r["kind"] == "horse":
        horse = eco.horse_get(int(r["ref"]))
        if horse:
            lines += [
                "",
                stat_lines(horse),
                f"戦績 {horse['races']}戦{horse['wins']}勝（3着以内 {horse['places']}）　賞金 {ui.pelica(horse['earnings'])}",
            ]
    else:
        item = shop_mod.BY_ID.get(r["ref"])
        if item and item.desc:
            lines += ["", item.desc]
    lines.append(f"\n売れると、売り手の受け取りから {p2p.FEE_RATE:.0%} を引きます。")
    return ui.embed(f"🤝 {eco.p2p_label(r)}", "\n".join(lines), ui.INFO)


ENDED_TEXT = {
    "sold": ("✅ 売れました", "{label} は、<@{buyer}> が **{price}** で買いました。"),
    "expired": ("⏳ 終了（売れ残り）", "{label} は、買い手がつかず、終了しました。"),
    "cancelled": ("🚫 取り消されました", "{label} の出品は、取り消されました。"),
}


def post_embed(eco, r: dict) -> discord.Embed:
    """告知の投稿。出ている間は、値段・締切・最高入札。終わったら、結果（売れた値段など）に書き換わる。"""
    label = eco.p2p_label(r)
    if r["status"] != "active":
        title, text = ENDED_TEXT.get(r["status"], ENDED_TEXT["expired"])
        sold = r["status"] == "sold"
        body = text.format(label=label, buyer=r["buyer_id"], price=ui.pelica(r["sold_price"]))
        return ui.embed(f"{title}: {label}", body, ui.OK if sold else ui.INFO)
    e = detail_embed(eco, r)
    e.title = f"📣 出品: {label}"
    e.description = (
        e.description or ""
    ) + "\n\n下の「🔍 見る・買う・入札」で、買えます（`/ショップ` → 「🕴️ 帝愛の競売」からも）。"
    return e
