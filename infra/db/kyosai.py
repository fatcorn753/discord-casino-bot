"""帝愛共済（#34）の記録。口・配当・超過損失の請求・回収と帝愛の持ち口の販売・譲渡・動議。

表の定義は infra/db/schema/tables.py（kyosai_units・kyosai_offers・kyosai_motions・kyosai_votes、
teiai_bank の kyosai_* と trust_premium）。数字と考え方は domain/kyosai.py。
"""

from typing import Any, Optional

from domain import kyosai
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
        if dst == kyosai.HOUSE and self.units_of_for(guild_id, kyosai.HOUSE) == 0:
            # 帝愛の持ち口が 0 から増えるときに、売値の数え始めをいまにする
            self._conn.execute("UPDATE teiai_bank SET kyosai_house_ts=? WHERE guild_id=?", (now, guild_id))
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
                self._bank.bank_pay_for(gid, to_users, now)
                for user_id, share in shares.items():
                    if user_id == kyosai.HOUSE or share <= 0:
                        continue
                    self._account.ensure(gid, user_id)
                    self._account.apply(gid, user_id, share, "共済:配当", income=share)
                    paid += share
                self._conn.execute("UPDATE teiai_bank SET kyosai_paid=kyosai_paid+? WHERE guild_id=?", (paid, gid))
            return {"pool": pool, "paid": paid, "recovered": recovered}

    def _recover_dormant_for(self, guild_id: int, now: float) -> int:
        """DORMANT_DAYS 日、口座が動いていない人（共済の配当だけの動きは数えない）の口を回収する。"""
        since = now - kyosai.DORMANT_DAYS * DAY
        recovered = 0
        for user_id, _units in self._holdings_for(guild_id, with_house=False):
            last = self._conn.execute(
                "SELECT MAX(ts) FROM ledger WHERE guild_id=? AND user_id=? AND reason NOT LIKE '共済%'",
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
    # 帝愛の持ち口を買う
    # ------------------------------------------------------------------ #
    def house_price_for(self, guild_id: int, now: float) -> int:
        since = self._conn.execute("SELECT kyosai_house_ts FROM teiai_bank WHERE guild_id=?", (guild_id,)).fetchone()
        return kyosai.auction_price(since[0] if since else now, now)

    def kyosai_buy_house(self, user_id: int, units: int, now: float) -> tuple[Any, ...]:
        """帝愛の持ち口を買う。(成功か, Failure か払った額)。代金は銀行の収入になる。"""
        if units <= 0:
            return False, fail("kyosai_bad_units")
        with self._db.tx():
            gid = self._g()
            self.bank_sync(gid, now)
            self._account.ensure(gid, user_id)
            have = self.units_of_for(gid, kyosai.HOUSE)
            if have < units:
                return False, fail("kyosai_house_short", have=have)
            debt = self._account.debt(gid, user_id)
            if debt > 0:
                return False, fail("debt_blocks", debt=debt, verb="買えません")
            cost = self.house_price_for(gid, now) * units
            bal = self._account.raw_balance(gid, user_id)
            if bal < cost:
                return False, fail("insufficient_balance", balance=bal)
            self._account.apply(gid, user_id, -cost, "共済:帝愛の持ち口を購入")
            self._bank.bank_income_for(gid, cost, now)
            self._move_units_for(gid, kyosai.HOUSE, user_id, units, now)
            return True, cost

    # ------------------------------------------------------------------ #
    # 譲渡（プレイヤー間の売買）
    # ------------------------------------------------------------------ #
    def _offered_units_for(self, guild_id: int, seller: int, now: float) -> int:
        return self._conn.execute(
            "SELECT COALESCE(SUM(units), 0) FROM kyosai_offers "
            "WHERE guild_id=? AND seller=? AND status='open' AND expires>?",
            (guild_id, seller, now),
        ).fetchone()[0]

    def kyosai_offer(self, seller: int, buyer: int, units: int, price: int, now: float) -> tuple[Any, ...]:
        """口の譲渡を申し出る（相手が受けると成立）。(成功か, Failure か申し出の番号)。"""
        if units <= 0:
            return False, fail("kyosai_bad_units")
        if price < 0:
            return False, fail("kyosai_bad_price")
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
            if row["price"]:
                self._account.apply(gid, buyer, -row["price"], "共済:口の購入")
                self._account.ensure(gid, row["seller"])
                self._account.apply(gid, row["seller"], row["price"], "共済:口の売却", income=row["price"])
            self._move_units_for(gid, row["seller"], buyer, row["units"], now)
            self._conn.execute("UPDATE kyosai_offers SET status='done' WHERE id=?", (offer_id,))
            return True, dict(row)

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
    # 動議（信託の上乗せ金利）
    # ------------------------------------------------------------------ #
    def _open_motion_for(self, guild_id: int) -> Optional[Any]:
        return self._conn.execute(
            "SELECT * FROM kyosai_motions WHERE guild_id=? AND status='open' ORDER BY id LIMIT 1", (guild_id,)
        ).fetchone()

    def kyosai_propose(self, user_id: int, premium: float, now: float) -> tuple[Any, ...]:
        """動議を出す（投票中の動議がないときだけ）。(成功か, Failure か動議の番号)。"""
        if not 0 <= premium <= kyosai.PREMIUM_MAX:
            return False, fail("kyosai_bad_premium", max_pct=kyosai.PREMIUM_MAX * 100)
        with self._db.tx():
            gid = self._g()
            if self.units_of_for(gid, user_id) <= 0:
                return False, fail("kyosai_no_units")
            if self._open_motion_for(gid) is not None:
                return False, fail("kyosai_motion_open")
            cur = self._conn.execute(
                "INSERT INTO kyosai_motions (guild_id, proposer, premium, closes) VALUES (?, ?, ?, ?)",
                (gid, user_id, premium, now + kyosai.MOTION_HOURS * 3600),
            )
            motion_id = cur.lastrowid
            self._conn.execute(
                "INSERT INTO kyosai_votes (motion_id, user_id, yes) VALUES (?, ?, 1)", (motion_id, user_id)
            )
            return True, motion_id

    def kyosai_vote(self, user_id: int, yes: bool, now: float) -> tuple[Any, ...]:
        """投票中の動議に投票する（あとから変えられる）。(成功か, Failure か動議の番号)。"""
        with self._db.tx():
            gid = self._g()
            motion = self._open_motion_for(gid)
            if motion is None or motion["closes"] <= now:
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

    def kyosai_close_motion(self, now: float) -> Optional[dict[str, Any]]:
        """締切を過ぎた動議を締める。可決なら信託の上乗せ金利を変える。締めたときだけ結果を返す。"""
        with self._db.tx():
            gid = self._g()
            motion = self._open_motion_for(gid)
            if motion is None or motion["closes"] > now:
                return None
            yes, no = self._tally_for(gid, motion["id"])
            passed = kyosai.motion_passes(yes, no)
            self._conn.execute(
                "UPDATE kyosai_motions SET status=?, yes=?, no=? WHERE id=?",
                ("passed" if passed else "rejected", yes, no, motion["id"]),
            )
            if passed:
                self.bank_sync(gid, now)
                self._conn.execute("UPDATE teiai_bank SET trust_premium=? WHERE guild_id=?", (motion["premium"], gid))
            return {**dict(motion), "status": "passed" if passed else "rejected", "yes": yes, "no": no}

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
            motion = self._open_motion_for(gid)
            tally = self._tally_for(gid, motion["id"]) if motion is not None else (0, 0)
            return {
                "units": self.units_of_for(gid, user_id),
                "total": total,
                "holders": len([1 for uid, _ in holdings if uid != kyosai.HOUSE]),
                "house": self.units_of_for(gid, kyosai.HOUSE),
                "house_price": self.house_price_for(gid, now),
                "paid": row["kyosai_paid"],
                "charged": row["kyosai_charged"],
                "next_dividend": (row["kyosai_div_ts"] + DAY) if row["kyosai_div_ts"] else None,
                "pool": kyosai.dividend_pool(available, liabilities),
                "theta": theta,
                "locked": self._gate_locked_for(gid, now),
                "premium": self._bank.trust_premium_for(gid),
                "motion": dict(motion) if motion is not None else None,
                "tally": tally,
            }
