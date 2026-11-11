"""ショップの商品一覧（カタログ）。景品はすべてゲーム内・サーバー内で完結するもので、現実の価値はない。"""

import dataclasses
from dataclasses import dataclass
from typing import Optional

from core import labor

TITLE, CARD_COLOR, CARD_ICON, CONSUMABLE, COLOR_ROLE = "title", "card_color", "card_icon", "consumable", "color_role"
RACING = "racing"  # 競馬の品（購入券・にんじん）。買うと持ち物に増え、`/競馬 厩舎` で使う（枚数が増える品なので、消耗品と同じ扱い）
SKIN_DICE, SKIN_TABLE, COLLECTION = "skin_dice", "skin_table", "collection"
RELIC = "relic"  # 発掘品（作業員が、鉱石を掘るときに、まれに見つける）。買えない。持っているだけで飾りになり、`/取引` で売り買いする
MARKET = "market"  # 帝愛市の品（裏情報メモなど）。持ち物には増えず、買うと、その場で効果が出る

KIND_LABEL = {
    TITLE: "🏷️ 称号",
    CARD_COLOR: "🎨 カードの色",
    CARD_ICON: "🖼️ カードのアイコン",
    CONSUMABLE: "🎟️ 消耗品",
    RACING: "🐎 競馬",
    MARKET: "🕴️ 帝愛市",
    RELIC: "🏺 発掘品",
    COLOR_ROLE: "🌈 名前の色（ロール）",
    SKIN_DICE: "🎲 演出（チンチロ用）",
    SKIN_TABLE: "🃏 演出（パチンコ・ブラックジャック用）",
    COLLECTION: "🏆 コレクション",
}

# 名前の色ロールの名前の接頭辞。この接頭辞のロールだけを Bot が作って付け外しする
ROLE_PREFIX = "ペリカ・"

WEEKLY_KING = "title_weekly_king"
DAILY_DOUBLE = "daily_double"
INSURANCE = "insurance"
STREAK_GUARD = "streak_guard"
HORSE_VOUCHER = "horse_voucher"
CARROT = "carrot"
INSIDER_MEMO = "insider_memo"
GOLD_CARROT = "gold_carrot"
LABOR_DRINK = "labor_drink"
AUTO_USED = (
    STREAK_GUARD,
    HORSE_VOUCHER,
    CARROT,
    GOLD_CARROT,
    LABOR_DRINK,
)  # 「使う」を押さなくてよい（持っているだけ・別の画面で使う）消耗品
# 保険券で戻る額の上限。券の値段（2,000）より小さくして、買って使うほど増える（期待値がプラスになる）ことがないようにする
INSURANCE_CAP = 1_000


