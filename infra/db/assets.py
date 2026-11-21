"""ファイナル総資産: ペリカに換算できるものを、すべて合計した資産（ランキングと、人ごとの内訳）。

`/ペリカ ランキング` の純資産（現金・預金・株・債券など）に、馬・持ち物・発掘品・地下作業の工具・預かり中のお金・貸し借りなどを足したもの。
各資産カテゴリの集計は IAssetCollector を実装する独立コレクタとして分離し、CompositeAssetCollector で合成する（OCP）。
"""

import json
from collections.abc import Callable
from typing import Optional

from domain import horse as horse_mod
from domain import labor as labor_mod
from domain import shop
from domain.ports.bridges import (
    AddAssetPart,
    IAssetCollector,
    IOverdueLoanCollector,
    IValuationSource,
)
from domain.ports.bridges import (
    CompositeAssetCollector as CompositeAssetCollector,
)
from domain.ports.contracts import RankEntry
from domain.ports.repos import IAssetRepo
from domain.types import (
    FIXED_KEYS as FIXED_KEYS,
)
from domain.types import (
    LIABILITY_KEYS as LIABILITY_KEYS,
)
from domain.types import (
    LIQUID_KEYS as LIQUID_KEYS,
)
from domain.types import PART_KEYS
from infra.db.base import EcoDb, ScopedRepo, fetch_all, fetch_one


def _item_worth(item_id: str) -> tuple[int, bool]:
    """(持ち物の1個あたりの価値, 発掘品か)。値段のない限定品は 0。"""
    if item_id in shop.RELIC_VALUE:
        return shop.RELIC_VALUE[item_id], True
    item = shop.BY_ID.get(item_id)
    return (item.price if item else 0), False


def _add_item_value(add: AddAssetPart, uid: int, item_id: str, qty: int) -> None:
    worth, relic = _item_worth(item_id)
    add(uid, "relics" if relic else "items", worth * qty)


def default_horse_worth(races: int, wins: int, places: int) -> int:
    price = shop.BY_ID[shop.HORSE_VOUCHER].price
    return max(0, price + horse_mod.retire_value(races, wins, places))


class CashAndDebtCollector(IAssetCollector):
    def __init__(self, db: EcoDb) -> None:
        self._conn = db.conn

    def collect(self, guild_id: int, add: AddAssetPart, now: float) -> None:
        for r in fetch_all(self._conn, "SELECT user_id, balance, debt FROM users WHERE guild_id=?", (guild_id,)):
            add(r["user_id"], "cash", r["balance"])
            add(r["user_id"], "debt", -r["debt"])


class ValuationSourceCollector(IAssetCollector):
    """IValuationSource の集計結果を任意の資産カテゴリキー（part_key）へ加算する汎用コレクタ（OCP）。"""

    def __init__(self, part_key: str, source: IValuationSource) -> None:
        self._part_key = part_key
        self._source = source

    def collect(self, guild_id: int, add: AddAssetPart, now: float) -> None:
        totals: dict[int, int] = {}
        self._source.collect_values(guild_id, totals)
        for uid, v in totals.items():
            add(uid, self._part_key, v)


class InventoryAssetCollector(IAssetCollector):
    def __init__(self, db: EcoDb) -> None:
        self._conn = db.conn

    def collect(self, guild_id: int, add: AddAssetPart, now: float) -> None:
        for r in fetch_all(
            self._conn, "SELECT user_id, item_id, qty FROM inventory WHERE guild_id=? AND qty>0", (guild_id,)
        ):
            _add_item_value(add, r["user_id"], r["item_id"], r["qty"])


class LaborAssetCollector(IAssetCollector):
    def __init__(self, db: EcoDb) -> None:
        self._conn = db.conn

    def collect(self, guild_id: int, add: AddAssetPart, now: float) -> None:
        for r in fetch_all(
            self._conn, "SELECT user_id, relics, pot, pick, glove, dorm FROM labor WHERE guild_id=?", (guild_id,)
        ):
            add(r["user_id"], "pot", r["pot"])
            add(
                r["user_id"],
                "tools",
                labor_mod.upgrade_spent(r["pick"])
                + labor_mod.upgrade_spent(r["glove"])
                + labor_mod.dorm_spent(r["dorm"]),
            )
            try:
                waiting = json.loads(r["relics"] or "{}")
            except ValueError:
                waiting = {}
            for item_id, qty in waiting.items():
                _add_item_value(add, r["user_id"], item_id, int(qty))


