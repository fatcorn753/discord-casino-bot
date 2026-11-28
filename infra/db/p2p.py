"""プレイヤー間の取引（定価の市場と競り）。数字と決まりは domain/p2p.py、表は infra/db/schema/tables.py の market_listings。"""

import random
from typing import Any

from domain import horse as horse_mod
from domain import p2p, teiai
from domain import shop as shop_mod
from domain import teiai_market as tm
from domain.errors import Failure, Outcome, fail
from domain.ports.bridges import (
    GraceReader,
    IBankCashFlow,
    IStatusReader,
)
from domain.ports.contracts import ListingRow, P2PMine
from domain.ports.repos import IP2PRepo
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo
from infra.db.discord_posts import mark_discord_post_done, set_discord_message
from infra.db.horse import HorseRepo
from infra.db.inventory import InventoryCore
from infra.db.notices import NoticeRepo
from infra.db.profile import ProfileCore

_SELECT_LISTING = (
    "SELECT l.id, l.guild_id, COALESCE(l.seller_id, 0) AS seller_id, l.kind, l.ref, l.qty, "
    "l.mode, l.price, COALESCE(b.amount, 0) AS bid, COALESCE(b.bidder_id, 0) AS bidder_id, "
    "l.created, l.end_at, l.status, COALESCE(c.buyer_id, 0) AS buyer_id, "
    "COALESCE(c.sold_price, 0) AS sold_price, c.closed_at, "
    "COALESCE(p.channel_id, 0) AS channel_id, COALESCE(p.message_id, 0) AS message_id, "
    "COALESCE(p.post_done, 0) AS post_done "
    "FROM market_listings l "
    "LEFT JOIN market_auction_bids b ON b.listing_id = l.id "
    "LEFT JOIN market_closures c ON c.listing_id = l.id "
    "LEFT JOIN discord_posts p ON p.entity_type = 'market_listing' AND p.entity_id = l.id"
)


