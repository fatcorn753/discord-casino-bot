"""帝愛の競売。破産で差し押さえたショップの品を、カタギが買えるようにする。

出品されるのは、消耗品と見た目の品（称号・カード・演出・色ロール）。コレクションと限定品は出さない。
値段は、定価の AUCTION_START から1日ごとに AUCTION_STEP ずつ下がり、AUCTION_FLOOR で止まる（domain/teiai.py）。
売上は帝愛銀行の収入になる。表は infra/db/schema/tables.py の seized_items。
"""

import random
from typing import Any

from domain import shop as shop_mod
from domain import teiai
from domain import teiai_market as tm
from domain.errors import fail
from domain.ports.bridges import IBankCashFlow, IStatusReader
from domain.ports.contracts import AuctionListing, TeiaiMarketListing
from domain.ports.repos import IAuctionRepo
from domain.types import MarketPurchase, PurchaseResult
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo
from infra.db.inventory import InventoryCore


def listable(item_id: str) -> bool:
    """競売に出せる品か。限定品・コレクション・称号は出さない（称号は、差し押さえたらそのまま没収する）。"""
    item = shop_mod.BY_ID.get(item_id)
    return (
        item is not None
        and not item.limited
        and item.kind not in (shop_mod.COLLECTION, shop_mod.TITLE)
        and item.price > 0
    )


