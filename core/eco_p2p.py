"""プレイヤー間の取引（定価の市場と競り）（Economy に混ぜ込む部品）。数字と決まりは core/p2p.py、表は core/schema.py の market_listings。

出品した品・馬は、出品の間、預かる（品は持ち物から出し、馬は owner_id を ESCROW_OWNER にする）。入札のお金も、預かる（残高から引く）。
売れたら、売り手へ（手数料を引いて）、買い手へ品を渡す。取り消し・売れ残り・買い手が受け取れないときは、品を売り手へ戻し、入札のお金を返す。
"""

import time
from typing import Optional

from core import horse as horse_mod
from core import p2p, teiai
from core import shop as shop_mod
from core.eco_base import EcoBase

GRACE_LOCK_TEXT = (
    "破産猶予中は、取引（出品・買う・入札）ができません（破産の前に、資産を移して、借金を踏み倒すのを防ぐためです）。"
)


class P2PMixin(EcoBase):
    # ---------------------------------------------------------------- #
    # 持ち数・取引額の確認                                              #
    # ---------------------------------------------------------------- #
    def _horse_count(self, user_id: int) -> int:
        """持っている馬の数。出品中（預かり中）の馬も数える（出品しておけば3頭を超えて持てる、を防ぐ）。"""
        owned = self._conn.execute(
            "SELECT COUNT(*) FROM horses WHERE guild_id=? AND owner_id=? AND rented_day IS NULL", (self._g(), user_id)
        ).fetchone()[0]
        listed = self._conn.execute(
            "SELECT COUNT(*) FROM market_listings WHERE guild_id=? AND seller_id=? AND kind='horse' AND status='active'",
            (self._g(), user_id),
        ).fetchone()[0]
        return owned + listed

    def _p2p_pair_total(self, a: int, b: int, now: float) -> int:
        """この2人（どちらが売り手でも）の、24時間の取引額の合計。"""
        return self._conn.execute(
            "SELECT COALESCE(SUM(sold_price), 0) FROM market_listings WHERE guild_id=? AND status='sold' AND closed_at>? "
            "AND ((seller_id=? AND buyer_id=?) OR (seller_id=? AND buyer_id=?))",
            (self._g(), now - 86400, a, b, b, a),
        ).fetchone()[0]

    def _p2p_can_receive(self, buyer_id: int, row) -> str:
        """買い手が、この出品を受け取れないなら、その理由。受け取れるなら空文字。"""
        if row["kind"] == "horse":
            if self._horse_count(buyer_id) >= horse_mod.MAX_HORSES:
                return f"持てる馬は {horse_mod.MAX_HORSES} 頭までです（出品中の馬も数えます）。"
            return ""
        item = shop_mod.BY_ID[row["ref"]]
        if (
            item.unique
            and self._conn.execute(
                "SELECT 1 FROM inventory WHERE guild_id=? AND user_id=? AND item_id=? AND qty>0",
                (self._g(), buyer_id, row["ref"]),
            ).fetchone()
        ):
            return "すでに持っています。"
        return ""

    def _p2p_buyer_check(self, buyer_id: int, row, amount: int, now: float) -> str:
        """買う・入札するときの、共通の確認。問題があれば、その理由。"""
        if buyer_id == row["seller_id"]:
            return "自分の出品は、買えません。"
        if self.status(buyer_id, now) != teiai.KATAGI:
            return "取引で買えるのは、借金も延滞もないカタギだけです。"
        if self._balance(buyer_id) < amount:
            return f"ペリカが足りません（{amount:,}ペリカ必要・残高 {self._balance(buyer_id):,}ペリカ）。"
        if reason := self._p2p_can_receive(buyer_id, row):
            return reason
        used = self._p2p_pair_total(row["seller_id"], buyer_id, now)
        if used + amount > p2p.PAIR_DAILY_LIMIT:
            return (
                f"同じ相手との取引は、24時間で合計 {p2p.PAIR_DAILY_LIMIT:,}ペリカ までです"
                f"（いま {used:,}ペリカ・今回 {amount:,}ペリカ）。"
            )
        return ""

    # ---------------------------------------------------------------- #
    # 出品                                                              #
    # ---------------------------------------------------------------- #
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
    ) -> tuple:
        """出品する。(成功か, 理由か出品の番号)。品・馬は、出品の間、預かる。"""
        now = time.time() if now is None else now
        if kind not in ("item", "horse") or mode not in ("fixed", "auction"):
            return False, "出品の指定が正しくありません。"
        if mode == "auction" and (hours is None or hours not in p2p.AUCTION_HOURS):
            return False, f"競りの長さは、{'・'.join(map(str, p2p.AUCTION_HOURS))} 時間から選んでください。"
        with self._lock, self._conn:
            self._ensure(user_id)
            if self.grace_until(user_id) is not None:
                return False, GRACE_LOCK_TEXT
            active = self._conn.execute(
                "SELECT COUNT(*) FROM market_listings WHERE guild_id=? AND seller_id=? AND status='active'",
                (self._g(), user_id),
            ).fetchone()[0]
            if active >= p2p.MAX_ACTIVE:
                return False, f"同時に出せるのは {p2p.MAX_ACTIVE} 件までです。"
            item, have, horse = None, None, None
            if kind == "item":
                if not p2p.tradable(ref):
                    return False, "その品は、取引できません（称号・コレクション・限定品・帝愛市の品は不可）。"
                item = shop_mod.BY_ID[ref]
                qty = 1 if item.unique else qty
                have = self._conn.execute(
                    "SELECT qty FROM inventory WHERE guild_id=? AND user_id=? AND item_id=?",
                    (self._g(), user_id, ref),
                ).fetchone()
                if qty < 1 or have is None or have["qty"] < qty:
                    return False, "その数は、持っていません。"
                lo, hi = p2p.price_range("item", ref, qty)
            else:
                qty = 1
                horse = self.horse_get(int(ref)) if str(ref).isdigit() else None
                if horse is None or horse["owner_id"] != user_id:
                    return False, "自分の馬を選んでください。"
                if horse["rented_day"] is not None:
                    return False, "レンタル馬は出品できません。"
                if self._conn.execute(
                    "SELECT 1 FROM horse_entries e JOIN horse_races r ON r.id=e.race_id "
                    "WHERE e.horse_id=? AND r.status IN ('entry','betting')",
                    (horse["id"],),
                ).fetchone():
                    return False, "出走予定のレースが終わるまで、出品できません。"
                lo, hi = p2p.price_range("horse")
            if not lo <= price <= hi:
                return False, f"値段は {lo:,}〜{hi:,}ペリカ にしてください（定価の幅に収めます）。"
            if item is not None and have is not None:
                self._conn.execute(
                    "UPDATE inventory SET qty = qty - ? WHERE guild_id=? AND user_id=? AND item_id=?",
                    (qty, self._g(), user_id, ref),
                )
                slot = p2p.SLOT_OF_KIND.get(item.kind)
                if slot and have["qty"] - qty <= 0:  # 装備中の品は、外れる
                    self._conn.execute(
                        f"UPDATE profile SET {slot}=NULL WHERE guild_id=? AND user_id=? AND {slot}=?",
                        (self._g(), user_id, ref),
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
                (self._g(), user_id, kind, str(ref), qty, mode, price, now, end_at),
            )
            return True, cur.lastrowid

    # ---------------------------------------------------------------- #
    # 取り出し・渡し・戻し                                              #
    # ---------------------------------------------------------------- #
    def _p2p_give(self, user_id: int, row) -> None:
        if row["kind"] == "horse":
            self._conn.execute("UPDATE horses SET owner_id=?, listed_at=NULL WHERE id=?", (user_id, int(row["ref"])))
        else:
            self._conn.execute(
                "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty = qty + excluded.qty",
                (self._g(), user_id, row["ref"], row["qty"]),
            )

    def p2p_label(self, row) -> str:
        """出品の名前（品の名前×個数、または馬の名前）。"""
        if row["kind"] == "horse":
            horse = self.horse_get(int(row["ref"]))
            return f"{horse['silks']} {horse['name']}" if horse else "競走馬"
        item = shop_mod.BY_ID.get(row["ref"])
        name = shop_mod.label(item) if item else row["ref"]
        return name + (f"×{row['qty']}" if row["qty"] > 1 else "")

    def _p2p_close(self, row_id: int, status: str, now: float, buyer: int = 0, sold: int = 0) -> None:
        self._conn.execute(
            "UPDATE market_listings SET status=?, buyer_id=?, sold_price=?, closed_at=? WHERE id=?",
            (status, buyer, sold, now, row_id),
        )

    def _p2p_sell(self, row, buyer_id: int, price: int, now: float) -> None:
        """売れた: 売り手へ（手数料を引いて）支払い、買い手へ品を渡す。買い手の支払いは、呼び出し側で済ませる。"""
        fee = p2p.fee_of(price)
        self._ensure(row["seller_id"])
        label = self.p2p_label(row)
        self._apply(row["seller_id"], price - fee, f"取引:売却（{label}）", income=price - fee)
        self._bank_income(fee, teiai.AUCTION_YIELD_SHARE, now)
        self._p2p_give(buyer_id, row)
        self._p2p_close(row["id"], "sold", now, buyer_id, price)
        self._note(
            row["seller_id"],
            f"🤝 **{label}** が **{price:,}ペリカ** で売れました（手数料 {fee:,}ペリカ・受け取り {price - fee:,}ペリカ）。",
        )

    # ---------------------------------------------------------------- #
    # 買う・入札・取り消し                                              #
    # ---------------------------------------------------------------- #
    def p2p_get(self, listing_id: int):
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM market_listings WHERE id=? AND guild_id=?", (listing_id, self._g())
            ).fetchone()
            return dict(r) if r else None

    def p2p_set_post(self, listing_id: int, channel_id: int, message_id: int) -> None:
        """告知を投稿したチャンネルとメッセージを覚える（入札・売れたときに、書き換えるため）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE market_listings SET channel_id=?, message_id=? WHERE id=? AND guild_id=?",
                (channel_id, message_id, listing_id, self._g()),
            )

    def p2p_buy(self, buyer_id: int, listing_id: int, now: Optional[float] = None) -> tuple:
        """定価の出品を買う。(成功か, 理由か支払った額)。"""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._ensure(buyer_id)
            if self.grace_until(buyer_id) is not None:
                return False, GRACE_LOCK_TEXT
            row = self.p2p_get(listing_id)
            if row is None or row["status"] != "active" or row["end_at"] <= now:
                return False, "その出品は、もうありません。"
            if row["mode"] != "fixed":
                return False, "これは競りです。入札してください。"
            if reason := self._p2p_buyer_check(buyer_id, row, row["price"], now):
                return False, reason
            self._apply(buyer_id, -row["price"], f"取引:購入（{self.p2p_label(row)}）")
            self._p2p_sell(row, buyer_id, row["price"], now)
            return True, row["price"]

    def p2p_bid(self, bidder_id: int, listing_id: int, amount: int, now: Optional[float] = None) -> tuple:
        """競りに入札する。お金は、残高から預かる（抜かれたら、返る）。(成功か, 理由か新しい締切)。"""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._ensure(bidder_id)
            if self.grace_until(bidder_id) is not None:
                return False, GRACE_LOCK_TEXT
            row = self.p2p_get(listing_id)
            if row is None or row["status"] != "active" or row["end_at"] <= now:
                return False, "その出品は、もうありません。"
            if row["mode"] != "auction":
                return False, "これは定価の出品です。「買う」を押してください。"
            if row["bidder_id"] == bidder_id:
                return False, "いま、あなたが最高入札者です。"
            need = p2p.min_next_bid(row["price"], row["bid"])
            if amount < need:
                return False, f"入札は {need:,}ペリカ 以上にしてください。"
            if reason := self._p2p_buyer_check(bidder_id, row, amount, now):
                return False, reason
            label = self.p2p_label(row)
            if row["bidder_id"]:  # 抜かれた人に、預かっていたお金を返す
                self._ensure(row["bidder_id"])
                self._apply(row["bidder_id"], row["bid"], f"取引:入札の返金（{label}）")
                self._note(
                    row["bidder_id"],
                    f"📉 **{label}** の入札を、**{amount:,}ペリカ** で抜かれました（預かっていた {row['bid']:,}ペリカ は返りました）。",
                )
            self._apply(bidder_id, -amount, f"取引:入札（{label}）")
            end_at = p2p.end_after_bid(row["end_at"], now)
            self._conn.execute(
                "UPDATE market_listings SET bid=?, bidder_id=?, end_at=? WHERE id=?",
                (amount, bidder_id, end_at, listing_id),
            )
            return True, end_at

    def p2p_cancel(self, user_id: int, listing_id: int, now: Optional[float] = None) -> tuple:
        """自分の出品を取り消して、品・馬を戻す。競りは、入札が入ったら取り消せない。"""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            row = self.p2p_get(listing_id)
            if row is None or row["status"] != "active" or row["seller_id"] != user_id:
                return False, "取り消せる出品が見つかりません。"
            if row["bidder_id"]:
                return False, "入札が入っているので、取り消せません。"
            self._p2p_give(user_id, row)
            self._p2p_close(listing_id, "cancelled", now)
            return True, ""

    # ---------------------------------------------------------------- #
    # 破産猶予・破産のとき: 出品の取り下げと、入札の取り消し                  #
    # ---------------------------------------------------------------- #
    def p2p_pull_back(self, user_id: int, now: Optional[float] = None) -> dict:
        """その人の出品を、すべて取り下げて品・馬を持ち物へ戻し、最高入札者になっている競りの入札は、お金を返して取り消す。

        破産猶予に入った人・破産する人が、預け先（出品・入札の預かり）に資産を隠して、借金を踏み倒すのを防ぐ。
        入札が入っている自分の出品も、強制的に取り下げる（入札者には、お金が返る）。呼び出し側のトランザクションの中で使う。
        {取り下げた出品の数, 取り消した入札の数} を返す。
        """
        now = time.time() if now is None else now
        pulled = {"listings": 0, "bids": 0}
        for row in [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM market_listings WHERE guild_id=? AND seller_id=? AND status='active'",
                (self._g(), user_id),
            )
        ]:
            label = self.p2p_label(row)
            if row["bidder_id"]:  # 最高入札者に、預かっていたお金を返す
                self._ensure(row["bidder_id"])
                self._apply(row["bidder_id"], row["bid"], f"取引:入札の返金（{label}・出品者の取引停止）")
                self._note(
                    row["bidder_id"],
                    f"⚠️ **{label}** の出品者が取引できなくなったため、競りは中止され、入札 {row['bid']:,}ペリカ は返りました。",
                )
            self._p2p_give(user_id, row)
            self._p2p_close(row["id"], "cancelled", now)
            pulled["listings"] += 1
        for row in [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM market_listings WHERE guild_id=? AND bidder_id=? AND status='active'",
                (self._g(), user_id),
            )
        ]:
            self._apply(user_id, row["bid"], f"取引:入札の返金（{self.p2p_label(row)}・取引停止）")
            self._conn.execute("UPDATE market_listings SET bid=0, bidder_id=0 WHERE id=?", (row["id"],))
            pulled["bids"] += 1
        return pulled

    def p2p_unsynced_posts(self) -> list:
        """終わった（売れた・取り消し・終了）のに、告知の投稿を、まだ書き換えていない出品。"""
        with self._lock:
            return [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM market_listings WHERE guild_id=? AND status!='active' AND message_id>0 AND post_done=0",
                    (self._g(),),
                )
            ]

    def p2p_mark_post_done(self, listing_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE market_listings SET post_done=1 WHERE id=? AND guild_id=?", (listing_id, self._g())
            )

    # ---------------------------------------------------------------- #
    # 締切の精算                                                        #
    # ---------------------------------------------------------------- #
    def p2p_settle(self, now: Optional[float] = None) -> list:
        """締切・期限を過ぎた出品を精算する。[(出品の行, 結果)]（結果は sold / expired / failed）を返す。"""
        now = time.time() if now is None else now
        out = []
        with self._lock, self._conn:
            rows = [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM market_listings WHERE guild_id=? AND status='active' AND end_at<=? ORDER BY end_at, id",
                    (self._g(), now),
                )
            ]
            for row in rows:
                label = self.p2p_label(row)
                if row["mode"] == "auction" and row["bidder_id"]:
                    buyer = row["bidder_id"]
                    reason = self._p2p_can_receive(buyer, row)
                    used = self._p2p_pair_total(row["seller_id"], buyer, now)
                    if not reason and used + row["bid"] > p2p.PAIR_DAILY_LIMIT:
                        reason = "同じ相手との取引の上限を超えます。"
                    if not reason:
                        self._p2p_sell(row, buyer, row["bid"], now)
                        self._note(buyer, f"🎉 **{label}** を **{row['bid']:,}ペリカ** で落札しました。")
                        out.append((row, "sold"))
                        continue
                    self._apply(
                        buyer, row["bid"], f"取引:入札の返金（{label}）"
                    )  # 受け取れないので、返金して出品も戻す
                    self._note(
                        buyer,
                        f"⚠️ **{label}** は落札しましたが、{reason}そのため取引は成立せず、{row['bid']:,}ペリカ は返りました。",
                    )
                    self._p2p_give(row["seller_id"], row)
                    self._p2p_close(row["id"], "expired", now)
                    self._note(
                        row["seller_id"],
                        f"**{label}** は、買い手が受け取れず、取引が成立しませんでした（品は戻りました）。",
                    )
                    out.append((row, "failed"))
                    continue
                self._p2p_give(row["seller_id"], row)  # 売れ残り: 売り手へ戻す
                self._p2p_close(row["id"], "expired", now)
                self._note(row["seller_id"], f"**{label}** は、売れ残りました（品は戻りました）。")
                out.append((row, "expired"))
        return out

    # ---------------------------------------------------------------- #
    # 一覧                                                              #
    # ---------------------------------------------------------------- #
    P2P_ORDERS = {
        "end": "end_at, id",  # 締切が近い順
        "price_asc": "CASE WHEN bid>0 THEN bid ELSE price END, id",  # 安い順（競りは、いまの最高入札）
        "price_desc": "CASE WHEN bid>0 THEN bid ELSE price END DESC, id",  # 高い順
        "new": "id DESC",  # 新しい順
    }

    def p2p_active(
        self,
        kind: Optional[str] = None,
        now: Optional[float] = None,
        limit: int = 25,
        mode: Optional[str] = None,
        order: str = "end",
    ) -> list:
        """出ている出品。kind は horse / item、mode は fixed / auction で絞れる。order は P2P_ORDERS のどれか（既定は締切の近い順）。"""
        now = time.time() if now is None else now
        with self._lock:
            sql = "SELECT * FROM market_listings WHERE guild_id=? AND status='active' AND end_at>?"
            args = [self._g(), now]
            if kind:
                sql += " AND kind=?"
                args.append(kind)
            if mode:
                sql += " AND mode=?"
                args.append(mode)
            ordering = self.P2P_ORDERS.get(order, self.P2P_ORDERS["end"])
            return [dict(r) for r in self._conn.execute(sql + f" ORDER BY {ordering} LIMIT ?", (*args, limit))]

    def p2p_count(self, now: Optional[float] = None) -> int:
        """出ている出品の数（画面のタブに出す）。"""
        now = time.time() if now is None else now
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM market_listings WHERE guild_id=? AND status='active' AND end_at>?",
                (self._g(), now),
            ).fetchone()[0]

    def p2p_mine(self, user_id: int, now: Optional[float] = None) -> dict:
        """自分の出品（出している）と、自分が最高入札者の競り。"""
        now = time.time() if now is None else now
        with self._lock:
            selling = [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM market_listings WHERE guild_id=? AND seller_id=? AND status='active' ORDER BY end_at",
                    (self._g(), user_id),
                )
            ]
            bidding = [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM market_listings WHERE guild_id=? AND bidder_id=? AND status='active' ORDER BY end_at",
                    (self._g(), user_id),
                )
            ]
        return {"selling": selling, "bidding": bidding}
