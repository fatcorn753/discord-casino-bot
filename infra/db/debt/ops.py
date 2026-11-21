"""借金の発生と種銭: 強制執行・貸付・信用枠の判定。"""

from typing import Any

from domain import teiai
from domain.errors import fail

from .base import _take_cash
from .lifecycle import DebtRepo as Base


class DebtRepo(Base):
    # 強制執行と、借金の発生（IDebtOps）

    def _seize_assets_for(self, guild_id: int, user_id: int, need: int, reason: str) -> int:
        now = self._now()
        covered = _take_cash(self._account, guild_id, user_id, need, reason)
        if covered >= need:
            return covered
        return covered + self._seizure_step.seize(guild_id, user_id, need - covered, reason, now)

    def seed_for(self, guild_id: int, user_id: int, amount: int) -> None:
        """帝愛が種銭を貸し付ける。手元に入り、同じ額が借金に加わる（天引きの対象にはならない）。"""
        self._account.apply(guild_id, user_id, amount, "帝愛の種銭貸付")
        self._conn.execute(
            "UPDATE users SET debt = debt + ? WHERE guild_id=? AND user_id=?", (amount, guild_id, user_id)
        )
        self._bank.grace_update_for(guild_id, user_id, self._now())
        self._notices.note_for(guild_id, user_id, "seed_loan", amount=amount)

    def borrow_seed_for(self, guild_id: int, user_id: int) -> tuple[Any, ...]:
        """（いつでも）手元が種銭の枠に満たなければ、枠まで帝愛から借りる。(成功か, 理由か借りた額)。"""
        with self._db.tx():
            self._account.ensure(guild_id, user_id)
            if self.status_for(guild_id, user_id, self._now()) == teiai.BANKRUPT:
                return False, fail("seed_bankrupt_blocked")
            if self._bank.grace_until_for(guild_id, user_id) is not None:
                return False, fail("seed_grace_blocked")
            short = teiai.SEED - self._account.raw_balance(guild_id, user_id)
            if short <= 0:
                return False, fail("seed_not_needed", seed=teiai.SEED)
            self.seed_for(guild_id, user_id, short)
            return True, short

    def borrow_seed(self, user_id: int) -> tuple[Any, ...]:
        return self.borrow_seed_for(self._g(), user_id)

    def can_borrow_for_bet_for(self, guild_id: int, user_id: int, need: int) -> tuple[Any, ...]:
        """手元が足りない need を、帝愛の信用で借りて賭けられるか。(可否, Failure | "")。"""
        with self._lock:
            if need <= 0:
                return True, ""
            if self.status_for(guild_id, user_id, self._now()) == teiai.BANKRUPT:
                return False, fail("credit_bankrupt_blocked")
            if self._bank.grace_until_for(guild_id, user_id) is not None:
                return False, fail("credit_grace_blocked")
            if need > teiai.SEED:
                return False, fail("credit_limit_exceeded", seed=teiai.SEED, need=need)
            return True, ""

    def can_borrow_for_bet(self, user_id: int, need: int) -> tuple[Any, ...]:
        return self.can_borrow_for_bet_for(self._g(), user_id, need)

    def _seed_if_empty_for(self, guild_id: int, user_id: int) -> int:
        if self._account.debt(guild_id, user_id) > 0 and self._account.raw_balance(guild_id, user_id) == 0:
            self.seed_for(guild_id, user_id, teiai.SEED)
            return teiai.SEED
        return 0

    def topup_seed_for(self, guild_id: int, user_id: int) -> int:
        short = teiai.SEED - self._account.raw_balance(guild_id, user_id)
        if self._account.debt(guild_id, user_id) > 0 and short > 0:
            self.seed_for(guild_id, user_id, short)
            return short
        return 0

    def owe_for(self, guild_id: int, user_id: int, shortfall: int, reason: str) -> int:
        """払えなかった shortfall を、強制執行したうえで、残りを帝愛の借金にする。借金に加えた額を返す。"""
        if shortfall <= 0:
            return 0
        need = shortfall - self._seize_assets_for(guild_id, user_id, shortfall, f"強制執行:{reason}")
        if need <= 0:
            return 0
        need -= self._bank.cds_cover_self_for(guild_id, user_id, need, self._now())
        if need <= 0:
            return 0
        self._conn.execute("UPDATE users SET debt = debt + ? WHERE guild_id=? AND user_id=?", (need, guild_id, user_id))
        self._account.ledger(
            guild_id, user_id, 0, self._account.raw_balance(guild_id, user_id), f"借金:{reason}（{need:,}ペリカ）"
        )
        self._seed_if_empty_for(guild_id, user_id)
        self._bank.grace_update_for(guild_id, user_id, self._now())
        return need
