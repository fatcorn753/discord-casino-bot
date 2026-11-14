"""ファイナル総資産: ペリカに換算できるものを、すべて合計した資産（ランキングと、人ごとの内訳）。

`/ペリカ ランキング` の純資産（現金・預金・株・債券など）に、馬・持ち物・発掘品・地下作業の工具・預かり中のお金・貸し借りなどを足したもの。
各資産カテゴリの集計は IAssetCollector を実装する独立コレクタとして分離し、CompositeAssetCollector で合成する（OCP）。
"""

import json
import time
from typing import Callable, Optional

from domain import horse as horse_mod
from domain import labor as labor_mod
from domain import shop
from domain.types import (
    FIXED_KEYS as FIXED_KEYS,  # noqa: F401
)
from domain.types import (
    LIABILITY_KEYS as LIABILITY_KEYS,  # noqa: F401
)
from domain.types import (
    LIQUID_KEYS as LIQUID_KEYS,  # noqa: F401
)
from domain.types import (
    PART_LABELS as PART_LABELS,  # noqa: F401
)
from infra.db.base import EcoBase

AddPart = Callable[[int, str, int], None]


def _item_worth(item_id: str) -> tuple[int, bool]:
    """(持ち物の1個あたりの価値, 発掘品か)。値段のない限定品は 0。"""
    if item_id in shop.RELIC_VALUE:
        return shop.RELIC_VALUE[item_id], True
    item = shop.BY_ID.get(item_id)
    return (item.price if item else 0), False


def _add_item_value(add: AddPart, uid: int, item_id: str, qty: int) -> None:
    worth, relic = _item_worth(item_id)
    add(uid, "relics" if relic else "items", worth * qty)


class IAssetCollector:
    """1 つの資産カテゴリを parts 辞書へ加算するコレクタ契約。"""

    def collect(self, eco: "AssetMixin", guild_id: int, add: AddPart, now: float) -> None:
        raise NotImplementedError


class CompositeAssetCollector(IAssetCollector):
    """複数の IAssetCollector を束ねて順次適用する Composite。"""

    def __init__(self, collectors: tuple[IAssetCollector, ...]) -> None:
        self._collectors = collectors

    def collect(self, eco: "AssetMixin", guild_id: int, add: AddPart, now: float) -> None:
        for collector in self._collectors:
            collector.collect(eco, guild_id, add, now)


class CashAndDebtCollector(IAssetCollector):
    def collect(self, eco: "AssetMixin", guild_id: int, add: AddPart, now: float) -> None:
        for r in eco._conn.execute("SELECT user_id, balance, debt FROM users WHERE guild_id=?", (guild_id,)):
            add(r["user_id"], "cash", r["balance"])
            add(r["user_id"], "debt", -r["debt"])


class BankAssetCollector(IAssetCollector):
    def collect(self, eco: "AssetMixin", guild_id: int, add: AddPart, now: float) -> None:
        for uid, v in eco.bank_values().items():
            add(uid, "deposit", v)


class CryptoAssetCollector(IAssetCollector):
    def collect(self, eco: "AssetMixin", guild_id: int, add: AddPart, now: float) -> None:
        for uid, v in eco.crypto_values().items():
            add(uid, "crypto", v)
        for uid, v in eco.margin_equity().items():
            add(uid, "crypto", v)


class BondAssetCollector(IAssetCollector):
    def collect(self, eco: "AssetMixin", guild_id: int, add: AddPart, now: float) -> None:
        for uid, v in eco.bond_values().items():
            add(uid, "bonds", v)


class OptionAssetCollector(IAssetCollector):
    def collect(self, eco: "AssetMixin", guild_id: int, add: AddPart, now: float) -> None:
        for uid, v in eco.option_values().items():
            add(uid, "options", v)


class InventoryAssetCollector(IAssetCollector):
    def collect(self, eco: "AssetMixin", guild_id: int, add: AddPart, now: float) -> None:
        for r in eco._conn.execute(
            "SELECT user_id, item_id, qty FROM inventory WHERE guild_id=? AND qty>0", (guild_id,)
        ):
            _add_item_value(add, r["user_id"], r["item_id"], r["qty"])


