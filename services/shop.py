"""ショップ・帝愛の競売・装備・称号のアプリケーションサービス。"""

from dataclasses import dataclass
from typing import Any, Optional

from domain import shop, teiai
from domain.earnings import MemoInfo
from domain.errors import Outcome
from domain.ports.clock import IClock
from domain.ports.repos import (
    IAuctionRepo,
    IDebtRepo,
    IEarningsRepo,
    IGuildRepo,
    IHorseRepo,
    IP2PRepo,
    IShopRepo,
    IWalletRepo,
)
from domain.types import CONSUMABLE_EFFECTS, MarketPurchase


@dataclass(frozen=True)
class BuyItemPrecheck:
    ok: bool
    status: str = ""
    stock: Optional[dict[str, Any]] = None
    balance: int = 0
    total: int = 0


class ShopService:
    """ショップ・帝愛の競売・持ち物・称号解決のユースケース調停役。"""

    def __init__(
        self,
        shop_repo: IShopRepo,
        auction: IAuctionRepo,
        horses: IHorseRepo,
        earnings: IEarningsRepo,
        debt: IDebtRepo,
        p2p: IP2PRepo,
        wallet: IWalletRepo,
        guild: IGuildRepo,
        clock: IClock,
    ) -> None:
        self._clock = clock
        self._shop = shop_repo
        self._auction = auction
        self._horses = horses
        self._earnings = earnings
        self._debt = debt
        self._p2p = p2p
        self._wallet = wallet
        self._guild = guild

    def peek_balance(self, guild_id: int, user_id: int) -> int:
        me = self._wallet.for_guild(guild_id).peek_stats(user_id)
        return me["balance"] if me else 0

    def stocks_for_items(self, guild_id: int, user_id: int, items: list[shop.Item]) -> dict[str, dict[str, Any]]:
        repo = self._shop.for_guild(guild_id)
        now = self._clock.now_ts()
        out: dict[str, dict[str, Any]] = {}
        for item in items:
            if item.id in shop.STOCK_RULES:
                st = repo.shop_stock(user_id, item.id, now)
                if st is not None:
                    out[item.id] = st
        return out

    def precheck_buy(self, guild_id: int, user_id: int, item: shop.Item, qty: int) -> BuyItemPrecheck:
        shop_repo = self._shop.for_guild(guild_id)
        balance = self._wallet.for_guild(guild_id).balance(user_id)
        total = item.price * qty
        if item.unique and shop_repo.inventory(user_id).get(item.id, 0) > 0:
            return BuyItemPrecheck(ok=False, status="owned", balance=balance, total=total)
        stock = shop_repo.shop_stock(user_id, item.id, self._clock.now_ts())
        if stock is not None and qty > stock["stock"]:
            return BuyItemPrecheck(ok=False, status="out_of_stock", stock=stock, balance=balance, total=total)
        if balance < total:
            return BuyItemPrecheck(ok=False, status="insufficient_balance", balance=balance, total=total)
        return BuyItemPrecheck(ok=True, balance=balance, total=total)

    def arm_consumable(self, guild_id: int, user_id: int, item_id: str) -> Optional[int]:
        """消耗品を有効化し、有効になった残り枚数を返す（未所持なら None）。"""
        repo = self._shop.for_guild(guild_id)
        if not repo.arm(user_id, item_id):
            return None
        return int(repo.get_profile(user_id)[CONSUMABLE_EFFECTS[item_id]])

    def catalog_snapshot(self, guild_id: int, user_id: int, tab: str, items: list[shop.Item]) -> dict[str, Any]:
        """カタログ画面描画・選択肢構築用のスナップショットを 1 回の取得で組む。"""
        inv = self._shop.for_guild(guild_id).inventory(user_id)
        balance = self.peek_balance(guild_id, user_id)
        waiting = (
            len(self._auction.for_guild(guild_id).auction_list(self._clock.now_ts()))
            + len(self._horses.for_guild(guild_id).auction_horses(self._clock.now_ts()))
            if tab == "home"
            else 0
        )
        stocks = self.stocks_for_items(guild_id, user_id, items) if tab != "home" else {}
        return {"inv": inv, "balance": balance, "waiting": waiting, "stocks": stocks}

    def auction_snapshot(
        self,
        guild_id: int,
        user_id: int,
        tab: Optional[str],
        p2p_enabled: bool,
        kind: Optional[str],
        mode: Optional[str],
        order: str,
        limit: int,
    ) -> dict[str, Any]:
        """競売画面描画・選択肢構築用のスナップショット（二重クエリ防止）。"""
        now = self._clock.now_ts()
        listed = self._auction.for_guild(guild_id).auction_list(now)
        market = self._auction.for_guild(guild_id).market_list(now)
        horses = self._horses.for_guild(guild_id).auction_horses(now)
        p2p_repo = self._p2p.for_guild(guild_id)
        p2p_total = p2p_repo.p2p_count(now) if p2p_enabled else 0
        resolved_tab = tab if tab is not None else ("players" if p2p_enabled and p2p_total else "teiai")
        if not p2p_enabled:
            resolved_tab = "teiai"
        p2p_rows: list[tuple[dict[str, Any], str]] = []
        if resolved_tab == "players":
            rows = p2p_repo.p2p_active(kind, now, limit=limit, mode=mode, order=order)
            p2p_rows = [(r, p2p_repo.p2p_label(r)) for r in rows]
        return {
            "balance": self.peek_balance(guild_id, user_id),
            "listed": listed,
            "market": market,
            "horses": horses,
            "p2p_total": p2p_total,
            "tab": resolved_tab,
            "p2p_rows": p2p_rows,
        }

    def equipped_title(self, guild_id: int, user_id: int) -> Optional[str]:
        item = shop.BY_ID.get(self._shop.for_guild(guild_id).get_profile(user_id)["title"])
        return item.name if item else None

    def nick_tag(self, guild_id: int, user_id: int) -> Optional[str]:
        status = self._debt.for_guild(guild_id).status(user_id)
        if status != teiai.KATAGI:
            return teiai.STATUS_NAME[status]
        if self._guild.nick_pref(guild_id, user_id):
            return self.equipped_title(guild_id, user_id)
        return (
            teiai.STATUS_NAME[teiai.WEALTHY]
            if self._wallet.for_guild(guild_id).display_status(user_id) == teiai.WEALTHY
            else None
        )

    # ------------------------------ 買う・身につける（Cog から使う） ------------------------------ #
    def inventory(self, guild_id: int, user_id: int) -> dict[str, Any]:
        return self._shop.for_guild(guild_id).inventory(user_id)

    def profile(self, guild_id: int, user_id: int) -> dict[str, Any]:
        return self._shop.for_guild(guild_id).get_profile(user_id)

    def purchase(self, guild_id: int, user_id: int, item: shop.Item, qty: int) -> Any:
        return self._shop.for_guild(guild_id).purchase(
            user_id, item.id, shop.label(item), item.price, item.unique, qty=qty, now=self._clock.now_ts()
        )

    def equip(self, guild_id: int, user_id: int, slot: str, item_id: Optional[str]) -> bool:
        """slot に item_id を身につける（None なら外す）。"""
        return self._shop.for_guild(guild_id).equip(user_id, slot, item_id)

    def auction_list(self, guild_id: int) -> list[dict[str, Any]]:
        return self._auction.for_guild(guild_id).auction_list(self._clock.now_ts())

    def auction_buy(self, guild_id: int, user_id: int, item_id: str, qty: int) -> Any:
        return self._auction.for_guild(guild_id).auction_buy(user_id, item_id, qty, self._clock.now_ts())

    # ---- 帝愛の出品（#72） ----
    def market_list(self, guild_id: int) -> list[dict[str, Any]]:
        return self._auction.for_guild(guild_id).market_list(self._clock.now_ts())

    def market_buy(self, guild_id: int, user_id: int, listing_id: int, qty: int) -> MarketPurchase:
        return self._auction.for_guild(guild_id).market_buy(user_id, listing_id, qty, self._clock.now_ts())

    def auction_horses(self, guild_id: int) -> list[dict[str, Any]]:
        return self._horses.for_guild(guild_id).auction_horses(self._clock.now_ts())

    def auction_buy_horse(self, guild_id: int, user_id: int, horse_id: int) -> Any:
        return self._horses.for_guild(guild_id).auction_buy_horse(user_id, horse_id, self._clock.now_ts())

    def insider_stock(self, guild_id: int) -> Optional[dict[str, Any]]:
        return self._earnings.for_guild(guild_id).insider_stock(self._clock.now_ts())

    def insider_buy(self, guild_id: int, user_id: int) -> Outcome[MemoInfo]:
        return self._earnings.for_guild(guild_id).insider_buy(user_id, self._clock.now_ts())
