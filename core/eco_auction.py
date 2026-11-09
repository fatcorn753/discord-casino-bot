"""帝愛の競売（Economy に混ぜ込む部品）。破産で差し押さえたショップの品を、カタギが買えるようにする。

出品されるのは、消耗品と見た目の品（称号・カード・演出・色ロール）。コレクションと限定品は出さない。
値段は、定価の AUCTION_START から1日ごとに AUCTION_STEP ずつ下がり、AUCTION_FLOOR で止まる（core/teiai.py）。
売上は帝愛銀行の収入になる。表は core/schema.py の seized_items。
"""

import time
from typing import Optional

from core import shop as shop_mod
from core import teiai
from core.eco_types import PurchaseResult


def listable(item_id: str) -> bool:
    """競売に出せる品か（限定品とコレクションは出さない）。"""
    item = shop_mod.BY_ID.get(item_id)
    return item is not None and not item.limited and item.kind != shop_mod.COLLECTION and item.price > 0


class AuctionMixin:
    def _auction_list_locked(self, item_id: str, qty: int, now: float) -> None:
        if qty > 0 and listable(item_id):
            self._conn.execute(
                "INSERT INTO seized_items (guild_id, item_id, qty, listed_at) VALUES (?, ?, ?, ?)",
                (self._g(), item_id, qty, now),
            )

    def auction_price(self, item_id: str, listed_at: float, now: Optional[float] = None) -> int:
        """1つあたりの、いまの値段（ペリカ）。"""
        now = time.time() if now is None else now
        return max(1, round(shop_mod.BY_ID[item_id].price * teiai.auction_rate(listed_at, now)))

    def auction_list(self, now: Optional[float] = None) -> list:
        """競売の棚。品ごとに、残りの数と、いちばん安い（古い）出品の値段。[{item_id, qty, price, rate}]"""
        now = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                "SELECT item_id, qty, listed_at FROM seized_items WHERE guild_id=? AND qty>0 ORDER BY listed_at, id",
                (self._g(),),
            ).fetchall()
        out = {}
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

    def auction_buy(self, user_id: int, item_id: str, qty: int = 1, now: Optional[float] = None) -> PurchaseResult:
        """競売で買う。カタギだけ。持っているもの（消耗品以外）は買えない。安い（古い）出品から順に売れる。"""
        now = time.time() if now is None else now
        item = shop_mod.BY_ID.get(item_id)
        if item is None or qty < 1:
            return PurchaseResult(False, "その品は競売にありません。")
        with self._lock, self._conn:
            self._ensure(user_id)
            if self.status(user_id, now) != teiai.KATAGI:
                return PurchaseResult(
                    False, "競売で買えるのは、借金も延滞もないカタギだけです。", self._balance(user_id)
                )
            have = self._conn.execute(
                "SELECT qty FROM inventory WHERE guild_id=? AND user_id=? AND item_id=? AND qty>0",
                (self._g(), user_id, item_id),
            ).fetchone()
            if item.unique and have:
                return PurchaseResult(False, "すでに持っています。", self._balance(user_id))
            if item.unique:
                qty = 1
            rows = self._conn.execute(
                "SELECT id, qty, listed_at FROM seized_items WHERE guild_id=? AND item_id=? AND qty>0 "
                "ORDER BY listed_at, id",
                (self._g(), item_id),
            ).fetchall()
            if sum(r["qty"] for r in rows) < qty:
                return PurchaseResult(False, "その数は残っていません。", self._balance(user_id))
            take, total, need = [], 0, qty
            for r in rows:
                n = min(need, r["qty"])
                take.append((r["id"], n))
                total += n * self.auction_price(item_id, r["listed_at"], now)
                need -= n
                if need == 0:
                    break
            if self._balance(user_id) < total:
                return PurchaseResult(
                    False,
                    f"ペリカが足りません（{total:,}ペリカ必要・残高 {self._balance(user_id):,}ペリカ）。",
                    self._balance(user_id),
                )
            self._apply(user_id, -total, f"競売:{shop_mod.label(item)}" + (f"×{qty}" if qty > 1 else ""))
            for row_id, n in take:
                self._conn.execute("UPDATE seized_items SET qty = qty - ? WHERE id=?", (n, row_id))
            self._conn.execute(
                "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty = qty + excluded.qty",
                (self._g(), user_id, item_id, qty),
            )
            self._bank_income(total, teiai.AUCTION_YIELD_SHARE, now)
            return PurchaseResult(True, balance=self._balance(user_id))
