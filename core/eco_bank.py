"""帝愛銀行（預金・準備率・取り付け騒ぎ）、破産猶予、CDS（破綻保証）（Economy に混ぜ込む部品）。

表の定義は core/schema.py（teiai_bank・teiai_deposits・cds、users.grace_until）。数字と考え方は core/teiai.py。

- 預金: カタギだけが預けられる。預けた額の RESERVE_RATIO だけが手元の現金（cash）になり、残りは地下融資（loaned）に回る。
  地下融資は、時間がたつと、少しずつ現金に戻ってくる
- 利回り: ピンハネ・CDS の掛け金・カジノの負け分の一部が、預金額に応じて預金者へ分配される（ペリカは新しく作らない）
- 利用可能現金 = 手元の現金 - 引当金のロック（破産猶予に入った人への CDS の配当見込み + 地下労働者の借金の一部）。
  これが預金総額に対して少ないほど、取り付け騒ぎの危険水域。0 以下なら、引き出せない（預金封鎖）
- 破産猶予: 借金が GRACE_DEBT 以上になると、GRACE_HOURS のカウントダウンに入る。戻せば回避、時間切れで強制破産
- CDS: 掛け金を払うと、対象が「強制破産」したときに配当（掛け金 × オッズ）。自分への CDS は、借金を肩代わりする保険。
  自分の資産を差し押さえても、なお足りない分にだけ効く（先に自分の資産を失う）。配当の見込みの合計は CDS_SELF_CAP まで。
  他人への CDS は、猶予の時間切れの強制破産のほか、猶予に入ってからの自己破産でも配当が出る（猶予に入る前の自己破産では出ない）。
  配当の合計は、対象の借金の CDS_PAYOUT_CAP_RATE までに抑える（結託の対策）
"""

import time
from typing import Optional

from core import teiai
from core.eco_base import EcoBase


