"""ショップの持ち物・装備・消耗品（Economy に混ぜ込む部品）。

表の定義は core/economy.py の _init_schema にある。
"""

import time
from typing import Optional

from domain import shop as shop_mod
from domain import teiai
from domain.types import CONSUMABLE_EFFECTS, PurchaseResult
from infra.db.base import EcoBase


class ShopMixin(EcoBase):
    """ショップの持ち物・装備・消耗品（Economy に混ぜ込む部品）。"""

    # ------------------------------------------------------------------ #
    # ショップ（持ち物・装備・消耗品）                                    #
    # ------------------------------------------------------------------ #
    SLOTS = ("title", "card_color", "card_icon", "skin_dice", "skin_table")

    def _ensure_profile(self, user_id: int) -> None:
        self._conn.execute("INSERT OR IGNORE INTO profile (guild_id, user_id) VALUES (?, ?)", (self._g(), user_id))

    def shop_stock(self, user_id: int, item_id: str, now: Optional[float] = None) -> Optional[dict]:
        """在庫のある品の、このプレイヤーの在庫 {stock: 買える数, max, next_at: 次に1つ増える時刻（満タンなら None）}。在庫のない品は None。

        読み取りだけ（記録がなければ、満タン）。
        """
        rule = shop_mod.STOCK_RULES.get(item_id)
        if rule is None:
            return None
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT stock, updated FROM shop_stock WHERE guild_id=? AND user_id=? AND item_id=?",
                (self._g(), user_id, item_id),
            ).fetchone()
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
        now: Optional[float] = None,
    ) -> PurchaseResult:
        """購入。支払い・持ち物の追加を1回の処理で行う。持っているもの（消耗品以外）は買えない。在庫のある品は、在庫の数まで。"""
        if qty < 1 or price <= 0:
            return PurchaseResult(False, "個数と値段は1以上にしてください。")
        total = price * qty
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._ensure(user_id)
            stock = self.shop_stock(user_id, item_id, now)
            if stock is not None and qty > stock["stock"]:
                when = f"（次の補充: <t:{int(stock['next_at'])}:R>）" if stock["next_at"] else ""
                return PurchaseResult(
                    False,
                    f"在庫が足りません（いまの在庫: {stock['stock']}個）{when}。在庫は1人ずつ別々で、時間がたつと補充されます。",
                    self._balance(user_id),
                )
            have = self._conn.execute(
                "SELECT qty FROM inventory WHERE guild_id=? AND user_id=? AND item_id=?", (self._g(), user_id, item_id)
            ).fetchone()
            if unique and have:
                return PurchaseResult(False, "すでに持っています。", self._balance(user_id))
            if self._debt(user_id) > 0:
                return PurchaseResult(
                    False,
                    f"借金が {self._debt(user_id):,}ペリカ 残っているので、買い物はできません。",
                    self._balance(user_id),
                )
            if self._balance(user_id) < total:
                return PurchaseResult(
                    False,
                    f"ペリカが足りません（{total:,}ペリカ必要・残高 {self._balance(user_id):,}ペリカ）。",
                    self._balance(user_id),
                )
            self._apply(user_id, -total, f"購入:{label}" + (f"×{qty}" if qty > 1 else ""))
            if stock is not None:  # 在庫を減らす（減らしたあとの在庫と、その時刻を記録する）
                self._conn.execute(
                    "INSERT INTO shop_stock (guild_id, user_id, item_id, stock, updated) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET stock=excluded.stock, updated=excluded.updated",
                    (self._g(), user_id, item_id, stock["exact"] - qty, now),
                )
            self._conn.execute(
                "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty = qty + excluded.qty",
                (self._g(), user_id, item_id, qty),
            )
            return PurchaseResult(True, balance=self._balance(user_id))

    def inventory(self, user_id: int) -> dict:
        with self._lock:
            return {
                r["item_id"]: r["qty"]
                for r in self._conn.execute(
                    "SELECT item_id, qty FROM inventory WHERE guild_id=? AND user_id=? AND qty>0", (self._g(), user_id)
                )
            }

    def get_profile(self, user_id: int, raw: bool = False) -> dict:
        """装備中の称号・カード・演出、消耗品の効果、身分（status）。読み取り専用（口座は作らない）。

        カタギでないとき（借金・延滞・破産者）は、買った称号・カード・演出が「差し押さえ中」なので、装備の欄は None で返り、
        frozen が True になる（装備の記録は残り、カタギに戻ると使える）。raw=True なら、差し押さえ中でも、記録のとおりに返す。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM profile WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
            prof = (
                dict(row)
                if row
                else {"user_id": user_id, **dict.fromkeys(self.SLOTS), "daily_boost": 0, "insurance": 0}
            )
            prof["status"] = self.status(user_id)
        prof["frozen"] = prof["status"] != teiai.KATAGI
        if prof["frozen"] and not raw:
            for slot in self.SLOTS:
                prof[slot] = None
        return prof

    def equip(self, user_id: int, slot: str, item_id: Optional[str]) -> bool:
        """称号・カードの色・アイコンを装備（None で外す）。持っていないものは装備できない。"""
        if slot not in self.SLOTS:
            raise ValueError(slot)
        with self._lock, self._conn:
            if (
                item_id is not None
                and not self._conn.execute(
                    "SELECT 1 FROM inventory WHERE guild_id=? AND user_id=? AND item_id=? AND qty>0",
                    (self._g(), user_id, item_id),
                ).fetchone()
            ):
                return False
            self._ensure_profile(user_id)
            self._conn.execute(
                f"UPDATE profile SET {slot}=? WHERE guild_id=? AND user_id=?", (item_id, self._g(), user_id)
            )
            return True

    def arm(self, user_id: int, item_id: str) -> bool:
        """消耗品を1つ使って、その効果を有効にする。持っていない・消耗品でないものは False。"""
        effect = CONSUMABLE_EFFECTS.get(item_id)
        if effect is None:
            return False
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE inventory SET qty = qty - 1 WHERE guild_id=? AND user_id=? AND item_id=? AND qty>0",
                (self._g(), user_id, item_id),
            )
            if not cur.rowcount:
                return False
            self._ensure_profile(user_id)
            self._conn.execute(
                f"UPDATE profile SET {effect} = {effect} + 1 WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            )
            return True