class P2PRepo(ScopedRepo, IP2PRepo):
    """プレイヤー間の取引・競り・強制引き戻し。"""

    P2P_ORDERS = {
        "end": "l.end_at, l.id",  # 締切が近い順
        "price_asc": "CASE WHEN COALESCE(b.amount, 0)>0 THEN COALESCE(b.amount, 0) ELSE l.price END, l.id",  # 安い順（競りは、いまの最高入札）
        "price_desc": "CASE WHEN COALESCE(b.amount, 0)>0 THEN COALESCE(b.amount, 0) ELSE l.price END DESC, l.id",  # 高い順
        "new": "l.id DESC",  # 新しい順
    }

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        horses: HorseRepo,
        status_reader: IStatusReader,
        bank: IBankCashFlow,
        grace_reader: GraceReader,
        notices: NoticeRepo,
        guild_id: int = 0,
        strict: bool = False,
        *,
        inventory: InventoryCore | None = None,
        profile: ProfileCore | None = None,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._horses = horses
        self._status_reader = status_reader
        self._bank = bank
        self._grace_reader: GraceReader = grace_reader
        self._notices = notices
        self._inventory = inventory or InventoryCore(db)
        self._profile = profile or ProfileCore(db)

    def prepare(self, guild_id: int, user_id: int, now: float) -> None:
        self.p2p_pull_back_for(guild_id, user_id, now)

    def on_grace_enter(self, guild_id: int, user_id: int, now: float) -> None:
        self.p2p_pull_back_for(guild_id, user_id, now)

    def _p2p_pair_total(self, guild_id: int, a: int, b: int, now: float) -> int:
        """この2人（どちらが売り手でも）の、24時間の取引額の合計。共済の口の譲渡（kyosai_offers）と合わせて数える。"""
        market_sum = self._scalar(
            "SELECT COALESCE(SUM(c.sold_price), 0) "
            "FROM market_listings l JOIN market_closures c ON c.listing_id = l.id "
            "WHERE l.guild_id=? AND l.status='sold' AND c.closed_at>? "
            "AND ((l.seller_id=? AND c.buyer_id=?) OR (l.seller_id=? AND c.buyer_id=?))",
            (guild_id, now - 86400, a, b, b, a),
        )
        kyosai_sum = self._scalar(
            "SELECT COALESCE(SUM(price), 0) FROM kyosai_offers WHERE guild_id=? AND status='done' AND done_at>? "
            "AND ((seller=? AND buyer=?) OR (seller=? AND buyer=?))",
            (guild_id, now - 86400, a, b, b, a),
        )
        return market_sum + kyosai_sum

    def _p2p_can_receive(self, guild_id: int, buyer_id: int, row: Any) -> Failure | None:
        """買い手が、この出品を受け取れないなら Failure。受け取れるなら None。"""
        if row["kind"] == "horse":
            if self._horses.horse_count_for(guild_id, buyer_id) >= horse_mod.MAX_HORSES:
                return fail("p2p_horse_limit", max_horses=horse_mod.MAX_HORSES)
            return None
        item = shop_mod.BY_ID[row["ref"]]
        if item.unique and self._inventory.has(guild_id, buyer_id, row["ref"]):
            return fail("already_owned")
        return None

    def _p2p_buyer_check(self, guild_id: int, buyer_id: int, row: Any, amount: int, now: float) -> Failure | None:
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
        if row["seller_id"] == tm.TEIAI_SELLER:  # 帝愛からの購入は、送金の抜け道にならない
            return None
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
        hours: int | None = None,
        now: float | None = None,
    ) -> Outcome[int]:
        """出品する。(成功か, Failure か出品の番号)。品・馬は、出品の間、預かる。"""
        now = self._now() if now is None else now
        if kind not in ("item", "horse") or mode not in ("fixed", "auction"):
            return False, fail("p2p_invalid_spec")
        if mode == "auction" and (hours is None or hours not in p2p.AUCTION_HOURS):
            return False, fail("p2p_invalid_auction_hours", hours=p2p.AUCTION_HOURS)
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            if self._grace_reader(gid, user_id) is not None:
                return False, fail("grace_lock")
            active = self._scalar(
                "SELECT COUNT(*) FROM market_listings WHERE guild_id=? AND seller_id=? AND status='active'",
                (gid, user_id),
            )
            if active >= p2p.MAX_ACTIVE:
                return False, fail("p2p_max_active", max_active=p2p.MAX_ACTIVE)
            item, have_qty, horse = None, 0, None
            if kind == "item":
                if not p2p.tradable(ref):
                    return False, fail("p2p_item_not_tradable")
                item = shop_mod.BY_ID[ref]
                qty = 1 if item.unique else qty
                have_qty = self._inventory.qty(gid, user_id, ref)
                if qty < 1 or have_qty < qty:
                    return False, fail("p2p_insufficient_qty")
                lo, hi = p2p.price_range("item", ref, qty)
            else:
                qty = 1
                horse = self._horses.horse_get_for(gid, int(ref)) if str(ref).isdigit() else None
                if horse is None or horse["owner_id"] != user_id:
                    return False, fail("horse_select_own")
                if self._horses.horse_is_in_active_race_for(horse["id"]):
                    return False, fail("p2p_horse_in_race")
                lo, hi = p2p.price_range("horse")
            if not lo <= price <= hi:
                return False, fail("p2p_price_range", lo=lo, hi=hi)
            if item is not None:
                self._inventory.consume(gid, user_id, ref, qty)
                slot = p2p.SLOT_OF_KIND.get(item.kind)
                if slot and have_qty - qty <= 0:  # 装備中の品は、外れる
                    self._profile.unequip_if_matches(gid, user_id, slot, ref)
            else:
                assert horse is not None
                self._horses.horse_set_escrow_for(horse["id"])
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
            lastrowid = cur.lastrowid
            assert lastrowid is not None
            return True, lastrowid

    def _p2p_give(self, guild_id: int, user_id: int, row: Any) -> None:
        if row["kind"] == "horse":
            self._horses.horse_assign_owner_for(guild_id, int(row["ref"]), user_id)
        else:
            self._inventory.add(guild_id, user_id, row["ref"], row["qty"])

    def _p2p_label_for(self, guild_id: int, row: ListingRow) -> str:
        if row["kind"] == "horse":
            horse = self._horses.horse_get_for(guild_id, int(row["ref"]))
            return f"{horse['silks']} {horse['name']}" if horse else "競走馬"
        item = shop_mod.BY_ID.get(row["ref"])
        name = shop_mod.label(item) if item else row["ref"]
        return name + (f"×{row['qty']}" if row["qty"] > 1 else "")

    def p2p_label(self, row: ListingRow) -> str:
        """出品の名前（品の名前×個数、または馬の名前）。"""
        return self._p2p_label_for(self._g(), row)

    def _p2p_close(self, row_id: int, status: str, now: float, buyer: int = 0, sold: int = 0) -> None:
        self._conn.execute("UPDATE market_listings SET status=? WHERE id=?", (status, row_id))
        self._conn.execute(
            "INSERT INTO market_closures (listing_id, closed_at, buyer_id, sold_price) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(listing_id) DO UPDATE SET "
            "closed_at=excluded.closed_at, buyer_id=excluded.buyer_id, sold_price=excluded.sold_price",
            (row_id, now, buyer if buyer > 0 else None, sold),
        )

    def _p2p_sell(self, guild_id: int, row: Any, buyer_id: int, price: int, now: float) -> None:
        """売れた: 売り手へ（手数料を引いて）支払い、買い手へ品を渡す。買い手の支払いは、呼び出し側で済ませる。

        帝愛の出品なら、代金はすべて帝愛銀行の収入にする。
        """
        if row["seller_id"] == tm.TEIAI_SELLER:
            self._bank.bank_income_for(guild_id, price, now)
            self._p2p_give(guild_id, buyer_id, row)
            self._p2p_close(row["id"], "sold", now, buyer_id, price)
            return
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

    def p2p_get(self, listing_id: int) -> ListingRow | None:
        with self._lock:
            return self._one(f"{_SELECT_LISTING} WHERE l.id=? AND l.guild_id=?", (listing_id, self._g()))

    def p2p_set_post(self, listing_id: int, channel_id: int, message_id: int) -> None:
        """告知を投稿したチャンネルとメッセージを覚える（入札・売れたときに、書き換えるため）。"""
        with self._db.tx():
            if self._one("SELECT 1 FROM market_listings WHERE id=? AND guild_id=?", (listing_id, self._g())) is None:
                return
            set_discord_message(self._conn, "market_listing", listing_id, message_id, channel_id)

    def p2p_buy(self, buyer_id: int, listing_id: int, now: float) -> Outcome[int]:
        """定価の出品を買う。(成功か, 理由か支払った額)。"""
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, buyer_id)
            if self._grace_reader(gid, buyer_id) is not None:
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

    def p2p_bid(self, bidder_id: int, listing_id: int, amount: int, now: float) -> Outcome[float]:
        """競りに入札する。お金は、残高から預かる（抜かれたら、返る）。(成功か, Failure か新しい締切)。"""
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, bidder_id)
            if self._grace_reader(gid, bidder_id) is not None:
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
                "INSERT INTO market_auction_bids (listing_id, bidder_id, amount) VALUES (?, ?, ?) "
                "ON CONFLICT(listing_id) DO UPDATE SET bidder_id=excluded.bidder_id, amount=excluded.amount",
                (listing_id, bidder_id, amount),
            )
            self._conn.execute("UPDATE market_listings SET end_at=? WHERE id=?", (end_at, listing_id))
            return True, end_at

    def p2p_cancel(self, user_id: int, listing_id: int, now: float) -> Outcome[str]:
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
            f"{_SELECT_LISTING} WHERE l.guild_id=? AND l.seller_id=? AND l.status='active'",
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
            f"{_SELECT_LISTING} WHERE l.guild_id=? AND b.bidder_id=? AND l.status='active'",
            (guild_id, user_id),
        ):
            self._account.apply(
                guild_id, user_id, row["bid"], f"取引:入札の返金（{self._p2p_label_for(guild_id, row)}・取引停止）"
            )
            self._conn.execute("DELETE FROM market_auction_bids WHERE listing_id=?", (row["id"],))
            pulled["bids"] += 1
        return pulled

    def p2p_pull_back(self, user_id: int, now: float | None = None) -> dict[str, int]:
        return self.p2p_pull_back_for(self._g(), user_id, self._now() if now is None else now)

    def p2p_unsynced_posts(self) -> list[ListingRow]:
        """終わった（売れた・取り消し・終了）のに、告知の投稿を、まだ書き換えていない出品。"""
        with self._lock:
            return self._all(
                f"{_SELECT_LISTING} WHERE l.guild_id=? AND l.status!='active' AND p.message_id>0 AND p.post_done=0",
                (self._g(),),
            )

    def p2p_mark_post_done(self, listing_id: int) -> None:
        with self._db.tx():
            if self._one("SELECT 1 FROM market_listings WHERE id=? AND guild_id=?", (listing_id, self._g())) is None:
                return
            mark_discord_post_done(self._conn, "market_listing", listing_id)

    def p2p_settle(self, now: float) -> list[tuple[ListingRow, str]]:
        """締切・期限を過ぎた出品を精算する。[(出品の行, 結果)]（結果は sold / expired / failed）を返す。"""
        out: list[tuple[ListingRow, str]] = []
        with self._db.tx():
            gid = self._g()
            rows: list[ListingRow] = self._all(
                f"{_SELECT_LISTING} WHERE l.guild_id=? AND l.status='active' AND l.end_at<=? ORDER BY l.end_at, l.id",
                (gid, now),
            )
            for row in rows:
                label = self._p2p_label_for(gid, row)
                teiai_row = row["seller_id"] == tm.TEIAI_SELLER  # 帝愛の売れ残りは、帝愛が引き取る（品は消える）
                if row["mode"] != "auction" or not row["bidder_id"]:
                    self._p2p_close(row["id"], "expired", now)
                    if not teiai_row:
                        self._p2p_give(gid, row["seller_id"], row)  # 売れ残り: 売り手へ戻す
                        self._notices.note_for(gid, row["seller_id"], "p2p_unsold", label=label)
                    out.append((row, "expired"))
                    continue
                buyer = row["bidder_id"]
                reason = self._p2p_can_receive(gid, buyer, row)
                used = 0 if teiai_row else self._p2p_pair_total(gid, row["seller_id"], buyer, now)
                if not reason and not teiai_row and used + row["bid"] > p2p.PAIR_DAILY_LIMIT:
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
                self._p2p_close(row["id"], "expired", now)
                if not teiai_row:
                    self._p2p_give(gid, row["seller_id"], row)
                    self._notices.note_for(gid, row["seller_id"], "p2p_buyer_cannot_receive", label=label)
                out.append((row, "failed"))
        return out

    def p2p_teiai_daily(self, now: float, rng: random.Random) -> list[int]:
        """帝愛の発掘品の競りを、その日（UTC）の分だけ出す。出した出品の番号を返す（もう出ていれば空）。"""
        with self._db.tx():
            gid = self._g()
            done = self._scalar(
                "SELECT COUNT(*) FROM market_listings WHERE guild_id=? AND seller_id IS NULL AND created>=?",
                (gid, tm.day_start(now)),
            )
            out = []
            for _ in range(max(0, teiai.RELIC_AUCTION_PER_DAY - done)):
                item_id, price = tm.pick_relic(rng)
                cur = self._conn.execute(
                    "INSERT INTO market_listings (guild_id, seller_id, kind, ref, qty, mode, price, created, end_at) "
                    "VALUES (?, NULL, 'item', ?, 1, 'auction', ?, ?, ?)",
                    (gid, item_id, price, now, now + teiai.RELIC_AUCTION_HOURS * 3600),
                )
                assert cur.lastrowid is not None
                out.append(cur.lastrowid)
            return out

    def p2p_active(
        self,
        kind: str | None = None,
        now: float | None = None,
        limit: int = 25,
        mode: str | None = None,
        order: str = "end",
    ) -> list[ListingRow]:
        """出ている出品。kind は horse / item、mode は fixed / auction で絞れる。order は P2P_ORDERS のどれか（既定は締切の近い順）。"""
        now = self._now() if now is None else now
        with self._lock:
            sql = f"{_SELECT_LISTING} WHERE l.guild_id=? AND l.status='active' AND l.end_at>?"
            args: list[Any] = [self._g(), now]
            if kind:
                sql += " AND l.kind=?"
                args.append(kind)
            if mode:
                sql += " AND l.mode=?"
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

    def p2p_mine(self, user_id: int, now: float | None = None) -> P2PMine:
        """自分の出品（出している）と、自分が最高入札者の競り。"""
        with self._lock:
            gid = self._g()
            selling: list[ListingRow] = self._all(
                f"{_SELECT_LISTING} WHERE l.guild_id=? AND l.seller_id=? AND l.status='active' ORDER BY l.end_at",
                (gid, user_id),
            )
            bidding: list[ListingRow] = self._all(
                f"{_SELECT_LISTING} WHERE l.guild_id=? AND b.bidder_id=? AND l.status='active' ORDER BY l.end_at",
                (gid, user_id),
            )
        return {"selling": selling, "bidding": bidding}