class AuctionRepo(ScopedRepo, IAuctionRepo):
    """帝愛の競売の棚管理・購入。"""

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        status_reader: IStatusReader,
        bank: IBankCashFlow,
        guild_id: int = 0,
        strict: bool = False,
        *,
        inventory: InventoryCore | None = None,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._status_reader = status_reader
        self._bank = bank
        self._inventory = inventory or InventoryCore(db)

    def auction_list_locked_for(self, guild_id: int, item_id: str, qty: int, now: float) -> None:
        if qty > 0 and listable(item_id):
            self._conn.execute(
                "INSERT INTO seized_items (guild_id, item_id, qty, listed_at) VALUES (?, ?, ?, ?)",
                (guild_id, item_id, qty, now),
            )

    def purge_unlistable_auctions(self) -> int:
        """競売の棚から、いまは競売に出さない品（称号など）の出品を消す（全サーバー）。消した行数を返す。"""
        ids = [r["item_id"] for r in self._all("SELECT DISTINCT item_id FROM seized_items")]
        gone = [i for i in ids if not listable(i)]
        for item_id in gone:
            self._conn.execute("DELETE FROM seized_items WHERE item_id=?", (item_id,))
        return len(gone)

    def auction_price(self, item_id: str, listed_at: float, now: float) -> int:
        """1つあたりの、いまの値段（ペリカ）。"""
        return max(1, round(shop_mod.BY_ID[item_id].price * teiai.auction_rate(listed_at, now)))

    def auction_list(self, now: float) -> list[AuctionListing]:
        """競売の棚。品ごとに、残りの数と、いちばん安い（古い）出品の値段。[{item_id, qty, price, rate}]"""
        with self._lock:
            rows = self._all(
                "SELECT item_id, qty, listed_at FROM seized_items WHERE guild_id=? AND qty>0 ORDER BY listed_at, id",
                (self._g(),),
            )
        out: dict[str, AuctionListing] = {}
        for r in rows:
            if r["item_id"] not in shop_mod.BY_ID:
                continue
            entry = out.setdefault(
                r["item_id"],
                {
                    "item_id": r["item_id"],
                    "qty": 0,
                    "price": self.auction_price(r["item_id"], r["listed_at"], now),
                    "rate": teiai.auction_rate(r["listed_at"], now),
                },
            )
            entry["qty"] += r["qty"]
        return list(out.values())

    def auction_buy(self, user_id: int, item_id: str, qty: int = 1, now: float | None = None) -> PurchaseResult:
        """競売で買う。カタギだけ。持っているもの（消耗品以外）は買えない。安い（古い）出品から順に売れる。"""
        now = self._now() if now is None else now
        item = shop_mod.BY_ID.get(item_id)
        if item is None or qty < 1:
            return PurchaseResult(False, fail("auction_item_unavailable"))
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            if self._status_reader.status_for(gid, user_id, now) != teiai.KATAGI:
                return PurchaseResult(False, fail("auction_katagi_only"), self._account.raw_balance(gid, user_id))
            have = self._inventory.has(gid, user_id, item_id)
            if item.unique and have:
                return PurchaseResult(False, fail("already_owned"), self._account.raw_balance(gid, user_id))
            if item.unique:
                qty = 1
            rows = self._all(
                "SELECT id, qty, listed_at FROM seized_items WHERE guild_id=? AND item_id=? AND qty>0 "
                "ORDER BY listed_at, id",
                (gid, item_id),
            )
            if sum(r["qty"] for r in rows) < qty:
                return PurchaseResult(False, fail("auction_insufficient_qty"), self._account.raw_balance(gid, user_id))
            take, total, need = [], 0, qty
            for r in rows:
                n = min(need, r["qty"])
                take.append((r["id"], n))
                total += n * self.auction_price(item_id, r["listed_at"], now)
                need -= n
                if need == 0:
                    break
            bal = self._account.raw_balance(gid, user_id)
            if bal < total:
                return PurchaseResult(
                    False,
                    fail("insufficient_balance_need", need=total, balance=bal),
                    bal,
                )
            self._account.apply(gid, user_id, -total, f"競売:{shop_mod.label(item)}" + (f"×{qty}" if qty > 1 else ""))
            for row_id, n in take:
                self._conn.execute("UPDATE seized_items SET qty = qty - ? WHERE id=?", (n, row_id))
                self._conn.execute("DELETE FROM seized_items WHERE id=? AND qty<=0", (row_id,))
            self._inventory.add(gid, user_id, item_id, qty)
            self._bank.bank_income_for(gid, total, now)
            return PurchaseResult(True, balance=self._account.raw_balance(gid, user_id))

    # ------------------------------------------------------------------ #
    # 帝愛の出品（#72）。期間限定で、帝愛がショップの品を出す（domain/teiai_market.py）
    # ------------------------------------------------------------------ #
    def _market_plan_locked(self, gid: int, now: float, rng: random.Random) -> None:
        """その日の出品の計画を、まだ無ければ作る。計画は SystemRandom で決めるので、前もって分からない。"""
        day = tm.day_start(now)
        done = self._one("SELECT 1 FROM teiai_market_days WHERE guild_id=? AND day=?", (gid, day))
        if done:
            return
        self._conn.execute("INSERT INTO teiai_market_days (guild_id, day) VALUES (?, ?)", (gid, day))
        for x in tm.plan_day(day, rng, tm.pool(listable)):
            self._conn.execute(
                "INSERT INTO teiai_market (guild_id, day, item_id, mode, qty, start_price, end_price, black, "
                "listed_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    gid,
                    day,
                    x.item_id,
                    x.mode,
                    x.qty,
                    x.start_price,
                    x.end_price,
                    int(x.black),
                    x.listed_at,
                    x.expires_at,
                ),
            )

    def _market_row(self, r: Any, now: float) -> TeiaiMarketListing:
        return {
            "id": r["id"],
            "item_id": r["item_id"],
            "mode": r["mode"],
            "qty": r["qty"],
            "black": bool(r["black"]),
            "price": tm.price_at(r["start_price"], r["end_price"], r["listed_at"], r["expires_at"], now),
            "rate": tm.price_at(r["start_price"], r["end_price"], r["listed_at"], r["expires_at"], now)
            / shop_mod.BY_ID[r["item_id"]].price,
            "expires_at": r["expires_at"],
        }

    def market_list(self, now: float, rng: random.Random | None = None) -> list[TeiaiMarketListing]:
        """いま出ている帝愛の出品（売り切れ・期限切れ・まだ出ていないものは除く）。期限の近い順。"""
        rng = rng or random.SystemRandom()
        with self._db.tx():
            gid = self._g()
            self._market_plan_locked(gid, now, rng)
            rows = self._all(
                "SELECT * FROM teiai_market WHERE guild_id=? AND qty>0 AND listed_at<=? AND expires_at>? "
                "ORDER BY expires_at, id",
                (gid, now, now),
            )
        return [self._market_row(r, now) for r in rows if r["item_id"] in shop_mod.BY_ID]

    def market_buy(
        self,
        user_id: int,
        listing_id: int,
        qty: int = 1,
        now: float | None = None,
        rng: random.Random | None = None,
    ) -> MarketPurchase:
        """帝愛の出品を買う。カタギだけ（差し押さえ品の競売と同じ）。闇市品は、買ったときに中身が決まる。"""
        now = self._now() if now is None else now
        rng = rng or random.SystemRandom()
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            bal = self._account.raw_balance(gid, user_id)
            r = self._one(
                "SELECT * FROM teiai_market WHERE id=? AND guild_id=? AND qty>0 AND listed_at<=? AND expires_at>?",
                (listing_id, gid, now, now),
            )
            if r is None or r["item_id"] not in shop_mod.BY_ID or qty < 1:
                return MarketPurchase(False, fail("auction_item_unavailable"), bal)
            if self._status_reader.status_for(gid, user_id, now) != teiai.KATAGI:
                return MarketPurchase(False, fail("auction_katagi_only"), bal)
            item = shop_mod.BY_ID[r["item_id"]]
            if item.unique:
                qty = 1
            if r["qty"] < qty:
                return MarketPurchase(False, fail("auction_insufficient_qty"), bal)
            owned = self._inventory.item_ids(gid, user_id)
            if item.unique and item.id in owned:
                return MarketPurchase(False, fail("already_owned"), bal)
            price = tm.price_at(r["start_price"], r["end_price"], r["listed_at"], r["expires_at"], now)
            total = price * qty
            if bal < total:
                return MarketPurchase(False, fail("insufficient_balance_need", need=total, balance=bal), bal)
            got, outcome = (item.id, tm.SAME)
            if r["black"]:
                got, outcome = tm.reveal(item.id, rng, tm.pool(listable))
                # 見た目の品のように1つしか持てない品で、もう持っているものになったら、表示どおりの品にする
                if shop_mod.BY_ID[got].unique and got in owned:
                    got, outcome = item.id, tm.SAME
            self._account.apply(
                gid, user_id, -total, f"帝愛の出品:{shop_mod.label(item)}" + (f"×{qty}" if qty > 1 else "")
            )
            self._conn.execute("UPDATE teiai_market SET qty = qty - ? WHERE id=?", (qty, listing_id))
            self._inventory.add(gid, user_id, got, qty)
            self._bank.bank_income_for(gid, total, now)
            return MarketPurchase(
                True,
                balance=self._account.raw_balance(gid, user_id),
                shown=item.id,
                got=got,
                outcome=outcome,
                qty=qty,
                total=total,
            )
