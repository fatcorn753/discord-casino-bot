"""自己破産時の競売査定ステップ: ショップの持ち物・装備・持ち馬の没収と出品。"""

from domain import shop as shop_mod
from domain import teiai
from domain.ports.bridges import SeizureValueStep, ShopBelongings
from infra.db.auction import AuctionRepo
from infra.db.horse import HorseRepo


def shop_and_horse_seizure_value_step(
    shop: ShopBelongings, auction: AuctionRepo, horses: HorseRepo
) -> SeizureValueStep:
    """ショップの持ち物・装備・持ち馬を没収して競売へ出品し、換金額（購入額の SEIZE_RATE）を返す。"""

    def seize(guild_id: int, user_id: int, now: float) -> int:
        value = 0
        for item_id, qty in shop(guild_id, user_id).items:
            item = shop_mod.BY_ID.get(item_id)
            value += (item.price if item else 0) * qty
            auction.auction_list_locked_for(guild_id, item_id, qty, now)
        value += horses.seize_horses_for(guild_id, user_id, now)
        return int(value * teiai.SEIZE_RATE)

    return seize
