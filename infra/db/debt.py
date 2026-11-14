"""帝愛ファイナンス: 借金・強制執行・種銭・身分・自己破産（Economy に混ぜ込む部品）。数字と考え方は core/teiai.py。

表の定義は core/schema.py（users の debt・bankrupt_at・blacklist_until）。
"""

import random
import time
from typing import Any, Optional

from domain import market as market_mod
from domain import shop as shop_mod
from domain import teiai
from domain.errors import Notice, fail, notice
from infra import catalogs
from infra.db.base import EcoBase

MAX_NOTICES = catalogs.economy().wallet.max_notices  # たまっている一言の上限（古いものから捨てる）


class DebtMixin(EcoBase):
    """借金・強制執行・種銭・身分・自己破産。"""

    # ------------------------------------------------------------------ #
    # 結果の画面に出す一言（強制執行・種銭など。精算のあとに、画面が取り出して表示する）
    # ------------------------------------------------------------------ #
    def _note(self, user_id: int, code: str, **params: Any) -> None:
        notes = self._notices.setdefault((self._g(), user_id), [])
        notes.append(notice(code, **params))
        del notes[:-MAX_NOTICES]  # 本人が遊ばないままたまり続けないよう、新しい数件だけ残す

    def take_notices(self, user_id: int) -> list[Notice]:
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
        """身分。読み取り専用で、口座は作らない。破産者は、借金が残っている間だけ（完済すれば、カタギ・地下労働者に戻る）。

        貸付を借りられない期間（ブラックリスト）は、身分とは別で、完済から BLACKLIST_DAYS 日続く（loan_blacklisted）。
        """
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT debt, bankrupt_at, blacklist_until FROM users WHERE guild_id=? AND user_id=?",
                (self._g(), user_id),
            ).fetchone()
            if row is None:
                return teiai.KATAGI
            if row["bankrupt_at"] is not None and row["debt"] > 0:
                return teiai.BANKRUPT
            if row["debt"] > 0 or self._has_overdue(user_id):
                return teiai.UNDERGROUND
            return teiai.KATAGI

    def loan_blacklisted(self, user_id: int, now: Optional[float] = None) -> bool:
        """破産のブラックリストの間か（貸付を借りられない）。破産してから、完済して BLACKLIST_DAYS 日たつまで。身分（破産者）より長い。"""
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT debt, bankrupt_at, blacklist_until FROM users WHERE guild_id=? AND user_id=?",
                (self._g(), user_id),
            ).fetchone()
        if row is None or row["bankrupt_at"] is None:
            return False
        return row["debt"] > 0 or row["blacklist_until"] is None or now < row["blacklist_until"]

    def _accrue_debt_interest(self, user_id: int, now: Optional[float] = None) -> int:
        """帝愛の利息（トイチ）を、借金に加える。加えた額を返す。呼び出し側のトランザクションの中で使う。

        経過した**日数（切り上げずに、満了した日だけ）**ぶんの単利で、1回に数えるのは DEBT_INTEREST_MAX_DAYS 日まで
        （しばらく来なかった人の分は、超えた日を数えない）。借金が DEBT_INTEREST_CEILING に届くと、それ以上は付けない。
        借金がなくなると、数え始めの時刻を忘れる（次に借金ができたときは、その時点から数える）。
        """
        now = time.time() if now is None else now
        row = self._conn.execute(
            "SELECT debt, debt_interest_at FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)
        ).fetchone()
        if row is None:
            return 0
        debt, since = row["debt"], row["debt_interest_at"]
        if debt <= 0:
            if since:
                self._conn.execute(
                    "UPDATE users SET debt_interest_at=0 WHERE guild_id=? AND user_id=?", (self._g(), user_id)
                )
            return 0
        if not since:  # 借金ができて、はじめて数える。この時点から
            self._conn.execute(
                "UPDATE users SET debt_interest_at=? WHERE guild_id=? AND user_id=?", (now, self._g(), user_id)
            )
            return 0
        days = int(max(0.0, now - since) // 86400)
        if days <= 0:
            return 0
        counted = min(days, teiai.DEBT_INTEREST_MAX_DAYS)
        add = max(0, min(int(debt * teiai.DEBT_RATE_PER_DAY * counted), teiai.DEBT_INTEREST_CEILING - debt))
        new_since = now if days > counted else since + days * 86400.0  # 数えきれなかった日は、捨てる
        self._conn.execute(
            "UPDATE users SET debt=debt+?, debt_interest_at=? WHERE guild_id=? AND user_id=?",
            (add, new_since, self._g(), user_id),
        )
        if add > 0:
            balance = self._conn.execute(
                "SELECT balance FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()["balance"]
            self._ledger(
                user_id,
                0,
                balance,
                f"帝愛の利息（トイチ・日利{teiai.DEBT_RATE_PER_DAY:.0%}・{counted}日分）+{add:,}",
            )
            self._note(user_id, "debt_interest", add=add, rate=teiai.DEBT_RATE_PER_DAY)
        return add

    def _after_debt_change(self, user_id: int) -> None:
        """借金が増減したあとの後始末。破産猶予の出入りを直し、破産者が完済したら、そこから BLACKLIST_DAYS 日が始まる。"""
        self._grace_update(user_id)
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

    def _repay_all_of(self, user_id: int, amount: int, label: str) -> int:
        """入ってきた amount（元本など、収入ではないお金）を、借金があれば全額、返済に充てる（天引きの3割ではなく）。返済した合計を返す。

        解約できないジャンク債などの満期で、元本が手元に入ってくる抜け道を塞ぐために使う。借金がなければ何もしない。
        """
        pay = min(self._debt(user_id), amount, self._balance(user_id))
        if pay > 0:
            self._conn.execute(
                "UPDATE users SET balance=balance-?, debt=debt-? WHERE guild_id=? AND user_id=?",
                (pay, pay, self._g(), user_id),
            )
            self._ledger(user_id, -pay, self._balance(user_id), label)
            self._after_debt_change(user_id)
        return pay + self._collect_overdue_for(user_id, min(amount - pay, self._balance(user_id)))

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
        """need を満たすまで、現金 → 空売り・信用買い → 現物 → 通常債（古い順）の順に強制執行する。充てた額を返す。"""
        covered = self._take_cash(user_id, need, reason)
        if covered < need:  # 預金は、引き出しの制限なしに、差し押さえる
            taken = self._deposit_take(user_id, need - covered)
            if taken:
                self._ledger(user_id, 0, self._balance(user_id), f"強制執行:預金の差し押さえ（{taken:,}ペリカ）")
                self._note(user_id, "seize_deposit", taken=taken)
                covered += taken
        if covered >= need:
            return covered
        prices = self.latest_prices()
        for side, symbol, *_ in self.margin_positions(user_id):  # 空売り・信用買いを、銘柄の順に強制決済
            if covered >= need or symbol not in prices:
                continue
            name = self.SIDE_NAME[side]
            result = self._margin_close_locked(user_id, side, None, prices[symbol], f"強制執行:{name}決済", symbol)
            self._note(
                user_id,
                "seize_margin",
                symbol_name=market_mod.BY_KEY[symbol].name,
                side_label=name,
                money=result.money,
            )
            covered += self._take_cash(user_id, need - covered, reason)
        for symbol, units, _ in self.holdings(user_id):  # 現物を、銘柄の順に差し押さえ
            if covered >= need or symbol not in prices:
                continue
            result = self._crypto_sell_locked(user_id, None, prices[symbol], symbol)
            self._note(
                user_id,
                "seize_spot",
                symbol_name=market_mod.BY_KEY[symbol].name,
                money=result.money,
            )
            covered += self._take_cash(user_id, need - covered, reason)
        for row in self._conn.execute(  # オプションを、時価（売値）で売って充てる
            "SELECT id FROM options WHERE guild_id=? AND user_id=? AND status='active' ORDER BY id",
            (self._g(), user_id),
        ).fetchall():
            if covered >= need:
                break
            ok, got = self._option_close_locked(user_id, row["id"], time.time())
            if ok and got > 0:
                self._note(user_id, "seize_option", got=got)
                covered += self._take_cash(user_id, need - covered, reason)
        for row in self._conn.execute(
            "SELECT id, kind FROM bonds WHERE guild_id=? AND user_id=? AND status='active' ORDER BY id",
            (self._g(), user_id),
        ).fetchall():
            if covered >= need:
                break
            ok, refund = self._bond_cancel_locked(user_id, row["id"])
            if ok:
                self._note(user_id, "seize_bond", refund=refund)
                covered += self._take_cash(user_id, need - covered, reason)
        return covered

    def _seed(self, user_id: int, amount: int) -> None:
        """帝愛が種銭を貸し付ける。手元に入り、同じ額が借金に加わる（天引きの対象にはならない）。"""
        self._apply(user_id, amount, "帝愛の種銭貸付")
        self._conn.execute(
            "UPDATE users SET debt = debt + ? WHERE guild_id=? AND user_id=?", (amount, self._g(), user_id)
        )
        self._grace_update(user_id)
        self._note(user_id, "seed_loan", amount=amount)

    def borrow_seed(self, user_id: int) -> tuple:
        """（いつでも）手元が種銭の枠に満たなければ、枠まで帝愛から借りる。借金が増え、地下労働者になる。(成功か, 理由か借りた額)。"""
        with self._lock, self._conn:
            self._ensure(user_id)
            if self.status(user_id) == teiai.BANKRUPT:
                return False, fail("seed_bankrupt_blocked")
            if self.grace_until(user_id) is not None:
                return False, fail("seed_grace_blocked")
            short = teiai.SEED - self._balance(user_id)
            if short <= 0:
                return False, fail("seed_not_needed", seed=teiai.SEED)
            self._seed(user_id, short)
            return True, short

    def can_borrow_for_bet(self, user_id: int, need: int) -> tuple:
        """（ダブルダウンなど）手元が足りない need を、帝愛の信用で借りて賭けられるか。(可否, Failure | "")。借りられるのは、種銭の枠まで。"""
        with self._lock:
            if need <= 0:
                return True, ""
            if self.status(user_id) == teiai.BANKRUPT:
                return False, fail("credit_bankrupt_blocked")
            if self.grace_until(user_id) is not None:
                return False, fail("credit_grace_blocked")
            if need > teiai.SEED:
                return False, fail("credit_limit_exceeded", seed=teiai.SEED, need=need)
            return True, ""

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
        need = shortfall - self._seize_assets(
            user_id, shortfall, f"強制執行:{reason}"
        )  # 先に、自分の資産で払う（ファーストロス）
        if need <= 0:
            return 0
        need -= self._cds_cover_self(user_id, need)  # それでも足りない分だけ、自分への CDS が帝愛の肩代わりで受ける
        if need <= 0:
            return 0
        self._conn.execute(
            "UPDATE users SET debt = debt + ? WHERE guild_id=? AND user_id=?", (need, self._g(), user_id)
        )
        self._ledger(user_id, 0, self._balance(user_id), f"借金:{reason}（{need:,}ペリカ）")
        self._seed_if_empty(user_id)
        self._grace_update(user_id)
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
                return False, fail("repay_no_debt")
            pay = min(debt, balance) if amount is None else amount
            if pay <= 0:
                return False, fail("repay_no_cash", balance=balance)
            if pay > debt:
                return False, fail("repay_exceeds_debt", debt=debt)
            if pay > balance:
                return False, fail("insufficient_balance", balance=balance)
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

        通常債は売却額（DCF）、売れない債券（ジャンク債）は、元本の SEIZE_RATE で換金する。
        """
        total = 0
        prices = self.latest_prices()
        for side, symbol, *_ in self.margin_positions(user_id):
            if symbol in prices:
                self._margin_close_locked(
                    user_id, side, None, prices[symbol], f"破産:{self.SIDE_NAME[side]}決済", symbol
                )
        for symbol, _, _ in self.holdings(user_id):
            if symbol in prices:
                self._crypto_sell_locked(user_id, None, prices[symbol], symbol)
        for row in self._conn.execute(
            "SELECT id, principal FROM bonds WHERE guild_id=? AND user_id=? AND status='active' ORDER BY id",
            (self._g(), user_id),
        ).fetchall():
            ok, _ = self._bond_cancel_locked(user_id, row["id"])
            if not ok:  # 解約できない債券は、元本の一部で換金して、差し押さえる
                self._apply(user_id, int(row["principal"] * teiai.SEIZE_RATE), "破産:債券の差し押さえ")
                self._conn.execute("UPDATE bonds SET status='cancelled' WHERE id=?", (row["id"],))
        for row in self._conn.execute(
            "SELECT id FROM options WHERE guild_id=? AND user_id=? AND status='active' ORDER BY id",
            (self._g(), user_id),
        ).fetchall():
            self._option_close_locked(user_id, row["id"], time.time())
        saved = self._deposit_take(user_id, self.deposit_of(user_id))
        if saved:
            self._apply(user_id, saved, "破産:預金の差し押さえ")
        total += self._take_cash(user_id, self._balance(user_id), "破産:現金の差し押さえ")
        return total

    def _seize_shop(self, user_id: int) -> int:
        """ショップの持ち物と装備をすべて消して、換金額（購入額の SEIZE_RATE）を返す（現金は動かさない）。"""
        value = 0
        now = time.time()
        for row in self._conn.execute(
            "SELECT item_id, qty FROM inventory WHERE guild_id=? AND user_id=? AND qty>0", (self._g(), user_id)
        ).fetchall():
            item = shop_mod.BY_ID.get(row["item_id"])
            value += (item.price if item else 0) * row["qty"]
            self._auction_list_locked(row["item_id"], row["qty"], now)  # 差し押さえた品は、帝愛の競売に出る
        prof = self._conn.execute(
            "SELECT daily_boost, insurance FROM profile WHERE guild_id=? AND user_id=?", (self._g(), user_id)
        ).fetchone()
        if prof:  # 使う前の消耗品（有効にしてある分）も、没収する
            value += prof["daily_boost"] * shop_mod.BY_ID[shop_mod.DAILY_DOUBLE].price
            value += prof["insurance"] * shop_mod.BY_ID[shop_mod.INSURANCE].price
            self._auction_list_locked(shop_mod.DAILY_DOUBLE, prof["daily_boost"], now)
            self._auction_list_locked(shop_mod.INSURANCE, prof["insurance"], now)
        value += self._seize_horses(user_id, now)  # 持ち馬は、購入券の値段で換金して、帝愛の競売に出す
        self._conn.execute("DELETE FROM inventory WHERE guild_id=? AND user_id=?", (self._g(), user_id))
        self._conn.execute(
            "UPDATE profile SET title=NULL, card_color=NULL, card_icon=NULL, skin_dice=NULL, skin_table=NULL, "
            "daily_boost=0, insurance=0 WHERE guild_id=? AND user_id=?",
            (self._g(), user_id),
        )
        return int(value * teiai.SEIZE_RATE)

    def escape_left(self, user_id: int) -> int:
        """海外逃亡を、あと何回試せるか。読み取り専用。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT escape_tries FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
        return max(0, teiai.ESCAPE_MAX_TRIES - (row["escape_tries"] if row else 0))

    def escape_attempt(self, user_id: int, now: Optional[float] = None, rng=None) -> tuple:
        """海外逃亡。成功すると、全財産を置いて、帝愛の借金が全額ゼロ（破産の扱い）。失敗すると、罰金が借金に加わり、クールダウン。通算 ESCAPE_MAX_TRIES 回まで。

        (成功か, 理由か結果の辞書) を返す。結果は {"escaped": 成功か, "fine": 罰金, "debt": いまの借金, "bankrupt": 成功時の破産の結果}。
        """
        now = time.time() if now is None else now
        rng = rng or random
        with self._lock, self._conn:
            self._ensure(user_id)
            if self.status(user_id, now) == teiai.BANKRUPT:
                return False, fail("escape_bankrupt_blocked")
            debt = self._debt(user_id)
            if debt < teiai.ESCAPE_MIN_DEBT:
                return False, fail("escape_min_debt", min_debt=teiai.ESCAPE_MIN_DEBT)
            row = self._conn.execute(
                "SELECT escape_at, escape_tries FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
            if row["escape_tries"] >= teiai.ESCAPE_MAX_TRIES:
                return False, fail("escape_max_tries", max_tries=teiai.ESCAPE_MAX_TRIES)
            ready = (row["escape_at"] or 0) + teiai.ESCAPE_COOLDOWN_HOURS * 3600
            if now < ready:
                return False, fail("escape_cooldown", ready=ready)
            self._conn.execute(
                "UPDATE users SET escape_at=?, escape_tries=escape_tries+1 WHERE guild_id=? AND user_id=?",
                (now, self._g(), user_id),
            )
            if rng.random() < teiai.ESCAPE_CHANCE:
                ok, result = self.bankrupt(user_id, escape=True)
                if not ok:
                    return False, result
                return True, {"escaped": True, "fine": 0, "debt": self._debt(user_id), "bankrupt": result}
            fine = max(teiai.ESCAPE_FINE_MIN, int(debt * teiai.ESCAPE_FINE_RATE))
            self._conn.execute(
                "UPDATE users SET debt = debt + ? WHERE guild_id=? AND user_id=?", (fine, self._g(), user_id)
            )
            self._ledger(user_id, 0, self._balance(user_id), f"海外逃亡に失敗（罰金 {fine:,}ペリカが借金に加わる）")
            self._after_debt_change(user_id)
            return True, {"escaped": False, "fine": fine, "debt": self._debt(user_id), "bankrupt": None}

    def bankrupt(self, user_id: int, forced: bool = False, escape: bool = False) -> tuple:
        """自己破産。全財産とショップの全品を差し押さえて換金し、帝愛 → 貸付の順に配当する。(成功か, 理由か結果) を返す。

        貸付の残りは免責（貸し倒れ）。帝愛の借金は、BANKRUPT_FLOOR まで圧縮される。破産者の身分になり、完済して
        BLACKLIST_DAYS 日たつまで、貸付を借りられない。

        他人からの CDS に配当が出るのは、強制破産（forced=True・破産猶予の時間切れ）と、破産猶予に入ってからの自己破産・海外逃亡のとき。
        猶予に入る前の自己破産では、出ない（結託して、わざと破産して配当を抜くのを防ぐ）。猶予に入った人には、新しく CDS を掛けられない。
        escape=True は、海外逃亡の成功。同じ差し押さえをして、帝愛の借金は圧縮ではなく、全額ゼロになる（種銭も貸さない）。
        """
        with self._lock, self._conn:
            self._ensure(user_id)
            if self.status(user_id) == teiai.BANKRUPT:
                return False, fail("bankrupt_already")
            loans = [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM loans WHERE guild_id=? AND borrower_id=? AND status IN ('active','overdue') ORDER BY id",
                    (self._g(), user_id),
                )
            ]
            if self._debt(user_id) <= 0 and not loans:
                return False, fail("bankrupt_no_debt")
            debt_before = self._debt(user_id)
            self.p2p_pull_back(user_id)  # 取引の出品・入札を取り下げて、品・馬・入札のお金を、差し押さえの対象に戻す
            in_grace = self.grace_until(user_id) is not None  # 猶予に入ってからの破産は、客観的なクレジットイベント
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
                at = now  # 延滞中の遅延損害金も、いまの時刻まで数える
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
            compressed = max(0, self._debt(user_id) - (0 if escape else teiai.BANKRUPT_FLOOR))
            self._conn.execute(
                "UPDATE users SET debt=debt-?, bankrupt_at=?, blacklist_until=NULL WHERE guild_id=? AND user_id=?",
                (compressed, now, self._g(), user_id),
            )
            self._after_debt_change(user_id)
            seed = self._seed_if_empty(user_id)
            self._conn.execute("UPDATE users SET grace_until=NULL WHERE guild_id=? AND user_id=?", (self._g(), user_id))
            payable = forced or in_grace
            cds_paid = self._cds_settle_default(user_id, debt_before, now) if payable else 0
            if not payable:
                self._cds_void(user_id)
            return True, {
                "forced": forced,
                "cds_paid": cds_paid,
                "seized": total_pool,
                "shop_value": shop_value,
                "senior_paid": to_senior,
                "junior_paid": junior_paid,
                "forgiven": forgiven,
                "compressed": compressed,
                "debt": self._debt(user_id),
                "seed": seed,
            }
