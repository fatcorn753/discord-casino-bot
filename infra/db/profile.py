"""ユーザープロフィール（装備・バフ）プリミティブ（user_equipment / user_buffs の単一所有者）。"""

from infra.db.base import EcoDb, fetch_all, fetch_scalar


class ProfileCore:
    """装備スロットと消耗バフの全読み書きを担う共有ストア（呼び出し側のトランザクション内・または読み取りロック内で使う）。"""

    def __init__(self, db: EcoDb) -> None:
        self._db = db
        self._conn = db.conn
        self._lock = db.lock

    def equipment(self, guild_id: int, user_id: int) -> dict[str, str]:
        rows = fetch_all(
            self._conn,
            "SELECT slot, item_id FROM user_equipment WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        return {r["slot"]: r["item_id"] for r in rows}

    def buffs(self, guild_id: int, user_id: int) -> dict[str, int]:
        rows = fetch_all(
            self._conn,
            "SELECT buff, charges FROM user_buffs WHERE guild_id=? AND user_id=? AND charges>0",
            (guild_id, user_id),
        )
        return {r["buff"]: int(r["charges"]) for r in rows}

    def equip(self, guild_id: int, user_id: int, slot: str, item_id: str) -> None:
        self._conn.execute(
            "INSERT INTO user_equipment (guild_id, user_id, slot, item_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id, slot) DO UPDATE SET item_id = excluded.item_id",
            (guild_id, user_id, slot, item_id),
        )

    def unequip(self, guild_id: int, user_id: int, slot: str) -> None:
        self._conn.execute(
            "DELETE FROM user_equipment WHERE guild_id=? AND user_id=? AND slot=?", (guild_id, user_id, slot)
        )

    def unequip_if_matches(self, guild_id: int, user_id: int, slot: str, item_id: str) -> None:
        self._conn.execute(
            "DELETE FROM user_equipment WHERE guild_id=? AND user_id=? AND slot=? AND item_id=?",
            (guild_id, user_id, slot, item_id),
        )

    def unequip_item_in_guild(self, guild_id: int, slot: str, item_id: str) -> None:
        self._conn.execute(
            "DELETE FROM user_equipment WHERE guild_id=? AND slot=? AND item_id=?", (guild_id, slot, item_id)
        )

    def add_buff(self, guild_id: int, user_id: int, buff: str, charges: int = 1) -> None:
        if charges <= 0:
            return
        self._conn.execute(
            "INSERT INTO user_buffs (guild_id, user_id, buff, charges) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id, buff) DO UPDATE SET charges = charges + excluded.charges",
            (guild_id, user_id, buff, charges),
        )

    def consume_buff(self, guild_id: int, user_id: int, buff: str) -> bool:
        charges = fetch_scalar(
            self._conn,
            "SELECT charges FROM user_buffs WHERE guild_id=? AND user_id=? AND buff=?",
            (guild_id, user_id, buff),
        )
        if charges is None:
            return False
        if int(charges) > 1:
            self._conn.execute(
                "UPDATE user_buffs SET charges = charges - 1 WHERE guild_id=? AND user_id=? AND buff=?",
                (guild_id, user_id, buff),
            )
        else:
            self._conn.execute(
                "DELETE FROM user_buffs WHERE guild_id=? AND user_id=? AND buff=?", (guild_id, user_id, buff)
            )
        return True

    def clear_user(self, guild_id: int, user_id: int) -> None:
        self._conn.execute("DELETE FROM user_equipment WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        self._conn.execute("DELETE FROM user_buffs WHERE guild_id=? AND user_id=?", (guild_id, user_id))

    def equipment_for_users(self, guild_id: int, user_ids: list[int]) -> list[tuple[int, str, str]]:
        if not user_ids:
            return []
        marks = ",".join("?" * len(user_ids))
        rows = fetch_all(
            self._conn,
            f"SELECT user_id, slot, item_id FROM user_equipment WHERE guild_id=? AND user_id IN ({marks})",
            [guild_id, *user_ids],
        )
        return [(r["user_id"], r["slot"], r["item_id"]) for r in rows]

    def buffs_for_users(self, guild_id: int, user_ids: list[int]) -> list[tuple[int, str, int]]:
        if not user_ids:
            return []
        marks = ",".join("?" * len(user_ids))
        rows = fetch_all(
            self._conn,
            f"SELECT user_id, buff, charges FROM user_buffs WHERE guild_id=? AND user_id IN ({marks})",
            [guild_id, *user_ids],
        )
        return [(r["user_id"], r["buff"], int(r["charges"])) for r in rows]
