"""ショップ・競売・持ち物画面のカタログ定数と埋め込み組み立て。"""

import discord

from cogs._p2p_embeds import entry_text
from core import p2p, shop, teiai, ui

MAX_QTY = 10
SHOWN = 25  # 競売の画面で、選択肢に出すプレイヤーの出品の数（Discord の選択肢の上限）

SLOT_OF_KIND = {
    shop.TITLE: "title",
    shop.CARD_COLOR: "card_color",
    shop.CARD_ICON: "card_icon",
    shop.SKIN_DICE: "skin_dice",
    shop.SKIN_TABLE: "skin_table",
}
EFFECT_OF_ITEM = {shop.DAILY_DOUBLE: "daily_boost", shop.INSURANCE: "insurance"}
UNEQUIP_NAMES = {
    "title": "称号",
    "card_color": "カードの色",
    "card_icon": "カードのアイコン",
    "role": "名前の色（ロール）",
    "skin_dice": "演出（チンチロ用）",
    "skin_table": "演出（パチンコ・ブラックジャック用）",
}  # 「外す」で選べるもの


def shop_slots() -> tuple:
    """持ち物の画面で「装備中」を判定する、プロフィールの装備の列（色ロールは Discord 側のロールなので入らない）。"""
    return tuple(dict.fromkeys(SLOT_OF_KIND.values()))


def kind_name(kind: str) -> str:
    return shop.KIND_LABEL[kind].split(" ", 1)[1]


# ショップの売り場（種類をまとめたもの）。画面上部のボタンで切り替える
TABS = {
    "home": ("🏠 トップ", ()),
    "title": ("🏷️ 称号", (shop.TITLE,)),
    "card": ("🎴 カード", (shop.CARD_COLOR, shop.CARD_ICON)),
    "item": ("🎟️ 消耗品", (shop.CONSUMABLE,)),
    "horse": ("🐎 競馬", (shop.RACING,)),
    "market": ("🕴️ 帝愛市", (shop.MARKET,)),
    "style": ("✨ 演出・名前の色", (shop.SKIN_DICE, shop.SKIN_TABLE, shop.COLOR_ROLE)),
    "collection": ("🏆 コレクション", (shop.COLLECTION,)),
}
TAB_NOTE = {
    "title": "名前の横に出る称号です（1つだけ装備できます）。",
    "card": "`/ペリカ 残高` のカードの、色とアイコンです。",
    "item": "使うと減る品です。デイリー2倍券・保険券は「🎒 持ち物」で使い、連続守り券は持っているだけで自動で使われます。栄養ドリンコは、`/地下 作業` の画面で飲みます。",
    "horse": "競走馬を迎える購入券と、ステータスを動かすにんじんです。どちらも `/競馬 厩舎` で使います。馬そのものは、使うと減る品ではありません。",
    "market": "帝愛が、情報を売る市です。決算の裏情報メモ（確度 80%・2割はガセ）が買えます。持ち物には増えず、買うとその場で、本人にだけ見えます。",
    "style": "ゲーム画面の演出と、名前の色（ロール）です。勝率と配当は変わりません。",
    "collection": "持っているだけで、`/ペリカ 残高` に飾られる自慢の品です。",
}
TIERS = (
    (10_000, "🌱 入門"),
    (100_000, "🥉 中堅"),
    (1_000_000, "🥈 大物"),
    (None, "🥇 頂点"),
)  # 値段の段階（称号・コレクション）
SECTION_NAME = {
    shop.CARD_COLOR: "🎨 色",
    shop.CARD_ICON: "🖼️ アイコン",
    shop.CONSUMABLE: "品ぞろえ（安い順）",
    shop.RACING: "品ぞろえ（安い順）",
    shop.SKIN_DICE: "🎲 チンチロの演出",
    shop.SKIN_TABLE: "🃏 パチンコ・ブラックジャックの演出",
    shop.COLOR_ROLE: "🌈 名前の色（ロール）",
}

P2P_FILTERS = {  # 絞り込み: 名前 → (種類, 方式)
    "all": ("すべて", None, None),
    "horse": ("🐴 馬", "horse", None),
    "item": ("📦 品物", "item", None),
    "auction": ("🔨 競りだけ", None, "auction"),
}
P2P_ORDERS = {"end": "締切が近い順", "price_asc": "安い順", "price_desc": "高い順", "new": "新しい順"}


def stock_text(stock: dict) -> str:
    """在庫の表示（1人ずつ別々）。例: 在庫 3/10（次の補充 <t:…:R>）。"""
    text = f"在庫 {stock['stock']}/{stock['max']}"
    return text + (f"（次の補充 <t:{int(stock['next_at'])}:R>）" if stock["next_at"] else "")