@dataclass(frozen=True)
class Item:
    id: str
    name: str
    kind: str
    price: int
    value: object = None  # 色(16進)・絵文字など
    desc: str = ""
    limited: bool = False  # 買えない限定品（ランキングなどで贈られる）。販売の一覧・購入の候補には出ない
    mark: str = ""  # ショップの画面で、名前の前に付ける絵文字（名前そのものには入れない。ニックネームなどに出る名前は変わらない）

    @property
    def unique(self) -> bool:
        """一度買えば持ち続けるもの（消耗品以外）。"""
        return self.kind not in (CONSUMABLE, RACING, MARKET, RELIC)


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
    Item("title_emperor", "地下の帝王", TITLE, 20_000_000),
    Item("title_creditor", "債権王", TITLE, 500_000),
    Item("title_fixer", "フィクサー", TITLE, 2_000_000),
    Item("title_100oku", "百億の男", TITLE, 10_000_000),
    # 限定称号（買えない）。週間ランキング1位に、次の発表まで贈られる
    Item(WEEKLY_KING, "👑週間王者", TITLE, 0, desc="先週のゲームの稼ぎ1位に贈られる。次の発表まで持てる", limited=True),
    # 残高カードの色・アイコン
    *_colors(
        "cardcolor",
        CARD_COLOR,
        3_000,
        [
            ("red", "赤", 0xE74C3C),
            ("blue", "青", 0x3498DB),
            ("green", "緑", 0x2ECC71),
            ("gold", "金", 0xF1C40F),
            ("purple", "紫", 0x9B59B6),
            ("dark", "黒", 0x2C2F33),
        ],
    ),
    *_colors(
        "cardicon",
        CARD_ICON,
        3_000,
        [
            ("slot", "スロット", "🎰"),
            ("gem", "宝石", "💎"),
            ("fire", "炎", "🔥"),
            ("moon", "月", "🌙"),
            ("dragon", "龍", "🐉"),
        ],
    ),
    Item("cardicon_crown", "王冠", CARD_ICON, 5_000, "👑"),
    # 消耗品（使うとなくなる）
    Item(DAILY_DOUBLE, "デイリー2倍券", CONSUMABLE, 3_000, desc="次のデイリーが2倍"),
    Item(
        INSURANCE,
        "保険券",
        CONSUMABLE,
        2_000,
        desc=f"負けた1回の賭け金の半分が戻る（最大{INSURANCE_CAP:,}）",
    ),
    Item(
        STREAK_GUARD,
        "連続守り券",
        CONSUMABLE,
        16_000,
        desc="受け取れなかった日を1日ぶん埋める（持つだけで自動で使われる）",
    ),
    Item(
        LABOR_DRINK,
        "栄養ドリンコ",
        CONSUMABLE,
        300,
        desc="地下作業の疲労を60回復する（`/地下 作業` の画面で飲む）",
    ),
    Item(
        HORSE_VOUCHER,
        "競走馬（購入券）",
        RACING,
        30_000,
        desc="競走馬を1頭迎える（`/競馬 厩舎`・3頭まで）",
    ),
    Item(
        CARROT,
        "にんじん",
        RACING,
        1_000,
        desc="馬のステータスを5点動かす（`/競馬 厩舎`）",
    ),
    Item(
        INSIDER_MEMO,
        "裏情報メモ（決算）",
        MARKET,
        5_000,
        desc="次の決算の方向についての、極秘メモ。確度 80%（2割はガセ）。決算ごとに1人1枚・サーバーで5枚まで",
    ),
    Item(
        GOLD_CARROT,
        "金にんじん",
        RACING,
        50_000,
        desc="馬のステータスの合計を10点増やす。5点ずつ、好きなところへ（`/競馬 厩舎`・1頭に1本まで）",
    ),
    # ゲーム演出の変更品（装備すると、結果画面のタイトル・飾り・地の文が変わる。勝率と配当は変わらない）。見た目は core/skins.py
    Item("skin_kuro", "黒服の立会い", SKIN_DICE, 30_000, desc="黒服が立ち会い、実況する"),
    Item("skin_gold_dice", "黄金の賽", SKIN_DICE, 150_000, desc="黄金の賽の演出になる"),
    Item("skin_glass_dice", "血のマニキュア / ガラスの賽", SKIN_DICE, 500_000, desc="高レート向けの特別演出"),
    Item("skin_vip", "VIP黒薔薇シート", SKIN_TABLE, 300_000, desc="黒薔薇のVIP席の演出になる"),
    Item("skin_numa", "沼（第3クギ突破仕様演出）", SKIN_TABLE, 1_000_000, desc="パチンコが、沼の演出になる"),
    # コレクション（持っているだけで残高カードに並ぶ。装備はない。資産価値は、買った値段の合計）
    Item("col_beer", "キンキンに冷えたビール", COLLECTION, 13_500, "🍺"),
    Item("col_yakitori", "焼き鳥・柿ピー盛り合わせ", COLLECTION, 25_000, "🍢"),
    Item("col_gaishutsu", "1日外出録（外出許可証レプリカ）", COLLECTION, 50_000, "🎫"),
    Item("col_sunglasses", "黒服のサングラス", COLLECTION, 100_000, "🕶️"),
    Item("col_teppan", "焼き土下座用の鉄板・炭火セット", COLLECTION, 250_000, "🔥"),
    Item("col_megaphone", "利根川の拡声器", COLLECTION, 500_000, "🎙️"),
    Item("col_gold_bar", "純金の延べ棒（1kg）", COLLECTION, 1_000_000, "🪙"),
    Item("col_stock", "帝愛グループ特別優待株券（額装）", COLLECTION, 3_000_000, "🏢"),
    Item("col_throne", "兵藤会長の玉座", COLLECTION, 10_000_000, "👑"),
    # サーバー内の名前の色（Bot がロールを作って付ける）。0x000000 は「色なし」扱いになるため、黒・白は少しずらす
    *_colors(
        "role",
        COLOR_ROLE,
        20_000,
        [
            ("red", "赤", 0xE74C3C),
            ("orange", "橙", 0xE67E22),
            ("yellow", "黄", 0xF1C40F),
            ("green", "緑", 0x2ECC71),
            ("cyan", "水色", 0x1ABC9C),
            ("blue", "青", 0x3498DB),
            ("purple", "紫", 0x9B59B6),
            ("pink", "桃", 0xFF69B4),
            ("gold", "金", 0xD4AF37),
            ("silver", "銀", 0xBDC3C7),
            ("black", "黒", 0x010101),
            ("white", "白", 0xFEFEFE),
        ],
    ),
]

