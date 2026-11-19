"""帝愛銀行（帝愛信託・準備率・取り付け騒ぎ）、破産猶予、CDS（破綻保証）。

表の定義は infra/db/schema/tables.py（teiai_bank・teiai_deposits・cds、users.grace_until）。数字と考え方は domain/teiai.py。
"""

import math
from typing import Any, Optional

from domain import market as market_mod
from domain import rates, teiai, thermostat
from domain.errors import fail
from domain.ports.bridges import (
    IBankruptExecutor,
    IExcessLossSink,
    IGraceEnterHook,
    IMarginRatioReader,
    INoticeSink,
    IStatusReader,
    NoOpBankruptExecutor,
    NoOpExcessLossSink,
    NoOpGraceEnterHook,
    NoOpMarginRatioReader,
    NoOpNoticeSink,
    NoOpStatusReader,
)
from domain.ports.contracts import BankStatus, CdsHoldings, CdsQuote, HouseAccount
from domain.ports.repos import IBankRepo
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo


class BankRepo(ScopedRepo, IBankRepo):
    """帝愛銀行・破産猶予・CDS。"""

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        notices: Optional[INoticeSink] = None,
        status_reader: Optional[IStatusReader] = None,
        margin_ratio_reader: Optional[IMarginRatioReader] = None,
        grace_enter_hook: Optional[IGraceEnterHook] = None,
        bankrupt_executor: Optional[IBankruptExecutor] = None,
        excess_sink: Optional[IExcessLossSink] = None,
        guild_id: int = 0,
        strict: bool = False,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._notices: INoticeSink = notices or NoOpNoticeSink()
        self._status_reader: IStatusReader = status_reader or NoOpStatusReader()
        self._margin_ratio_reader: IMarginRatioReader = margin_ratio_reader or NoOpMarginRatioReader()
        self._grace_enter_hook: IGraceEnterHook = grace_enter_hook or NoOpGraceEnterHook()
        self._bankrupt_executor: IBankruptExecutor = bankrupt_executor or NoOpBankruptExecutor()
        self._excess_sink: IExcessLossSink = excess_sink or NoOpExcessLossSink()

    # ------------------------------------------------------------------ #
    # 銀行の手元・預金・課税（IBankCashFlow / IHouseAccount / IGainsTaxCollector / IValuationSource）
    # ------------------------------------------------------------------ #
    def bank_sync_for(self, guild_id: int, now: float) -> dict[str, float]:
        """銀行の手元を取り出す（なければ作る）。地下融資の回収を、いまの時刻まで反映する。"""
        row = self._conn.execute("SELECT cash, loaned, ts FROM teiai_bank WHERE guild_id=?", (guild_id,)).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO teiai_bank (guild_id, cash, loaned, ts) VALUES (?, 0, 0, ?)", (guild_id, now)
            )
            return {"cash": 0.0, "loaned": 0.0}
        cash, loaned, hours = row["cash"], row["loaned"], (now - row["ts"]) / 3600
        if hours > 0:
            if loaned > 0:
                moved = loaned * (1 - (1 - teiai.LOAN_RECOVERY_PER_HOUR) ** hours)
                cash, loaned = cash + moved, loaned - moved
            self._conn.execute(
                "UPDATE teiai_bank SET cash=?, loaned=?, ts=? WHERE guild_id=?", (cash, loaned, now, guild_id)
            )
        return {"cash": cash, "loaned": loaned}

    def bank_set_for(self, guild_id: int, cash: float, loaned: float) -> None:
        self._conn.execute("UPDATE teiai_bank SET cash=?, loaned=? WHERE guild_id=?", (cash, loaned, guild_id))

    # ------------------------------------------------------------------ #
    # 最前線サーモスタット（数字と考え方は domain/thermostat.py）
    # ------------------------------------------------------------------ #
    def _thermo_state_for(self, guild_id: int, now: float) -> thermostat.State:
        """いまの時刻まで冷ました状態（銀行の行がある前提。bank_sync_for のあとで呼ぶ）。"""
        row = self._conn.execute(
            "SELECT thermo_load, thermo_capacity, thermo_ts FROM teiai_bank WHERE guild_id=?",
            (guild_id,),
        ).fetchone()
        state = thermostat.State(load=row["thermo_load"], capacity=max(row["thermo_capacity"], thermostat.E_MIN))
        return thermostat.decay(state, (now - row["thermo_ts"]) / 86400)

    def _thermo_save_for(
        self, guild_id: int, state: thermostat.State, now: float, burned: float = 0.0, bailed: float = 0.0
    ) -> None:
        # bailout_debt 列は #32 の見直しで使わなくなった（列だけ残している）
        self._conn.execute(
            "UPDATE teiai_bank SET thermo_load=?, thermo_capacity=?, thermo_ts=?, "
            "thermo_burned=thermo_burned+?, thermo_bailed=thermo_bailed+? WHERE guild_id=?",
            (state.load, state.capacity, now, burned, bailed, guild_id),
        )

    def _thermo_inflow_for(self, guild_id: int, amount: int, now: float) -> float:
        """流入を通し、銀行に入れてよい額を返す（観測だけの間は、全額）。"""
        state, net, burn = thermostat.absorb_inflow(self._thermo_state_for(guild_id, now), amount)
        self._thermo_save_for(guild_id, state, now, burned=burn)
        return net if thermostat.ENFORCE else float(amount)

    def _thermo_shock_for(self, guild_id: int, amount: int, now: float) -> float:
        """流出（損失・通常の支払い）を通し、熱浴から銀行へ注入する額を返す（観測だけの間は、0）。"""
        state, _effective, bailout = thermostat.absorb_shock(self._thermo_state_for(guild_id, now), amount)
        self._thermo_save_for(guild_id, state, now, bailed=bailout)
        return bailout if thermostat.ENFORCE else 0.0

    def bank_thermo_batch_for(self, guild_id: int, net: float, now: float) -> None:
        """流入と流出が混ざるバッチの純額 net（流入がプラス）を、サーモスタットに1回だけ通す。

        流入と流出は通す順番で結果が変わるため、バッチの中の個々の入出金は thermo=False で動かし、
        ここで純額だけを通す。銀行の現金は、burn の分だけ減り、bailout の分だけ増える（観測だけの間は動かさない）。
        純額が流出なら支払いの前に、流入なら入金のあとに呼ぶ（現金が一時的にマイナスにならないように）。
        """
        if net == 0:
            return
        state = self.bank_sync_for(guild_id, now)
        thermo, burn, bailout = thermostat.absorb_net(self._thermo_state_for(guild_id, now), net)
        self._thermo_save_for(guild_id, thermo, now, burned=burn, bailed=bailout)
        if thermostat.ENFORCE:
            self.bank_set_for(guild_id, max(state["cash"] - burn, 0.0) + bailout, state["loaned"])

    def _bonds_outstanding_for(self, guild_id: int) -> int:
        """帝愛社債（保有中の債券）の元本残高。銀行の負債として準備率に数える（#35）。"""
        return self._conn.execute(
            "SELECT COALESCE(SUM(principal), 0) FROM bonds WHERE guild_id=? AND status='active'", (guild_id,)
        ).fetchone()[0]

    def _deposits_for(self, guild_id: int) -> list[tuple[int, int]]:
        return [
            (r["user_id"], r["amount"])
            for r in self._conn.execute(
                "SELECT user_id, amount FROM teiai_deposits WHERE guild_id=? AND amount>0 ORDER BY user_id",
                (guild_id,),
            )
        ]

    def deposit_of_for(self, guild_id: int, user_id: int) -> int:
        with self._lock:
            r = self._conn.execute(
                "SELECT amount FROM teiai_deposits WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            ).fetchone()
            return r["amount"] if r else 0

    def deposit_of(self, user_id: int) -> int:
        return self.deposit_of_for(self._g(), user_id)

    def bank_values(self) -> dict[int, int]:
        """{ユーザーID: 帝愛への預金額}（このサーバー。ランキングの総資産に含める）。"""
        with self._lock:
            return dict(self._deposits_for(self._g()))

    def collect_values(self, guild_id: int, totals: dict[int, int]) -> None:
        with self._lock:
            for uid, amt in self._deposits_for(guild_id):
                totals[uid] = totals.get(uid, 0) + amt

    def bank_locks_for(self, guild_id: int, now: float) -> float:
        """引当金のロック額: 破産猶予中の人への CDS の配当見込み（対象の借金の上限つき）+ 借金のある人の借金の LOCK_DEBT_RATE。"""
        locks = 0.0
        for r in self._conn.execute(
            "SELECT user_id, debt, grace_until FROM users WHERE guild_id=? AND debt>0", (guild_id,)
        ).fetchall():
            locks += teiai.LOCK_DEBT_RATE * r["debt"]
            if r["grace_until"] is not None:
                claims = self._conn.execute(
                    "SELECT COALESCE(SUM(stake * odds), 0) FROM cds WHERE guild_id=? AND target_id=? AND buyer_id!=target_id "
                    "AND status='active'",
                    (guild_id, r["user_id"]),
                ).fetchone()[0]
                locks += min(claims, teiai.CDS_PAYOUT_CAP_RATE * r["debt"])
        return locks

    def bank_status(self, now: float) -> BankStatus:
        """銀行の状態（表示用）。available は利用可能現金、ratio は available ÷ (信託総額 + 社債の元本残高)（どちらもなければ None）。"""
        with self._db.tx():
            gid = self._g()
            state = self.bank_sync_for(gid, now)
            total = sum(a for _, a in self._deposits_for(gid))
            bonds = self._bonds_outstanding_for(gid)
            locks = self.bank_locks_for(gid, now)
            available = state["cash"] - locks
            liabilities = total + bonds  # 銀行が返す約束をしている額（信託と、帝愛社債の元本）
            ratio = available / liabilities if liabilities > 0 else None
            if available <= 0 and liabilities > 0:
                level = "frozen"
            elif ratio is None or ratio >= teiai.RUN_WARN_RATIO:
                level = "safe"
            elif ratio >= teiai.RUN_DANGER_RATIO:
                level = "warn"
            else:
                level = "danger"
            thermo = self._thermo_state_for(gid, now)
            totals = self._conn.execute(
                "SELECT thermo_burned, thermo_bailed, trust_paid, excess_unpaid FROM teiai_bank WHERE guild_id=?",
                (gid,),
            ).fetchone()
            return {
                "cash": state["cash"],
                "loaned": state["loaned"],
                "deposits": total,
                "bonds": bonds,
                "locks": locks,
                "available": available,
                "ratio": ratio,
                "level": level,
                "thermostat": {
                    "enforce": thermostat.ENFORCE,
                    "load": thermo.load,
                    "capacity": thermo.capacity,
                    "room": max(thermo.capacity - thermostat.COUPLING * thermo.load, 0.0) / thermostat.COUPLING,
                    "burned": totals["thermo_burned"],
                    "bailed": totals["thermo_bailed"],
                },
                "trust_rate": self.trust_rate_per_day_for(gid, now),
                "risk_free": self.risk_free_per_day_for(gid, now),
                "trust_paid": totals["trust_paid"],
                "excess_unpaid": totals["excess_unpaid"],
            }

    def house_income_for(self, guild_id: int, amount: int) -> None:
        """帝愛の胴元の口座に、ピンハネの amount を入れる（呼び出し側のトランザクションの中で使う）。"""
        if amount <= 0:
            return
        self._conn.execute(
            "INSERT INTO teiai_house (guild_id, balance, skimmed) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET balance=balance+excluded.balance, skimmed=skimmed+excluded.skimmed",
            (guild_id, amount, amount),
        )

    def house_balance_for(self, guild_id: int) -> int:
        row = self._conn.execute("SELECT balance FROM teiai_house WHERE guild_id=?", (guild_id,)).fetchone()
        return row["balance"] if row else 0

    def house_receive_for(self, guild_id: int, amount: int) -> None:
        """胴元の口座に、ピンハネ以外の amount を入れる（ピンハネの合計には数えない）。"""
        if amount <= 0:
            return
        self._conn.execute(
            "INSERT INTO teiai_house (guild_id, balance, skimmed) VALUES (?, ?, 0) "
            "ON CONFLICT(guild_id) DO UPDATE SET balance=balance+excluded.balance",
            (guild_id, amount),
        )

    def house_pay_for(self, guild_id: int, amount: int) -> int:
        """胴元の口座から amount を払う。残高を超えては払わない。払えた額を返す。"""
        paid = max(0, min(amount, self.house_balance_for(guild_id)))
        if paid:
            self._conn.execute("UPDATE teiai_house SET balance=balance-? WHERE guild_id=?", (paid, guild_id))
        return paid

    def house_grant_left_for(self, guild_id: int, day: str, cap: int) -> int:
        """会社の買取枠（胴元の口座が足りないとき、帝愛が新しく出す分）の、day の残り。1日の上限は cap。"""
        row = self._conn.execute(
            "SELECT grant_day, grant_used FROM teiai_house WHERE guild_id=?", (guild_id,)
        ).fetchone()
        used = row["grant_used"] if row and row["grant_day"] == day else 0
        return max(0, cap - used)

    def house_grant_use_for(self, guild_id: int, day: str, amount: int) -> None:
        """買取枠から amount を出したと記録する（日が変わっていたら、数え直す）。"""
        if amount <= 0:
            return
        self._conn.execute(
            "INSERT INTO teiai_house (guild_id, balance, skimmed, grant_day, grant_used) VALUES (?, 0, 0, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            "grant_used=CASE WHEN grant_day=excluded.grant_day THEN grant_used ELSE 0 END + excluded.grant_used, "
            "grant_day=excluded.grant_day",
            (guild_id, day, amount),
        )

    def house_account(self) -> HouseAccount:
        """帝愛の胴元の口座 {balance: いまの残高, skimmed: これまでのピンハネの合計}。読み取りだけ（なければ 0）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT balance, skimmed FROM teiai_house WHERE guild_id=?", (self._g(),)
            ).fetchone()
        return {"balance": row["balance"], "skimmed": row["skimmed"]} if row else {"balance": 0, "skimmed": 0}

    def bank_income_for(
        self,
        guild_id: int,
        amount: int,
        now: float,
        needs_depositors: bool = False,
        *,
        thermo: bool = True,
    ) -> None:
        """銀行に amount が入る（手元の現金へ）。信託への分配はしない（信託は、決まった利息を受け取る。#33）。

        needs_depositors=True のときは、信託の残高がなければ何も入らない（カジノの負け分など、もとからの約束）。
        thermo=False は、バッチの純額を bank_thermo_batch_for で別に通すときだけ使う。
        """
        if amount <= 0:
            return
        if needs_depositors and sum(a for _, a in self._deposits_for(guild_id)) <= 0:
            return
        state = self.bank_sync_for(guild_id, now)
        received = self._thermo_inflow_for(guild_id, amount, now) if thermo else float(amount)
        self.bank_set_for(guild_id, state["cash"] + received, state["loaned"])

    def bank_pay_for(self, guild_id: int, amount: int, now: float, *, thermo: bool = True) -> int:
        """銀行が amount を払う。払えた額を返す。

        払う順番（ウォーターフォール。#33）:
        1. 手元の現金
        2. 帝愛信託（第一損失）: 足りない分を、信託の残高に応じて按分して減らす。信託はマイナスにはならない
        3. 信託でも足りない超過損失だけを熱浴へ: サーモスタットで「熱浴の肩代わり」と「共済の負担」に分ける

        サーモスタットに流出として通すのは「現金で払った分 + 超過損失」だけ（信託の取り崩しで、負側の器を使わない）。
        損失の支払いも、プレミアムなどの通常の支払いも、同じ流出として扱う。
        thermo=False は、バッチの純額を bank_thermo_batch_for で別に通すときだけ使う。
        """
        return self._outflow_for(guild_id, amount, now, use_trust=True, thermo=thermo)

    def _outflow_for(self, guild_id: int, amount: int, now: float, *, use_trust: bool, thermo: bool) -> int:
        """bank_pay_for の本体。use_trust=False は、信託への利息のように、信託から取ると自分で自分に払うことになる流出。"""
        if amount <= 0:
            return 0
        state = self.bank_sync_for(guild_id, now)
        cash_pay = min(float(amount), max(state["cash"], 0.0))
        cash = state["cash"] - cash_pay
        shortfall = amount - cash_pay
        trust_take = self._trust_haircut_for(guild_id, shortfall) if use_trust and shortfall > 0 else 0
        excess = max(0.0, shortfall - trust_take)  # 信託でも受け止めきれない超過損失 Δraw
        bailout = 0.0
        if thermo:
            bailout = self._thermo_shock_for(guild_id, int(round(cash_pay + excess)), now)
        cover = min(excess, bailout)  # 熱浴の肩代わりは、まず超過損失の穴埋めに使う
        cash += bailout - cover  # 余った肩代わりは、手元へ
        charged = 0
        left = int(excess - cover)
        if left > 0:  # 残りは共済の負担（#34 まではつながっておらず、払えない）
            charged = min(left, max(0, self._excess_sink.charge_excess_for(guild_id, left, now)))
            if left > charged:
                self._conn.execute(
                    "UPDATE teiai_bank SET excess_unpaid=excess_unpaid+? WHERE guild_id=?",
                    (left - charged, guild_id),
                )
        self.bank_set_for(guild_id, cash, state["loaned"])
        return min(amount, int(round(cash_pay + trust_take + cover + charged)))

    def _trust_haircut_for(self, guild_id: int, need: float) -> int:
        """帝愛信託の残高を、按分して need まで減らす（第一損失）。減らした額を返す（信託の総額が上限）。"""
        deposits = self._deposits_for(guild_id)
        total = sum(a for _, a in deposits)
        if total <= 0:
            return 0
        take = min(math.ceil(need - 1e-9), total)  # 払う側に端数の不足を出さないよう、切り上げて取る
        factor = max(0.0, 1 - take / total)
        for uid, amt in deposits:
            self._conn.execute(
                "UPDATE teiai_deposits SET amount=? WHERE guild_id=? AND user_id=?",
                (int(amt * factor), guild_id, uid),
            )
        return take

    # ------------------------------------------------------------------ #
    # 帝愛信託の利息（#33。確定した流出として、毎時払う）
    # ------------------------------------------------------------------ #
    def risk_free_per_day_for(self, guild_id: int, now: float) -> float:
        """無リスク金利 r_f（1日あたり。連続複利）。いまは通常債の利率から決まる固定値（#18 で準備率に連動させる）。"""
        return rates.BASE_RISK_FREE_PER_DAY

    def trust_rate_per_day_for(self, guild_id: int, now: float) -> float:
        """帝愛信託の利率 r_trust = r_f + 上乗せ（1日あたり。連続複利）。"""
        return self.risk_free_per_day_for(guild_id, now) + teiai.TRUST_PREMIUM_PER_DAY

    def bank_trust_accrue(self, now: float) -> int:
        """帝愛信託の利息を、前回からの経過時間ぶん払う（1時間たっていなければ何もしない）。払った額を返す。

        利息は銀行の確定した流出: 手元の現金から払い、足りない分は信託からは取らず（自分で自分に払うことになるため）、
        超過損失として熱浴・共済へ回す。払えた分を、信託の残高に応じて按分して上乗せする。
        """
        with self._db.tx():
            gid = self._g()
            self.bank_sync_for(gid, now)
            row = self._conn.execute(
                "SELECT trust_ts, trust_accrued FROM teiai_bank WHERE guild_id=?", (gid,)
            ).fetchone()
            if row["trust_ts"] <= 0:
                self._conn.execute("UPDATE teiai_bank SET trust_ts=? WHERE guild_id=?", (now, gid))
                return 0
            hours = (now - row["trust_ts"]) / 3600
            if hours < 1:
                return 0
            deposits = self._deposits_for(gid)
            total = sum(a for _, a in deposits)
            rate = self.trust_rate_per_day_for(gid, now)
            due = total * rates.growth(rate, hours / 24) + row["trust_accrued"]
            paid = self._outflow_for(gid, int(due), now, use_trust=False, thermo=True) if total > 0 else 0
            credited = 0
            for uid, amt in deposits:
                gain = paid * amt // total
                if gain:
                    credited += gain
                    self._conn.execute(
                        "UPDATE teiai_deposits SET amount=amount+? WHERE guild_id=? AND user_id=?", (gain, gid, uid)
                    )
            # 端数（按分の切り捨て）は手元へ戻し、未払いの小数は次回へ持ち越す
            if paid > credited:
                cash = self.bank_sync_for(gid, now)
                self.bank_set_for(gid, cash["cash"] + paid - credited, cash["loaned"])
            carry = max(0.0, due - int(due)) + (paid - credited if total > 0 else 0)
            self._conn.execute(
                "UPDATE teiai_bank SET trust_ts=?, trust_accrued=?, trust_paid=trust_paid+? WHERE guild_id=?",
                (now, carry, credited, gid),
            )
            return credited

    def charge_gains_tax_for(self, guild_id: int, user_id: int, profit: int, taxable: bool, what: str) -> int:
        """利益 profit にかかる税を、帝愛の銀行（預金者の利回り）へ納める。納めた額を返す。"""
        tax = market_mod.capital_gains_tax(profit) if taxable else 0
        if tax > 0:
            self._account.ledger(
                guild_id,
                user_id,
                0,
                self._account.raw_balance(guild_id, user_id),
                f"帝愛のキャピタルゲイン税（{what} 利益{profit:,} → 税{tax:,}）",
            )
            self.bank_income_for(guild_id, tax, self._now())
        return tax

    # ------------------------------------------------------------------ #
    # 預金
    # ------------------------------------------------------------------ #
    def bank_deposit(self, user_id: int, amount: int, now: float) -> tuple[Any, ...]:
        """帝愛に預ける（カタギだけ）。(成功か, 理由か預金残高) を返す。"""
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            if self._status_reader.status_for(gid, user_id, now) != teiai.KATAGI:
                return False, fail("bank_katagi_only")
            if amount < teiai.BANK_MIN_DEPOSIT:
                return False, fail("bank_min_deposit", min_deposit=teiai.BANK_MIN_DEPOSIT)
            bal = self._account.raw_balance(gid, user_id)
            if bal < amount:
                return False, fail("insufficient_balance", balance=bal)
            state = self.bank_sync_for(gid, now)
            self._account.apply(gid, user_id, -amount, "帝愛への預金")
            self._conn.execute(
                "INSERT INTO teiai_deposits (guild_id, user_id, amount) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET amount=amount+excluded.amount",
                (gid, user_id, amount),
            )
            reserve = amount * teiai.RESERVE_RATIO
            self.bank_set_for(gid, state["cash"] + reserve, state["loaned"] + amount - reserve)
            return True, self.deposit_of_for(gid, user_id)

    def bank_withdraw(self, user_id: int, amount: Optional[int] = None, now: Optional[float] = None) -> tuple[Any, ...]:
        """預金を引き出す（amount が None なら全額）。利用可能現金の範囲まで。(成功か, 理由か引き出した額) を返す。"""
        now = self._now() if now is None else now
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            have = self.deposit_of_for(gid, user_id)
            if have <= 0:
                return False, fail("bank_no_deposit")
            want = have if amount is None else amount
            if want <= 0 or want > have:
                return False, fail("bank_withdraw_range", max_amount=have)
            state = self.bank_sync_for(gid, now)
            available = int(state["cash"] - self.bank_locks_for(gid, now))
            if available <= 0:
                return False, fail("bank_frozen")
            if want > available:
                return False, fail("bank_available_limit", available=available)
            self._conn.execute(
                "UPDATE teiai_deposits SET amount=amount-? WHERE guild_id=? AND user_id=?", (want, gid, user_id)
            )
            self.bank_set_for(gid, state["cash"] - want, state["loaned"])
            self._account.apply(gid, user_id, want, "帝愛からの引き出し")
            return True, want

    def deposit_take_for(self, guild_id: int, user_id: int, need: int, now: float) -> int:
        """強制執行・破産のとき、預金から need まで差し押さえる（引き出しの制限は受けない）。取れた額を返す（手元には入れない）。"""
        have = self.deposit_of_for(guild_id, user_id)
        take = min(have, need)
        if take <= 0:
            return 0
        state = self.bank_sync_for(guild_id, now)
        self._conn.execute(
            "UPDATE teiai_deposits SET amount=amount-? WHERE guild_id=? AND user_id=?", (take, guild_id, user_id)
        )
        self.bank_set_for(guild_id, max(0.0, state["cash"] - take), state["loaned"])
        return take

    # ------------------------------------------------------------------ #
    # 破産猶予（IGraceReader）
    # ------------------------------------------------------------------ #
    def grace_update_for(self, guild_id: int, user_id: int, now: float) -> None:
        """借金の額に合わせて、破産猶予に入れる／外す。入るときに一言を残す。"""
        row = self._conn.execute(
            "SELECT debt, grace_until FROM users WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        ).fetchone()
        if row is None:
            return
        if row["debt"] >= teiai.GRACE_DEBT and row["grace_until"] is None:
            until = now + teiai.GRACE_HOURS * 3600
            self._conn.execute(
                "UPDATE users SET grace_until=? WHERE guild_id=? AND user_id=?", (until, guild_id, user_id)
            )
            self._grace_enter_hook.on_grace_enter(guild_id, user_id, now)
            self._notices.note_for(guild_id, user_id, "grace_entered", grace_debt=teiai.GRACE_DEBT, until=until)
        elif row["debt"] < teiai.GRACE_DEBT and row["grace_until"] is not None:
            self._conn.execute("UPDATE users SET grace_until=NULL WHERE guild_id=? AND user_id=?", (guild_id, user_id))
            self._notices.note_for(guild_id, user_id, "grace_exited")

    def grace_until_for(self, guild_id: int, user_id: int) -> Optional[float]:
        with self._lock:
            r = self._conn.execute(
                "SELECT grace_until FROM users WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            ).fetchone()
            return r["grace_until"] if r else None

    def grace_until(self, user_id: int) -> Optional[float]:
        return self.grace_until_for(self._g(), user_id)

    def grace_list(self) -> list[Any]:
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

    def grace_expire(self, now: float) -> list[Any]:
        """猶予の時間切れになった人を、強制破産させる。CDS の失効もここで済ます。[(ユーザーID, 破産の結果)] を返す。"""
        done = []
        with self._db.tx():
            gid = self._g()
            self._conn.execute(
                "UPDATE cds SET status='expired' WHERE guild_id=? AND status='active' AND expires<=?", (gid, now)
            )
            for r in self._conn.execute(
                "SELECT user_id FROM users WHERE guild_id=? AND grace_until IS NOT NULL AND grace_until<=? ORDER BY grace_until",
                (gid, now),
            ).fetchall():
                if self._account.debt(gid, r["user_id"]) < teiai.GRACE_DEBT:  # 取りこぼしの保険（外し忘れ）
                    self.grace_update_for(gid, r["user_id"], now)
                    continue
                ok, result = self._bankrupt_executor.bankrupt_for(gid, r["user_id"], forced=True)
                if ok:
                    done.append((r["user_id"], result))
        return done

    # ------------------------------------------------------------------ #
    # CDS（破綻保証）
    # ------------------------------------------------------------------ #
    def cds_quote(self, target_id: int, now: float) -> CdsQuote:
        """対象の、いまの CDS のオッズと状況。読み取り専用。"""
        gid = self._g()
        status = self._status_reader.status_for(gid, target_id, now)
        debt = self._account.debt_readonly(gid, target_id)
        ratio = self._margin_ratio_reader.margin_ratio_for(gid, target_id)
        return {
            "odds": teiai.cds_odds(status, debt, ratio),
            "status": status,
            "debt": debt,
            "grace": self.grace_until_for(gid, target_id) is not None,
        }

    def cds_buy(self, buyer_id: int, target_id: int, stake: int, now: float) -> tuple[Any, ...]:
        """CDS を買う（自分にも、他人にも）。(成功か, 理由かオッズ) を返す。"""
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, buyer_id)
            if stake < teiai.CDS_MIN_STAKE:
                return False, fail("cds_min_stake", min_stake=teiai.CDS_MIN_STAKE)
            if (
                self._conn.execute("SELECT 1 FROM users WHERE guild_id=? AND user_id=?", (gid, target_id)).fetchone()
                is None
            ):
                return False, fail("user_not_registered")
            bal = self._account.raw_balance(gid, buyer_id)
            if bal < stake:
                return False, fail("insufficient_balance", balance=bal)
            if self.grace_until_for(gid, target_id) is not None:
                return False, fail("cds_target_in_grace")
            if self._status_reader.status_for(gid, target_id, now) == teiai.BANKRUPT:
                return False, fail("cds_target_bankrupt")
            odds = self.cds_quote(target_id, now)["odds"]
            if buyer_id == target_id:  # 自分への保証は、積める額に上限がある
                have = self._conn.execute(
                    "SELECT COALESCE(SUM(stake * odds), 0) FROM cds WHERE guild_id=? AND buyer_id=? AND target_id=? "
                    "AND status='active' AND expires>?",
                    (gid, buyer_id, buyer_id, now),
                ).fetchone()[0]
                if have + stake * odds > teiai.CDS_SELF_CAP:
                    room = max(0, int((teiai.CDS_SELF_CAP - have) / odds))
                    return False, fail(
                        "cds_self_cap",
                        cap=teiai.CDS_SELF_CAP,
                        have=int(have),
                        room=room,
                    )
            self._account.apply(gid, buyer_id, -stake, f"CDS の掛け金（対象 {target_id}・{odds}倍）")
            self.bank_income_for(gid, stake, now)
            self._conn.execute(
                "INSERT INTO cds (guild_id, buyer_id, target_id, stake, odds, ts, expires) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (gid, buyer_id, target_id, stake, odds, now, now + teiai.CDS_TERM_DAYS * 86400),
            )
            return True, odds

    def cds_list(self, user_id: int) -> CdsHoldings:
        """自分が買っている CDS（有効なもの）と、自分に掛けられている件数。"""
        with self._lock:
            gid = self._g()
            mine = [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM cds WHERE guild_id=? AND buyer_id=? AND status='active' ORDER BY id",
                    (gid, user_id),
                )
            ]
            on_me = self._conn.execute(
                "SELECT COUNT(*) FROM cds WHERE guild_id=? AND target_id=? AND buyer_id!=target_id AND status='active'",
                (gid, user_id),
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

    def cds_cover_self_for(self, guild_id: int, user_id: int, shortfall: int, now: float) -> int:
        """払えなかった shortfall を、自分への CDS が肩代わりする（古い順に、1件ずつ使い切り）。肩代わりした額を返す。現金は渡らない。"""
        covered = 0
        for r in self._conn.execute(
            "SELECT id, stake, odds FROM cds WHERE guild_id=? AND buyer_id=? AND target_id=? AND status='active' "
            "AND expires>? ORDER BY id",
            (guild_id, user_id, user_id, now),
        ).fetchall():
            if covered >= shortfall:
                break
            pay = self.bank_pay_for(guild_id, min(shortfall - covered, int(r["stake"] * r["odds"])), now)
            self._conn.execute("UPDATE cds SET status='paid', paid=? WHERE id=?", (pay, r["id"]))
            covered += pay
            self._notices.note_for(guild_id, user_id, "cds_self_covered", pay=pay)
        return covered

    def cds_settle_default_for(self, guild_id: int, target_id: int, debt_before: int, now: float) -> int:
        """対象が強制破産したとき、他人からの CDS に配当を払う。合計は、対象の借金の CDS_PAYOUT_CAP_RATE まで（超える分は、一律に減らす）。払った合計を返す。"""
        rows = self._conn.execute(
            "SELECT id, buyer_id, stake, odds FROM cds WHERE guild_id=? AND target_id=? AND buyer_id!=target_id "
            "AND status='active' AND expires>? ORDER BY id",
            (guild_id, target_id, now),
        ).fetchall()
        claims = [(r["id"], r["buyer_id"], int(r["stake"] * r["odds"])) for r in rows]
        total_claim = sum(c for _, _, c in claims)
        cap = int(teiai.CDS_PAYOUT_CAP_RATE * debt_before)
        scale = min(1.0, cap / total_claim) if total_claim > 0 else 0.0
        total_paid = 0
        for cid, buyer, claim in claims:
            pay = self.bank_pay_for(guild_id, int(claim * scale), now)
            if pay > 0:
                self._account.ensure(guild_id, buyer)
                self._account.apply(guild_id, buyer, pay, f"CDS の配当（対象 {target_id} が強制破産）", income=pay)
                self._notices.note_for(guild_id, buyer, "cds_default_payout", target_id=target_id, pay=pay)
            self._conn.execute("UPDATE cds SET status='paid', paid=? WHERE id=?", (pay, cid))
            total_paid += pay
        self.cds_void_for(guild_id, target_id)
        return total_paid

    def cds_void_for(self, guild_id: int, target_id: int) -> None:
        """対象が破産したあと、まだ有効な CDS（配当されなかったもの）を無効にする。"""
        self._conn.execute(
            "UPDATE cds SET status='void' WHERE guild_id=? AND target_id=? AND status='active'",
            (guild_id, target_id),
        )
