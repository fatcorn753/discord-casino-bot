"""帝愛信託の預金: 残高の読み取り・利率の決定・入出金と強制差し押さえ。"""

from domain import teiai
from domain.errors import Outcome, fail

from .thermostat import BankRepo as Base


class BankRepo(Base):
    def _deposits_for(self, guild_id: int) -> list[tuple[int, int]]:
        return [
            (r["user_id"], r["amount"])
            for r in self._all(
                "SELECT user_id, amount FROM teiai_deposits WHERE guild_id=? AND amount>0 ORDER BY user_id",
                (guild_id,),
            )
        ]

    def deposit_of_for(self, guild_id: int, user_id: int) -> int:
        with self._lock:
            v = self._scalar("SELECT amount FROM teiai_deposits WHERE guild_id=? AND user_id=?", (guild_id, user_id))
            return v if v is not None else 0

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

    def trust_rate_per_day_for(self, guild_id: int, now: float) -> float:
        """帝愛信託の利率 r_trust = r_f + 上乗せ（1日あたり。連続複利）。"""
        return self.risk_free_per_day_for(guild_id, now) + self.trust_premium_for(guild_id)

    def trust_premium_for(self, guild_id: int) -> float:
        """信託の上乗せ金利。共済の動議で決まっていればその値、なければ設定の既定値（#34）。"""
        row = self._one("SELECT premium FROM teiai_trust WHERE guild_id=?", (guild_id,))
        if row is None or row["premium"] is None:
            return teiai.TRUST_PREMIUM_PER_DAY
        return row["premium"]

    def set_trust_premium_for(self, guild_id: int, premium: float | None, now: float) -> None:
        """共済の動議で決まった信託の上乗せ金利を保存する。"""
        self.bank_sync_for(guild_id, now)
        self._conn.execute(
            "INSERT INTO teiai_trust (guild_id, premium) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET premium=excluded.premium",
            (guild_id, premium),
        )

    # 預金

    def bank_deposit(self, user_id: int, amount: int, now: float) -> Outcome[int]:
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

    def bank_withdraw(self, user_id: int, amount: int | None = None, now: float | None = None) -> Outcome[int]:
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