# ショップの画面で使う絵文字（適したものがある品だけ。カードのアイコンとコレクションは、もともと絵文字を持っている）
MARKS = {
    "title_rookie": "🎲",
    "title_broke": "💸",
    "title_regular": "🎰",
    "title_zawa": "😨",
    "title_underground": "🕳️",
    "title_lucky": "🍀",
    "title_emperor": "🔱",
    "title_creditor": "💰",
    "title_fixer": "🕴️",
    "title_100oku": "💴",
    "cardcolor_red": "🔴",
    "cardcolor_blue": "🔵",
    "cardcolor_green": "🟢",
    "cardcolor_gold": "🟡",
    "cardcolor_purple": "🟣",
    "cardcolor_dark": "⚫",
    DAILY_DOUBLE: "🎟️",
    INSURANCE: "🛡️",
    STREAK_GUARD: "🔥",
    HORSE_VOUCHER: "🐎",
    CARROT: "🥕",
    GOLD_CARROT: "🥇",
    INSIDER_MEMO: "🕴️",
    LABOR_DRINK: "🥤",
    "skin_kuro": "🕴️",
    "skin_gold_dice": "🎲",
    "skin_glass_dice": "💅",
    "skin_vip": "🌹",
    "skin_numa": "🌀",
    "role_red": "🔴",
    "role_orange": "🟠",
    "role_yellow": "🟡",
    "role_green": "🟢",
    "role_cyan": "🩵",
    "role_blue": "🔵",
    "role_purple": "🟣",
    "role_pink": "🩷",
    "role_gold": "🥇",
    "role_silver": "🥈",
    "role_black": "⚫",
    "role_white": "⚪",
}
CATALOG = [dataclasses.replace(i, mark=MARKS.get(i.id, "")) for i in CATALOG]
# 発掘品（買えない限定品。ショップの値段は 0）。数字と出る確率は core/labor.py の RELICS。取引の値段の幅の目安は RELIC_VALUE
CATALOG += [
    Item(
        labor.RELIC_ID[rid],
        name,
        RELIC,
        0,
        mark,
        desc="作業員が、まれに見つける、コレクション用の発掘品。持っているだけで飾りになり、`/取引` で売り買いできる",
        limited=True,
        mark=mark,
    )
    for rid, name, mark, _p, _value in labor.RELICS
]
RELIC_VALUE = {
    labor.RELIC_ID[rid]: value for rid, _n, _m, _p, value in labor.RELICS
}  # 目安の価値（取引の値段の幅の元）
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
    icon = item.value if item.kind in (CARD_ICON, COLLECTION) else item.mark if item.kind == RELIC else ""
    return f"{KIND_LABEL[item.kind].split(' ', 1)[1]}：{icon}{item.name}"


def of_kind(kind: str, include_limited: bool = False) -> list:
    """その種類の商品。限定品は、販売の一覧には出さない（持ち物や称号の判定では include_limited=True）。"""
    return [i for i in CATALOG if i.kind == kind and (include_limited or not i.limited)]


def collection_value(inventory: dict) -> tuple:
    """持っているコレクションの (商品の並び, 資産価値)。資産価値は、買った値段の合計。"""
    owned = [i for i in of_kind(COLLECTION) if inventory.get(i.id, 0) > 0]
    return owned, sum(i.price for i in owned)


# ---------------------------------------------------------------- #
# 在庫（買える数）。1人ずつ別々に持ち、買うと減り、時間で補充される。買い占めを防ぐ
# ---------------------------------------------------------------- #
@dataclass(frozen=True)
class Stock:
    max: int  # 在庫の上限（最初は、満タン）
    per_hour: float  # 1時間に補充される数


STOCK_RULES = {LABOR_DRINK: Stock(max=10, per_hour=2.0)}  # 栄養ドリンコ: 10本まで、30分に1本ずつ補充


def stock_now(rule: Stock, stock: float, updated: float, now: float) -> float:
    """最後に記録してから時間がたった、いまの在庫（小数。上限まで）。"""
    return min(float(rule.max), stock + max(0.0, now - updated) / 3600.0 * rule.per_hour)


def next_refill_at(rule: Stock, stock: float, now: float) -> Optional[float]:
    """次に1つ増える時刻（在庫が小数のとき、整数に届く時刻）。満タンなら None。"""
    if stock >= rule.max:
        return None
    return now + (1.0 - (stock - int(stock))) / rule.per_hour * 3600.0
