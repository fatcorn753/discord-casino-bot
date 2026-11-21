"""自己破産時の競売査定ステップ: ショップの持ち物・装備・持ち馬の没収と出品。"""

from domain import shop as shop_mod
from domain import teiai
from domain.ports.bridges import ISeizureValueStep, IShopBelongings
from infra.db.auction import AuctionRepo
from infra.db.horse import HorseRepo


class ShopAndHorseSeizureStep(ISeizureValueStep):
    """ショップの持ち物・装備・持ち馬を没収して競売へ出品し、換金額（購入額の SEIZE_RATE）を返す。"""

    def __init__(self, shop: IShopBelongings, auction: AuctionRepo, horses: HorseRepo) -> None:
        self._shop = shop
        self._auction = auction
        self._horses = horses

    def seize(self, guild_id: int, user_id: int, now: float) -> int:
        value = 0
        for item_id, qty in self._shop.seize_belongings_for(guild_id, user_id).items:
            item = shop_mod.BY_ID.get(item_id)
            value += (item.price if item else 0) * qty
            self._auction.auction_list_locked_for(guild_id, item_id, qty, now)
        value += self._horses.seize_horses_for(guild_id, user_id, now)
        return int(value * teiai.SEIZE_RATE)
