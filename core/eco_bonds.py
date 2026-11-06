"""債券の記録（Economy に混ぜ込む部品）。購入・満期の決着・解約。

表の定義は core/economy.py の _init_schema にある。利率などの数字は core/bonds.py。
"""

import random

from core import bonds as bonds_mod


class BondMixin:
    """債券の記録（Economy に混ぜ込む部品）。購入・満期の決着・解約。"""

    # ------------------------------------------------------------------ #
    # 債券（元本を預かり、満期に利息つきで返す。ジャンク債は倒産することがある）#
    # ------------------------------------------------------------------ #
    def bond_buy(self, user_id: int, kind_key: str, amount: int, now: float) -> tuple:
        """債券を買う。(成功か, 理由) を返す。"""
        kind = bonds_mod.KINDS[kind_key]
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
                return False, f"{kind.name}は元本の合計が {kind.max_total:,}ペリカ まで（いま {held:,}ペリカ 保有）。あと {kind.max_total - held:,}ペリカ まで買えます。"
            self._apply(user_id, -amount, f"債券購入:{kind.name}", "債券")
            self._conn.execute("INSERT INTO bonds (guild_id, user_id, kind, principal, due, ts) VALUES (?, ?, ?, ?, ?, ?)",
                               (self._g(), user_id, kind_key, amount, now + kind.hours * 3600, now))
            return True, ""

    def _bond_held(self, user_id: int, kind_key: str) -> int:
        return self._conn.execute("SELECT COALESCE(SUM(principal), 0) FROM bonds WHERE guild_id=? AND user_id=? AND kind=? AND status='active'",
                                  (self._g(), user_id, kind_key)).fetchone()[0]

    def bond_values(self) -> dict:
        """{ユーザーID: 預けている元本の合計}（このサーバーの、保有中の債券。ランキングの総資産に含める）。"""
        with self._lock:
            return {r["user_id"]: r["p"] for r in self._conn.execute(
                "SELECT user_id, SUM(principal) AS p FROM bonds WHERE guild_id=? AND status='active' GROUP BY user_id", (self._g(),))}

    def bond_list(self, user_id: int, recent: int = 5) -> dict:
        """保有中（満期が近い順）と、直近に決着したもの（新しい順）。"""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM bonds WHERE guild_id=? AND user_id=? ORDER BY id", (self._g(), user_id)).fetchall()
        active = sorted((dict(r) for r in rows if r["status"] == "active"), key=lambda r: r["due"])
        done = [dict(r) for r in reversed(rows) if r["status"] != "active"][:recent]
        return {"active": active, "done": done}

    def bond_settle(self, now: float, rng=None) -> list:
        """満期を過ぎた債券を決着させる（このサーバー全員分）。決着した行（status と paid つき）を返す。"""
        rng = rng or random
        out = []
        with self._lock, self._conn:
            for row in self._conn.execute("SELECT * FROM bonds WHERE guild_id=? AND status='active' AND due<=? ORDER BY id", (self._g(), now)).fetchall():
                kind = bonds_mod.KINDS[row["kind"]]
                if rng.random() * 100 < kind.default_pct:
                    status, paid = "defaulted", 0
                else:
                    status, paid = "paid", bonds_mod.maturity_payout(kind, row["principal"])
                    self._ensure(row["user_id"])
                    self._apply(row["user_id"], paid, f"債券償還:{kind.name}", "債券")
                self._conn.execute("UPDATE bonds SET status=?, paid=? WHERE id=?", (status, paid, row["id"]))
                out.append({**dict(row), "status": status, "paid": paid})
        return out

    def bond_cancel(self, user_id: int, bond_id: int) -> tuple:
        """途中解約（通常債だけ）。(成功か, 理由か返した額) を返す。"""
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM bonds WHERE id=? AND guild_id=? AND user_id=? AND status='active'", (bond_id, self._g(), user_id)).fetchone()
            if row is None:
                return False, "その番号の債券は持っていません。"
            kind = bonds_mod.KINDS[row["kind"]]
            if kind.cancel_penalty is None:
                return False, f"{kind.name}は解約できません。"
            refund = bonds_mod.cancel_refund(kind, row["principal"])
            self._apply(user_id, refund, "返金:債券解約", "債券")      # もともと自分のお金なので、借金の徴収はしない
            self._conn.execute("UPDATE bonds SET status='cancelled', paid=? WHERE id=?", (refund, bond_id))
            return True, refund