class LaborAssetCollector(IAssetCollector):
    def collect(self, eco: "AssetMixin", guild_id: int, add: AddPart, now: float) -> None:
        for r in eco._conn.execute(
            "SELECT user_id, relics, pot, pick, glove, dorm FROM labor WHERE guild_id=?", (guild_id,)
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
    def collect(self, eco: "AssetMixin", guild_id: int, add: AddPart, now: float) -> None:
        for r in eco._conn.execute(
            "SELECT owner_id, races, wins, places FROM horses WHERE guild_id=? AND owner_id>0 AND rented_day IS NULL",
            (guild_id,),
        ):
            add(r["owner_id"], "horses", eco.horse_worth(r["races"], r["wins"], r["places"]))


class P2PAssetCollector(IAssetCollector):
    """競り・出品中の品・馬は売り手、入札中のお金は入札者の資産として加算する。"""

    def collect(self, eco: "AssetMixin", guild_id: int, add: AddPart, now: float) -> None:
        for r in eco._conn.execute("SELECT * FROM market_listings WHERE guild_id=? AND status='active'", (guild_id,)):
            if r["kind"] == "item":
                _add_item_value(add, r["seller_id"], r["ref"], r["qty"])
            else:
                h = eco._conn.execute("SELECT races, wins, places FROM horses WHERE id=?", (int(r["ref"]),)).fetchone()
                if h:
                    add(r["seller_id"], "horses", eco.horse_worth(h["races"], h["wins"], h["places"]))
            if r["bidder_id"]:
                add(r["bidder_id"], "escrow", r["bid"])


class LoanAssetCollector(IAssetCollector):
    def collect(self, eco: "AssetMixin", guild_id: int, add: AddPart, now: float) -> None:
        for r in eco._conn.execute(
            "SELECT * FROM loans WHERE guild_id=? AND status IN ('active','overdue')", (guild_id,)
        ):
            left = eco.loan_remaining(dict(r), now)
            add(r["lender_id"], "loans_lent", left)
            add(r["borrower_id"], "loans_owed", -left)


DEFAULT_ASSET_COLLECTOR = CompositeAssetCollector(
    (
        CashAndDebtCollector(),
        BankAssetCollector(),
        CryptoAssetCollector(),
        BondAssetCollector(),
        OptionAssetCollector(),
        InventoryAssetCollector(),
        LaborAssetCollector(),
        HorseAssetCollector(),
        P2PAssetCollector(),
        LoanAssetCollector(),
    )
)


class AssetMixin(EcoBase):
    def _item_worth(self, item_id: str) -> tuple:
        return _item_worth(item_id)

    def horse_worth(self, races: int, wins: int, places: int) -> int:
        """馬1頭の評価額: 購入価格に、いま引退したときの引退金（戦績が悪ければ引退費用）を足す。0 未満にはしない。"""
        return max(0, self.HORSE_PRICE + horse_mod.retire_value(races, wins, places))

    def asset_parts(self, now: Optional[float] = None) -> dict:
        """このサーバーの {ユーザーID: {内訳のキー: 金額}}。登録のある全員。"""
        now = time.time() if now is None else now
        with self._lock:
            g = self._g()
            parts = {
                r["user_id"]: dict.fromkeys(PART_LABELS, 0)
                for r in self._conn.execute("SELECT user_id FROM users WHERE guild_id=?", (g,))
            }

            def add(uid: int, key: str, value: int) -> None:
                if uid in parts and value:
                    parts[uid][key] += value

            DEFAULT_ASSET_COLLECTOR.collect(self, g, add, now)
        return parts

    def final_assets(self, user_ids: Optional[set] = None, now: Optional[float] = None) -> list:
        """[(ユーザーID, ファイナル総資産, 内訳)] を多い順に（同額はユーザーID順）。user_ids を渡すと、その人たちだけ。"""
        out = [
            (uid, sum(p.values()), p) for uid, p in self.asset_parts(now).items() if user_ids is None or uid in user_ids
        ]
        return sorted(out, key=lambda t: (-t[1], t[0]))

    def final_top(self, limit: int = 10, user_ids: Optional[set] = None) -> list:
        """[(順位, ユーザーID, ファイナル総資産), ...]。"""
        return [(i + 1, uid, total) for i, (uid, total, _p) in enumerate(self.final_assets(user_ids)[:limit])]

    def final_rank_of(self, user_id: int, user_ids: Optional[set] = None) -> Optional[tuple]:
        """(順位, ファイナル総資産, 対象人数)。登録がなければ None。"""
        ranked = self.final_assets(user_ids)
        for i, (uid, total, _p) in enumerate(ranked):
            if uid == user_id:
                return i + 1, total, len(ranked)
        return None
