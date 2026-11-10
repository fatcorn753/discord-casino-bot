"""定期決算イベントの記録と、裏情報メモ（Economy に混ぜ込む部品）。数字と抽選は core/earnings.py。

市場は全サーバー共通なので、決算の予定はサーバーに属さない（表は core/schema.py の earnings）。メモは、サーバーごと・人ごと。
"""

import random
import time
from typing import Optional

from core import earnings as ea
from core import market as market_mod
from core import shop as shop_mod
from core import teiai


class EarningsMixin:
    def earnings_ensure(self, now: float, tz_hours: float, rng=None) -> Optional[dict]:
        """発表前の決算が無ければ、次の決算を決めて登録する。登録したら、その行を返す。"""
        rng = rng or random.SystemRandom()
        with self._lock, self._conn:
            if self._conn.execute("SELECT 1 FROM earnings WHERE released=0 LIMIT 1").fetchone():
                return None
            p = ea.plan(now, tz_hours, rng)
            cur = self._conn.execute(
                "INSERT INTO earnings (symbol, announce_at, release_at, direction, size) VALUES (?, ?, ?, ?, ?)",
                (p["symbol"], p["announce_at"], p["release_at"], p["direction"], p["size"]),
            )
            self._conn.execute("DELETE FROM earnings WHERE released=1 AND release_at<?", (now - 30 * 86400,))
            return {**p, "id": cur.lastrowid}

    def earnings_upcoming(self, now: Optional[float] = None) -> Optional[dict]:
        """みんなに告知ずみの、発表前の決算（対象と時刻だけ。方向と大きさは入れない）。月曜 9:00 までは None。"""
        now = time.time() if now is None else now
        with self._lock:
            r = self._conn.execute(
                "SELECT id, symbol, announce_at, release_at FROM earnings WHERE released=0 AND announce_at<=? "
                "ORDER BY release_at LIMIT 1",
                (now,),
            ).fetchone()
            return dict(r) if r else None

    def earnings_to_announce(self, now: float) -> list:
        """告知の時刻を過ぎて、まだ告知していない決算。呼ぶと、告知ずみにする。"""
        with self._lock, self._conn:
            rows = [
                dict(r)
                for r in self._conn.execute(
                    "SELECT id, symbol, release_at FROM earnings WHERE released=0 AND announced=0 AND announce_at<=?",
                    (now,),
                )
            ]
            for r in rows:
                self._conn.execute("UPDATE earnings SET announced=1 WHERE id=?", (r["id"],))
            return rows

    def earnings_release_due(self, now: float) -> list:
        """発表の時刻を過ぎた決算（まだ発表していないもの）を、発表ずみにして返す。理論価格に反映するのは、呼び出し側。"""
        with self._lock, self._conn:
            rows = [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM earnings WHERE released=0 AND release_at<=? ORDER BY release_at", (now,)
                )
            ]
            for r in rows:
                self._conn.execute("UPDATE earnings SET released=1 WHERE id=?", (r["id"],))
            return rows

    def earnings_memo(self, user_id: int, now: Optional[float] = None, rng=None) -> Optional[str]:
        """デイリーのあとに、確率で届く、裏情報メモ。発表前の決算が無い・この決算のメモをもう受け取っている・抽選に外れたら None。"""
        now = time.time() if now is None else now
        rng = rng or random.SystemRandom()
        with self._lock, self._conn:
            ev = self._conn.execute(
                "SELECT id, symbol, direction, release_at FROM earnings WHERE released=0 AND release_at>? "
                "ORDER BY release_at LIMIT 1",
                (now,),
            ).fetchone()
            if ev is None or rng.random() >= ea.MEMO_PROB:
                return None
            if self._conn.execute(
                "SELECT 1 FROM earnings_memos WHERE event_id=? AND guild_id=? AND user_id=?",
                (ev["id"], self._g(), user_id),
            ).fetchone():
                return None
            hint = ea.memo_direction(ev["direction"], rng)
            self._conn.execute(
                "INSERT INTO earnings_memos (event_id, guild_id, user_id, hint) VALUES (?, ?, ?, ?)",
                (ev["id"], self._g(), user_id, hint),
            )
            return ea.memo_text(market_mod.BY_KEY[ev["symbol"]].name, hint, ev["release_at"])

    def insider_stock(self, now: Optional[float] = None) -> Optional[dict]:
        """帝愛市の裏情報メモの在庫。{残り, 全部の数}。発表前の決算が無ければ None（売っていない）。"""
        now = time.time() if now is None else now
        with self._lock:
            ev = self._conn.execute(
                "SELECT id FROM earnings WHERE released=0 AND release_at>? ORDER BY release_at LIMIT 1", (now,)
            ).fetchone()
            if ev is None:
                return None
            sold = self._conn.execute(
                "SELECT COUNT(*) FROM earnings_shop WHERE event_id=? AND guild_id=?", (ev["id"], self._g())
            ).fetchone()[0]
            return {"left": max(0, ea.SHOP_STOCK - sold), "total": ea.SHOP_STOCK}

    def insider_buy(self, user_id: int, now: Optional[float] = None, rng=None) -> tuple:
        """帝愛市で、裏情報メモを買う。(成功か, 理由かメモの文)。決算ごとに1人1枚、サーバーで SHOP_STOCK 枚まで。"""
        now = time.time() if now is None else now
        rng = rng or random.SystemRandom()
        price = shop_mod.BY_ID[shop_mod.INSIDER_MEMO].price
        with self._lock, self._conn:
            self._ensure(user_id)
            ev = self._conn.execute(
                "SELECT id, symbol, direction, release_at FROM earnings WHERE released=0 AND release_at>? "
                "ORDER BY release_at LIMIT 1",
                (now,),
            ).fetchone()
            if ev is None:
                return False, "いま、売れる情報はありません（次の決算が決まるまで、お待ちください）。"
            if self._conn.execute(
                "SELECT 1 FROM earnings_shop WHERE event_id=? AND guild_id=? AND user_id=?",
                (ev["id"], self._g(), user_id),
            ).fetchone():
                return False, "この決算の裏情報メモは、もう買っています。"
            stock = self.insider_stock(now)
            if stock["left"] <= 0:
                return False, "この決算の裏情報メモは、売り切れです（サーバーで5枚まで）。"
            if self._debt(user_id) > 0:
                return False, f"借金が {self._debt(user_id):,}ペリカ 残っているので、買い物はできません。"
            if self._balance(user_id) < price:
                return False, f"ペリカが足りません（{price:,}ペリカ必要・残高 {self._balance(user_id):,}ペリカ）。"
            hint = ea.memo_direction(ev["direction"], rng, ea.SHOP_TRUTH)
            self._apply(user_id, -price, "購入:裏情報メモ（帝愛市）")
            self._bank_income(price, teiai.AUCTION_YIELD_SHARE, now)
            self._conn.execute(
                "INSERT INTO earnings_shop (event_id, guild_id, user_id, hint, price, ts) VALUES (?, ?, ?, ?, ?, ?)",
                (ev["id"], self._g(), user_id, hint, price, now),
            )
            return True, ea.memo_text(market_mod.BY_KEY[ev["symbol"]].name, hint, ev["release_at"], ea.SHOP_TRUTH)
