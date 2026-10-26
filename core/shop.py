"""ショップの商品一覧（カタログ）。景品はすべてゲーム内・サーバー内で完結するもので、現実の価値はない。"""

from dataclasses import dataclass
from typing import Optional

TITLE, CARD_COLOR, CARD_ICON, CONSUMABLE, COLOR_ROLE = "title", "card_color", "card_icon", "consumable", "color_role"

KIND_LABEL = {
    TITLE: "🏷️ 称号",
    CARD_COLOR: "🎨 カードの色",
    CARD_ICON: "🖼️ カードのアイコン",
    CONSUMABLE: "🎟️ 消耗品",
    COLOR_ROLE: "🌈 名前の色（ロール）",
}

# 名前の色ロールの名前の接頭辞。この接頭辞のロールだけを Bot が作って付け外しする
ROLE_PREFIX = "ペリカ・"

DAILY_DOUBLE = "daily_double"
INSURANCE = "insurance"
# 保険券で戻る額の上限。券の値段（2,000）より小さくして、買って使うほど増える（期待値がプラスになる）ことがないようにする
INSURANCE_CAP = 1_000


@dataclass(frozen=True)
class Item:
    id: str
    name: str
    kind: str
    price: int
    value: object = None        # 色(16進)・絵文字など
    desc: str = ""

    @property
    def unique(self) -> bool:
        """一度買えば持ち続けるもの（消耗品以外）。"""
        return self.kind != CONSUMABLE


def _colors(prefix: str, kind: str, price: int, items: list) -> list:
    return [Item(f"{prefix}_{key}", name, kind, price, value) for key, name, value in items]


CATALOG = [
    # 称号: 残高カードとランキングの名前の横に表示される
    Item("title_rookie", "見習いギャンブラー", TITLE, 2_000),
    Item("title_broke", "破産常連", TITLE, 3_000),
    Item("title_regular", "常連", TITLE, 5_000),
    Item("title_zawa", "ざわ…ざわ…", TITLE, 8_000),
    Item("title_underground", "地下の住人", TITLE, 12_000),
    Item("title_lucky", "強運の持ち主", TITLE, 20_000),
    Item("title_emperor", "地下の帝王", TITLE, 100_000),
    # 残高カードの色・アイコン
    *_colors("cardcolor", CARD_COLOR, 3_000, [
        ("red", "赤", 0xE74C3C), ("blue", "青", 0x3498DB), ("green", "緑", 0x2ECC71),
        ("gold", "金", 0xF1C40F), ("purple", "紫", 0x9B59B6), ("dark", "黒", 0x2C2F33)]),
    *_colors("cardicon", CARD_ICON, 3_000, [
        ("slot", "スロット", "🎰"), ("gem", "宝石", "💎"), ("fire", "炎", "🔥"),
        ("moon", "月", "🌙"), ("dragon", "龍", "🐉")]),
    Item("cardicon_crown", "王冠", CARD_ICON, 5_000, "👑"),
    # 消耗品（使うとなくなる）
    Item(DAILY_DOUBLE, "デイリー2倍券", CONSUMABLE, 3_000, desc="次に受け取るデイリーボーナスが2倍になる"),
    Item(INSURANCE, "保険券", CONSUMABLE, 2_000, desc=f"次に負けたゲーム1回で、賭け金の半分が戻る（最大{INSURANCE_CAP:,}ペリカ。倍払いの追加分は対象外）"),
    # サーバー内の名前の色（Bot がロールを作って付ける）。0x000000 は「色なし」扱いになるため、黒・白は少しずらす
    *_colors("role", COLOR_ROLE, 20_000, [
        ("red", "赤", 0xE74C3C), ("orange", "橙", 0xE67E22), ("yellow", "黄", 0xF1C40F),
        ("green", "緑", 0x2ECC71), ("cyan", "水色", 0x1ABC9C), ("blue", "青", 0x3498DB),
        ("purple", "紫", 0x9B59B6), ("pink", "桃", 0xFF69B4), ("gold", "金", 0xD4AF37),
        ("silver", "銀", 0xBDC3C7), ("black", "黒", 0x010101), ("white", "白", 0xFEFEFE)]),
]

BY_ID = {i.id: i for i in CATALOG}
BY_NAME = {}
for _i in CATALOG:
    # 同じ名前（例: 「赤」）は種類ごとに別物なので、「種類名」つきでも引けるようにする
    BY_NAME.setdefault(_i.name, _i)
    BY_NAME[f"{KIND_LABEL[_i.kind].split(' ', 1)[1]}:{_i.name}"] = _i


def find(key: str) -> Optional[Item]:
    """ID または名前から商品を探す。"""
    key = (key or "").strip()
    return BY_ID.get(key) or BY_NAME.get(key)


def find_all(key: str) -> list:
    """ID・名前・「種類:名前」・表示名のどれかに合う商品をすべて返す（「赤」のように同名が複数ある場合のため）。"""
    key = (key or "").strip()
    return [i for i in CATALOG if key in (i.id, i.name, label(i), f"{KIND_LABEL[i.kind].split(' ', 1)[1]}:{i.name}")]


def label(item: Item) -> str:
    """一覧・候補に出す表示名（種類が分かる形）。"""
    icon = item.value if item.kind == CARD_ICON else ""
    return f"{KIND_LABEL[item.kind].split(' ', 1)[1]}：{icon}{item.name}"


def of_kind(kind: str) -> list:
    return [i for i in CATALOG if i.kind == kind]
