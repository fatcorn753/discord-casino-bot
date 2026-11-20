"""帝愛共済（#34）の記録。口・配当・超過損失の請求・回収と帝愛の持ち口の競り・譲渡・動議。

表の定義は infra/db/schema/tables.py（kyosai_units・kyosai_auctions・kyosai_offers・kyosai_motions・kyosai_votes、
teiai_bank の kyosai_* と trust_premium）。数字と考え方は domain/kyosai.py。
"""

from typing import Any, Optional

from domain import kyosai, p2p
from domain.errors import fail
from domain.ports.bridges import IExcessLossSink
from domain.ports.repos import IKyosaiRepo
from infra.db.account import AccountCore
from infra.db.bank import BankRepo
from infra.db.base import EcoDb, ScopedRepo
from infra.db.notices import NoticeRepo

DAY = 86400


class KyosaiRepo(ScopedRepo, IKyosaiRepo, IExcessLossSink):
    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        bank: BankRepo,
        notices: NoticeRepo,
        guild_id: int = 0,
        strict: bool = False,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._bank = bank
        self._note = notices

    # ------------------------------------------------------------------ #
    # 口
    # ------------------------------------------------------------------ #
    def _holdings_for(self, guild_id: int, *, with_house: bool = True) -> list[tuple[int, int]]:
        rows = self._conn.execute(
            "SELECT user_id, units FROM kyosai_units WHERE guild_id=? AND units>0 ORDER BY user_id", (guild_id,)
        ).fetchall()
        return [(r["user_id"], r["units"]) for r in rows if with_house or r["user_id"] != kyosai.HOUSE]

    def units_of_for(self, guild_id: int, user_id: int) -> int:
        row = self._conn.execute(
            "SELECT units FROM kyosai_units WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        ).fetchone()
        return row["units"] if row else 0

    def _move_units_for(self, guild_id: int, src: int, dst: int, units: int, now: float) -> None:
        self._conn.execute(
            "UPDATE kyosai_units SET units=units-? WHERE guild_id=? AND user_id=?", (units, guild_id, src)
        )
        self._conn.execute(
            "INSERT INTO kyosai_units (guild_id, user_id, units) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET units=units+excluded.units",
            (guild_id, dst, units),
        )

    def recover_units_for(self, guild_id: int, user_id: int, now: float) -> int:
        """その人の口を、すべて帝愛の持ち口へ回収する（破産・休眠）。回収した口数を返す。"""
        units = self.units_of_for(guild_id, user_id)
        if units > 0 and user_id != kyosai.HOUSE:
            self.bank_sync(guild_id, now)
            self._move_units_for(guild_id, user_id, kyosai.HOUSE, units, now)
            self._conn.execute(
                "UPDATE kyosai_offers SET status='cancelled' WHERE guild_id=? AND seller=? AND status='open'",
                (guild_id, user_id),
            )
        return units

    def bank_sync(self, guild_id: int, now: float) -> None:
        self._bank.bank_sync_for(guild_id, now)  # 銀行の行を作っておく（kyosai_* の列がそこにある）
        self._ensure_supply_for(guild_id)

    def _ensure_supply_for(self, guild_id: int) -> None:
        """口がまだ1つもないサーバーでは、総口数をすべて帝愛の持ち口として用意する（競りで売り出す。#93）。"""
        total = self._conn.execute(
            "SELECT COALESCE(SUM(units), 0) FROM kyosai_units WHERE guild_id=?", (guild_id,)
        ).fetchone()[0]
        if total <= 0:
            self._conn.execute(
                "INSERT INTO kyosai_units (guild_id, user_id, units) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET units=excluded.units",
                (guild_id, kyosai.HOUSE, kyosai.TOTAL_UNITS),
            )

    def _gate_locked_for(self, guild_id: int, now: float) -> bool:
        theta = self._bank.bank_reserve_for(guild_id, now)[2]
        debtors = self._conn.execute(
            "SELECT COUNT(*) FROM users WHERE guild_id=? AND grace_until IS NOT NULL", (guild_id,)
        ).fetchone()[0]
        return kyosai.gate_locked(theta, debtors)

    # ------------------------------------------------------------------ #
    # 超過損失（IExcessLossSink）
    # ------------------------------------------------------------------ #
    def charge_excess_for(self, guild_id: int, amount: int, now: float) -> int:
        """超過損失を口数に応じて請求する。手元から取り、足りない分は借金にする。

        帝愛の持ち口の分は、誰も払わない（払われずに残る）。請求できた額（手元 + 借金）を返す。
        """
        collected = 0
        for user_id, share in kyosai.split(amount, self._holdings_for(guild_id)).items():
            if user_id == kyosai.HOUSE or share <= 0:
                continue
            self._account.ensure(guild_id, user_id)
            took = -self._account.apply(guild_id, user_id, -share, "共済:超過損失の負担")
            rest = share - took
            if rest > 0:
                self._conn.execute(
                    "UPDATE users SET debt=debt+? WHERE guild_id=? AND user_id=?", (rest, guild_id, user_id)
                )
                self._account.ledger(  # 借金が増えたことを履歴に残す（手元は動かないので 0）
                    guild_id,
                    user_id,
                    0,
                    self._account.raw_balance(guild_id, user_id),
                    f"共済:超過損失の負担（借金 {rest:,}ペリカ）",
                )
                self._account.after_debt_change(guild_id, user_id)
            self._note.note_for(guild_id, user_id, "kyosai_charged", amount=share, debt=rest)
            collected += share
        if collected:
            self._conn.execute(
                "UPDATE teiai_bank SET kyosai_charged=kyosai_charged+? WHERE guild_id=?", (collected, guild_id)
            )
        return collected

    # ------------------------------------------------------------------ #
    # 毎日の配当と休眠の回収
    # ------------------------------------------------------------------ #
    def kyosai_daily(self, now: float) -> Optional[dict[str, Any]]:
        """1日に1回: 休眠の人の口を回収し、余剰から配当を配る。配ったときだけ {pool, paid, recovered} を返す。"""
        with self._db.tx():
            gid = self._g()
            self.bank_sync(gid, now)
            last = self._conn.execute("SELECT kyosai_div_ts FROM teiai_bank WHERE guild_id=?", (gid,)).fetchone()[0]
            if last == 0:
                self._conn.execute("UPDATE teiai_bank SET kyosai_div_ts=? WHERE guild_id=?", (now, gid))
                return None
            if now - last < DAY:
                return None
            self._conn.execute("UPDATE teiai_bank SET kyosai_div_ts=? WHERE guild_id=?", (now, gid))
            recovered = self._recover_dormant_for(gid, now)
            available, liabilities, _ = self._bank.bank_reserve_for(gid, now)
            holdings = self._holdings_for(gid)
            pool = kyosai.dividend_pool(available, liabilities)
            shares = kyosai.split(pool, holdings)
            paid = 0
            to_users = sum(s for uid, s in shares.items() if uid != kyosai.HOUSE)  # 帝愛の持ち口の分は、銀行に残す
            if to_users > 0:
                # 配当は余剰の分配で損失ではないため、サーモスタットに通さない（L < 0 のときに熱浴の現金が湧かないように）
                self._bank.bank_pay_for(gid, to_users, now, thermo=False)

                for user_id, share in shares.items():
                    if user_id == kyosai.HOUSE or share <= 0:
                        continue
                    self._account.ensure(gid, user_id)
                    self._account.apply(gid, user_id, share, "共済:配当", income=share)
                    paid += share
                self._conn.execute("UPDATE teiai_bank SET kyosai_paid=kyosai_paid+? WHERE guild_id=?", (paid, gid))
            return {"pool": pool, "paid": paid, "recovered": recovered}

    def _recover_dormant_for(self, guild_id: int, now: float) -> int:
        """DORMANT_DAYS 日、口座が動いていない人の口を回収する。

        本人が何もしなくても起きる動き（共済の配当・超過損失の負担、信託の利息）は数えない。
        口の売買や競りの入札のような、本人の操作は数える。
        """
        since = now - kyosai.DORMANT_DAYS * DAY
        recovered = 0
        for user_id, _units in self._holdings_for(guild_id, with_house=False):
            last = self._conn.execute(
                "SELECT MAX(ts) FROM ledger WHERE guild_id=? AND user_id=? "
                "AND reason NOT IN ('共済:配当', '帝愛信託:利息') AND reason NOT LIKE '共済:超過損失の負担%'",
                (guild_id, user_id),
            ).fetchone()[0]
            if last is None:
                row = self._conn.execute(
                    "SELECT created_at FROM users WHERE guild_id=? AND user_id=?", (guild_id, user_id)
                ).fetchone()
                last = row["created_at"] if row and row["created_at"] else None
            if last is not None and last < since:
                recovered += self.recover_units_for(guild_id, user_id, now)
        return recovered

    # ------------------------------------------------------------------ #
    # 帝愛の持ち口の競り（#93。値段は入札で決まる）
    # ------------------------------------------------------------------ #
    def _open_auction_for(self, guild_id: int) -> Optional[Any]:
        return self._conn.execute(
            "SELECT * FROM kyosai_auctions WHERE guild_id=? AND status='open' ORDER BY id LIMIT 1", (guild_id,)
        ).fetchone()

    def kyosai_auction_tick(self, now: float) -> list[dict[str, Any]]:
        """締切を過ぎた競りを締め（落札なら口を渡し、代金を銀行の収入にする）、帝愛の持ち口があれば次の競りを出す。

        流動性ゲート（準備率が低い・破産猶予中の人がいる）の間も、競りは止めない（落札の代金で銀行に現金が入り、
        流動性を助けるため。止めるのは、プレイヤーどうしの譲渡だけ）。起きたこと [{event: sold/unsold/listed, ...}] を返す。
        """
        events: list[dict[str, Any]] = []
        with self._db.tx():
            gid = self._g()
            self.bank_sync(gid, now)
            row = self._open_auction_for(gid)
            if row is not None and row["ends"] <= now:
                if row["top_bidder"] is not None and row["top_bid"] > 0:
                    self._account.ensure(gid, row["top_bidder"])
                    self._move_units_for(gid, kyosai.HOUSE, row["top_bidder"], row["units"], now)
                    self._bank.bank_income_for(gid, row["top_bid"], now)
                    self._conn.execute("UPDATE kyosai_auctions SET status='sold' WHERE id=?", (row["id"],))
                    events.append({**dict(row), "event": "sold"})
                else:
                    self._conn.execute("UPDATE kyosai_auctions SET status='unsold' WHERE id=?", (row["id"],))
                    events.append({**dict(row), "event": "unsold"})
                row = None
            house = self.units_of_for(gid, kyosai.HOUSE)
            if row is None and house > 0:
                units = min(house, kyosai.AUCTION_LOT)
                ends = now + kyosai.AUCTION_HOURS * 3600
                cur = self._conn.execute(
                    "INSERT INTO kyosai_auctions (guild_id, units, ends) VALUES (?, ?, ?)", (gid, units, ends)
                )
                listed = {"id": cur.lastrowid, "units": units, "ends": ends, "top_bid": 0, "top_bidder": None}
                # 売れ残りの出し直しは告知しない（同じ口の出し直しで、告知チャンネルを埋めないように）
                if not any(e["event"] == "unsold" for e in events):
                    events.append({**listed, "event": "listed"})
        return events

    def kyosai_bid(self, user_id: int, amount: int, now: float) -> tuple[Any, ...]:
        """出ている競りに入札する（入札額は手元から預かり、前の最高値の人へ返す）。(成功か, Failure か競りの行)。"""
        with self._db.tx():
            gid = self._g()
            self.bank_sync(gid, now)
            row = self._open_auction_for(gid)
            if row is None or row["ends"] <= now:
                return False, fail("kyosai_no_auction")
            self._account.ensure(gid, user_id)
            debt = self._account.debt(gid, user_id)
            if debt > 0:
                return False, fail("debt_blocks", debt=debt, verb="入札できません")
            need = p2p.min_next_bid(kyosai.AUCTION_MIN_BID, row["top_bid"])
            if amount < need:
                return False, fail("kyosai_bid_low", need=need)
            own = row["top_bid"] if row["top_bidder"] == user_id else 0  # 自分の最高値を上げるときは、差額だけ要る
            bal = self._account.raw_balance(gid, user_id)
            if bal + own < amount:
                return False, fail("insufficient_balance_need", need=amount, balance=bal)
            if row["top_bidder"] is not None and row["top_bid"] > 0:
                self._account.ensure(gid, row["top_bidder"])
                self._account.apply(gid, row["top_bidder"], row["top_bid"], "共済:競りの入札の返金")
                if row["top_bidder"] != user_id:
                    self._note.note_for(gid, row["top_bidder"], "kyosai_outbid", units=row["units"], amount=amount)
            self._account.apply(gid, user_id, -amount, "共済:競りの入札（預かり）")
            ends = p2p.end_after_bid(row["ends"], now)
            self._conn.execute(
                "UPDATE kyosai_auctions SET top_bid=?, top_bidder=?, ends=? WHERE id=?",
                (amount, user_id, ends, row["id"]),
            )
            return True, {**dict(row), "top_bid": amount, "top_bidder": user_id, "ends": ends}

    # ------------------------------------------------------------------ #
    # 譲渡（プレイヤー間の売買）
    # ------------------------------------------------------------------ #
    def _offered_units_for(self, guild_id: int, seller: int, now: float) -> int:
        return self._conn.execute(
            "SELECT COALESCE(SUM(units), 0) FROM kyosai_offers "
            "WHERE guild_id=? AND seller=? AND status='open' AND expires>?",
            (guild_id, seller, now),
        ).fetchone()[0]

    def _pair_total_for(self, guild_id: int, a: int, b: int, now: float) -> int:
        """この2人（どちらが売り手でも）の、24時間の取引額の合計。P2P の取引と、共済の口の譲渡を合わせて数える。"""
        kyosai_sum = self._conn.execute(
            "SELECT COALESCE(SUM(price), 0) FROM kyosai_offers WHERE guild_id=? AND status='done' AND done_at>? "
            "AND ((seller=? AND buyer=?) OR (seller=? AND buyer=?))",
            (guild_id, now - DAY, a, b, b, a),
        ).fetchone()[0]
        market_sum = self._conn.execute(
            "SELECT COALESCE(SUM(sold_price), 0) FROM market_listings WHERE guild_id=? AND status='sold' "
            "AND closed_at>? AND ((seller_id=? AND buyer_id=?) OR (seller_id=? AND buyer_id=?))",
            (guild_id, now - DAY, a, b, b, a),
        ).fetchone()[0]
        return kyosai_sum + market_sum

    def kyosai_offer(self, seller: int, buyer: int, units: int, price: int, now: float) -> tuple[Any, ...]:
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
            return True, cur.lastrowid

    def kyosai_accept(self, buyer: int, offer_id: int, now: float) -> tuple[Any, ...]:
        """自分あての申し出を受ける。(成功か, Failure か申し出の行)。"""
        with self._db.tx():
            gid = self._g()
            self.bank_sync(gid, now)
            row = self._conn.execute(
                "SELECT * FROM kyosai_offers WHERE id=? AND guild_id=? AND buyer=? AND status='open' AND expires>?",
                (offer_id, gid, buyer, now),
            ).fetchone()
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
            return True, {**dict(row), "fee": fee}

    def kyosai_cancel(self, user_id: int, offer_id: int) -> bool:
        """自分が出した（または自分あての）申し出を取り消す。"""
        with self._db.tx():
            cur = self._conn.execute(
                "UPDATE kyosai_offers SET status='cancelled' "
                "WHERE id=? AND guild_id=? AND status='open' AND (seller=? OR buyer=?)",
                (offer_id, self._g(), user_id, user_id),
            )
            return cur.rowcount > 0

    def kyosai_offers_of(self, user_id: int, now: float) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM kyosai_offers WHERE guild_id=? AND status='open' AND expires>? "
                    "AND (seller=? OR buyer=?) ORDER BY id",
                    (self._g(), now, user_id, user_id),
                )
            ]

    # ------------------------------------------------------------------ #
    # 動議（信託の上乗せ金利。#94: いくつも並行でき、締めた順に後勝ち）
    # ------------------------------------------------------------------ #
    def _open_motions_for(self, guild_id: int) -> list[Any]:
        return self._conn.execute(
            "SELECT * FROM kyosai_motions WHERE guild_id=? AND status='open' ORDER BY closes, id", (guild_id,)
        ).fetchall()

    def kyosai_propose(self, user_id: int, premium: float, now: float) -> tuple[Any, ...]:
        """動議を出す。(成功か, Failure か動議の行)。

        通知の荒らしを防ぐため、口が MOTION_MIN_UNITS 以上の人だけが、1人 MOTION_MAX_OPEN_PER_USER 件まで、
        前に出してから MOTION_COOLDOWN_HOURS たってから出せる。提案した人は賛成に数える。
        """
        if not kyosai.premium_ok(premium):
            return False, fail("kyosai_bad_premium", max_pct=kyosai.PREMIUM_LIMIT * 100)
        with self._db.tx():
            gid = self._g()
            self.bank_sync(gid, now)
            units = self.units_of_for(gid, user_id)
            if units < kyosai.MOTION_MIN_UNITS:
                return False, fail("kyosai_motion_units", need=kyosai.MOTION_MIN_UNITS, have=units)
            mine = [m for m in self._open_motions_for(gid) if m["proposer"] == user_id]
            if len(mine) >= kyosai.MOTION_MAX_OPEN_PER_USER:
                return False, fail("kyosai_motion_open", max=kyosai.MOTION_MAX_OPEN_PER_USER)
            last = self._conn.execute(
                "SELECT MAX(created) FROM kyosai_motions WHERE guild_id=? AND proposer=?", (gid, user_id)
            ).fetchone()[0]
            if last and now - last < kyosai.MOTION_COOLDOWN_HOURS * 3600:
                return False, fail("kyosai_motion_cooldown", until=last + kyosai.MOTION_COOLDOWN_HOURS * 3600)
            closes = now + kyosai.MOTION_HOURS * 3600
            cur = self._conn.execute(
                "INSERT INTO kyosai_motions (guild_id, proposer, premium, created, closes) VALUES (?, ?, ?, ?, ?)",
                (gid, user_id, premium, now, closes),
            )
            motion_id = cur.lastrowid
            self._conn.execute(
                "INSERT INTO kyosai_votes (motion_id, user_id, yes) VALUES (?, ?, 1)", (motion_id, user_id)
            )
            return True, {"id": motion_id, "proposer": user_id, "premium": premium, "closes": closes}

    def kyosai_vote(self, user_id: int, yes: bool, now: float, motion_id: Optional[int] = None) -> tuple[Any, ...]:
        """投票中の動議に投票する（あとから変えられる）。番号を省けるのは、投票中が1件だけのとき。

        (成功か, Failure か動議の番号)。
        """
        with self._db.tx():
            gid = self._g()
            motions = [m for m in self._open_motions_for(gid) if m["closes"] > now]
            if motion_id is None:
                if not motions:
                    return False, fail("kyosai_no_motion")
                if len(motions) > 1:
                    return False, fail("kyosai_motion_ambiguous", ids=", ".join(f"#{m['id']}" for m in motions))
                motion = motions[0]
            else:
                motion = next((m for m in motions if m["id"] == motion_id), None)
                if motion is None:
                    return False, fail("kyosai_no_motion")
            if self.units_of_for(gid, user_id) <= 0:
                return False, fail("kyosai_no_units")
            self._conn.execute(
                "INSERT INTO kyosai_votes (motion_id, user_id, yes) VALUES (?, ?, ?) "
                "ON CONFLICT(motion_id, user_id) DO UPDATE SET yes=excluded.yes",
                (motion["id"], user_id, 1 if yes else 0),
            )
            return True, motion["id"]

    def _tally_for(self, guild_id: int, motion_id: int) -> tuple[int, int]:
        """賛成・反対の口数（締めるときの、いまの口数で数える。譲った口が二重に数えられないように）。"""
        yes = no = 0
        for r in self._conn.execute("SELECT user_id, yes FROM kyosai_votes WHERE motion_id=?", (motion_id,)):
            units = self.units_of_for(guild_id, r["user_id"])
            if r["yes"]:
                yes += units
            else:
                no += units
        return yes, no

    def _circulating_for(self, guild_id: int) -> int:
        """流通口数（帝愛の持ち口を除いた口の合計。可決に要る賛成の割合の分母）。"""
        return sum(u for _, u in self._holdings_for(guild_id, with_house=False))

    def kyosai_close_motions(self, now: float) -> list[dict[str, Any]]:
        """締切を過ぎた動議を、締切の順にすべて締める。可決なら、その順に信託の上乗せ金利を上書きする（後勝ち）。

        締めた動議の結果を、締めた順に返す（なければ空）。
        """
        out: list[dict[str, Any]] = []
        with self._db.tx():
            gid = self._g()
            circulating = self._circulating_for(gid)
            for motion in self._open_motions_for(gid):
                if motion["closes"] > now:
                    break
                yes, no = self._tally_for(gid, motion["id"])
                passed = kyosai.motion_passes(yes, no, circulating)
                status = "passed" if passed else "rejected"
                self._conn.execute(
                    "UPDATE kyosai_motions SET status=?, yes=?, no=? WHERE id=?", (status, yes, no, motion["id"])
                )
                if passed:
                    self.bank_sync(gid, now)
                    self._conn.execute(
                        "UPDATE teiai_bank SET trust_premium=? WHERE guild_id=?", (motion["premium"], gid)
                    )
                out.append({**dict(motion), "status": status, "yes": yes, "no": no, "circulating": circulating})
        return out

    def kyosai_motion(self, motion_id: int, now: float) -> Optional[dict[str, Any]]:
        """動議1件と、いまの賛成・反対の口数（ボタンの投稿の書き換え用）。"""
        with self._lock:
            gid = self._g()
            row = self._conn.execute(
                "SELECT * FROM kyosai_motions WHERE id=? AND guild_id=?", (motion_id, gid)
            ).fetchone()
            if row is None:
                return None
            yes, no = self._tally_for(gid, motion_id) if row["status"] == "open" else (row["yes"], row["no"])
            return {**dict(row), "yes": yes, "no": no, "circulating": self._circulating_for(gid)}

    # ------------------------------------------------------------------ #
    # 表示
    # ------------------------------------------------------------------ #
    def kyosai_status(self, user_id: int, now: float) -> dict[str, Any]:
        with self._db.tx():
            gid = self._g()
            self.bank_sync(gid, now)
            holdings = self._holdings_for(gid)
            total = sum(u for _, u in holdings)
            row = self._conn.execute(
                "SELECT kyosai_paid, kyosai_charged, kyosai_div_ts FROM teiai_bank WHERE guild_id=?", (gid,)
            ).fetchone()
            available, liabilities, theta = self._bank.bank_reserve_for(gid, now)
            motions = []
            for m in self._open_motions_for(gid):
                yes, no = self._tally_for(gid, m["id"])
                motions.append({**dict(m), "yes": yes, "no": no})
            auction = self._open_auction_for(gid)
            return {
                "units": self.units_of_for(gid, user_id),
                "total": total,
                "holders": len([1 for uid, _ in holdings if uid != kyosai.HOUSE]),
                "house": self.units_of_for(gid, kyosai.HOUSE),
                "auction": dict(auction) if auction is not None else None,
                "paid": row["kyosai_paid"],
                "charged": row["kyosai_charged"],
                "next_dividend": (row["kyosai_div_ts"] + DAY) if row["kyosai_div_ts"] else None,
                "pool": kyosai.dividend_pool(available, liabilities),
                "theta": theta,
                "locked": self._gate_locked_for(gid, now),
                "premium": self._bank.trust_premium_for(gid),
                "circulating": self._circulating_for(gid),
                "motions": motions,
            }
