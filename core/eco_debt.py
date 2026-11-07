"""帝愛ファイナンス: 借金・強制執行・種銭・身分・自己破産（Economy に混ぜ込む部品）。数字と考え方は core/teiai.py。

表の定義は core/schema.py（users の debt・bankrupt_at・blacklist_until）。
"""

import time
from typing import Optional

from core import market as market_mod
from core import shop as shop_mod
from core import teiai

MAX_NOTICES = 5  # たまっている一言の上限（古いものから捨てる）


class DebtMixin:
    """借金・強制執行・種銭・身分・自己破産。"""

    # ------------------------------------------------------------------ #
    # 結果の画面に出す一言（強制執行・種銭など。精算のあとに、画面が取り出して表示する）
    # ------------------------------------------------------------------ #
    def _note(self, user_id: int, text: str) -> None:
        notes = self._notices.setdefault((self._g(), user_id), [])
        notes.append(text)
        del notes[:-MAX_NOTICES]  # 本人が遊ばないままたまり続けないよう、新しい数件だけ残す

    def take_notices(self, user_id: int) -> list:
        """たまっている一言を取り出す（取り出すと空になる）。"""
        return self._notices.pop((self._g(), user_id), [])

    # ------------------------------------------------------------------ #
    # 身分
    # ------------------------------------------------------------------ #
    def _has_overdue(self, user_id: int) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM loans WHERE guild_id=? AND borrower_id=? AND status='overdue' LIMIT 1",
                (self._g(), user_id),
            ).fetchone()
            is not None
        )

    def status(self, user_id: int, now: Optional[float] = None) -> str:
        """身分。読み取り専用で、口座は作らない。破産者は、完済して BLACKLIST_DAYS 日たつと、カタギに戻る。"""
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT debt, bankrupt_at, blacklist_until FROM users WHERE guild_id=? AND user_id=?",
                (self._g(), user_id),
            ).fetchone()
            if row is None:
                return teiai.KATAGI
            if row["bankrupt_at"] is not None and (row["blacklist_until"] is None or now < row["blacklist_until"]):
                return teiai.BANKRUPT
            if row["debt"] > 0 or self._has_overdue(user_id):
                return teiai.UNDERGROUND
            return teiai.KATAGI

    def _go_underground(self, user_id: int) -> None:
        """借金や延滞で、カタギから落ちる。連続ログインは 0 に戻る（すでに落ちていても、同じ）。"""
        self._conn.execute(
            "UPDATE users SET daily_streak=0 WHERE guild_id=? AND user_id=?",
            (self._g(), user_id),
        )

    def _after_debt_change(self, user_id: int) -> None:
        """借金が増減したあとの後始末。破産者が完済したら、そこから BLACKLIST_DAYS 日が始まる。"""
        if self._debt(user_id) > 0:
            return
        self._conn.execute(
            "UPDATE users SET blacklist_until=? WHERE guild_id=? AND user_id=? AND bankrupt_at IS NOT NULL "
            "AND blacklist_until IS NULL",
            (time.time() + teiai.BLACKLIST_DAYS * 86400, self._g(), user_id),
        )

    # ------------------------------------------------------------------ #
    # 収入からの天引き
    # ------------------------------------------------------------------ #
    def _withhold(self, user_id: int, income: int) -> int:
        """収入 income の REPAY_RATE を、借金の返済へ天引きする。先に帝愛、残りを延滞中の貸付へ。天引きした合計を返す。"""
        amount = teiai.withhold(income)
        if amount <= 0:
            return 0
        pay = min(self._debt(user_id), amount, self._balance(user_id))
        if pay > 0:
            self._conn.execute(
                "UPDATE users SET balance=balance-?, debt=debt-? WHERE guild_id=? AND user_id=?",
                (pay, pay, self._g(), user_id),
            )
            self._ledger(user_id, -pay, self._balance(user_id), "帝愛への返済（収入から天引き）")
            self._after_debt_change(user_id)
        return pay + self._collect_overdue_for(user_id, amount - pay)

    # ------------------------------------------------------------------ #
    # 強制執行と、借金の発生
    # ------------------------------------------------------------------ #
    def _take_cash(self, user_id: int, need: int, reason: str) -> int:
        """手元の現金から、need まで差し押さえる。取れた額を返す。"""
        take = min(self._balance(user_id), need)
        if take > 0:
            self._apply(user_id, -take, reason)
        return take

    def _seize_assets(self, user_id: int, need: int, reason: str) -> int:
        """need を満たすまで、現金 → 空売り → CSトークン → 通常債（古い順）の順に強制執行する。充てた額を返す。"""
        covered = self._take_cash(user_id, need, reason)
        if covered >= need:
            return covered
        price = (self.market_latest() or (0, 0, 0))[1]
        if price > 0 and self.short_position(user_id)[0] > 0:
            result = self._short_close_locked(user_id, None, price, "強制執行:空売り決済")
            self._note(user_id, f"💥 空売りの建玉が強制決済されました（戻り {result.money:,}ペリカ）。")
            covered += self._take_cash(user_id, need - covered, reason)
        if covered < need and price > 0 and self.crypto_holding(user_id)[0] > 0:
            result = self._crypto_sell_locked(user_id, None, price)
            self._note(user_id, f"💥 保有していた{market_mod.NAME}が差し押さえられました（{result.money:,}ペリカ）。")
            covered += self._take_cash(user_id, need - covered, reason)
        for row in self._conn.execute(
            "SELECT id, kind FROM bonds WHERE guild_id=? AND user_id=? AND status='active' ORDER BY id",
            (self._g(), user_id),
        ).fetchall():
            if covered >= need:
                break
            ok, refund = self._bond_cancel_locked(user_id, row["id"])
            if ok:
                self._note(user_id, f"💥 通常債が強制解約されました（{refund:,}ペリカ）。")
                covered += self._take_cash(user_id, need - covered, reason)
        return covered

    def _seed(self, user_id: int, amount: int) -> None:
        """帝愛が種銭を貸し付ける。手元に入り、同じ額が借金に加わる（天引きの対象にはならない）。"""
        self._apply(user_id, amount, "帝愛の種銭貸付")
        self._conn.execute(
            "UPDATE users SET debt = debt + ? WHERE guild_id=? AND user_id=?", (amount, self._g(), user_id)
        )
        self._note(
            user_id, f"🕴️ 帝愛ファイナンスから種銭 {amount:,}ペリカ が貸し付けられました。「これで取り返してこい…！」"
        )

    def _seed_if_empty(self, user_id: int) -> int:
        """借金があって手元が空なら、種銭を貸す。貸した額を返す。"""
        if self._debt(user_id) > 0 and self._balance(user_id) == 0:
            self._seed(user_id, teiai.SEED)
            return teiai.SEED
        return 0

    def _topup_seed(self, user_id: int) -> int:
        """（デイリー）借金があって、手元が種銭の枠に満たなければ、枠まで貸す。貸した額を返す。"""
        short = teiai.SEED - self._balance(user_id)
        if self._debt(user_id) > 0 and short > 0:
            self._seed(user_id, short)
            return short
        return 0

    def _owe(self, user_id: int, shortfall: int, reason: str) -> int:
        """払えなかった shortfall を、強制執行したうえで、残りを帝愛の借金にする。借金に加えた額を返す。"""
        if shortfall <= 0:
            return 0
        need = shortfall - self._seize_assets(user_id, shortfall, f"強制執行:{reason}")
        if need <= 0:
            return 0
        self._conn.execute(
            "UPDATE users SET debt = debt + ? WHERE guild_id=? AND user_id=?", (need, self._g(), user_id)
        )
        self._ledger(user_id, 0, self._balance(user_id), f"借金:{reason}（{need:,}ペリカ）")
        self._go_underground(user_id)
        self._seed_if_empty(user_id)
        return need

    # ------------------------------------------------------------------ #
    # 返済（自分から）
    # ------------------------------------------------------------------ #
    def repay_debt(self, user_id: int, amount: Optional[int] = None) -> tuple:
        """手元の現金から、帝愛の借金を返す（amount が None なら、返せる限り全額）。(成功か, 理由か返した額) を返す。"""
        with self._lock, self._conn:
            self._ensure(user_id)
            debt, balance = self._debt(user_id), self._balance(user_id)
            if debt <= 0:
                return False, "帝愛への借金はありません。"
            pay = min(debt, balance) if amount is None else amount
            if pay <= 0:
                return False, f"手元にペリカがありません（いまの残高: {balance:,}ペリカ）。"
            if pay > debt:
                return False, f"返す額が借金を超えています（借金: {debt:,}ペリカ）。"
            if pay > balance:
                return False, f"残高が足りません（いまの残高: {balance:,}ペリカ）。"
            self._conn.execute(
                "UPDATE users SET balance=balance-?, debt=debt-? WHERE guild_id=? AND user_id=?",
                (pay, pay, self._g(), user_id),
            )
            self._ledger(user_id, -pay, self._balance(user_id), "帝愛への返済")
            self._after_debt_change(user_id)
            return True, pay

    # ------------------------------------------------------------------ #
    # 自己破産
    # ------------------------------------------------------------------ #
    def _liquidate_everything(self, user_id: int) -> int:
        """空売り・CSトークン・債券をすべて現金にして、手元の現金ごと、すべて差し押さえる。差し押さえた現金の合計を返す。

        通常債は途中解約の返金額、解約できない債券（ジャンク債）は、元本の SEIZE_RATE で換金する。
        """
        total = 0
        price = (self.market_latest() or (0, 0, 0))[1]
        if price > 0 and self.short_position(user_id)[0] > 0:
            self._short_close_locked(user_id, None, price, "破産:空売り決済")
        if price > 0 and self.crypto_holding(user_id)[0] > 0:
            self._crypto_sell_locked(user_id, None, price)
        for row in self._conn.execute(
            "SELECT id, principal FROM bonds WHERE guild_id=? AND user_id=? AND status='active' ORDER BY id",
            (self._g(), user_id),
        ).fetchall():
            ok, _ = self._bond_cancel_locked(user_id, row["id"])
            if not ok:  # 解約できない債券は、元本の一部で換金して、差し押さえる
                self._apply(user_id, int(row["principal"] * teiai.SEIZE_RATE), "破産:債券の差し押さえ")
                self._conn.execute("UPDATE bonds SET status='cancelled' WHERE id=?", (row["id"],))
        total += self._take_cash(user_id, self._balance(user_id), "破産:現金の差し押さえ")
        return total

    def _seize_shop(self, user_id: int) -> int:
        """ショップの持ち物と装備をすべて消して、換金額（購入額の SEIZE_RATE）を返す（現金は動かさない）。"""
        value = 0
        for row in self._conn.execute(
            "SELECT item_id, qty FROM inventory WHERE guild_id=? AND user_id=? AND qty>0", (self._g(), user_id)
        ).fetchall():
            item = shop_mod.BY_ID.get(row["item_id"])
            value += (item.price if item else 0) * row["qty"]
        prof = self._conn.execute(
            "SELECT daily_boost, insurance FROM profile WHERE guild_id=? AND user_id=?", (self._g(), user_id)
        ).fetchone()
        if prof:  # 使う前の消耗品（有効にしてある分）も、没収する
            value += prof["daily_boost"] * shop_mod.BY_ID[shop_mod.DAILY_DOUBLE].price
            value += prof["insurance"] * shop_mod.BY_ID[shop_mod.INSURANCE].price
        self._conn.execute("DELETE FROM inventory WHERE guild_id=? AND user_id=?", (self._g(), user_id))
        self._conn.execute(
            "UPDATE profile SET title=NULL, card_color=NULL, card_icon=NULL, skin_dice=NULL, skin_table=NULL, "
            "daily_boost=0, insurance=0 WHERE guild_id=? AND user_id=?",
            (self._g(), user_id),
        )
        return int(value * teiai.SEIZE_RATE)

    def bankrupt(self, user_id: int) -> tuple:
        """自己破産。全財産とショップの全品を差し押さえて換金し、帝愛 → 貸付の順に配当する。(成功か, 理由か結果) を返す。

        貸付の残りは免責（貸し倒れ）。帝愛の借金は、BANKRUPT_FLOOR まで圧縮される。破産者の身分になり、完済して
        BLACKLIST_DAYS 日たつまで、貸付を借りられない。
        """
        with self._lock, self._conn:
            self._ensure(user_id)
            if self.status(user_id) == teiai.BANKRUPT:
                return False, "すでに破産者です。"
            loans = [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM loans WHERE guild_id=? AND borrower_id=? AND status IN ('active','overdue') ORDER BY id",
                    (self._g(), user_id),
                )
            ]
            if self._debt(user_id) <= 0 and not loans:
                return False, "借金も貸付の借り入れもないので、破産できません。"
            pool = self._liquidate_everything(user_id)
            shop_value = self._seize_shop(user_id)
            pool += shop_value
            total_pool = pool
            to_senior = min(pool, self._debt(user_id))  # 第1順位: 帝愛
            self._conn.execute(
                "UPDATE users SET debt=debt-? WHERE guild_id=? AND user_id=?", (to_senior, self._g(), user_id)
            )
            pool -= to_senior
            junior_paid, forgiven = 0, 0
            now = time.time()
            for loan in loans:  # 第2順位: 貸付（古い順）。残りは免責
                at = loan["due"] if loan["status"] == "overdue" else now
                pay = min(pool, self.loan_remaining(loan, at))
                if pay > 0:
                    self._ensure(loan["lender_id"])
                    self._apply(loan["lender_id"], pay, f"貸付の配当（借り手の自己破産）←{user_id}（#{loan['id']}）")
                    loan["paid"] += pay
                    self._conn.execute("UPDATE loans SET paid=? WHERE id=?", (loan["paid"], loan["id"]))
                    pool -= pay
                    junior_paid += pay
                left = self.loan_remaining(loan, at)
                forgiven += left
                self._conn.execute(
                    "UPDATE loans SET status=? WHERE id=?", ("repaid" if left <= 0 else "discharged", loan["id"])
                )
            if pool > 0:  # 借金をすべて払ってもあまった分は、手元に残す
                self._apply(user_id, pool, "破産の整理のあまり")
            compressed = max(0, self._debt(user_id) - teiai.BANKRUPT_FLOOR)
            self._conn.execute(
                "UPDATE users SET debt=debt-?, bankrupt_at=?, blacklist_until=NULL WHERE guild_id=? AND user_id=?",
                (compressed, now, self._g(), user_id),
            )
            self._go_underground(user_id)
            self._after_debt_change(user_id)
            seed = self._seed_if_empty(user_id)
            return True, {
                "seized": total_pool,
                "shop_value": shop_value,
                "senior_paid": to_senior,
                "junior_paid": junior_paid,
                "forgiven": forgiven,
                "compressed": compressed,
                "debt": self._debt(user_id),
                "seed": seed,
            }
