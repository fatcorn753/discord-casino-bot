"""競走馬・競売。"""

from domain import horse as horse_mod
from domain import shop as shop_mod
from domain import teiai
from domain.errors import Failure, Outcome, fail
from domain.ports.bridges import IBankCashFlow, IStatusReader
from domain.ports.contracts import AuctionHorseRow, HorseRow
from domain.ports.repos import IHorseRepo
from domain.shop import CARROT, GOLD_CARROT, HORSE_VOUCHER
from domain.types import PurchaseResult
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo


class HorseRepo(ScopedRepo, IHorseRepo):
    """競走馬の作成・調教・引退・競売。"""

    HORSE_PRICE = shop_mod.BY_ID[HORSE_VOUCHER].price  # 馬の価値（差し押さえの換金額と、競売の定価の元）

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        status_reader: IStatusReader,
        bank: IBankCashFlow,
        guild_id: int = 0,
        strict: bool = False,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._status_reader = status_reader
        self._bank = bank

    def _consume(self, guild_id: int, user_id: int, item_id: str) -> bool:
        cur = self._conn.execute(
            "UPDATE inventory SET qty = qty - 1 WHERE guild_id=? AND user_id=? AND item_id=? AND qty>0",
            (guild_id, user_id, item_id),
        )
        return bool(cur.rowcount)

    def horse_count_for(self, guild_id: int, user_id: int) -> int:
        """持っている馬の数。出品中（預かり中）の馬も数える（出品しておけば3頭を超えて持てる、を防ぐ）。"""
        owned = self._scalar("SELECT COUNT(*) FROM horses WHERE guild_id=? AND owner_id=?", (guild_id, user_id))
        listed = self._scalar(
            "SELECT COUNT(*) FROM market_listings WHERE guild_id=? AND seller_id=? AND kind='horse' AND status='active'",
            (guild_id, user_id),
        )
        return owned + listed

    def horse_get_for(self, guild_id: int, horse_id: int) -> HorseRow | None:
        with self._lock:
            return self._one("SELECT * FROM horses WHERE id=? AND guild_id=?", (horse_id, guild_id))

    def horse_worth(self, races: int, wins: int, places: int) -> int:
        """馬1頭の評価額: 購入価格に、いま引退したときの引退金（成績が悪ければ引退費用）を足す。0 未満にはしない。"""
        return max(0, self.HORSE_PRICE + horse_mod.retire_value(races, wins, places))

    def horse_create(self, user_id: int, name: str, coat: str, silks: str) -> Outcome[int]:
        """購入券を1枚使って、競走馬を迎える。(成功か, 理由か馬の番号)。ステータスは、全部 50 から始まる。"""
        ok, checked = horse_mod.check_name(name)
        if not ok:
            assert isinstance(checked, Failure)
            return False, checked
        assert isinstance(checked, str)
        name = checked
        if coat not in horse_mod.COATS or silks not in horse_mod.SILKS:
            return False, fail("horse_invalid_look")
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            owned = self.horse_count_for(gid, user_id)  # 出品中の馬も数える
            if owned >= horse_mod.MAX_HORSES:
                return False, fail("horse_max_owned", max_horses=horse_mod.MAX_HORSES)
            if not self._consume(gid, user_id, HORSE_VOUCHER):
                return False, fail("horse_no_voucher")
            s = horse_mod.DEFAULT_STATS
            cur = self._conn.execute(
                "INSERT INTO horses (guild_id, owner_id, name, coat, silks, speed, stamina, power, guts, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (gid, user_id, name, coat, silks, s["speed"], s["stamina"], s["power"], s["guts"], self._now()),
            )
            lastrowid = cur.lastrowid
            assert lastrowid is not None
            return True, lastrowid

    def horse_list(self, user_id: int) -> list[HorseRow]:
        with self._lock:
            return self._all("SELECT * FROM horses WHERE guild_id=? AND owner_id=? ORDER BY id", (self._g(), user_id))

    def horse_get(self, horse_id: int) -> HorseRow | None:
        return self.horse_get_for(self._g(), horse_id)

    def horse_carrot(self, user_id: int, horse_id: int, src: str, dst: str) -> Outcome[dict[str, int]]:
        """にんじん1本で、ステータスを CARROT_POINTS 点、src から dst へ動かす。(成功か, 理由か新しいステータス)。"""
        with self._db.tx():
            gid = self._g()
            h = self.horse_get_for(gid, horse_id)
            if h is None or h["owner_id"] != user_id:
                return False, fail("horse_select_own")
            ok, result = horse_mod.move_points({k: h[k] for k in horse_mod.STAT_KEYS}, src, dst)
            if not ok:
                return False, result
            assert not isinstance(result, Failure)
            if not self._consume(gid, user_id, CARROT):
                return False, fail("horse_no_carrot")
            self._conn.execute(
                "UPDATE horses SET speed=?, stamina=?, power=?, guts=? WHERE id=?",
                (result["speed"], result["stamina"], result["power"], result["guts"], horse_id),
            )
            return True, result

    def horse_retire(self, user_id: int, horse_id: int) -> Outcome[int]:
        """馬を引退させる。成績に応じて、引退金をもらう（成績が悪いと、引退費用を払う）。(成功か, 理由かもらった額（払ったらマイナス）)。

        出走予定（申し込み中・発売中）の馬は、引退できない。引退した馬は、持ち馬から消える（owner_id=-2）。
        """
        with self._db.tx():
            gid = self._g()
            h = self.horse_get_for(gid, horse_id)
            if h is None or h["owner_id"] != user_id:
                return False, fail("horse_select_own")
            busy = self._one(
                "SELECT 1 FROM horse_entries e JOIN horse_races r ON r.id=e.race_id "
                "WHERE e.horse_id=? AND r.status IN ('entry','betting')",
                (horse_id,),
            )
            if busy:
                return False, fail("horse_busy_no_retire")
            value = horse_mod.retire_value(h["races"], h["wins"], h["places"])
            self._account.ensure(gid, user_id)
            bal = self._account.raw_balance(gid, user_id)
            if value < 0 and bal < -value:
                return False, fail("horse_retire_fee_short", fee=-value, balance=bal)
            if value:
                self._account.apply(
                    gid,
                    user_id,
                    value,
                    f"競馬:引退{'金' if value > 0 else '費用'}（{h['name']}）",
                    income=max(0, value),
                )
            self._conn.execute("UPDATE horses SET owner_id=-2 WHERE id=?", (horse_id,))
            return True, value

    def horse_set_style(self, user_id: int, horse_id: int, style: str | None) -> Outcome[str]:
        """脚質を選ぶ（style=None で、ステータスからの自動判定に戻す）。(成功か, Failure | "")。"""
        if style is not None and style not in horse_mod.STYLE_NAMES:
            return False, fail("horse_select_style")
        with self._db.tx():
            h = self.horse_get_for(self._g(), horse_id)
            if h is None or h["owner_id"] != user_id:
                return False, fail("horse_select_own")
            self._conn.execute("UPDATE horses SET style=? WHERE id=?", (style, horse_id))
            return True, ""

    def horse_gold_carrot(self, user_id: int, horse_id: int, first: str, second: str) -> Outcome[dict[str, int]]:
        """金にんじん1本で、first と second に5点ずつ足す（ステータスの合計が10点増える。1頭に GOLD_CARROT_MAX 本まで）。"""
        with self._db.tx():
            gid = self._g()
            h = self.horse_get_for(gid, horse_id)
            if h is None or h["owner_id"] != user_id:
                return False, fail("horse_select_own")
            if h["bonus"] >= horse_mod.GOLD_CARROT_MAX * 2 * horse_mod.GOLD_CARROT_POINTS:
                return False, fail("horse_gold_carrot_max", max_count=horse_mod.GOLD_CARROT_MAX)
            ok, result = horse_mod.add_points({k: h[k] for k in horse_mod.STAT_KEYS}, first, second)
            if not ok:
                return False, result
            assert not isinstance(result, Failure)
            if not self._consume(gid, user_id, GOLD_CARROT):
                return False, fail("horse_no_gold_carrot")
            self._conn.execute(
                "UPDATE horses SET speed=?, stamina=?, power=?, guts=?, bonus=bonus+? WHERE id=?",
                (
                    result["speed"],
                    result["stamina"],
                    result["power"],
                    result["guts"],
                    2 * horse_mod.GOLD_CARROT_POINTS,
                    horse_id,
                ),
            )
            return True, result

    def seize_horses_for(self, guild_id: int, user_id: int, now: float) -> int:
        """破産で、持ち馬をすべて差し押さえて、帝愛の競売に出す。換金の元になる額（馬1頭 = 購入券の値段）を返す。

        出走の申し込み中の馬は取り下げ（出走料は戻らない）。すでに出走馬に決まっている馬は、帝愛の馬（NPC）に替わる。
        """
        rows = self._all("SELECT id FROM horses WHERE guild_id=? AND owner_id=?", (guild_id, user_id))
        for r in rows:
            self._conn.execute("UPDATE horses SET owner_id=-1, listed_at=? WHERE id=?", (now, r["id"]))
            self._conn.execute("DELETE FROM horse_entries WHERE horse_id=? AND lane<0", (r["id"],))
            self._conn.execute(
                "UPDATE horse_entries SET owner_id=0, horse_id=0 WHERE horse_id=? AND race_id IN "
                "(SELECT id FROM horse_races WHERE status IN ('entry','betting'))",
                (r["id"],),
            )
        return len(rows) * self.HORSE_PRICE

    def horse_auction_price(self, listed_at: float, now: float) -> int:
        return max(1, round(self.HORSE_PRICE * teiai.auction_rate(listed_at, now)))

    def auction_horses(self, now: float) -> list[AuctionHorseRow]:
        """帝愛の競売に出ている馬。[{馬の行, price, rate}]（出品の早い順）。"""
        with self._lock:
            rows: list[HorseRow] = self._all(
                "SELECT * FROM horses WHERE guild_id=? AND owner_id=-1 ORDER BY listed_at, id", (self._g(),)
            )
        return [
            {
                **r,
                "price": self.horse_auction_price(r["listed_at"] or 0.0, now),
                "rate": teiai.auction_rate(r["listed_at"] or 0.0, now),
            }
            for r in rows
        ]

    def auction_buy_horse(self, user_id: int, horse_id: int, now: float) -> PurchaseResult:
        """競売の馬を買う。カタギだけ・持てるのは MAX_HORSES 頭まで。名前とステータスは、そのまま引き継ぐ。"""
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            if self._status_reader.status_for(gid, user_id, now) != teiai.KATAGI:
                return PurchaseResult(False, fail("auction_katagi_only"), self._account.raw_balance(gid, user_id))
            row = self._one("SELECT * FROM horses WHERE id=? AND guild_id=? AND owner_id=-1", (horse_id, gid))
            if row is None:
                return PurchaseResult(False, fail("auction_horse_sold"), self._account.raw_balance(gid, user_id))
            owned = self.horse_count_for(gid, user_id)  # 出品中の馬も数える
            if owned >= horse_mod.MAX_HORSES:
                return PurchaseResult(
                    False,
                    fail("horse_max_owned", max_horses=horse_mod.MAX_HORSES),
                    self._account.raw_balance(gid, user_id),
                )
            price = self.horse_auction_price(row["listed_at"], now)
            bal = self._account.raw_balance(gid, user_id)
            if bal < price:
                return PurchaseResult(
                    False,
                    fail("insufficient_balance_need", need=price, balance=bal),
                    bal,
                )
            self._account.apply(gid, user_id, -price, f"競売:競走馬（{row['name']}）")
            self._conn.execute("UPDATE horses SET owner_id=?, listed_at=NULL WHERE id=?", (user_id, horse_id))
            self._bank.bank_income_for(gid, price, now)
            return PurchaseResult(True, balance=self._account.raw_balance(gid, user_id))
