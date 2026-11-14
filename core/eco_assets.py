"""ファイナル総資産: ペリカに換算できるものを、すべて合計した資産（ランキングと、人ごとの内訳）。

`/ペリカ ランキング` の純資産（現金・預金・株・債券など）に、馬・持ち物・発掘品・地下作業の工具・預かり中のお金・貸し借りなどを足したもの。
売れない着せ替え・コレクションも、買値で数える。評価の元は、次のとおり（売値ではなく、目安の価値）。
"""

import json
import time
from typing import Optional

from core import horse as horse_mod
from core import labor as labor_mod
from core import shop

# 内訳の名前（表示順）。キーは asset_parts() の辞書のキー
PART_LABELS = {
    "cash": "💴 現金（借金を引く）",
    "deposit": "🏦 帝愛への預金",
    "crypto": "📈 株・CS（現物と証拠金）",
    "bonds": "📜 債券",
    "options": "🎯 オプション",
    "items": "🎁 持ち物（買値）",
    "relics": "🏺 発掘品（目安の価値）",
    "horses": "🐎 競走馬（購入価格＋引退金の見込み）",
    "pot": "⛏ 受け取る前の地下作業の稼ぎ",
    "tools": "🧰 地下作業の工具・寮（これまでの強化費用）",
    "escrow": "🔒 競りへの入札（預かり中）",
    "loans": "🤝 貸し借り（貸した分−借りた分）",
}


class AssetMixin:
    def _item_worth(self, item_id: str) -> tuple:
        """(持ち物の1個あたりの価値, 発掘品か)。値段のない限定品は 0。"""
        if item_id in shop.RELIC_VALUE:
            return shop.RELIC_VALUE[item_id], True
        item = shop.BY_ID.get(item_id)
        return (item.price if item else 0), False

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

            def add(uid, key, value):
                if uid in parts and value:
                    parts[uid][key] += value

            for r in self._conn.execute("SELECT user_id, balance - debt AS v FROM users WHERE guild_id=?", (g,)):
                add(r["user_id"], "cash", r["v"])
            for uid, v in self.bank_values().items():
                add(uid, "deposit", v)
            for uid, v in self.crypto_values().items():
                add(uid, "crypto", v)
            for uid, v in self.margin_equity().items():
                add(uid, "crypto", v)
            for uid, v in self.bond_values().items():
                add(uid, "bonds", v)
            for uid, v in self.option_values().items():
                add(uid, "options", v)

            def item(uid, item_id, qty):
                worth, relic = self._item_worth(item_id)
                add(uid, "relics" if relic else "items", worth * qty)

            for r in self._conn.execute("SELECT user_id, item_id, qty FROM inventory WHERE guild_id=? AND qty>0", (g,)):
                item(r["user_id"], r["item_id"], r["qty"])
            for r in self._conn.execute(
                "SELECT user_id, relics, pot, pick, glove, dorm FROM labor WHERE guild_id=?", (g,)
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
                    item(r["user_id"], item_id, int(qty))
            for r in self._conn.execute(
                "SELECT owner_id, races, wins, places FROM horses WHERE guild_id=? AND owner_id>0 AND rented_day IS NULL",
                (g,),
            ):
                add(r["owner_id"], "horses", self.horse_worth(r["races"], r["wins"], r["places"]))
            # 競りに出している品・馬は、まだ持ち主のもの。入札中のお金は、預かり中でも入札した人のもの
            for r in self._conn.execute("SELECT * FROM market_listings WHERE guild_id=? AND status='active'", (g,)):
                if r["kind"] == "item":
                    item(r["seller_id"], r["ref"], r["qty"])
                else:
                    h = self._conn.execute(
                        "SELECT races, wins, places FROM horses WHERE id=?", (int(r["ref"]),)
                    ).fetchone()
                    if h:
                        add(r["seller_id"], "horses", self.horse_worth(h["races"], h["wins"], h["places"]))
                if r["bidder_id"]:
                    add(r["bidder_id"], "escrow", r["bid"])
            for r in self._conn.execute(
                "SELECT * FROM loans WHERE guild_id=? AND status IN ('active','overdue')", (g,)
            ):
                left = self.loan_remaining(dict(r), now)
                add(r["lender_id"], "loans", left)
                add(r["borrower_id"], "loans", -left)
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