class BankMixin(EcoBase):
    """帝愛銀行・破産猶予・CDS。"""

    # ------------------------------------------------------------------ #
    # 銀行の手元
    # ------------------------------------------------------------------ #
    def _bank_sync(self, now: float) -> dict:
        """銀行の手元を取り出す（なければ作る）。地下融資の回収を、いまの時刻まで反映する。"""
        row = self._conn.execute("SELECT cash, loaned, ts FROM teiai_bank WHERE guild_id=?", (self._g(),)).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO teiai_bank (guild_id, cash, loaned, ts) VALUES (?, 0, 0, ?)", (self._g(), now)
            )
            return {"cash": 0.0, "loaned": 0.0}
        cash, loaned, hours = row["cash"], row["loaned"], (now - row["ts"]) / 3600
        if hours > 0:
            if loaned > 0:
                moved = loaned * (1 - (1 - teiai.LOAN_RECOVERY_PER_HOUR) ** hours)
                cash, loaned = cash + moved, loaned - moved
            self._conn.execute(
                "UPDATE teiai_bank SET cash=?, loaned=?, ts=? WHERE guild_id=?", (cash, loaned, now, self._g())
            )
        return {"cash": cash, "loaned": loaned}

    def _bank_set(self, cash: float, loaned: float) -> None:
        self._conn.execute("UPDATE teiai_bank SET cash=?, loaned=? WHERE guild_id=?", (cash, loaned, self._g()))

    def _deposits(self) -> list:
        return [
            (r["user_id"], r["amount"])
            for r in self._conn.execute(
                "SELECT user_id, amount FROM teiai_deposits WHERE guild_id=? AND amount>0 ORDER BY user_id",
                (self._g(),),
            )
        ]

    def deposit_of(self, user_id: int) -> int:
        with self._lock:
            r = self._conn.execute(
                "SELECT amount FROM teiai_deposits WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
            return r["amount"] if r else 0

    def bank_values(self) -> dict:
        """{ユーザーID: 帝愛への預金額}（このサーバー。ランキングの総資産に含める）。"""
        with self._lock:
            return dict(self._deposits())

    def _bank_locks(self, now: float) -> float:
        """引当金のロック額: 破産猶予中の人への CDS の配当見込み（対象の借金の上限つき）+ 借金のある人の借金の LOCK_DEBT_RATE。"""
        locks = 0.0
        for r in self._conn.execute(
            "SELECT user_id, debt, grace_until FROM users WHERE guild_id=? AND debt>0", (self._g(),)
        ).fetchall():
            locks += teiai.LOCK_DEBT_RATE * r["debt"]
            if r["grace_until"] is not None:
                claims = self._conn.execute(
                    "SELECT COALESCE(SUM(stake * odds), 0) FROM cds WHERE guild_id=? AND target_id=? AND buyer_id!=target_id "
                    "AND status='active'",
                    (self._g(), r["user_id"]),
                ).fetchone()[0]
                locks += min(claims, teiai.CDS_PAYOUT_CAP_RATE * r["debt"])
        return locks

    def bank_status(self, now: Optional[float] = None) -> dict:
        """銀行の状態（表示用）。available は利用可能現金、ratio は available ÷ 預金総額（預金がなければ None）。"""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            state = self._bank_sync(now)
            total = sum(a for _, a in self._deposits())
            locks = self._bank_locks(now)
            available = state["cash"] - locks
            ratio = available / total if total > 0 else None
            if available <= 0 and total > 0:
                level = "frozen"
            elif ratio is None or ratio >= teiai.RUN_WARN_RATIO:
                level = "safe"
            elif ratio >= teiai.RUN_DANGER_RATIO:
                level = "warn"
            else:
                level = "danger"
            return {
                "cash": state["cash"],
                "loaned": state["loaned"],
                "deposits": total,
                "locks": locks,
                "available": available,
                "ratio": ratio,
                "level": level,
            }

    def _house_income(self, amount: int) -> None:
        """帝愛の胴元の口座に、ピンハネの amount を入れる（呼び出し側のトランザクションの中で使う）。"""
        if amount <= 0:
            return
        self._conn.execute(
            "INSERT INTO teiai_house (guild_id, balance, skimmed) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET balance=balance+excluded.balance, skimmed=skimmed+excluded.skimmed",
            (self._g(), amount, amount),
        )

    def house_account(self) -> dict:
        """帝愛の胴元の口座 {balance: いまの残高, skimmed: これまでのピンハネの合計}。読み取りだけ（なければ 0）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT balance, skimmed FROM teiai_house WHERE guild_id=?", (self._g(),)
            ).fetchone()
        return {"balance": row["balance"], "skimmed": row["skimmed"]} if row else {"balance": 0, "skimmed": 0}

    def _bank_income(
        self, amount: int, share: float, now: Optional[float] = None, needs_depositors: bool = False
    ) -> None:
        """銀行に amount が入る。そのうち share の割合を、預金額に応じて預金者へ分配する（残りは手元の現金）。

        預金者がいなければ全部、手元に残る。ただし needs_depositors=True（ピンハネ・カジノの負け分）のときは、
        預金者がいなければ何も起こさない（これまで通り、お金が消えるだけ。誰にも預けられていない銀行に、現金を積み上げない）。
        """
        if amount <= 0:
            return
        now = time.time() if now is None else now
        deposits = self._deposits()
        total = sum(a for _, a in deposits)
        if needs_depositors and total <= 0:
            return
        state = self._bank_sync(now)
        self._bank_set(state["cash"] + amount, state["loaned"])
        distribute = int(amount * share)
        if total <= 0 or distribute <= 0:
            return
        for uid, amt in deposits:
            gain = distribute * amt // total
            if gain:
                self._conn.execute(
                    "UPDATE teiai_deposits SET amount=amount+? WHERE guild_id=? AND user_id=?", (gain, self._g(), uid)
                )

    def _bank_pay(self, amount: int, now: Optional[float] = None) -> int:
        """銀行が amount を払う。手元の現金から払い、足りない分は、預金者の預金を一律に減らして充てる（AIG ショック）。払えた額を返す。

        払える限度は、手元の現金と預金の合計まで（それを超える分は、払えない）。
        """
        if amount <= 0:
            return 0
        now = time.time() if now is None else now
        state = self._bank_sync(now)
        deposits = self._deposits()
        total = sum(a for _, a in deposits)
        paid = int(min(amount, state["cash"] + total))
        if paid <= state["cash"]:
            self._bank_set(state["cash"] - paid, state["loaned"])
            return paid
        shortfall = paid - state["cash"]
        self._bank_set(0.0, state["loaned"])
        factor = max(0.0, 1 - shortfall / total)
        for uid, amt in deposits:
            self._conn.execute(
                "UPDATE teiai_deposits SET amount=? WHERE guild_id=? AND user_id=?", (int(amt * factor), self._g(), uid)
            )
        return paid

    # ------------------------------------------------------------------ #
    # 預金
    # ------------------------------------------------------------------ #
    def bank_deposit(self, user_id: int, amount: int, now: Optional[float] = None) -> tuple:
        """帝愛に預ける（カタギだけ）。(成功か, 理由か預金残高) を返す。"""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._ensure(user_id)
            if self.status(user_id, now) != teiai.KATAGI:
                return False, "帝愛に預けられるのは、🟢 カタギだけです（借金か延滞がある人、破産者は預けられません）。"
            if amount < teiai.BANK_MIN_DEPOSIT:
                return False, f"預金は {teiai.BANK_MIN_DEPOSIT:,}ペリカ 以上からです。"
            if self._balance(user_id) < amount:
                return False, f"残高が足りません（いまの残高: {self._balance(user_id):,}ペリカ）。"
            state = self._bank_sync(now)
            self._apply(user_id, -amount, "帝愛への預金")
            self._conn.execute(
                "INSERT INTO teiai_deposits (guild_id, user_id, amount) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET amount=amount+excluded.amount",
                (self._g(), user_id, amount),
            )
            reserve = amount * teiai.RESERVE_RATIO
            self._bank_set(state["cash"] + reserve, state["loaned"] + amount - reserve)
            return True, self.deposit_of(user_id)

    def bank_withdraw(self, user_id: int, amount: Optional[int] = None, now: Optional[float] = None) -> tuple:
        """預金を引き出す（amount が None なら全額）。利用可能現金の範囲まで。(成功か, 理由か引き出した額) を返す。"""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._ensure(user_id)
            have = self.deposit_of(user_id)
            if have <= 0:
                return False, "預金がありません。"
            want = have if amount is None else amount
            if want <= 0 or want > have:
                return False, f"引き出せるのは 1〜{have:,}ペリカ です（いまの預金）。"
            state = self._bank_sync(now)
            available = int(state["cash"] - self._bank_locks(now))
            if available <= 0:
                return False, "🚨 帝愛は支払停止中です（預金封鎖）。手元の現金が、引当金のロックで尽きています。"
            if want > available:
                return (
                    False,
                    f"いま引き出せるのは {available:,}ペリカ までです（帝愛の利用可能現金）。残りは、現金が戻ってからです。",
                )
            self._conn.execute(
                "UPDATE teiai_deposits SET amount=amount-? WHERE guild_id=? AND user_id=?", (want, self._g(), user_id)
            )
            self._bank_set(state["cash"] - want, state["loaned"])
            self._apply(user_id, want, "帝愛からの引き出し")
            return True, want

    def _deposit_take(self, user_id: int, need: int, now: Optional[float] = None) -> int:
        """強制執行・破産のとき、預金から need まで差し押さえる（引き出しの制限は受けない）。取れた額を返す（手元には入れない）。"""
        have = self.deposit_of(user_id)
        take = min(have, need)
        if take <= 0:
            return 0
        now = time.time() if now is None else now
        state = self._bank_sync(now)
        self._conn.execute(
            "UPDATE teiai_deposits SET amount=amount-? WHERE guild_id=? AND user_id=?", (take, self._g(), user_id)
        )
        self._bank_set(max(0.0, state["cash"] - take), state["loaned"])
        return take

    # ------------------------------------------------------------------ #
    # 破産猶予
    # ------------------------------------------------------------------ #
    def _grace_update(self, user_id: int, now: Optional[float] = None) -> None:
        """借金の額に合わせて、破産猶予に入れる／外す。入るときに一言を残す。"""
        now = time.time() if now is None else now
        row = self._conn.execute(
            "SELECT debt, grace_until FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)
        ).fetchone()
        if row is None:
            return
        if row["debt"] >= teiai.GRACE_DEBT and row["grace_until"] is None:
            until = now + teiai.GRACE_HOURS * 3600
            self._conn.execute(
                "UPDATE users SET grace_until=? WHERE guild_id=? AND user_id=?", (until, self._g(), user_id)
            )
            self.p2p_pull_back(user_id, now)  # 取引の出品・入札は、取り下げる（資産隠しの防止）
            self._note(
                user_id,
                f"⚠️ 借金が {teiai.GRACE_DEBT:,}ペリカ に達し、**破産猶予**に入りました。<t:{int(until)}:R> までに借金を {teiai.GRACE_DEBT:,}ペリカ 未満へ戻せなければ、強制破産です。",
            )
        elif row["debt"] < teiai.GRACE_DEBT and row["grace_until"] is not None:
            self._conn.execute("UPDATE users SET grace_until=NULL WHERE guild_id=? AND user_id=?", (self._g(), user_id))
            self._note(user_id, "😮‍💨 借金が減り、破産猶予から抜けました。")

    def grace_until(self, user_id: int) -> Optional[float]:
        with self._lock:
            r = self._conn.execute(
                "SELECT grace_until FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
            return r["grace_until"] if r else None

    def grace_list(self) -> list:
        """破産猶予中の人 [(ユーザーID, 借金, 期限)]（期限の早い順）。"""
        with self._lock:
            return [
                (r["user_id"], r["debt"], r["grace_until"])
                for r in self._conn.execute(
                    "SELECT user_id, debt, grace_until FROM users WHERE guild_id=? AND grace_until IS NOT NULL "
                    "ORDER BY grace_until",
                    (self._g(),),
                )
            ]

    def grace_expire(self, now: float) -> list:
        """猶予の時間切れになった人を、強制破産させる。CDS の失効もここで済ます。[(ユーザーID, 破産の結果)] を返す。"""
        done = []
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE cds SET status='expired' WHERE guild_id=? AND status='active' AND expires<=?", (self._g(), now)
            )
            for r in self._conn.execute(
                "SELECT user_id FROM users WHERE guild_id=? AND grace_until IS NOT NULL AND grace_until<=? ORDER BY grace_until",
                (self._g(), now),
            ).fetchall():
                if self._debt(r["user_id"]) < teiai.GRACE_DEBT:  # 取りこぼしの保険（外し忘れ）
                    self._grace_update(r["user_id"], now)
                    continue
                ok, result = self.bankrupt(r["user_id"], forced=True)
                if ok:
                    done.append((r["user_id"], result))
        return done

    # ------------------------------------------------------------------ #
    # CDS（破綻保証）
    # ------------------------------------------------------------------ #
    def cds_quote(self, target_id: int, now: Optional[float] = None) -> dict:
        """対象の、いまの CDS のオッズと状況。読み取り専用。"""
        now = time.time() if now is None else now
        status = self.status(target_id, now)
        debt = self._debt_read(target_id)
        ratio = self.margin_status(target_id)["ratio"]
        return {
            "odds": teiai.cds_odds(status, debt, ratio),
            "status": status,
            "debt": debt,
            "grace": self.grace_until(target_id) is not None,
        }

    def _debt_read(self, user_id: int) -> int:
        return self.debt(user_id)

    def cds_buy(self, buyer_id: int, target_id: int, stake: int, now: Optional[float] = None) -> tuple:
        """CDS を買う（自分にも、他人にも）。(成功か, 理由かオッズ) を返す。"""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._ensure(buyer_id)
            if stake < teiai.CDS_MIN_STAKE:
                return False, f"掛け金は {teiai.CDS_MIN_STAKE:,}ペリカ 以上からです。"
            if (
                self._conn.execute(
                    "SELECT 1 FROM users WHERE guild_id=? AND user_id=?", (self._g(), target_id)
                ).fetchone()
                is None
            ):
                return False, "その人は、まだこのサーバーで遊んでいません。"
            if self._balance(buyer_id) < stake:
                return False, f"残高が足りません（いまの残高: {self._balance(buyer_id):,}ペリカ）。"
            if self.grace_until(target_id) is not None:
                return False, "その人は、すでに破産猶予に入っているので、掛けられません。"
            if self.status(target_id, now) == teiai.BANKRUPT:
                return False, "その人は、破産者なので、掛けられません。"
            odds = self.cds_quote(target_id, now)["odds"]
            if buyer_id == target_id:  # 自分への保証は、積める額に上限がある
                have = self._conn.execute(
                    "SELECT COALESCE(SUM(stake * odds), 0) FROM cds WHERE guild_id=? AND buyer_id=? AND target_id=? "
                    "AND status='active' AND expires>?",
                    (self._g(), buyer_id, buyer_id, now),
                ).fetchone()[0]
                if have + stake * odds > teiai.CDS_SELF_CAP:
                    room = max(0, int((teiai.CDS_SELF_CAP - have) / odds))
                    return (
                        False,
                        f"自分への CDS は、配当の見込みの合計が {teiai.CDS_SELF_CAP:,}ペリカ までです"
                        f"（いま {int(have):,}ペリカ分。あと掛けられるのは {room:,}ペリカ）。",
                    )
            self._apply(buyer_id, -stake, f"CDS の掛け金（対象 {target_id}・{odds}倍）")
            self._bank_income(stake, teiai.PREMIUM_YIELD_SHARE, now)
            self._conn.execute(
                "INSERT INTO cds (guild_id, buyer_id, target_id, stake, odds, ts, expires) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self._g(), buyer_id, target_id, stake, odds, now, now + teiai.CDS_TERM_DAYS * 86400),
            )
            return True, odds

    def cds_list(self, user_id: int) -> dict:
        """自分が買っている CDS（有効なもの）と、自分に掛けられている件数。"""
        with self._lock:
            mine = [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM cds WHERE guild_id=? AND buyer_id=? AND status='active' ORDER BY id",
                    (self._g(), user_id),
                )
            ]
            on_me = self._conn.execute(
                "SELECT COUNT(*) FROM cds WHERE guild_id=? AND target_id=? AND buyer_id!=target_id AND status='active'",
                (self._g(), user_id),
            ).fetchone()[0]
            return {"bought": mine, "on_me": on_me}

    def cds_total_claims(self) -> int:
        """このサーバーの、有効な CDS の配当見込みの合計（表示用）。"""
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COALESCE(SUM(stake * odds), 0) FROM cds WHERE guild_id=? AND status='active'", (self._g(),)
                ).fetchone()[0]
            )

    def _cds_cover_self(self, user_id: int, shortfall: int, now: Optional[float] = None) -> int:
        """払えなかった shortfall を、自分への CDS が肩代わりする（古い順に、1件ずつ使い切り）。肩代わりした額を返す。現金は渡らない。"""
        now = time.time() if now is None else now
        covered = 0
        for r in self._conn.execute(
            "SELECT id, stake, odds FROM cds WHERE guild_id=? AND buyer_id=? AND target_id=? AND status='active' "
            "AND expires>? ORDER BY id",
            (self._g(), user_id, user_id, now),
        ).fetchall():
            if covered >= shortfall:
                break
            pay = self._bank_pay(min(shortfall - covered, int(r["stake"] * r["odds"])), now)
            self._conn.execute("UPDATE cds SET status='paid', paid=? WHERE id=?", (pay, r["id"]))
            covered += pay
            self._note(user_id, f"🛡️ 自分への CDS が、借金 {pay:,}ペリカ を肩代わりしました（帝愛が払います）。")
        return covered

    def _cds_settle_default(self, target_id: int, debt_before: int, now: Optional[float] = None) -> int:
        """対象が強制破産したとき、他人からの CDS に配当を払う。合計は、対象の借金の CDS_PAYOUT_CAP_RATE まで（超える分は、一律に減らす）。払った合計を返す。"""
        now = time.time() if now is None else now
        rows = self._conn.execute(
            "SELECT id, buyer_id, stake, odds FROM cds WHERE guild_id=? AND target_id=? AND buyer_id!=target_id "
            "AND status='active' AND expires>? ORDER BY id",
            (self._g(), target_id, now),
        ).fetchall()
        claims = [(r["id"], r["buyer_id"], int(r["stake"] * r["odds"])) for r in rows]
        total_claim = sum(c for _, _, c in claims)
        cap = int(teiai.CDS_PAYOUT_CAP_RATE * debt_before)
        scale = min(1.0, cap / total_claim) if total_claim > 0 else 0.0
        total_paid = 0
        for cid, buyer, claim in claims:
            pay = self._bank_pay(int(claim * scale), now)
            if pay > 0:
                self._ensure(buyer)
                self._apply(buyer, pay, f"CDS の配当（対象 {target_id} が強制破産）", income=pay)
                self._note(
                    buyer, f"💀 CDS が的中しました。<@{target_id}> の強制破産で、{pay:,}ペリカ を受け取りました。"
                )
            self._conn.execute("UPDATE cds SET status='paid', paid=? WHERE id=?", (pay, cid))
            total_paid += pay
        self._cds_void(target_id)
        return total_paid

    def _cds_void(self, target_id: int) -> None:
        """対象が破産したあと、まだ有効な CDS（配当されなかったもの）を無効にする。"""
        self._conn.execute(
            "UPDATE cds SET status='void' WHERE guild_id=? AND target_id=? AND status='active'", (self._g(), target_id)
        )
