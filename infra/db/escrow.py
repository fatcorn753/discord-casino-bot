"""ゲームの賭け金預かり（escrow）と精算・返金。"""

from domain import teiai
from domain.games import jackpot
from domain.ports.bridges import IBankCashFlow, IDebtOps
from domain.ports.repos import IEscrowRepo
from domain.types import INSURANCE_CAP
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo
from infra.db.profile import ProfileCore


class EscrowRepo(ScopedRepo, IEscrowRepo):
    """賭け金の預かり・倍賭け・精算・返金。"""

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        bank: IBankCashFlow,
        debt: IDebtOps,
        guild_id: int = 0,
        strict: bool = False,
        *,
        profile: ProfileCore | None = None,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._bank = bank
        self._debt = debt
        self._profile = profile or ProfileCore(db)

    def place_bet(self, user_id: int, amount: int, game: str) -> int | None:
        """賭け金を残高から預かり、預かり金IDを返す。残高不足なら None。"""
        if amount <= 0:
            return None
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            if self._account.raw_balance(gid, user_id) < amount:
                return None
            self._account.apply(gid, user_id, -amount, f"賭け:{game}", kind="game_bet")
            cur = self._conn.execute(
                "INSERT INTO escrow (guild_id, user_id, amount, game, created_at) VALUES (?, ?, ?, ?, ?)",
                (gid, user_id, amount, game, self._now()),
            )
            self._account.record_wager(gid, user_id, amount)
            return cur.lastrowid

    def add_to_escrow(self, escrow_id: int, extra: int, credit: bool = False) -> bool:
        """預かり金を増やす（ブラックジャックのダブルダウン）。残高不足なら False。

        credit=True なら、足りない分を帝愛の信用で借りて（借金になる）追加する（can_borrow_for_bet で許される範囲だけ）。
        """
        with self._db.tx():
            gid = self._g()
            row = self._one("SELECT user_id, game FROM escrow WHERE id=? AND guild_id=?", (escrow_id, gid))
            if row is None or extra <= 0:
                return False
            short = extra - self._account.raw_balance(gid, row["user_id"])
            if short > 0:
                if not credit or not self._debt.can_borrow_for_bet_for(gid, row["user_id"], short)[0]:
                    return False
                self._debt.seed_for(gid, row["user_id"], short)  # 手元に足りない分だけ、帝愛が貸す
            self._account.apply(gid, row["user_id"], -extra, f"追加の賭け:{row['game']}", kind="game_bet_add")
            self._conn.execute(
                "UPDATE escrow SET amount = amount + ? WHERE id=? AND guild_id=?", (extra, escrow_id, gid)
            )
            self._account.record_wager(gid, row["user_id"], extra)
            return True

    def escrow_amount(self, escrow_id: int) -> int:
        with self._lock:
            v = self._scalar("SELECT amount FROM escrow WHERE id=? AND guild_id=?", (escrow_id, self._g()))
            return v if v is not None else 0

    def settle(self, escrow_id: int, payout: int, game: str) -> int | None:
        """預かり金を精算する。payout は「受け取る総額」（賭け金を含む。負けなら 0）。

        すでに精算済みなら何もせず None（二重払いを防ぐ）。成功時は損益（payout - 賭け金）を返す。
        """
        result = self.settle_with_penalty(escrow_id, payout, game, 0)
        return None if result is None else result[0]

    def settle_with_penalty(self, escrow_id: int, payout: int, game: str, penalty: int) -> tuple[int, int, int] | None:
        """精算と、倍払いなどの追加の支払いを、1回のトランザクションで行う。

        penalty はまず残高から引き、足りない分は借金にする。負けて払い戻しが 0 のとき、保険券があれば賭け金の半分を返す。
        (損益（追加の支払い・保険を含む）, 追加の支払い（借金にした分を含む）, 保険で戻った額) を返す。
        すでに精算済みなら何もせず None（二重に精算も追加の支払いもしない）。
        """
        with self._db.tx():
            gid = self._g()
            row = self._one("SELECT user_id, amount FROM escrow WHERE id=? AND guild_id=?", (escrow_id, gid))
            if row is None:
                return None
            user_id, amount = row["user_id"], row["amount"]
            payout = max(0, int(payout))
            insured = (
                min(amount // 2, INSURANCE_CAP) if payout == 0 and self._profile.consume_buff(gid, user_id, "insurance") else 0
            )
            total = payout + insured
            if total:
                # 払い戻しのうち、賭け金を超えた分（純利益）だけが収入。賭け金の返金や、保険券の返金からは天引きしない
                self._account.apply(
                    gid,
                    user_id,
                    total,
                    f"払い戻し:{game}" if payout else f"保険券で半額返金:{game}",
                    income=max(0, payout - amount),
                    kind="game_payout" if payout else "game_insurance",
                )
            self._conn.execute("DELETE FROM escrow WHERE id=? AND guild_id=?", (escrow_id, gid))
            lost = max(0, amount - total)
            self._bank.bank_income_for(
                gid, int(lost * teiai.LOSS_SHARE), self._now()
            )  # 負けた額の一部が、帝愛銀行の収入になる
            if jackpot.contribution(lost) > 0:  # 負けて消える分の一部を、共有ジャックポットに積む
                self._conn.execute(
                    "INSERT INTO jackpot (guild_id, pool) VALUES (?, ?) "
                    "ON CONFLICT(guild_id) DO UPDATE SET pool = pool + excluded.pool",
                    (gid, jackpot.contribution(lost)),
                )
            taken = (
                -self._account.apply(gid, user_id, -penalty, f"追加の支払い:{game}", kind="game_penalty")
                if penalty > 0
                else 0
            )
            if penalty > taken:  # 残高で足りない分は、強制執行したうえで、残りを帝愛の借金にする
                self._debt.owe_for(gid, user_id, penalty - taken, f"{game}の倍払い")
            taken = max(penalty, 0)
            net = total - amount - taken
            self._account.record_game_result(gid, user_id, total, net)
            return net, taken, insured

    def jackpot_pool(self) -> int:
        """いまの共有ジャックポットの積み立て。"""
        with self._lock:
            v = self._scalar("SELECT pool FROM jackpot WHERE guild_id=?", (self._g(),))
            return v if v is not None else 0

    def jackpot_claim(self, user_id: int) -> int:
        """積み立てを全額 user_id に払い、0 に戻す。払った額を返す（収入として扱う。借金があれば天引きされる）。"""
        with self._db.tx():
            gid = self._g()
            v = self._scalar("SELECT pool FROM jackpot WHERE guild_id=?", (gid,))
            pool = v if v is not None else 0
            if pool <= 0:
                return 0
            self._account.ensure(gid, user_id)
            self._account.apply(gid, user_id, pool, "ジャックポット", income=pool)
            self._conn.execute(
                "UPDATE jackpot SET pool=0, last_user=?, last_amount=?, last_at=? WHERE guild_id=?",
                (user_id, pool, self._now(), gid),
            )
            return pool

    def refund(self, escrow_id: int) -> bool:
        """エラー時の返金。賭け金（追加分を含む）を全額返す。成績（遊んだ回数・勝ち額）には数えない。"""
        with self._db.tx():
            gid = self._g()
            row = self._one("SELECT user_id, amount, game FROM escrow WHERE id=? AND guild_id=?", (escrow_id, gid))
            if row is None:
                return False
            self._account.apply(gid, row["user_id"], row["amount"], f"返金:{row['game']}", kind="game_refund")
            self._conn.execute("DELETE FROM escrow WHERE id=? AND guild_id=?", (escrow_id, gid))
            return True

    def refund_all_escrows(self) -> int:
        """起動時に呼ぶ。Bot が落ちて残った預かり金を、すべて持ち主に返す。返した件数を返す。"""
        with self._db.tx():
            rows = self._all("SELECT id, guild_id, user_id, amount, game FROM escrow")
            for r in rows:  # 全サーバー分。それぞれ、預かったサーバーの口座へ返す
                gid = int(r["guild_id"])
                if gid < 0 or (
                    self._strict and gid == 0
                ):  # 出どころ不明の預かり金で、起動全体を止めない（返せないので、記録だけ残す）
                    print(
                        f"出どころ不明の預かり金を破棄します: escrow={r['id']} guild={r['guild_id']} user={r['user_id']} amount={r['amount']} game={r['game']}"
                    )
                    self._conn.execute("DELETE FROM escrow WHERE id=?", (r["id"],))
                    continue
                self._account.ensure(gid, r["user_id"])
                self._account.apply(
                    gid, r["user_id"], r["amount"], f"返金（中断したゲーム）:{r['game']}", kind="game_refund"
                )
                self._conn.execute("DELETE FROM escrow WHERE id=?", (r["id"],))
            return len(rows)