class HorseAssetCollector(IAssetCollector):
    def __init__(self, db: EcoDb, horse_worth_fn: Callable[[int, int, int], int]) -> None:
        self._conn = db.conn
        self._horse_worth = horse_worth_fn

    def collect(self, guild_id: int, add: AddAssetPart, now: float) -> None:
        for r in fetch_all(
            self._conn,
            "SELECT owner_id, races, wins, places FROM horses WHERE guild_id=? AND owner_id>0",
            (guild_id,),
        ):
            add(r["owner_id"], "horses", self._horse_worth(r["races"], r["wins"], r["places"]))


class P2PAssetCollector(IAssetCollector):
    """競り・出品中の品・馬は売り手、入札中のお金は入札者の資産として加算する。"""

    def __init__(self, db: EcoDb, horse_worth_fn: Callable[[int, int, int], int]) -> None:
        self._conn = db.conn
        self._horse_worth = horse_worth_fn

    def collect(self, guild_id: int, add: AddAssetPart, now: float) -> None:
        for r in fetch_all(
            self._conn, "SELECT * FROM market_listings WHERE guild_id=? AND status='active'", (guild_id,)
        ):
            if r["kind"] == "item":
                _add_item_value(add, r["seller_id"], r["ref"], r["qty"])
            else:
                h = fetch_one(self._conn, "SELECT races, wins, places FROM horses WHERE id=?", (int(r["ref"]),))
                if h:
                    add(r["seller_id"], "horses", self._horse_worth(h["races"], h["wins"], h["places"]))
            if r["bidder_id"]:
                add(r["bidder_id"], "escrow", r["bid"])


class LoanAssetCollector(IAssetCollector):
    def __init__(self, db: EcoDb, loans: IOverdueLoanCollector) -> None:
        self._conn = db.conn
        self._loans = loans

    def collect(self, guild_id: int, add: AddAssetPart, now: float) -> None:
        for r in fetch_all(
            self._conn, "SELECT * FROM loans WHERE guild_id=? AND status IN ('active','overdue')", (guild_id,)
        ):
            left = self._loans.loan_remaining(dict(r), now)
            add(r["lender_id"], "loans_lent", left)
            add(r["borrower_id"], "loans_owed", -left)


class AssetRepo(ScopedRepo, IAssetRepo):
    """総資産内訳とファイナルランキング。"""

    def __init__(
        self,
        db: EcoDb,
        collector: IAssetCollector,
        horse_worth_fn: Optional[Callable[[int, int, int], int]] = None,
        guild_id: int = 0,
        strict: bool = False,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._collector = collector
        self._horse_worth_fn = horse_worth_fn or default_horse_worth

    def horse_worth(self, races: int, wins: int, places: int) -> int:
        """馬1頭の評価額: 購入価格に、いま引退したときの引退金（成績が悪ければ引退費用）を足す。0 未満にはしない。"""
        return self._horse_worth_fn(races, wins, places)

    def asset_parts(self, now: float) -> dict[int, dict[str, int]]:
        """このサーバーの {ユーザーID: {内訳のキー: 金額}}。登録のある全員。"""
        with self._lock:
            g = self._g()
            parts: dict[int, dict[str, int]] = {
                r["user_id"]: dict.fromkeys(PART_KEYS, 0)
                for r in self._all("SELECT user_id FROM users WHERE guild_id=?", (g,))
            }

            def add(uid: int, key: str, value: int) -> None:
                if uid in parts and value:
                    parts[uid][key] += value

            self._collector.collect(g, add, now)
        return parts

    def final_assets(
        self, user_ids: Optional[set[int]] = None, now: Optional[float] = None
    ) -> list[tuple[int, int, dict[str, int]]]:
        """[(ユーザーID, ファイナル総資産, 内訳)] を多い順に（同額はユーザーID順）。user_ids を渡すと、その人たちだけ。"""
        out = [
            (uid, sum(p.values()), p)
            for uid, p in self.asset_parts(self._now() if now is None else now).items()
            if user_ids is None or uid in user_ids
        ]
        return sorted(out, key=lambda t: (-t[1], t[0]))

    def final_top(self, limit: int = 10, user_ids: Optional[set[int]] = None) -> list[RankEntry]:
        """[(順位, ユーザーID, ファイナル総資産), ...]。"""
        return [(i + 1, uid, total) for i, (uid, total, _p) in enumerate(self.final_assets(user_ids)[:limit])]

    def final_rank_of(self, user_id: int, user_ids: Optional[set[int]] = None) -> Optional[RankEntry]:
        """(順位, ファイナル総資産, 対象人数)。登録がなければ None。"""
        ranked = self.final_assets(user_ids)
        for i, (uid, total, _p) in enumerate(ranked):
            if uid == user_id:
                return i + 1, total, len(ranked)
        return None
