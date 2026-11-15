"""ショップ・競売・持ち物画面の静的タブ・見出し・表示ラベル定義（プレゼンテーション静的テキスト層）。"""

from domain import shop

UNEQUIP_NAMES: dict[str, str] = {  # 「外す」で選べるもの
    "title": "称号",
    "card_color": "カードの色",
    "card_icon": "カードのアイコン",
    "role": "名前の色（ロール）",
    "skin_dice": "演出（チンチロ用）",
    "skin_table": "演出（パチンコ・ブラックジャック用）",
}

# ショップの売り場（種類をまとめたもの）。画面上部のボタンで切り替える
TABS: dict[str, tuple[str, tuple[str, ...]]] = {
    "home": ("🏠 トップ", ()),
    "title": ("🏷️ 称号", (shop.TITLE,)),
    "card": ("🎴 カード", (shop.CARD_COLOR, shop.CARD_ICON)),
    "item": ("🎟️ 消耗品", (shop.CONSUMABLE,)),
    "horse": ("🐎 競馬", (shop.RACING,)),
    "market": ("🕴️ 帝愛市", (shop.MARKET,)),
    "style": ("✨ 演出・名前の色", (shop.SKIN_DICE, shop.SKIN_TABLE, shop.COLOR_ROLE)),
    "collection": ("🏆 コレクション", (shop.COLLECTION,)),
}

TAB_NOTE: dict[str, str] = {
    "title": "名前の横に出る称号です（1つだけ装備できます）。",
    "card": "`/ペリカ 残高` のカードの、色とアイコンです。",
    "item": "使うと減る品です。デイリー2倍券・保険券は「🎒 持ち物」で使い、連続守り券は持っているだけで自動で使われます。栄養ドリンコは、`/地下 作業` の画面で飲みます。",
    "horse": "競走馬を迎える購入券と、ステータスを動かすにんじんです。どちらも `/競馬 厩舎` で使います。馬そのものは、使うと減る品ではありません。",
    "market": "帝愛が、情報を売る市です。決算の裏情報メモ（確度 80%・2割はガセ）が買えます。持ち物には増えず、買うとその場で、本人にだけ見えます。",
    "style": "ゲーム画面の演出と、名前の色（ロール）です。勝率と配当は変わりません。",
    "collection": "持っているだけで、`/ペリカ 残高` に飾られる自慢の品です。",
}

TIERS: tuple[tuple[int | None, str], ...] = (  # 値段の段階（称号・コレクション）
    (10_000, "🌱 入門"),
    (100_000, "🥉 中堅"),
    (1_000_000, "🥈 大物"),
    (None, "🥇 頂点"),
)

SECTION_NAME: dict[str, str] = {
    shop.CARD_COLOR: "🎨 色",
    shop.CARD_ICON: "🖼️ アイコン",
    shop.CONSUMABLE: "品ぞろえ（安い順）",
    shop.RACING: "品ぞろえ（安い順）",
    shop.SKIN_DICE: "🎲 チンチロの演出",
    shop.SKIN_TABLE: "🃏 パチンコ・ブラックジャックの演出",
    shop.COLOR_ROLE: "🌈 名前の色（ロール）",
}

P2P_FILTERS: dict[str, tuple[str, str | None, str | None]] = {  # 絞り込み: キー → (ラベル, 種類, 方式)
    "all": ("すべて", None, None),
    "horse": ("🐴 馬", "horse", None),
    "item": ("📦 品物", "item", None),
    "auction": ("🔨 競りだけ", None, "auction"),
}

P2P_ORDERS: dict[str, str] = {
    "end": "締切が近い順",
    "price_asc": "安い順",
    "price_desc": "高い順",
    "new": "新しい順",
}
