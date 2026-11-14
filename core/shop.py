"""ショップの商品一覧（カタログ）。景品はすべてゲーム内・サーバー内で完結するもので、現実の価値はない。"""

from dataclasses import dataclass
from typing import Optional

from core import catalogs, labor, relics

TITLE, CARD_COLOR, CARD_ICON, CONSUMABLE, COLOR_ROLE = "title", "card_color", "card_icon", "consumable", "color_role"
RACING = "racing"  # 競馬の品（購入券・にんじん）。買うと持ち物に増え、`/競馬 厩舎` で使う（枚数が増える品なので、消耗品と同じ扱い）
SKIN_DICE, SKIN_TABLE, COLLECTION = "skin_dice", "skin_table", "collection"
RELIC = "relic"  # 発掘品（作業員が、鉱石を掘るときに、まれに見つける）。買えない。持っているだけで飾りになり、競売の画面で売り買いする
MARKET = "market"  # 帝愛市の品（裏情報メモなど）。持ち物には増えず、買うと、その場で効果が出る

_CFG = catalogs.shop()
KIND_LABEL = dict(_CFG.kind_labels)
# 名前の色ロールの名前の接頭辞。この接頭辞のロールだけを Bot が作って付け外しする
ROLE_PREFIX = _CFG.role_prefix

WEEKLY_KING = "title_weekly_king"
DAILY_DOUBLE = "daily_double"
INSURANCE = "insurance"
STREAK_GUARD = "streak_guard"
HORSE_VOUCHER = "horse_voucher"
CARROT = "carrot"
INSIDER_MEMO = "insider_memo"
GOLD_CARROT = "gold_carrot"
LABOR_DRINK = "labor_drink"
AUTO_USED = _CFG.auto_used  # 「使う」を押さなくてよい（持っているだけ・別の画面で使う）消耗品
INSURANCE_CAP = _CFG.insurance_cap  # 保険券で戻る額の上限


@dataclass(frozen=True)
class Item:
    id: str
    name: str
    kind: str
    price: int
    value: object = None  # 色(16進)・絵文字など
    desc: str = ""
    limited: bool = False  # 買えない限定品（ランキングなどで贈られる）。販売の一覧・購入の候補には出ない
    mark: str = ""  # ショップの画面で、名前の前に付ける絵文字（名前そのものには入らない。ニックネームなどに出る名前は変わらない）

    @property
    def unique(self) -> bool:
        """一度買えば持ち続けるもの（消耗品以外）。"""
        return self.kind not in (CONSUMABLE, RACING, MARKET, RELIC)


CATALOG = [
    Item(
        i.id,
        i.name,
        i.kind,
        i.price,
        i.value,
        i.desc.format(insurance_cap=INSURANCE_CAP) if "{insurance_cap" in i.desc else i.desc,
        i.limited,
        i.mark,
    )
    for i in _CFG.items
]
# 発掘品（買えない限定品。ショップの値段は 0）。数字と出る確率は core/labor.py の RELICS。取引の値段の幅の目安は RELIC_VALUE
CATALOG += [
    Item(
        labor.RELIC_ID[rid],
        name,
        RELIC,
        0,
        mark,
        desc="作業員が、まれに見つける、コレクション用の発掘品。競売の画面（「🕴️ 帝愛の競売」）で売り買いできる。"
        + relics.buff_text(labor.RELIC_ID[rid]),
        limited=True,
        mark=mark,
    )
    for rid, name, mark, _p, _value in labor.RELICS
]
# 発掘品を合体してできる★つき（★1〜）。★が1つ増えるごとに、目安の価値が10倍
CATALOG += [
    Item(
        relics.star_id(labor.RELIC_ID[rid], star),
        name + relics.stars(star),
        RELIC,
        0,
        mark,
        desc=f"{name}を{relics.MERGE_COUNT**star}個ぶん合体した、★{star}の発掘品。競売の画面で売り買いできる。"
        + relics.buff_text(relics.star_id(labor.RELIC_ID[rid], star)),
        limited=True,
        mark=mark,
    )
    for rid, name, mark, _p, _value in labor.RELICS
    for star in range(1, relics.STAR_MAX + 1)
]
RELIC_VALUE = {
    relics.star_id(labor.RELIC_ID[rid], star): value * relics.MERGE_COUNT**star
    for rid, _n, _m, _p, value in labor.RELICS
    for star in range(relics.STAR_MAX + 1)
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


STOCK_RULES = {
    k: Stock(max=v.max, per_hour=v.per_hour) for k, v in _CFG.stock_rules.items()
}  # 栄養ドリンコ: 10本まで、30分に1本ずつ補充


def stock_now(rule: Stock, stock: float, updated: float, now: float) -> float:
    """最後に記録してから時間がたった、いまの在庫（小数。上限まで）。"""
    return min(float(rule.max), stock + max(0.0, now - updated) / 3600.0 * rule.per_hour)


def next_refill_at(rule: Stock, stock: float, now: float) -> Optional[float]:
    """次に1つ増える時刻（在庫が小数のとき、整数に届く時刻）。満タンなら None。"""
    if stock >= rule.max:
        return None
    return now + (1.0 - (stock - int(stock))) / rule.per_hour * 3600.0