def item_line(item, inv: dict, stock: dict = None) -> str:
    """売り場の1行: 持っていれば ✅、値段をそろえて、名前。説明があれば、次の行に。在庫のある品は、このプレイヤーの在庫も。"""
    icon = item.value if item.kind in (shop.CARD_ICON, shop.COLLECTION) else item.mark
    mark = "✅" if item.unique and inv.get(item.id, 0) > 0 else "▫️"
    line = f"{mark} `{item.price:>10,}`　{icon}{item.name}"
    if item.kind in (shop.CONSUMABLE, shop.RACING) and inv.get(item.id, 0) > 0:
        line += f"　（持っている: {inv[item.id]}）"
    line += f"\n　　📦 {stock_text(stock)}" if stock else ""
    return line + (f"\n　　└ {item.desc}" if item.desc else "")


def tab_items(tab: str) -> list:
    """売り場の商品を、値段の安い順に（限定品は出さない）。"""
    items = [i for k in TABS[tab][1] for i in shop.of_kind(k)]
    return sorted(items, key=lambda i: i.price)  # 同じ値段は、カタログの順のまま


def catalog_embed(eco, user_id: int, tab: str = "home") -> discord.Embed:
    """売り場の画面。トップは、売り場の案内だけ。各売り場は、値段の安い順に、持っている品に ✅ を付けて並べる。"""
    inv = eco.inventory(user_id)
    me = eco.peek_stats(user_id)
    balance = me["balance"] if me else 0
    if tab == "home":
        e = ui.embed(
            "🛍️ ペリカショップ",
            "売り場を、上のボタンで選んでください。景品は、ゲーム内・サーバー内のものだけです（現実の価値はありません）。",
            ui.GOLD,
        )
        lines = []
        for key in ("title", "card", "item", "horse", "market", "style", "collection"):
            items = tab_items(key)
            lines.append(
                f"**{TABS[key][0]}**　{len(items)}種　{items[0].price:,}〜{items[-1].price:,}ペリカ\n　└ {TAB_NOTE[key]}"
            )
        e.add_field(name="売り場", value="\n".join(lines)[:1024], inline=False)
        waiting = len(eco.auction_list()) + len(eco.auction_horses())
        e.add_field(
            name="🕴️ 帝愛の競売",
            value="破産で差し押さえた品が、定価の9割から安く出ます。"
            + (f"（いま **{waiting}種** 出品中）" if waiting else ""),
            inline=False,
        )
        e.add_field(name="🎒 持ち物", value="買った品を身につける・使うのは、こちらです。", inline=False)
    else:
        e = ui.embed(f"{TABS[tab][0]}", TAB_NOTE[tab] + "\n価格はペリカ。✅ は持っている品です。", ui.GOLD)
        items = tab_items(tab)
        stocks = {i.id: eco.shop_stock(user_id, i.id) for i in items if i.id in shop.STOCK_RULES}  # 1人ずつ別々の在庫
        if tab in ("title", "collection"):  # 値段の段階ごとに、まとめる
            low = 0
            for limit, name in TIERS:
                group = [i for i in items if i.price >= low and (limit is None or i.price < limit)]
                if group:
                    e.add_field(
                        name=name,
                        value="\n".join(item_line(i, inv, stocks.get(i.id)) for i in group)[:1024],
                        inline=False,
                    )
                low = limit if limit is not None else low
        else:
            for kind in TABS[tab][1]:
                group = [i for i in items if i.kind == kind]
                if kind == shop.COLOR_ROLE and group:
                    owned = [i.name for i in group if inv.get(i.id, 0) > 0]
                    value = f"{'・'.join(i.mark + i.name for i in group)}\n**各 {group[0].price:,}**" + (
                        f"\n✅ 持っている: {'・'.join(owned)}" if owned else ""
                    )
                else:
                    value = "\n".join(item_line(i, inv, stocks.get(i.id)) for i in group)
                if group:
                    e.add_field(name=SECTION_NAME[kind], value=value[:1024], inline=False)
    e.set_footer(text=f"あなたの残高: {ui.pelica(balance)}")
    return e


