"""ゲームの賭け金預かり（escrow）と精算・返金（Economy に混ぜ込む部品）。"""

import time
from typing import Optional

from domain import teiai
from domain.types import INSURANCE_CAP
from infra.db.base import EcoBase


class EscrowMixin(EcoBase):
    """賭け金の預かり・倍賭け・精算・返金。"""

    def place_bet(self, user_id: int, amount: int, game: str) -> Optional[int]:
        """賭け金を残高から預かり、預かり金IDを返す。残高不足なら None。"""
        if amount <= 0:
            return None
        with self._lock, self._conn:
            self._ensure(user_id)
            if self._balance(user_id) < amount:
                return None
            self._apply(user_id, -amount, f"賭け:{game}")
            cur = self._conn.execute(
                "INSERT INTO escrow (guild_id, user_id, amount, game, created_at) VALUES (?, ?, ?, ?, ?)",
                (self._g(), user_id, amount, game, time.time()),
            )
            self._conn.execute(
                "UPDATE users SET total_wagered = total_wagered + ? WHERE guild_id=? AND user_id=?",
                (amount, self._g(), user_id),
            )
            return cur.lastrowid

    def add_to_escrow(self, escrow_id: int, extra: int, credit: bool = False) -> bool:
        """預かり金を増やす（ブラックジャックのダブルダウン）。残高不足なら False。

        credit=True なら、足りない分を帝愛の信用で借りて（借金になる）追加する（can_borrow_for_bet で許される範囲だけ）。
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT user_id, game FROM escrow WHERE id=? AND guild_id=?", (escrow_id, self._g())
            ).fetchone()
            if row is None or extra <= 0:
                return False
            short = extra - self._balance(row["user_id"])
            if short > 0:
                if not credit or not self.can_borrow_for_bet(row["user_id"], short)[0]:
                    return False
                self._seed(row["user_id"], short)  # 手元に足りない分だけ、帝愛が貸す
            self._apply(row["user_id"], -extra, f"追加の賭け:{row['game']}")
            self._conn.execute(
                "UPDATE escrow SET amount = amount + ? WHERE id=? AND guild_id=?", (extra, escrow_id, self._g())
            )
            self._conn.execute(
                "UPDATE users SET total_wagered = total_wagered + ? WHERE guild_id=? AND user_id=?",
                (extra, self._g(), row["user_id"]),
            )
            return True

    def escrow_amount(self, escrow_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT amount FROM escrow WHERE id=? AND guild_id=?", (escrow_id, self._g())
            ).fetchone()
            return row["amount"] if row else 0

    def settle(self, escrow_id: int, payout: int, game: str) -> Optional[int]:
        """預かり金を精算する。payout は「受け取る総額」（賭け金を含む。負けなら 0）。

        すでに精算済みなら何もせず None（二重払いを防ぐ）。成功時は損益（payout - 賭け金）を返す。
        """
        result = self.settle_with_penalty(escrow_id, payout, game, 0)
        return None if result is None else result[0]

    def settle_with_penalty(self, escrow_id: int, payout: int, game: str, penalty: int) -> Optional[tuple]:
        """精算と、倍払いなどの追加の支払いを、1回のトランザクションで行う。

        penalty はまず残高から引き、足りない分は借金にする。負けて払い戻しが 0 のとき、保険券があれば賭け金の半分を返す。
        (損益（追加の支払い・保険を含む）, 追加の支払い（借金にした分を含む）, 保険で戻った額) を返す。
        すでに精算済みなら何もせず None（二重に精算も追加の支払いもしない）。
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT user_id, amount FROM escrow WHERE id=? AND guild_id=?", (escrow_id, self._g())
            ).fetchone()
            if row is None:
                return None
            user_id, amount = row["user_id"], row["amount"]
            payout = max(0, int(payout))
            insured = 0
            if payout == 0:
                prof = self._conn.execute(
                    "SELECT insurance FROM profile WHERE guild_id=? AND user_id=?", (self._g(), user_id)
                ).fetchone()
                if prof and prof["insurance"] > 0:
                    insured = min(amount // 2, INSURANCE_CAP)
                    self._conn.execute(
                        "UPDATE profile SET insurance = insurance - 1 WHERE guild_id=? AND user_id=?",
                        (self._g(), user_id),
                    )
            total = payout + insured
            if total:
                # 払い戻しのうち、賭け金を超えた分（純利益）だけが収入。賭け金の返金や、保険券の返金からは天引きしない
                self._apply(
                    user_id,
                    total,
                    f"払い戻し:{game}" if payout else f"保険券で半額返金:{game}",
                    income=max(0, payout - amount),
                )
            self._conn.execute("DELETE FROM escrow WHERE id=? AND guild_id=?", (escrow_id, self._g()))
            self._bank_income(
                int(max(0, amount - total) * teiai.LOSS_SHARE), 1.0, needs_depositors=True
            )  # 負けた額の一部が、預金者の利回りになる
            taken = -self._apply(user_id, -penalty, f"追加の支払い:{game}") if penalty > 0 else 0
            if penalty > taken:  # 残高で足りない分は、強制執行したうえで、残りを帝愛の借金にする
                self._owe(user_id, penalty - taken, f"{game}の倍払い")
            taken = max(penalty, 0)
            net = total - amount - taken
            self._conn.execute(
                "UPDATE users SET games_played = games_played + 1, total_won = total_won + ?, "
                "biggest_win = MAX(biggest_win, ?) WHERE guild_id=? AND user_id=?",
                (total, net, self._g(), user_id),
            )
            return net, taken, insured

    def refund(self, escrow_id: int) -> bool:
        """エラー時の返金。賭け金（追加分を含む）を全額返す。成績（遊んだ回数・勝ち額）には数えない。"""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT user_id, amount, game FROM escrow WHERE id=? AND guild_id=?", (escrow_id, self._g())
            ).fetchone()
            if row is None:
                return False
            self._apply(row["user_id"], row["amount"], f"返金:{row['game']}")
            self._conn.execute("DELETE FROM escrow WHERE id=? AND guild_id=?", (escrow_id, self._g()))
            return True

    def refund_all_escrows(self) -> int:
        """起動時に呼ぶ。Bot が落ちて残った預かり金を、すべて持ち主に返す。返した件数を返す。"""
        with self._lock, self._conn:
            rows = self._conn.execute("SELECT id, guild_id, user_id, amount, game FROM escrow").fetchall()
            for r in rows:  # 全サーバー分。それぞれ、預かったサーバーの口座へ返す
                try:
                    view = self.for_guild(r["guild_id"])
                except ValueError:  # 出どころ不明の預かり金で、起動全体を止めない（返せないので、記録だけ残す）
                    print(
                        f"出どころ不明の預かり金を破棄します: escrow={r['id']} guild={r['guild_id']} user={r['user_id']} amount={r['amount']} game={r['game']}"
                    )
                    self._conn.execute("DELETE FROM escrow WHERE id=?", (r["id"],))
                    continue
                view._ensure(r["user_id"])
                view._apply(r["user_id"], r["amount"], f"返金（中断したゲーム）:{r['game']}")
                self._conn.execute("DELETE FROM escrow WHERE id=?", (r["id"],))
            return len(rows)
