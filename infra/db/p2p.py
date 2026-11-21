"""プレイヤー間の取引（定価の市場と競り）。数字と決まりは domain/p2p.py、表は infra/db/schema/tables.py の market_listings。"""

from typing import Any, Optional

from domain import horse as horse_mod
from domain import p2p, teiai
from domain import shop as shop_mod
from domain.errors import Failure, fail
from domain.ports.bridges import (
    IBankCashFlow,
    IBankruptPrepHook,
    IGraceEnterHook,
    IGraceReader,
    IStatusReader,
)
from domain.ports.repos import IP2PRepo
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo
from infra.db.horse import HorseRepo
from infra.db.notices import NoticeRepo


class P2PRepo(ScopedRepo, IP2PRepo, IBankruptPrepHook, IGraceEnterHook):
    """プレイヤー間の取引・競り・強制引き戻し。"""

    P2P_ORDERS = {
        "end": "end_at, id",  # 締切が近い順
        "price_asc": "CASE WHEN bid>0 THEN bid ELSE price END, id",  # 安い順（競りは、いまの最高入札）
        "price_desc": "CASE WHEN bid>0 THEN bid ELSE price END DESC, id",  # 高い順
        "new": "id DESC",  # 新しい順
    }

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        horses: HorseRepo,
        status_reader: IStatusReader,
        bank: IBankCashFlow,
        grace_reader: IGraceReader,
        notices: NoticeRepo,
        guild_id: int = 0,
        strict: bool = False,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._horses = horses
        self._status_reader = status_reader
        self._bank = bank
        self._grace_reader = grace_reader
        self._notices = notices

    def prepare(self, guild_id: int, user_id: int, now: float) -> None:
        self.p2p_pull_back_for(guild_id, user_id, now)

    def on_grace_enter(self, guild_id: int, user_id: int, now: float) -> None:
        self.p2p_pull_back_for(guild_id, user_id, now)

    def _p2p_pair_total(self, guild_id: int, a: int, b: int, now: float) -> int:
        """この2人（どちらが売り手でも）の、24時間の取引額の合計。共済の口の譲渡（kyosai_offers）と合わせて数える。"""
        market_sum = self._scalar(
            "SELECT COALESCE(SUM(sold_price), 0) FROM market_listings WHERE guild_id=? AND status='sold' AND closed_at>? "
            "AND ((seller_id=? AND buyer_id=?) OR (seller_id=? AND buyer_id=?))",
            (guild_id, now - 86400, a, b, b, a),
        )
        kyosai_sum = self._scalar(
            "SELECT COALESCE(SUM(price), 0) FROM kyosai_offers WHERE guild_id=? AND status='done' AND done_at>? "
            "AND ((seller=? AND buyer=?) OR (seller=? AND buyer=?))",
            (guild_id, now - 86400, a, b, b, a),
        )
        return market_sum + kyosai_sum

    def _p2p_can_receive(self, guild_id: int, buyer_id: int, row: Any) -> Optional[Failure]:
        """買い手が、この出品を受け取れないなら Failure。受け取れるなら None。"""
        if row["kind"] == "horse":
            if self._horses.horse_count_for(guild_id, buyer_id) >= horse_mod.MAX_HORSES:
                return fail("p2p_horse_limit", max_horses=horse_mod.MAX_HORSES)
            return None
        item = shop_mod.BY_ID[row["ref"]]
        if item.unique and self._one(
            "SELECT 1 FROM inventory WHERE guild_id=? AND user_id=? AND item_id=? AND qty>0",
            (guild_id, buyer_id, row["ref"]),
        ):
            return fail("already_owned")
        return None

    def _p2p_buyer_check(self, guild_id: int, buyer_id: int, row: Any, amount: int, now: float) -> Optional[Failure]:
        """買う・入札するときの、共通の確認。問題があれば Failure。"""
        if buyer_id == row["seller_id"]:
            return fail("p2p_self_buy")
        if self._status_reader.status_for(guild_id, buyer_id, now) != teiai.KATAGI:
            return fail("p2p_katagi_only")
        bal = self._account.raw_balance(guild_id, buyer_id)
        if bal < amount:
            return fail("insufficient_balance_need", need=amount, balance=bal)
        if reason := self._p2p_can_receive(guild_id, buyer_id, row):
            return reason
        used = self._p2p_pair_total(guild_id, row["seller_id"], buyer_id, now)
        if used + amount > p2p.PAIR_DAILY_LIMIT:
            return fail("p2p_pair_limit", limit=p2p.PAIR_DAILY_LIMIT, used=used, amount=amount)
        return None

    def p2p_list(
        self,
        user_id: int,
        kind: str,
        ref: str,
        qty: int,
        mode: str,
        price: int,
        hours: Optional[int] = None,
        now: Optional[float] = None,
    ) -> tuple[Any, ...]:
        """出品する。(成功か, Failure か出品の番号)。品・馬は、出品の間、預かる。"""
        now = self._now() if now is None else now
        if kind not in ("item", "horse") or mode not in ("fixed", "auction"):
            return False, fail("p2p_invalid_spec")
        if mode == "auction" and (hours is None or hours not in p2p.AUCTION_HOURS):
            return False, fail("p2p_invalid_auction_hours", hours=p2p.AUCTION_HOURS)
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            if self._grace_reader.grace_until_for(gid, user_id) is not None:
                return False, fail("grace_lock")
            active = self._scalar(
                "SELECT COUNT(*) FROM market_listings WHERE guild_id=? AND seller_id=? AND status='active'",
                (gid, user_id),
            )
            if active >= p2p.MAX_ACTIVE:
                return False, fail("p2p_max_active", max_active=p2p.MAX_ACTIVE)
            item, have, horse = None, None, None
            if kind == "item":
                if not p2p.tradable(ref):
                    return False, fail("p2p_item_not_tradable")
                item = shop_mod.BY_ID[ref]
                qty = 1 if item.unique else qty
                have = self._one(
                    "SELECT qty FROM inventory WHERE guild_id=? AND user_id=? AND item_id=?",
                    (gid, user_id, ref),
                )
                if qty < 1 or have is None or have["qty"] < qty:
                    return False, fail("p2p_insufficient_qty")
                lo, hi = p2p.price_range("item", ref, qty)
            else:
                qty = 1
                horse = self._horses.horse_get_for(gid, int(ref)) if str(ref).isdigit() else None
                if horse is None or horse["owner_id"] != user_id:
                    return False, fail("horse_select_own")
                if self._one(
                    "SELECT 1 FROM horse_entries e JOIN horse_races r ON r.id=e.race_id "
                    "WHERE e.horse_id=? AND r.status IN ('entry','betting')",
                    (horse["id"],),
                ):
                    return False, fail("p2p_horse_in_race")
                lo, hi = p2p.price_range("horse")
            if not lo <= price <= hi:
                return False, fail("p2p_price_range", lo=lo, hi=hi)
            if item is not None and have is not None:
                self._conn.execute(
                    "UPDATE inventory SET qty = qty - ? WHERE guild_id=? AND user_id=? AND item_id=?",
                    (qty, gid, user_id, ref),
                )
                slot = p2p.SLOT_OF_KIND.get(item.kind)
                if slot and have["qty"] - qty <= 0:  # 装備中の品は、外れる
                    self._conn.execute(
                        f"UPDATE profile SET {slot}=NULL WHERE guild_id=? AND user_id=? AND {slot}=?",
                        (gid, user_id, ref),
                    )
            else:
                assert horse is not None
                self._conn.execute("UPDATE horses SET owner_id=? WHERE id=?", (p2p.ESCROW_OWNER, horse["id"]))
            duration_sec = (
                (hours if hours is not None else p2p.AUCTION_HOURS[0]) * 3600
                if mode == "auction"
                else p2p.FIXED_DAYS * 86400
            )
            end_at = now + duration_sec
            cur = self._conn.execute(
                "INSERT INTO market_listings (guild_id, seller_id, kind, ref, qty, mode, price, created, end_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (gid, user_id, kind, str(ref), qty, mode, price, now, end_at),
            )
            return True, cur.lastrowid

    def _p2p_give(self, guild_id: int, user_id: int, row: Any) -> None:
        if row["kind"] == "horse":
            self._conn.execute("UPDATE horses SET owner_id=?, listed_at=NULL WHERE id=?", (user_id, int(row["ref"])))
        else:
            self._conn.execute(
                "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty = qty + excluded.qty",
                (guild_id, user_id, row["ref"], row["qty"]),
            )

    def _p2p_label_for(self, guild_id: int, row: Any) -> str:
        if row["kind"] == "horse":
            horse = self._horses.horse_get_for(guild_id, int(row["ref"]))
            return f"{horse['silks']} {horse['name']}" if horse else "競走馬"
        item = shop_mod.BY_ID.get(row["ref"])
        name = shop_mod.label(item) if item else row["ref"]
        return name + (f"×{row['qty']}" if row["qty"] > 1 else "")

    def p2p_label(self, row: Any) -> str:
        """出品の名前（品の名前×個数、または馬の名前）。"""
        return self._p2p_label_for(self._g(), row)

    def _p2p_close(self, row_id: int, status: str, now: float, buyer: int = 0, sold: int = 0) -> None:
        self._conn.execute(
            "UPDATE market_listings SET status=?, buyer_id=?, sold_price=?, closed_at=? WHERE id=?",
            (status, buyer, sold, now, row_id),
        )

    def _p2p_sell(self, guild_id: int, row: Any, buyer_id: int, price: int, now: float) -> None:
        """売れた: 売り手へ（手数料を引いて）支払い、買い手へ品を渡す。買い手の支払いは、呼び出し側で済ませる。"""
        fee = p2p.fee_of(price)
        self._account.ensure(guild_id, row["seller_id"])
        label = self._p2p_label_for(guild_id, row)
        self._account.apply(guild_id, row["seller_id"], price - fee, f"取引:売却（{label}）", income=price - fee)
        self._bank.bank_income_for(guild_id, fee, now)
        self._p2p_give(guild_id, buyer_id, row)
        self._p2p_close(row["id"], "sold", now, buyer_id, price)
        self._notices.note_for(
            guild_id, row["seller_id"], "p2p_sold", label=label, price=price, fee=fee, net=price - fee
        )

    def p2p_get(self, listing_id: int) -> Any:
        with self._lock:
            return self._one("SELECT * FROM market_listings WHERE id=? AND guild_id=?", (listing_id, self._g()))

    def p2p_set_post(self, listing_id: int, channel_id: int, message_id: int) -> None:
        """告知を投稿したチャンネルとメッセージを覚える（入札・売れたときに、書き換えるため）。"""
        with self._db.tx():
            self._conn.execute(
                "UPDATE market_listings SET channel_id=?, message_id=? WHERE id=? AND guild_id=?",
                (channel_id, message_id, listing_id, self._g()),
            )

    def p2p_buy(self, buyer_id: int, listing_id: int, now: float) -> tuple[Any, ...]:
        """定価の出品を買う。(成功か, 理由か支払った額)。"""
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, buyer_id)
            if self._grace_reader.grace_until_for(gid, buyer_id) is not None:
                return False, fail("grace_lock")
            row = self.p2p_get(listing_id)
            if row is None or row["status"] != "active" or row["end_at"] <= now:
                return False, fail("p2p_not_found")
            if row["mode"] != "fixed":
                return False, fail("p2p_is_auction")
            if reason := self._p2p_buyer_check(gid, buyer_id, row, row["price"], now):
                return False, reason
            self._account.apply(gid, buyer_id, -row["price"], f"取引:購入（{self._p2p_label_for(gid, row)}）")
            self._p2p_sell(gid, row, buyer_id, row["price"], now)
            return True, row["price"]

    def p2p_bid(self, bidder_id: int, listing_id: int, amount: int, now: float) -> tuple[Any, ...]:
        """競りに入札する。お金は、残高から預かる（抜かれたら、返る）。(成功か, Failure か新しい締切)。"""
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, bidder_id)
            if self._grace_reader.grace_until_for(gid, bidder_id) is not None:
                return False, fail("grace_lock")
            row = self.p2p_get(listing_id)
            if row is None or row["status"] != "active" or row["end_at"] <= now:
                return False, fail("p2p_not_found")
            if row["mode"] != "auction":
                return False, fail("p2p_is_fixed")
            if row["bidder_id"] == bidder_id:
                return False, fail("p2p_already_highest_bidder")
            need = p2p.min_next_bid(row["price"], row["bid"])
            if amount < need:
                return False, fail("p2p_bid_min", need=need)
            if reason := self._p2p_buyer_check(gid, bidder_id, row, amount, now):
                return False, reason
            label = self._p2p_label_for(gid, row)
            if row["bidder_id"]:  # 抜かれた人に、預かっていたお金を返す
                self._account.ensure(gid, row["bidder_id"])
                self._account.apply(gid, row["bidder_id"], row["bid"], f"取引:入札の返金（{label}）")
                self._notices.note_for(
                    gid, row["bidder_id"], "p2p_outbid", label=label, amount=amount, refund=row["bid"]
                )
            self._account.apply(gid, bidder_id, -amount, f"取引:入札（{label}）")
            end_at = p2p.end_after_bid(row["end_at"], now)
            self._conn.execute(
                "UPDATE market_listings SET bid=?, bidder_id=?, end_at=? WHERE id=?",
                (amount, bidder_id, end_at, listing_id),
            )
            return True, end_at

    def p2p_cancel(self, user_id: int, listing_id: int, now: float) -> tuple[Any, ...]:
        """自分の出品を取り消して、品・馬を戻す。競りは、入札が入ったら取り消せない。"""
        with self._db.tx():
            gid = self._g()
            row = self.p2p_get(listing_id)
            if row is None or row["status"] != "active" or row["seller_id"] != user_id:
                return False, fail("p2p_cancel_not_found")
            if row["bidder_id"]:
                return False, fail("p2p_cancel_has_bid")
            self._p2p_give(gid, user_id, row)
            self._p2p_close(listing_id, "cancelled", now)
            return True, ""

    def p2p_pull_back_for(self, guild_id: int, user_id: int, now: float) -> dict[str, int]:
        """その人の出品を、すべて取り下げて品・馬を持ち物へ戻し、最高入札者になっている競りの入札は、お金を返して取り消す。"""
        pulled = {"listings": 0, "bids": 0}
        for row in self._all(
            "SELECT * FROM market_listings WHERE guild_id=? AND seller_id=? AND status='active'",
            (guild_id, user_id),
        ):
            label = self._p2p_label_for(guild_id, row)
            if row["bidder_id"]:  # 最高入札者に、預かっていたお金を返す
                self._account.ensure(guild_id, row["bidder_id"])
                self._account.apply(
                    guild_id, row["bidder_id"], row["bid"], f"取引:入札の返金（{label}・出品者の取引停止）"
                )
                self._notices.note_for(
                    guild_id, row["bidder_id"], "p2p_seller_pulled_back", label=label, refund=row["bid"]
                )
            self._p2p_give(guild_id, user_id, row)
            self._p2p_close(row["id"], "cancelled", now)
            pulled["listings"] += 1
        for row in self._all(
            "SELECT * FROM market_listings WHERE guild_id=? AND bidder_id=? AND status='active'",
            (guild_id, user_id),
        ):
            self._account.apply(
                guild_id, user_id, row["bid"], f"取引:入札の返金（{self._p2p_label_for(guild_id, row)}・取引停止）"
            )
            self._conn.execute("UPDATE market_listings SET bid=0, bidder_id=0 WHERE id=?", (row["id"],))
            pulled["bids"] += 1
        return pulled

    def p2p_pull_back(self, user_id: int, now: Optional[float] = None) -> dict[str, int]:
        return self.p2p_pull_back_for(self._g(), user_id, self._now() if now is None else now)

    def p2p_unsynced_posts(self) -> list[Any]:
        """終わった（売れた・取り消し・終了）のに、告知の投稿を、まだ書き換えていない出品。"""
        with self._lock:
            return self._all(
                "SELECT * FROM market_listings WHERE guild_id=? AND status!='active' AND message_id>0 AND post_done=0",
                (self._g(),),
            )

    def p2p_mark_post_done(self, listing_id: int) -> None:
        with self._db.tx():
            self._conn.execute(
                "UPDATE market_listings SET post_done=1 WHERE id=? AND guild_id=?", (listing_id, self._g())
            )

    def p2p_settle(self, now: float) -> list[Any]:
        """締切・期限を過ぎた出品を精算する。[(出品の行, 結果)]（結果は sold / expired / failed）を返す。"""
        out = []
        with self._db.tx():
            gid = self._g()
            rows = self._all(
                "SELECT * FROM market_listings WHERE guild_id=? AND status='active' AND end_at<=? ORDER BY end_at, id",
                (gid, now),
            )
            for row in rows:
                label = self._p2p_label_for(gid, row)
                if row["mode"] == "auction" and row["bidder_id"]:
                    buyer = row["bidder_id"]
                    reason = self._p2p_can_receive(gid, buyer, row)
                    used = self._p2p_pair_total(gid, row["seller_id"], buyer, now)
                    if not reason and used + row["bid"] > p2p.PAIR_DAILY_LIMIT:
                        reason = fail("p2p_pair_limit_short")
                    if not reason:
                        self._p2p_sell(gid, row, buyer, row["bid"], now)
                        self._notices.note_for(gid, buyer, "p2p_won", label=label, bid=row["bid"])
                        out.append((row, "sold"))
                        continue
                    self._account.apply(
                        gid, buyer, row["bid"], f"取引:入札の返金（{label}）"
                    )  # 受け取れないので、返金して出品も戻す
                    self._notices.note_for(gid, buyer, "p2p_won_failed", label=label, reason=reason, refund=row["bid"])
                    self._p2p_give(gid, row["seller_id"], row)
                    self._p2p_close(row["id"], "expired", now)
                    self._notices.note_for(gid, row["seller_id"], "p2p_buyer_cannot_receive", label=label)
                    out.append((row, "failed"))
                    continue
                self._p2p_give(gid, row["seller_id"], row)  # 売れ残り: 売り手へ戻す
                self._p2p_close(row["id"], "expired", now)
                self._notices.note_for(gid, row["seller_id"], "p2p_unsold", label=label)
                out.append((row, "expired"))
        return out

    def p2p_active(
        self,
        kind: Optional[str] = None,
        now: Optional[float] = None,
        limit: int = 25,
        mode: Optional[str] = None,
        order: str = "end",
    ) -> list[Any]:
        """出ている出品。kind は horse / item、mode は fixed / auction で絞れる。order は P2P_ORDERS のどれか（既定は締切の近い順）。"""
        now = self._now() if now is None else now
        with self._lock:
            sql = "SELECT * FROM market_listings WHERE guild_id=? AND status='active' AND end_at>?"
            args: list[Any] = [self._g(), now]
            if kind:
                sql += " AND kind=?"
                args.append(kind)
            if mode:
                sql += " AND mode=?"
                args.append(mode)
            ordering = self.P2P_ORDERS.get(order, self.P2P_ORDERS["end"])
            return self._all(sql + f" ORDER BY {ordering} LIMIT ?", (*args, limit))

    def p2p_count(self, now: float) -> int:
        """出ている出品の数（画面のタブに出す）。"""
        with self._lock:
            return self._scalar(
                "SELECT COUNT(*) FROM market_listings WHERE guild_id=? AND status='active' AND end_at>?",
                (self._g(), now),
            )

    def p2p_mine(self, user_id: int, now: Optional[float] = None) -> dict[str, Any]:
        """自分の出品（出している）と、自分が最高入札者の競り。"""
        with self._lock:
            gid = self._g()
            selling = self._all(
                "SELECT * FROM market_listings WHERE guild_id=? AND seller_id=? AND status='active' ORDER BY end_at",
                (gid, user_id),
            )
            bidding = self._all(
                "SELECT * FROM market_listings WHERE guild_id=? AND bidder_id=? AND status='active' ORDER BY end_at",
                (gid, user_id),
            )
        return {"selling": selling, "bidding": bidding}
