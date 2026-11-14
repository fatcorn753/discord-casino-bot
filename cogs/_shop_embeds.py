"""ショップ・競売・持ち物画面のカタログ定数と埋め込み組み立て。"""

import discord

from cogs._p2p_embeds import entry_text
from core import catalogs, eco_types, p2p, shop, teiai, ui

_SHOP_CFG = catalogs.shop()
_SHOP_UI = _SHOP_CFG.ui

MAX_QTY = 10
SHOWN = 25  # 競売の画面で、選択肢に出すプレイヤーの出品の数（Discord の選択肢の上限）

SLOT_OF_KIND = dict(_SHOP_UI.slot_of_kind)
EFFECT_OF_ITEM = dict(eco_types.CONSUMABLE_EFFECTS)
UNEQUIP_NAMES = dict(_SHOP_UI.unequip_names)  # 「外す」で選べるもの


def shop_slots() -> tuple:
    """持ち物の画面で「装備中」を判定する、プロフィールの装備の列（色ロールは Discord 側のロールなので入らない）。"""
    return tuple(dict.fromkeys(SLOT_OF_KIND.values()))


def kind_name(kind: str) -> str:
    return shop.KIND_LABEL[kind].split(" ", 1)[1]


# ショップの売り場（種類をまとめたもの）。画面上部のボタンで切り替える
TABS = {k: (v.label, tuple(v.kinds)) for k, v in _SHOP_UI.tabs.items()}
TAB_NOTE = dict(_SHOP_UI.tab_notes)
TIERS = tuple((t.max_price, t.label) for t in _SHOP_UI.tiers)  # 値段の段階（称号・コレクション）
SECTION_NAME = dict(_SHOP_UI.section_names)

P2P_FILTERS = {  # 絞り込み: 名前 → (種類, 方式)
    k: (v.label, v.kind, v.mode) for k, v in _SHOP_UI.p2p_filters.items()
}
P2P_ORDERS = dict(_SHOP_UI.p2p_orders)


def stock_text(stock: dict) -> str:
    """在庫の表示（1人ずつ別々）。例: 在庫 3/10（次の補充 <t:…:R>）。"""
    text = f"在庫 {stock['stock']}/{stock['max']}"
    return text + (f"（次の補充 <t:{int(stock['next_at'])}:R>）" if stock["next_at"] else "")


def item_line(item, inv: dict, stock: dict | None = None) -> str:
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


def catalog_embed(
    inv: dict[str, int],
    balance: int,
    tab: str = "home",
    waiting: int = 0,
    stocks: dict[str, dict] | None = None,
) -> discord.Embed:
    """売り場の画面。トップは、売り場の案内だけ。各売り場は、値段の安い順に、持っている品に ✅ を付けて並べる。"""
    stocks = stocks or {}
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
    balance: int,
    tab: str = "teiai",
    p2p_filter: str = "all",
    p2p_order: str = "end",
    p2p_rows: list[tuple[dict, str]] | tuple[tuple[dict, str], ...] = (),
    listed: list[dict] | tuple[dict, ...] = (),
    horses: list[dict] | tuple[dict, ...] = (),
) -> discord.Embed:
    """帝愛の競売の画面。tab は、プレイヤーの出品（players）か、帝愛の棚（teiai。破産で差し押さえた品。値段は日ごとに下がる）。"""
    footer = f"あなたの残高: {ui.pelica(balance)}"
    if tab == "players":
        label, _kind, _mode = P2P_FILTERS[p2p_filter]
        e = ui.embed(
            "🤝 帝愛の競売 — プレイヤーの出品",
            "プレイヤーが出した品・馬です。下の選択肢で出品を選ぶと、**買う**（定価）・**入札する**（競り）ができます。"
            f"自分の品・馬は「📤 出品する」で出せます（売れると{p2p.FEE_RATE:.0%}の手数料）。\n"
            f"絞り込み: **{label}**　並び順: **{P2P_ORDERS[p2p_order]}**　（{len(p2p_rows)}件）",
            ui.GOLD,
        )
        if p2p_rows:
            shown = p2p_rows[:8]
            e.add_field(
                name="出品（上から8件。残りは、下の選択肢に出ます）" if len(p2p_rows) > 8 else "出品",
                value="\n".join(entry_text(r, item_label) for r, item_label in shown)[:1024],
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
    e = ui.embed(
        "🕴️ 帝愛の競売 — 帝愛の棚",
        "破産で差し押さえた品を、帝愛が売りに出します。**買えるのはカタギだけ**。"
        f"値段は定価の{teiai.AUCTION_START:.0%}から、1日ごとに{teiai.AUCTION_STEP:.0%}ずつ下がり、{teiai.AUCTION_FLOOR:.0%}で止まります。\n"
        "売上は、帝愛銀行の収入（預金者の利回り）になります。",
        ui.GOLD,
    )
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


def inventory_embed(inv: dict[str, int], prof: dict) -> discord.Embed:
    """持ち物と、装備中のもの、使用中の効果。何も持っていなければ、その案内。"""
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
