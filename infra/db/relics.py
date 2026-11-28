"""地下作業の発掘品（保有照会・効果集計・合体）。"""

from domain import relics as relics_mod
from domain.errors import Outcome, fail
from domain.ports.repos import IRelicRepo
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo
from infra.db.inventory import InventoryCore


class RelicRepo(ScopedRepo, IRelicRepo):
    """発掘品のバフ集計・在庫照会・合体。"""

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        guild_id: int = 0,
        strict: bool = False,
        *,
        inventory: InventoryCore | None = None,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._inventory = inventory or InventoryCore(db)

    def relic_buffs_for(self, guild_id: int, user_id: int) -> dict[str, float]:
        """持っている発掘品の効果 {効果の名前: 上がり幅}（個数ぶん重なる。出品中の品は持ち物にないので、効かない）。"""
        held = self._inventory.items_with_prefix(guild_id, user_id, "relic_")
        return relics_mod.buffs_of(held)

    def _relic_buffs(self, user_id: int) -> dict[str, float]:
        return self.relic_buffs_for(self._g(), user_id)

    def relic_stock_for(self, guild_id: int, user_id: int) -> dict[str, int]:
        """持ち物にある発掘品 {品のID: 個数}（★つきも含む）。"""
        with self._lock:
            return self._inventory.items_with_prefix(guild_id, user_id, "relic_")

    def relic_stock(self, user_id: int) -> dict[str, int]:
        return self.relic_stock_for(self._g(), user_id)

    def relic_merge(self, user_id: int, item_id: str) -> Outcome[str]:
        """同じ発掘品を MERGE_COUNT 個つかって、★が1つ増えた1個に合体する。(成功か, 理由か合体後の品のID)。"""
        base, star = relics_mod.split_id(item_id)
        if base not in relics_mod.BUFFS:
            return False, fail("relic_not_relic")
        if star >= relics_mod.STAR_MAX:
            return False, fail("relic_max_star", star_max=relics_mod.STAR_MAX)
        new_id = relics_mod.star_id(base, star + 1)
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            if not self._inventory.consume(gid, user_id, item_id, relics_mod.MERGE_COUNT):
                return False, fail("relic_need_more", count=relics_mod.MERGE_COUNT)
            self._inventory.add(gid, user_id, new_id, 1)
        return True, new_id
