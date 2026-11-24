"""プレイヤー間の市場と競り（出品・購入・入札・取り消し・締切の精算）のアプリケーションサービス。"""

import random

from domain.errors import Outcome
from domain.ports.clock import IClock
from domain.ports.contracts import HorseRow, ListingRow, P2PMine
from domain.ports.repos import IHorseRepo, IP2PRepo, IShopRepo, IWalletRepo


class P2PService:
    """出品・購入・入札・取り消し・精算と、出品できる持ち物の照会。"""

    def __init__(self, p2p: IP2PRepo, horses: IHorseRepo, shop: IShopRepo, wallet: IWalletRepo, clock: IClock) -> None:
        self._clock = clock
        self._p2p = p2p
        self._horses = horses
        self._shop = shop
        self._wallet = wallet

    def _repo(self, guild_id: int) -> IP2PRepo:
        return self._p2p.for_guild(guild_id)

    # ------------------------------ 照会 ------------------------------ #
    def get(self, guild_id: int, listing_id: int) -> ListingRow | None:
        return self._repo(guild_id).p2p_get(listing_id)

    def label(self, guild_id: int, row: ListingRow) -> str:
        return self._repo(guild_id).p2p_label(row)

    def horse_of(self, guild_id: int, row: ListingRow) -> HorseRow | None:
        """馬の出品なら、その馬。それ以外は None。"""
        return self._horses.for_guild(guild_id).horse_get(int(row["ref"])) if row["kind"] == "horse" else None

    def mine(self, guild_id: int, user_id: int) -> P2PMine:
        return self._repo(guild_id).p2p_mine(user_id)

    def sellable_items(self, guild_id: int, user_id: int) -> dict[str, int]:
        return self._shop.for_guild(guild_id).inventory(user_id)

    def sellable_horses(self, guild_id: int, user_id: int) -> list[HorseRow]:
        return self._horses.for_guild(guild_id).horse_list(user_id)

    def balance_of(self, guild_id: int, user_id: int) -> int:
        return self._wallet.for_guild(guild_id).balance(user_id)

    # ------------------------------ 取引 ------------------------------ #
    def list_item(
        self,
        guild_id: int,
        user_id: int,
        kind: str,
        ref: str,
        qty: int,
        mode: str,
        price: int,
        hours: int | None,
    ) -> Outcome[int]:
        return self._repo(guild_id).p2p_list(user_id, kind, ref, qty, mode, price, hours, self._clock.now_ts())

    def buy(self, guild_id: int, user_id: int, listing_id: int) -> Outcome[int]:
        return self._repo(guild_id).p2p_buy(user_id, listing_id, self._clock.now_ts())

    def bid(self, guild_id: int, user_id: int, listing_id: int, amount: int) -> Outcome[float]:
        return self._repo(guild_id).p2p_bid(user_id, listing_id, amount, self._clock.now_ts())

    def cancel(self, guild_id: int, user_id: int, listing_id: int) -> Outcome[str]:
        return self._repo(guild_id).p2p_cancel(user_id, listing_id, self._clock.now_ts())

    def settle(self, guild_id: int) -> list[tuple[ListingRow, str]]:
        """締切の来た出品を精算する。[(出品, 結果), ...]。"""
        return self._repo(guild_id).p2p_settle(self._clock.now_ts())

    def teiai_daily(self, guild_id: int) -> list[int]:
        """帝愛の発掘品の競りを、その日の分だけ出す。出した出品の番号（もう出ていれば空）。"""
        return self._repo(guild_id).p2p_teiai_daily(self._clock.now_ts(), random.SystemRandom())

    # ------------------------------ 告知の投稿の記録 ------------------------------ #
    def set_post(self, guild_id: int, listing_id: int, channel_id: int, message_id: int) -> None:
        self._repo(guild_id).p2p_set_post(listing_id, channel_id, message_id)

    def mark_post_done(self, guild_id: int, listing_id: int) -> None:
        self._repo(guild_id).p2p_mark_post_done(listing_id)

    def unsynced_posts(self, guild_id: int) -> list[ListingRow]:
        return self._repo(guild_id).p2p_unsynced_posts()
