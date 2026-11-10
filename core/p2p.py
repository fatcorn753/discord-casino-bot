"""プレイヤー間の取引（定価の市場と競り）の数字と決まり。Discord と DB に依存しない純粋なロジック。

- 売れるもの: 競走馬と、消耗品・見た目の品（カードの色とアイコン・演出・名前の色）、発掘品。称号・コレクション・限定品・帝愛市の品は売れない。
- 売り方: 定価（出した値段で、すぐ買える）と、競り（入札で値段が上がる。締切の時点で、最高値の人が買う）。
- 手数料: 売り手の受け取りから FEE_RATE を引き、帝愛銀行の収入にする。
- 送金の抜け道をふさぐ: 値段は、品の定価の幅に収める。同じ2人の取引は、1日の合計に上限がある。
"""

import math

from core import shop

FEE_RATE = 0.05  # 売れたとき、売り手の受け取りから引く割合（帝愛銀行の収入）
MAX_ACTIVE = 5  # 1人が同時に出せる数
FIXED_DAYS = 7  # 定価の出品の有効期間（過ぎると、売れ残りとして戻る）
AUCTION_HOURS = (1, 6, 24, 72)  # 競りの長さ（時間）
MIN_INCREMENT_RATE, MIN_INCREMENT = 0.05, 100  # 入札は、いまの最高値より、5%（最低100）以上高く
SNIPE_SECONDS = 300  # 締切の5分前から入札があると、締切が5分延びる（ぎりぎりの入札だけで決まらないように）
PAIR_DAILY_LIMIT = 100_000  # 同じ2人（売り手と買い手）の、24時間の取引額の合計の上限（送金の抜け道をふさぐ）
MIN_PRICE = 100
ITEM_PRICE_RANGE = (0.10, 5.0)  # 品の値段の幅（定価 × 個数 に対する割合）
HORSE_PRICE_RANGE = (3_000, 300_000)  # 競走馬の値段の幅（購入券は30,000。金にんじんで育てた馬は、高く売れる）
ESCROW_OWNER = -3  # 出品中の馬の持ち主（預かり中。厩舎にも出ず、レースにも出られない）

TRADABLE_KINDS = (
    shop.CONSUMABLE,
    shop.RACING,
    shop.CARD_COLOR,
    shop.CARD_ICON,
    shop.SKIN_DICE,
    shop.SKIN_TABLE,
    shop.COLOR_ROLE,
)
SLOT_OF_KIND = {
    shop.CARD_COLOR: "card_color",
    shop.CARD_ICON: "card_icon",
    shop.SKIN_DICE: "skin_dice",
    shop.SKIN_TABLE: "skin_table",
}  # 出品すると、外れる装備（色ロールは、Discord 側のロールなので、ここには入らない）


def tradable(item_id: str) -> bool:
    """市場で売れる品か。発掘品は、買えない限定品だが、取引できる（取引のために掘る品）。"""
    item = shop.BY_ID.get(item_id)
    if item is None:
        return False
    if item.kind == shop.RELIC:
        return item_id in shop.RELIC_VALUE
    return item.price > 0 and item.kind in TRADABLE_KINDS and not item.limited


def price_range(kind: str, item_id: str = "", qty: int = 1) -> tuple:
    """出品の値段（ペリカ）の幅 (下限, 上限)。品は定価 × 個数 の割合、馬は決まった幅。"""
    if kind == "horse":
        return HORSE_PRICE_RANGE
    base = shop.RELIC_VALUE.get(item_id, shop.BY_ID[item_id].price) * max(1, qty)  # 発掘品は、目安の価値
    lo, hi = ITEM_PRICE_RANGE
    return max(MIN_PRICE, math.ceil(base * lo)), min(
        PAIR_DAILY_LIMIT, math.floor(base * hi)
    )  # 上限は、同じ相手との1日の上限まで


def fee_of(price: int) -> int:
    """売れた値段 price にかかる手数料。"""
    return int(price * FEE_RATE)


def min_next_bid(start: int, current: int) -> int:
    """次の入札の最低額。まだ入札がなければ開始価格、あればいまの最高値より、5%（最低100）以上高く。"""
    if current <= 0:
        return start
    return current + max(MIN_INCREMENT, math.ceil(current * MIN_INCREMENT_RATE))


def end_after_bid(end_at: float, now: float) -> float:
    """入札のあとの締切。締切の SNIPE_SECONDS 前を切っていたら、いまから SNIPE_SECONDS 後まで延ばす。"""
    return max(end_at, now + SNIPE_SECONDS) if end_at - now < SNIPE_SECONDS else end_at
