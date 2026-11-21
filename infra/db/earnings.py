"""定期決算イベントの記録と、裏情報メモ。数字と抽選は domain/earnings.py。

市場は全サーバー共通なので、決算の予定はサーバーに属さない（表は infra/db/schema/tables.py の earnings）。メモは、サーバーごと・人ごと。
"""

import random
from typing import Any, Optional

from domain import earnings as ea
from domain import market as market_mod
from domain import shop as shop_mod
from domain.errors import fail
from domain.ports.bridges import IBankCashFlow
from domain.ports.repos import IEarningsRepo
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo


class EarningsRepo(ScopedRepo, IEarningsRepo):
    """定期決算の予定・裏情報メモ・帝愛市の販売。"""

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        bank: IBankCashFlow,
        guild_id: int = 0,
        strict: bool = False,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._bank = bank

    def earnings_ensure(
        self, now: float, tz_hours: float, rng: Optional[random.Random] = None
    ) -> Optional[dict[str, Any]]:
        """発表前の決算が無ければ、次の決算を決めて登録する。登録したら、その行を返す。"""
        rng = rng or random.SystemRandom()
        with self._db.tx():
            if self._one("SELECT 1 FROM earnings WHERE released=0 LIMIT 1"):
                return None
            p = ea.plan(now, tz_hours, rng)
            cur = self._conn.execute(
                "INSERT INTO earnings (symbol, announce_at, release_at, direction, size) VALUES (?, ?, ?, ?, ?)",
                (p["symbol"], p["announce_at"], p["release_at"], p["direction"], p["size"]),
            )
            self._conn.execute("DELETE FROM earnings WHERE released=1 AND release_at<?", (now - 30 * 86400,))
            return {**p, "id": cur.lastrowid}

    def earnings_jumps(self, now: float, seconds: float) -> list[dict[str, Any]]:
        """now から seconds 秒の間に発表がある決算を、オプションの値付け用の形にする（対象・発表の時刻・分散）。方向は入れない。"""
        with self._lock:
            return [
                {"symbol": r["symbol"], "jump_at": r["release_at"], "jump_var": ea.jump_variance()}
                for r in self._all(
                    "SELECT symbol, release_at FROM earnings WHERE released=0 AND release_at>? AND release_at<=?",
                    (now, now + seconds),
                )
            ]

    def earnings_upcoming(self, now: float) -> Optional[dict[str, Any]]:
        """みんなに告知ずみの、発表前の決算（対象と時刻だけ。方向と大きさは入れない）。月曜 9:00 までは None。"""
        with self._lock:
            return self._one(
                "SELECT id, symbol, announce_at, release_at FROM earnings WHERE released=0 AND announce_at<=? "
                "ORDER BY release_at LIMIT 1",
                (now,),
            )

    def earnings_to_announce(self, now: float) -> list[dict[str, Any]]:
        """告知の時刻を過ぎて、まだ告知していない決算。呼ぶと、告知ずみにする。"""
        with self._db.tx():
            rows = self._all(
                "SELECT id, symbol, release_at FROM earnings WHERE released=0 AND announced=0 AND announce_at<=?",
                (now,),
            )
            for r in rows:
                self._conn.execute("UPDATE earnings SET announced=1 WHERE id=?", (r["id"],))
            return rows

    def earnings_release_due(self, now: float) -> list[dict[str, Any]]:
        """発表の時刻を過ぎた決算（まだ発表していないもの）を、発表ずみにして返す。理論価格に反映するのは、呼び出し側。"""
        with self._db.tx():
            rows = self._all("SELECT * FROM earnings WHERE released=0 AND release_at<=? ORDER BY release_at", (now,))
            for r in rows:
                self._conn.execute("UPDATE earnings SET released=1 WHERE id=?", (r["id"],))
            return rows

    def earnings_memo(self, user_id: int, now: float, rng: Optional[random.Random] = None) -> Optional[ea.MemoInfo]:
        """デイリーのあとに、確率で届く、裏情報メモ。発表前の決算が無い・発表まで MEMO_LEAD_HOURS より先・
        この決算のメモをもう受け取っている・抽選に外れたら None。"""
        rng = rng or random.SystemRandom()
        with self._db.tx():
            gid = self._g()
            ev = self._one(
                "SELECT id, symbol, direction, release_at FROM earnings WHERE released=0 AND release_at>? "
                "ORDER BY release_at LIMIT 1",
                (now,),
            )
            if ev is not None and ev["release_at"] - now > ea.MEMO_LEAD_HOURS * 3600:
                return None  # 発表がまだ先なので、このデイリーでは抽選しない(帝愛市のメモ売りは今までどおり)
            if ev is None or rng.random() >= ea.MEMO_PROB:
                return None
            if self._one(
                "SELECT 1 FROM earnings_memos WHERE event_id=? AND guild_id=? AND user_id=?",
                (ev["id"], gid, user_id),
            ):
                return None
            hint = ea.memo_direction(ev["direction"], rng)
            self._conn.execute(
                "INSERT INTO earnings_memos (event_id, guild_id, user_id, hint) VALUES (?, ?, ?, ?)",
                (ev["id"], gid, user_id, hint),
            )
            return ea.build_memo(market_mod.BY_KEY[ev["symbol"]].name, hint, ev["release_at"])

    def insider_stock(self, now: float) -> Optional[dict[str, Any]]:
        """帝愛市の裏情報メモの在庫。{残り, 全部の数}。発表前の決算が無ければ None（売っていない）。"""
        with self._lock:
            gid = self._g()
            ev = self._one(
                "SELECT id FROM earnings WHERE released=0 AND release_at>? ORDER BY release_at LIMIT 1", (now,)
            )
            if ev is None:
                return None
            sold = self._scalar("SELECT COUNT(*) FROM earnings_shop WHERE event_id=? AND guild_id=?", (ev["id"], gid))
            return {"left": max(0, ea.SHOP_STOCK - sold), "total": ea.SHOP_STOCK}

    def insider_buy(self, user_id: int, now: float, rng: Optional[random.Random] = None) -> tuple[Any, ...]:
        """帝愛市で、裏情報メモを買う。(成功か, 理由かメモの文)。決算ごとに1人1枚、サーバーで SHOP_STOCK 枚まで。"""
        rng = rng or random.SystemRandom()
        price = shop_mod.BY_ID[shop_mod.INSIDER_MEMO].price
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            ev = self._one(
                "SELECT id, symbol, direction, release_at FROM earnings WHERE released=0 AND release_at>? "
                "ORDER BY release_at LIMIT 1",
                (now,),
            )
            if ev is None:
                return False, fail("insider_no_event")
            if self._one(
                "SELECT 1 FROM earnings_shop WHERE event_id=? AND guild_id=? AND user_id=?",
                (ev["id"], gid, user_id),
            ):
                return False, fail("insider_already_bought")
            stock = self.insider_stock(now)
            if stock is None or stock["left"] <= 0:
                return False, fail("insider_sold_out")
            debt = self._account.debt(gid, user_id)
            if debt > 0:
                return False, fail("debt_blocks", debt=debt, verb="買い物はできません")
            bal = self._account.raw_balance(gid, user_id)
            if bal < price:
                return False, fail("insufficient_balance_need", need=price, balance=bal)
            hint = ea.memo_direction(ev["direction"], rng, ea.SHOP_TRUTH)
            self._account.apply(gid, user_id, -price, "購入:裏情報メモ（帝愛市）")
            self._bank.bank_income_for(gid, price, now)
            self._conn.execute(
                "INSERT INTO earnings_shop (event_id, guild_id, user_id, hint, price, ts) VALUES (?, ?, ?, ?, ?, ?)",
                (ev["id"], gid, user_id, hint, price, now),
            )
            return True, ea.build_memo(market_mod.BY_KEY[ev["symbol"]].name, hint, ev["release_at"], ea.SHOP_TRUTH)