def auction_embed(
    eco, user_id: int, tab: str = "teiai", p2p_filter: str = "all", p2p_order: str = "end"
) -> discord.Embed:
    """帝愛の競売の画面。tab は、プレイヤーの出品（players）か、帝愛の棚（teiai。破産で差し押さえた品。値段は日ごとに下がる）。"""
    me = eco.peek_stats(user_id)
    footer = f"あなたの残高: {ui.pelica(me['balance'] if me else 0)}"
    if tab == "players":
        label, kind, mode = P2P_FILTERS[p2p_filter]
        rows = eco.p2p_active(kind, limit=SHOWN, mode=mode, order=p2p_order)
        e = ui.embed(
            "🤝 帝愛の競売 — プレイヤーの出品",
            "プレイヤーが出した品・馬です。下の選択肢で出品を選ぶと、**買う**（定価）・**入札する**（競り）ができます。"
            f"自分の品・馬は「📤 出品する」で出せます（売れると{p2p.FEE_RATE:.0%}の手数料）。\n"
            f"絞り込み: **{label}**　並び順: **{P2P_ORDERS[p2p_order]}**　（{len(rows)}件）",
            ui.GOLD,
        )
        if rows:
            shown = rows[:8]
            e.add_field(
                name="出品（上から8件。残りは、下の選択肢に出ます）" if len(rows) > 8 else "出品",
                value="\n".join(entry_text(eco, r) for r in shown)[:1024],
                inline=False,
            )
        else:
            e.add_field(
                name="出品",
                value="いまは、出ている品・馬はありません。「📤 出品する」で、最初の出品者になれます。",
                inline=False,
            )
        e.set_footer(text=footer)
        return e
    listed = eco.auction_list()
    e = ui.embed(
        "🕴️ 帝愛の競売 — 帝愛の棚",
        "破産で差し押さえた品を、帝愛が売りに出します。**買えるのはカタギだけ**。"
        f"値段は定価の{teiai.AUCTION_START:.0%}から、1日ごとに{teiai.AUCTION_STEP:.0%}ずつ下がり、{teiai.AUCTION_FLOOR:.0%}で止まります。\n"
        "売上は、帝愛銀行の収入（預金者の利回り）になります。",
        ui.GOLD,
    )
    horses = eco.auction_horses()
    if not listed and not horses:
        e.add_field(name="棚", value="いまは、売りに出ている品はありません。", inline=False)
    else:
        if listed:
            e.add_field(
                name="棚",
                value="\n".join(
                    f"{shop.label(shop.BY_ID[x['item_id']])} — **{ui.pelica(x['price'])}**（定価の{x['rate']:.0%}）"
                    + (f" ×{x['qty']}" if not shop.BY_ID[x["item_id"]].unique else "")
                    for x in listed
                )[:1024],
                inline=False,
            )
        if horses:
            e.add_field(
                name="🐴 競走馬（名前とステータスは、そのまま引き継ぎます）",
                value="\n".join(
                    f"{h['silks']} **{h['name']}**（{h['coat']}）— **{ui.pelica(h['price'])}**（定価の{h['rate']:.0%}）"
                    f"　速{h['speed']} ス{h['stamina']} パ{h['power']} 根{h['guts']}　{h['races']}戦{h['wins']}勝"
                    for h in horses
                )[:1024],
                inline=False,
            )
    e.set_footer(text=footer)
    return e


def inventory_embed(eco, user_id: int) -> discord.Embed:
    """持ち物と、装備中のもの、使用中の効果。何も持っていなければ、その案内。"""
    inv = eco.inventory(user_id)
    prof = eco.get_profile(user_id)
    if not inv and not prof["daily_boost"] and not prof["insurance"]:
        return ui.embed("🎒 持ち物", "まだ何も持っていません。「🛍️ ショップ」の画面で、景品を買えます。", ui.INFO)
    equipped = {prof[slot] for slot in shop_slots()}
    e = ui.embed(
        "🎒 持ち物",
        "下の選択肢から、身につける・使う品を選びます。【装備中】は、いま身につけている品です。",
        ui.INFO,
    )
    for k in shop.KIND_LABEL:
        lines = []
        for it in shop.of_kind(k, include_limited=True):
            if it.id in inv:
                mark = " 【装備中】" if it.id in equipped else ""
                lines.append(
                    "・"
                    + f"{it.value if k in (shop.CARD_ICON, shop.COLLECTION, shop.RELIC) else ''}{it.name}"
                    + (f" ×{inv[it.id]}" if not it.unique else "")
                    + mark
                )
        if lines:
            e.add_field(name=shop.KIND_LABEL[k], value="\n".join(lines)[:1024], inline=False)
    active = []
    if prof["daily_boost"]:
        active.append(f"デイリー2倍券（次のデイリーが2倍）×{prof['daily_boost']}")
    if prof["insurance"]:
        active.append(f"保険券（次に負けたとき半額返金）×{prof['insurance']}")
    if active:
        e.add_field(name="⏳ 使用中の効果", value="\n".join(active), inline=False)
    return e
