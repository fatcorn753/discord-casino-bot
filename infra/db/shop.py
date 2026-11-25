"""ショップの持ち物・装備・消耗品。

表の定義は infra/db/schema/ddl/shop.py にある。
"""

from typing import Any, cast

from domain import shop as shop_mod
from domain import teiai
from domain.errors import fail
from domain.ports.bridges import IStatusReader
from domain.ports.contracts import ProfileView, SeizedBelongings, ShopStockInfo
from domain.ports.repos import IShopRepo
from domain.types import CONSUMABLE_EFFECTS, PurchaseResult
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo


class ShopRepo(ScopedRepo, IShopRepo):
    """ショップの持ち物・装備・消耗品。"""

    SLOTS = ("title", "card_color", "card_icon", "skin_dice", "skin_table")

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        status_reader: IStatusReader,
        guild_id: int = 0,
        strict: bool = False,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._status_reader = status_reader

    def seize_belongings_for(self, guild_id: int, user_id: int) -> SeizedBelongings:
        """破産で、持ち物・装備・消耗品（デイリー2倍券・保険券）をすべて没収する。没収した品と個数を返す。

        呼び出し側のトランザクションの中で使う。
        """
        items = [
            (r["item_id"], r["qty"])
            for r in self._all(
                "SELECT item_id, qty FROM inventory WHERE guild_id=? AND user_id=? AND qty>0", (guild_id, user_id)
            )
        ]
        buff_items = {"daily_boost": shop_mod.DAILY_DOUBLE, "insurance": shop_mod.INSURANCE}
        for r in self._all(
            "SELECT buff, charges FROM user_buffs WHERE guild_id=? AND user_id=? AND charges>0", (guild_id, user_id)
        ):
            items.append((buff_items[r["buff"]], r["charges"]))
        self._conn.execute("DELETE FROM inventory WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        self._conn.execute("DELETE FROM user_equipment WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        self._conn.execute("DELETE FROM user_buffs WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        return SeizedBelongings(items=tuple(items))

    def shop_stock(self, user_id: int, item_id: str, now: float) -> ShopStockInfo | None:
        """在庫のある品の、このプレイヤーの在庫 {stock: 買える数, max, next_at: 次に1つ増える時刻（満タンなら None）}。在庫のない品は None。

        読み取りだけ（記録がなければ、満タン）。
        """
        rule = shop_mod.STOCK_RULES.get(item_id)
        if rule is None:
            return None
        with self._lock:
            row = self._one(
                "SELECT stock, updated FROM shop_stock WHERE guild_id=? AND user_id=? AND item_id=?",
                (self._g(), user_id, item_id),
            )
        exact = shop_mod.stock_now(rule, row["stock"], row["updated"], now) if row else float(rule.max)
        return {
            "stock": int(exact + 1e-9),
            "exact": exact,
            "max": rule.max,
            "next_at": shop_mod.next_refill_at(rule, exact, now),
        }

    def purchase(
        self,
        user_id: int,
        item_id: str,
        label: str,
        price: int,
        unique: bool,
        qty: int = 1,
        now: float | None = None,
    ) -> PurchaseResult:
        """購入。支払い・持ち物の追加を1回の処理で行う。持っているもの（消耗品以外）は買えない。在庫のある品は、在庫の数まで。"""
        if qty < 1 or price <= 0:
            return PurchaseResult(False, fail("shop_invalid_qty_or_price"))
        total = price * qty
        now = self._now() if now is None else now
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            stock = self.shop_stock(user_id, item_id, now)
            if stock is not None and qty > stock["stock"]:
                return PurchaseResult(
                    False,
                    fail("shop_out_of_stock", stock=stock["stock"], next_at=stock["next_at"]),
                    self._account.raw_balance(gid, user_id),
                )
            have = self._one(
                "SELECT qty FROM inventory WHERE guild_id=? AND user_id=? AND item_id=?",
                (gid, user_id, item_id),
            )
            if unique and have:
                return PurchaseResult(False, fail("already_owned"), self._account.raw_balance(gid, user_id))
            debt = self._account.debt(gid, user_id)
            if debt > 0:
                return PurchaseResult(
                    False,
                    fail("debt_blocks", debt=debt, verb="買い物はできません"),
                    self._account.raw_balance(gid, user_id),
                )
            bal = self._account.raw_balance(gid, user_id)
            if bal < total:
                return PurchaseResult(
                    False,
                    fail("insufficient_balance_need", need=total, balance=bal),
                    bal,
                )
            self._account.apply(gid, user_id, -total, f"購入:{label}" + (f"×{qty}" if qty > 1 else ""))
            if stock is not None:  # 在庫を減らす（減らしたあとの在庫と、その時刻を記録する）
                self._conn.execute(
                    "INSERT INTO shop_stock (guild_id, user_id, item_id, stock, updated) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET stock=excluded.stock, updated=excluded.updated",
                    (gid, user_id, item_id, stock["exact"] - qty, now),
                )
            self._conn.execute(
                "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty = qty + excluded.qty",
                (gid, user_id, item_id, qty),
            )
            return PurchaseResult(True, balance=self._account.raw_balance(gid, user_id))

    def inventory(self, user_id: int) -> dict[str, int]:
        with self._lock:
            return {
                r["item_id"]: r["qty"]
                for r in self._all(
                    "SELECT item_id, qty FROM inventory WHERE guild_id=? AND user_id=? AND qty>0",
                    (self._g(), user_id),
                )
            }

    def _load_profile(self, guild_id: int, user_id: int) -> dict[str, Any]:
        equip_rows = self._all(
            "SELECT slot, item_id FROM user_equipment WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )
        buff_rows = self._all(
            "SELECT buff, charges FROM user_buffs WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )
        prof: dict[str, Any] = {"user_id": user_id, **dict.fromkeys(self.SLOTS), "daily_boost": 0, "insurance": 0}
        if equip_rows or buff_rows:
            prof["guild_id"] = guild_id
        for r in equip_rows:
            if r["slot"] in self.SLOTS:
                prof[r["slot"]] = r["item_id"]
        for r in buff_rows:
            if r["buff"] in ("daily_boost", "insurance"):
                prof[r["buff"]] = r["charges"]
        return prof

    def get_profile(self, user_id: int, raw: bool = False) -> ProfileView:
        """装備中の称号・カード・演出、消耗品の効果、身分（status）。読み取り専用（口座は作らない）。

        カタギでないとき（借金・延滞・破産者）は、買った称号・カード・演出が「差し押さえ中」なので、装備の欄は None で返り、
        frozen が True になる（装備の記録は残り、カタギに戻ると使える）。raw=True なら、差し押さえ中でも、記録のとおりに返す。
        """
        with self._lock:
            gid = self._g()
            prof = self._load_profile(gid, user_id)
            prof["status"] = self._status_reader.status_for(gid, user_id, self._now())
        prof["frozen"] = prof["status"] != teiai.KATAGI
        if prof["frozen"] and not raw:
            for slot in self.SLOTS:
                prof[slot] = None
        return cast("ProfileView", prof)

    def equip(self, user_id: int, slot: str, item_id: str | None) -> bool:
        """称号・カードの色・アイコンを装備（None で外す）。持っていないものは装備できない。"""
        if slot not in self.SLOTS:
            raise ValueError(slot)
        with self._db.tx():
            gid = self._g()
            if item_id is None:
                self._conn.execute(
                    "DELETE FROM user_equipment WHERE guild_id=? AND user_id=? AND slot=?",
                    (gid, user_id, slot),
                )
                return True
            if not self._one(
                "SELECT 1 FROM inventory WHERE guild_id=? AND user_id=? AND item_id=? AND qty>0",
                (gid, user_id, item_id),
            ):
                return False
            self._conn.execute(
                "INSERT INTO user_equipment (guild_id, user_id, slot, item_id) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id, slot) DO UPDATE SET item_id=excluded.item_id",
                (gid, user_id, slot, item_id),
            )
            return True

    def arm(self, user_id: int, item_id: str) -> bool:
        """消耗品を1つ使って、その効果を有効にする。持っていない・消耗品でないものは False。"""
        effect = CONSUMABLE_EFFECTS.get(item_id)
        if effect is None:
            return False
        with self._db.tx():
            gid = self._g()
            cur = self._conn.execute(
                "UPDATE inventory SET qty = qty - 1 WHERE guild_id=? AND user_id=? AND item_id=? AND qty>0",
                (gid, user_id, item_id),
            )
            if not cur.rowcount:
                return False
            self._conn.execute(
                "INSERT INTO user_buffs (guild_id, user_id, buff, charges) VALUES (?, ?, ?, 1) "
                "ON CONFLICT(guild_id, user_id, buff) DO UPDATE SET charges = charges + 1",
                (gid, user_id, effect),
            )
            return True
