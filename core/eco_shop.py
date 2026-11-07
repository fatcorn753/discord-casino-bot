"""ショップの持ち物・装備・消耗品（Economy に混ぜ込む部品）。

表の定義は core/economy.py の _init_schema にある。
"""

from typing import Optional

from core.eco_types import CONSUMABLE_EFFECTS, PurchaseResult


class ShopMixin:
    """ショップの持ち物・装備・消耗品（Economy に混ぜ込む部品）。"""

    # ------------------------------------------------------------------ #
    # ショップ（持ち物・装備・消耗品）                                    #
    # ------------------------------------------------------------------ #
    SLOTS = ("title", "card_color", "card_icon")

    def _ensure_profile(self, user_id: int) -> None:
        self._conn.execute("INSERT OR IGNORE INTO profile (guild_id, user_id) VALUES (?, ?)", (self._g(), user_id))

    def purchase(
        self, user_id: int, item_id: str, label: str, price: int, unique: bool, qty: int = 1
    ) -> PurchaseResult:
        """購入。支払い・持ち物の追加を1回の処理で行う。持っているもの（消耗品以外）は買えない。"""
        if qty < 1 or price <= 0:
            return PurchaseResult(False, "個数と値段は1以上にしてください。")
        total = price * qty
        with self._lock, self._conn:
            self._ensure(user_id)
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

    def get_profile(self, user_id: int) -> dict:
        """装備中の称号・カード、消耗品の効果。読み取り専用（口座は作らない）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM profile WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
        return (
            dict(row)
            if row
            else {
                "user_id": user_id,
                "title": None,
                "card_color": None,
                "card_icon": None,
                "daily_boost": 0,
                "insurance": 0,
            }
        )

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
