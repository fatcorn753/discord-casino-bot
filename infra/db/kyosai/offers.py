"""口の譲渡（プレイヤー間の売買）: 申し出・承諾・取消と、送金抜け道対策の上限。"""

from domain import kyosai, p2p
from domain.errors import Outcome, fail
from domain.ports.contracts import KyosaiOfferDoneRow, KyosaiOfferRow

from .auction import KyosaiRepo as Base
from .base import DAY


class KyosaiRepo(Base):
    def _offered_units_for(self, guild_id: int, seller: int, now: float) -> int:
        return self._scalar(
            "SELECT COALESCE(SUM(units), 0) FROM kyosai_offers "
            "WHERE guild_id=? AND seller=? AND status='open' AND expires>?",
            (guild_id, seller, now),
        )

    def _pair_total_for(self, guild_id: int, a: int, b: int, now: float) -> int:
        """この2人（どちらが売り手でも）の、24時間の取引額の合計。P2P の取引と、共済の口の譲渡を合わせて数える。"""
        kyosai_sum = self._scalar(
            "SELECT COALESCE(SUM(price), 0) FROM kyosai_offers WHERE guild_id=? AND status='done' AND done_at>? "
            "AND ((seller=? AND buyer=?) OR (seller=? AND buyer=?))",
            (guild_id, now - DAY, a, b, b, a),
        )
        market_sum = self._scalar(
            "SELECT COALESCE(SUM(c.sold_price), 0) "
            "FROM market_listings l JOIN market_closures c ON c.listing_id = l.id "
            "WHERE l.guild_id=? AND l.status='sold' AND c.closed_at>? "
            "AND ((l.seller_id=? AND c.buyer_id=?) OR (l.seller_id=? AND c.buyer_id=?))",
            (guild_id, now - DAY, a, b, b, a),
        )
        return kyosai_sum + market_sum

    def kyosai_offer(self, seller: int, buyer: int, units: int, price: int, now: float) -> Outcome[int]:
        """口の譲渡を申し出る（相手が受けると成立）。(成功か, Failure か申し出の番号)。

        送金の抜け道（高値で譲って、あとで 0 で返す）をふさぐため、P2P と同じく、売り手の受け取りから手数料を引き、
        同じ2人の24時間の取引額に上限を設ける（#34 のレビュー）。
        """
        if units <= 0:
            return False, fail("kyosai_bad_units")
        if price < 0:
            return False, fail("kyosai_bad_price")
        if price > p2p.PAIR_DAILY_LIMIT:
            return False, fail("p2p_pair_limit", limit=p2p.PAIR_DAILY_LIMIT, used=0, amount=price)
        if seller == buyer or buyer == kyosai.HOUSE:
            return False, fail("kyosai_bad_buyer")
        with self._db.tx():
            gid = self._g()
            self.bank_sync(gid, now)
            if self._gate_locked_for(gid, now):
                return False, fail("kyosai_gate_locked")
            free = self.units_of_for(gid, seller) - self._offered_units_for(gid, seller, now)
            if free < units:
                return False, fail("kyosai_units_short", have=max(0, free))
            cur = self._conn.execute(
                "INSERT INTO kyosai_offers (guild_id, seller, buyer, units, price, expires) VALUES (?, ?, ?, ?, ?, ?)",
                (gid, seller, buyer, units, price, now + kyosai.OFFER_HOURS * 3600),
            )
            lastrowid = cur.lastrowid
            assert lastrowid is not None
            return True, lastrowid

    def kyosai_accept(self, buyer: int, offer_id: int, now: float) -> Outcome[KyosaiOfferDoneRow]:
        """自分あての申し出を受ける。(成功か, Failure か申し出の行)。"""
        with self._db.tx():
            gid = self._g()
            self.bank_sync(gid, now)
            row: KyosaiOfferRow | None = self._one(
                "SELECT * FROM kyosai_offers WHERE id=? AND guild_id=? AND buyer=? AND status='open' AND expires>?",
                (offer_id, gid, buyer, now),
            )
            if row is None:
                return False, fail("kyosai_offer_not_found")
            if self._gate_locked_for(gid, now):
                return False, fail("kyosai_gate_locked")
            if self.units_of_for(gid, row["seller"]) < row["units"]:
                self._conn.execute("UPDATE kyosai_offers SET status='cancelled' WHERE id=?", (offer_id,))
                return False, fail("kyosai_offer_not_found")
            self._account.ensure(gid, buyer)
            debt = self._account.debt(gid, buyer)
            if debt > 0:
                return False, fail("debt_blocks", debt=debt, verb="買えません")
            bal = self._account.raw_balance(gid, buyer)
            if bal < row["price"]:
                return False, fail("insufficient_balance", balance=bal)
            used = self._pair_total_for(gid, row["seller"], buyer, now)
            if used + row["price"] > p2p.PAIR_DAILY_LIMIT:
                return False, fail("p2p_pair_limit", limit=p2p.PAIR_DAILY_LIMIT, used=used, amount=row["price"])
            fee = p2p.fee_of(row["price"])
            if row["price"]:
                self._account.apply(gid, buyer, -row["price"], "共済:口の購入")
                self._account.ensure(gid, row["seller"])
                net = row["price"] - fee
                self._account.apply(gid, row["seller"], net, "共済:口の売却", income=net)
                self._bank.bank_income_for(gid, fee, now)
            self._move_units_for(gid, row["seller"], buyer, row["units"], now)
            self._conn.execute("UPDATE kyosai_offers SET status='done', done_at=? WHERE id=?", (now, offer_id))
            return True, {**row, "fee": fee}

    def kyosai_cancel(self, user_id: int, offer_id: int) -> bool:
        """自分が出した（または自分あての）申し出を取り消す。"""
        with self._db.tx():
            cur = self._conn.execute(
                "UPDATE kyosai_offers SET status='cancelled' "
                "WHERE id=? AND guild_id=? AND status='open' AND (seller=? OR buyer=?)",
                (offer_id, self._g(), user_id, user_id),
            )
            return cur.rowcount > 0

    def kyosai_offers_of(self, user_id: int, now: float) -> list[KyosaiOfferRow]:
        with self._lock:
            return self._all(
                "SELECT * FROM kyosai_offers WHERE guild_id=? AND status='open' AND expires>? "
                "AND (seller=? OR buyer=?) ORDER BY id",
                (self._g(), now, user_id, user_id),
            )
