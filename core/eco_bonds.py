"""債券の記録（Economy に混ぜ込む部品）。購入・満期の決着・倒産抽選・売却。

表の定義は core/economy.py の _init_schema にある。利率などの数字は core/bonds.py。
満期の利息は、帝愛の手元現金から払う（新しく作らない。現金が足りなければ利息が減る。元本は必ず全額返す）。
"""

import random
import time
from typing import Optional

from core import bonds as bonds_mod
from core import market as market_mod


class BondMixin:
    """債券（元本を預かり、満期に利息つきで返す。ジャンク債は倒産することがある）。購入・満期の決着・解約。"""

    def bond_buy(self, user_id: int, kind_key: str, amount: int, now: float) -> tuple:
        """債券を買う。(成功か, 理由) を返す。"""
        kind = bonds_mod.KINDS.get(kind_key)
        if kind is None:
            return False, "債券の種類が正しくありません。"
        with self._lock, self._conn:
            self._ensure(user_id)
            if amount < bonds_mod.MIN_AMOUNT:
                return False, f"{bonds_mod.MIN_AMOUNT:,}ペリカ以上で買ってください。"
            if self._debt(user_id) > 0:
                return False, f"借金が {self._debt(user_id):,}ペリカ 残っているので、買えません。"
            if self._balance(user_id) < amount:
                return False, f"残高が足りません（いまの残高: {self._balance(user_id):,}ペリカ）。"
            held = self._bond_held(user_id, kind_key)
            if held + amount > kind.max_total:
                return (
                    False,
                    f"{kind.name}は元本の合計が {kind.max_total:,}ペリカ まで（いま {held:,}ペリカ 保有）。あと {kind.max_total - held:,}ペリカ まで買えます。",
                )
            self._apply(user_id, -amount, f"債券購入:{kind.name}")
            self._conn.execute(
                "INSERT INTO bonds (guild_id, user_id, kind, principal, due, ts) VALUES (?, ?, ?, ?, ?, ?)",
                (self._g(), user_id, kind_key, amount, now + kind.hours * 3600, now),
            )
            return True, ""

    def _bond_held(self, user_id: int, kind_key: str) -> int:
        return self._conn.execute(
            "SELECT COALESCE(SUM(principal), 0) FROM bonds WHERE guild_id=? AND user_id=? AND kind=? AND status='active'",
            (self._g(), user_id, kind_key),
        ).fetchone()[0]

    def bond_values(self) -> dict:
        """{ユーザーID: 預けている元本の合計}（このサーバーの、保有中の債券。ランキングの総資産に含める）。"""
        with self._lock:
            return {
                r["user_id"]: r["p"]
                for r in self._conn.execute(
                    "SELECT user_id, SUM(principal) AS p FROM bonds WHERE guild_id=? AND status='active' GROUP BY user_id",
                    (self._g(),),
                )
            }

    def bond_list(self, user_id: int, recent: int = 5) -> dict:
        """保有中（満期が近い順）と、直近に決着したもの（新しい順）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM bonds WHERE guild_id=? AND user_id=? ORDER BY id", (self._g(), user_id)
            ).fetchall()
        active = sorted((dict(r) for r in rows if r["status"] == "active"), key=lambda r: r["due"])
        done = [dict(r) for r in reversed(rows) if r["status"] != "active"][:recent]
        return {"active": active, "done": done}

    def bond_settle(self, now: float, rng=None) -> list:
        """ジャンク債の倒産抽選と、満期を過ぎた債券の決着（このサーバー全員分）。決着した行（status と paid つき）を返す。

        倒産抽選は、前回の抽選から now（満期を過ぎていれば満期）までの時間ぶんを、まとめて1回で引く（確率 1-exp(-λ×経過時間)）。
        1分ごとに呼ばれても、止まっていたあとにまとめて呼ばれても、倒産する確率の合計は同じ。
        """
        rng = rng or random
        out = []
        with self._lock, self._conn:
            for row in self._conn.execute(
                "SELECT * FROM bonds WHERE guild_id=? AND status='active' ORDER BY id", (self._g(),)
            ).fetchall():
                kind = bonds_mod.KINDS[row["kind"]]
                until = min(now, row["due"])
                since = row["checked"] if row["checked"] is not None else row["ts"]
                if kind.default_pct and until > since:
                    if rng.random() < bonds_mod.default_probability(kind, (until - since) / 3600):
                        self._conn.execute("UPDATE bonds SET status='defaulted', paid=0 WHERE id=?", (row["id"],))
                        out.append({**dict(row), "status": "defaulted", "paid": 0})
                        continue
                    self._conn.execute("UPDATE bonds SET checked=? WHERE id=?", (until, row["id"]))
                if row["due"] > now:
                    continue
                # 利息は、帝愛の手元現金から払う（新しく作らない。預金者には手を付けない。現金が足りなければ、利息が減る）
                interest = max(0, bonds_mod.maturity_payout(kind, row["principal"]) - row["principal"])
                got_interest = 0
                if interest:
                    state = self._bank_sync(now)
                    got_interest = int(min(interest, state["cash"]))
                    if got_interest:
                        self._bank_set(state["cash"] - got_interest, state["loaned"])
                paid = row["principal"] + got_interest
                self._ensure(row["user_id"])
                self._apply(row["user_id"], paid, f"債券償還:{kind.name}", income=got_interest)
                self._repay_all_of(row["user_id"], row["principal"], f"帝愛への返済（債券の元本:{kind.name}）")
                self._conn.execute("UPDATE bonds SET status='paid', paid=? WHERE id=?", (paid, row["id"]))
                out.append({**dict(row), "status": "paid", "paid": paid})
        return out

    def bond_price(self, row: dict, now: float) -> int:
        """その債券を、いま売ったときの手取り（売れない債券は 0）。表示用。"""
        kind = bonds_mod.KINDS[row["kind"]]
        return bonds_mod.sale_value(kind, row["principal"], now, row["due"]) if kind.sellable else 0

    def bond_cancel(self, user_id: int, bond_id: int, now: Optional[float] = None) -> tuple:
        """満期前に、DCF の値段で売る（通常債だけ）。(成功か, 理由か受け取った額) を返す。"""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT kind, principal FROM bonds WHERE id=? AND guild_id=? AND user_id=? AND status='active'",
                (bond_id, self._g(), user_id),
            ).fetchone()
            if row is not None and self._margin_rows(user_id):
                drop = row["principal"] * market_mod.COLLATERAL_HAIRCUT.get(row["kind"], 0.0)
                after = self._margin_status_locked(user_id, self.latest_prices(), drop_collateral=drop)
                if after["ratio"] is not None and after["ratio"] < market_mod.OPEN_RATIO:
                    return (
                        False,
                        f"信用取引の担保になっているので、売れません（売ると維持率が {after['ratio']:.0%}・{market_mod.OPEN_RATIO:.0%} 以上が必要）。"
                        "先に建玉を決済してください。",
                    )
            return self._bond_cancel_locked(user_id, bond_id, now)

    def _bond_cancel_locked(self, user_id: int, bond_id: int, now: Optional[float] = None) -> tuple:
        """bond_cancel の本体（呼び出し側が、ロックとトランザクションの中で使う）。"""
        row = self._conn.execute(
            "SELECT * FROM bonds WHERE id=? AND guild_id=? AND user_id=? AND status='active'",
            (bond_id, self._g(), user_id),
        ).fetchone()
        if row is None:
            return False, "その番号の債券は持っていません。"
        kind = bonds_mod.KINDS[row["kind"]]
        if not kind.sellable:
            return False, f"{kind.name}は売れません（満期まで持つだけです）。"
        refund = bonds_mod.sale_value(kind, row["principal"], time.time() if now is None else now, row["due"])
        self._apply(user_id, refund, "返金:債券売却")  # もともと自分のお金なので、借金の徴収はしない
        self._conn.execute("UPDATE bonds SET status='cancelled', paid=? WHERE id=?", (refund, bond_id))
        return True, refund
