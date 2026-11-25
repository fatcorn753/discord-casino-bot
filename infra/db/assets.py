"""貸借対照表（B/S）: ペリカに換算できる資産と負債を、人ごとに区分して集計する（ランキングと、人ごとの内訳）。

流動資産（現金・信託・株式・債券など）＋固定資産（馬・持ち物・発掘品・会社・地下作業の工具など）＝総資産、
総資産 − 負債（帝愛への借金・個人からの借入）＝純資産。用語は docs/glossary.md を参照。
各資産カテゴリの集計は独立したファクトリ関数が `AssetCollector`（Callable）を返す形に分離し、
`compose_asset_collectors` で合成する（OCP）。
"""

from collections.abc import Callable

from domain import horse as horse_mod
from domain import labor as labor_mod
from domain import shop
from domain.ports.bridges import (
    AddAssetPart,
    AssetCollector,
    IOverdueLoanCollector,
    ValuationSource,
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


def cash_and_debt_collector(db: EcoDb) -> AssetCollector:
    conn = db.conn

    def collect(guild_id: int, add: AddAssetPart, now: float) -> None:
        for r in fetch_all(
            conn,
            "SELECT a.user_id, a.balance, COALESCE(d.debt, 0) AS debt "
            "FROM accounts a LEFT JOIN teiai_debts d ON d.guild_id=a.guild_id AND d.user_id=a.user_id "
            "WHERE a.guild_id=?",
            (guild_id,),
        ):
            add(r["user_id"], "cash", r["balance"])
            add(r["user_id"], "debt", -r["debt"])

    return collect


def valuation_source_collector(part_key: str, source: ValuationSource) -> AssetCollector:
    """ValuationSource の集計結果を任意の資産カテゴリキー（part_key）へ加算する汎用コレクタ（OCP）。"""

    def collect(guild_id: int, add: AddAssetPart, now: float) -> None:
        totals: dict[int, int] = {}
        source(guild_id, totals)
        for uid, v in totals.items():
            add(uid, part_key, v)

    return collect


def inventory_asset_collector(db: EcoDb) -> AssetCollector:
    conn = db.conn

    def collect(guild_id: int, add: AddAssetPart, now: float) -> None:
        for r in fetch_all(conn, "SELECT user_id, item_id, qty FROM inventory WHERE guild_id=? AND qty>0", (guild_id,)):
            _add_item_value(add, r["user_id"], r["item_id"], r["qty"])

    return collect


def labor_asset_collector(db: EcoDb) -> AssetCollector:
    conn = db.conn

    def collect(guild_id: int, add: AddAssetPart, now: float) -> None:
        for r in fetch_all(conn, "SELECT user_id, pot FROM labor_pot WHERE guild_id=? AND pot>0", (guild_id,)):
            add(r["user_id"], "pot", r["pot"])
        for r in fetch_all(
            conn, "SELECT user_id, pick, glove, dorm FROM labor_upgrades WHERE guild_id=?", (guild_id,)
        ):
            add(
                r["user_id"],
                "tools",
                labor_mod.upgrade_spent(r["pick"])
                + labor_mod.upgrade_spent(r["glove"])
                + labor_mod.dorm_spent(r["dorm"]),
            )
        for r in fetch_all(
            conn,
            "SELECT user_id, item_id, qty FROM labor_pending_relics WHERE guild_id=? AND qty>0",
            (guild_id,),
        ):
            _add_item_value(add, r["user_id"], r["item_id"], r["qty"])

    return collect


def horse_asset_collector(db: EcoDb, horse_worth_fn: Callable[[int, int, int], int]) -> AssetCollector:
    conn = db.conn

    def collect(guild_id: int, add: AddAssetPart, now: float) -> None:
        for r in fetch_all(
            conn,
            "SELECT o.owner_id, h.races, h.wins, h.places "
            "FROM horse_ownership o JOIN horses h ON h.id=o.horse_id WHERE o.guild_id=?",
            (guild_id,),
        ):
            add(r["owner_id"], "horses", horse_worth_fn(r["races"], r["wins"], r["places"]))

    return collect


def p2p_asset_collector(db: EcoDb, horse_worth_fn: Callable[[int, int, int], int]) -> AssetCollector:
    """競り・出品中の品・馬は売り手、入札中のお金は入札者の資産として加算する。"""
    conn = db.conn

    def collect(guild_id: int, add: AddAssetPart, now: float) -> None:
        for r in fetch_all(
            conn,
            "SELECT l.seller_id, l.kind, l.ref, l.qty, COALESCE(b.bidder_id, 0) AS bidder_id, "
            "COALESCE(b.amount, 0) AS bid "
            "FROM market_listings l LEFT JOIN market_auction_bids b ON b.listing_id = l.id "
            "WHERE l.guild_id=? AND l.status='active'",
            (guild_id,),
        ):
            if r["seller_id"] is not None and r["kind"] == "item":
                _add_item_value(add, r["seller_id"], r["ref"], r["qty"])
            elif r["seller_id"] is not None:
                h = fetch_one(conn, "SELECT races, wins, places FROM horses WHERE id=?", (int(r["ref"]),))
                if h:
                    add(r["seller_id"], "horses", horse_worth_fn(h["races"], h["wins"], h["places"]))
            if r["bidder_id"]:
                add(r["bidder_id"], "escrow", r["bid"])

    return collect


def loan_asset_collector(db: EcoDb, loans: IOverdueLoanCollector) -> AssetCollector:
    conn = db.conn

    def collect(guild_id: int, add: AddAssetPart, now: float) -> None:
        for r in fetch_all(
            conn,
            "SELECT l.id, l.guild_id, COALESCE(l.lender_id, 0) AS lender_id, l.borrower_id, "
            "l.principal, l.rate_tenths, l.term_days, l.status, l.created, l.offer_expires, "
            "t.accepted_at, t.due, COALESCE(t.paid, 0) AS paid, COALESCE(t.reminded, 0) AS reminded "
            "FROM loans l LEFT JOIN loan_terms t ON t.loan_id = l.id "
            "WHERE l.guild_id=? AND l.status IN ('active','overdue')",
            (guild_id,),
        ):
            left = loans.loan_remaining(dict(r), now)
            add(r["lender_id"], "loans_lent", left)
            add(r["borrower_id"], "loans_owed", -left)

    return collect


class AssetRepo(ScopedRepo, IAssetRepo):
    """資産の内訳（B/S）と、純資産のランキング。"""

    def __init__(
        self,
        db: EcoDb,
        collector: AssetCollector,
        horse_worth_fn: Callable[[int, int, int], int] | None = None,
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
                for r in self._all("SELECT user_id FROM accounts WHERE guild_id=?", (g,))
            }

            def add(uid: int, key: str, value: int) -> None:
                if uid in parts and value:
                    parts[uid][key] += value

            self._collector(g, add, now)
        return parts

    def net_assets(
        self, user_ids: set[int] | None = None, now: float | None = None
    ) -> list[tuple[int, int, dict[str, int]]]:
        """[(ユーザーID, 純資産, 内訳)] を多い順に（同額はユーザーID順）。user_ids を渡すと、その人たちだけ。"""
        out = [
            (uid, sum(p.values()), p)
            for uid, p in self.asset_parts(self._now() if now is None else now).items()
            if user_ids is None or uid in user_ids
        ]
        return sorted(out, key=lambda t: (-t[1], t[0]))

    def net_top(self, limit: int = 10, user_ids: set[int] | None = None) -> list[RankEntry]:
        """[(順位, ユーザーID, 純資産), ...]。"""
        return [(i + 1, uid, total) for i, (uid, total, _p) in enumerate(self.net_assets(user_ids)[:limit])]

    def net_rank_of(self, user_id: int, user_ids: set[int] | None = None) -> RankEntry | None:
        """(順位, 純資産, 対象人数)。登録がなければ None。"""
        ranked = self.net_assets(user_ids)
        for i, (uid, total, _p) in enumerate(ranked):
            if uid == user_id:
                return i + 1, total, len(ranked)
        return None
