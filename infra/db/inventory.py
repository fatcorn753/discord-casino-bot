"""所持品インベントリプリミティブ（全リポジトリがコンポジションで共有する inventory 台帳）。"""

from infra.db.base import EcoDb, fetch_all, fetch_scalar


class InventoryCore:
    """アイテム所持数の全読み書きを担う共有ストア（呼び出し側のトランザクション内・または読み取りロック内で使う）。"""

    def __init__(self, db: EcoDb) -> None:
        self._db = db
        self._conn = db.conn
        self._lock = db.lock

    def qty(self, guild_id: int, user_id: int, item_id: str) -> int:
        v = fetch_scalar(
            self._conn,
            "SELECT qty FROM inventory WHERE guild_id=? AND user_id=? AND item_id=?",
            (guild_id, user_id, item_id),
        )
        return int(v) if v is not None else 0

    def has(self, guild_id: int, user_id: int, item_id: str) -> bool:
        return (
            fetch_scalar(
                self._conn,
                "SELECT 1 FROM inventory WHERE guild_id=? AND user_id=? AND item_id=? AND qty>0",
                (guild_id, user_id, item_id),
            )
            is not None
        )

    def items(self, guild_id: int, user_id: int) -> dict[str, int]:
        rows = fetch_all(
            self._conn,
            "SELECT item_id, qty FROM inventory WHERE guild_id=? AND user_id=? AND qty>0",
            (guild_id, user_id),
        )
        return {r["item_id"]: int(r["qty"]) for r in rows}

    def item_ids(self, guild_id: int, user_id: int) -> set[str]:
        rows = fetch_all(
            self._conn,
            "SELECT item_id FROM inventory WHERE guild_id=? AND user_id=? AND qty>0",
            (guild_id, user_id),
        )
        return {r["item_id"] for r in rows}

    def items_with_prefix(self, guild_id: int, user_id: int, prefix: str) -> dict[str, int]:
        rows = fetch_all(
            self._conn,
            "SELECT item_id, qty FROM inventory WHERE guild_id=? AND user_id=? AND qty>0 AND item_id LIKE ?",
            (guild_id, user_id, f"{prefix}%"),
        )
        return {r["item_id"]: int(r["qty"]) for r in rows}

    def holders_of(self, guild_id: int, item_id: str) -> list[int]:
        rows = fetch_all(
            self._conn,
            "SELECT user_id FROM inventory WHERE guild_id=? AND item_id=?",
            (guild_id, item_id),
        )
        return [int(r["user_id"]) for r in rows]

    def add(self, guild_id: int, user_id: int, item_id: str, qty: int = 1) -> None:
        if qty <= 0:
            return
        self._conn.execute(
            "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty = qty + excluded.qty",
            (guild_id, user_id, item_id, qty),
        )

    def consume(self, guild_id: int, user_id: int, item_id: str, qty: int = 1) -> bool:
        if qty <= 0:
            return False
        cur = self._conn.execute(
            "UPDATE inventory SET qty = qty - ? WHERE guild_id=? AND user_id=? AND item_id=? AND qty >= ?",
            (qty, guild_id, user_id, item_id, qty),
        )
        return cur.rowcount > 0

    def delete_for_user(self, guild_id: int, user_id: int) -> None:
        self._conn.execute("DELETE FROM inventory WHERE guild_id=? AND user_id=?", (guild_id, user_id))

    def delete_item_in_guild(self, guild_id: int, item_id: str) -> None:
        self._conn.execute("DELETE FROM inventory WHERE guild_id=? AND item_id=?", (guild_id, item_id))
