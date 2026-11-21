"""個人間貸付（提案・募集・承諾・返済・期日回収）のアプリケーションサービス。"""

from dataclasses import dataclass
from typing import Any, Optional

from domain.errors import Outcome
from domain.ports.clock import IClock
from domain.ports.contracts import LoanBook, LoanCredit
from domain.ports.repos import ILoanRepo


@dataclass(frozen=True)
class LoanTickOutcome:
    expired: list[dict[str, Any]]
    due_soon: list[dict[str, Any]]
    collected: list[dict[str, Any]]


class LoanService:
    """個人間貸付の時刻基準ユースケース調停・定期期限チェック。"""

    def __init__(self, loans: ILoanRepo, clock: IClock) -> None:
        self._loans = loans
        self.clock = clock

    def now_ts(self) -> float:
        return self.clock.now_ts()

    def get(self, guild_id: int, loan_id: int) -> Optional[dict[str, Any]]:
        return self._loans.for_guild(guild_id).loan_get(loan_id)

    def credit(self, guild_id: int, user_id: int) -> LoanCredit:
        return self._loans.for_guild(guild_id).loan_credit(user_id, self.clock.now_ts())

    def list_for(self, guild_id: int, user_id: int) -> LoanBook:
        return self._loans.for_guild(guild_id).loan_list(user_id)

    def set_message(self, guild_id: int, loan_id: int, message_id: int) -> None:
        self._loans.for_guild(guild_id).loan_set_message(loan_id, message_id)

    def decline(self, guild_id: int, loan_id: int, user_id: int) -> Outcome[str]:
        return self._loans.for_guild(guild_id).loan_decline(loan_id, user_id)

    def cancel(self, guild_id: int, loan_id: int, user_id: int) -> Outcome[str]:
        return self._loans.for_guild(guild_id).loan_cancel(loan_id, user_id)

    def remaining(self, guild_id: int, loan: dict[str, Any], now: Optional[float] = None) -> int:
        ts = self.clock.now_ts() if now is None else now
        return self._loans.for_guild(guild_id).loan_remaining(loan, ts)

    def offer(
        self,
        guild_id: int,
        lender_id: int,
        borrower_id: int,
        principal: int,
        rate_tenths: int,
        term_days: int,
        channel_id: int,
    ) -> Outcome[int]:
        return self._loans.for_guild(guild_id).loan_offer(
            lender_id, borrower_id, principal, rate_tenths, term_days, channel_id, self.clock.now_ts()
        )

    def request(
        self,
        guild_id: int,
        borrower_id: int,
        principal: int,
        rate_tenths: int,
        term_days: int,
        channel_id: int,
    ) -> Outcome[int]:
        return self._loans.for_guild(guild_id).loan_request(
            borrower_id, principal, rate_tenths, term_days, channel_id, self.clock.now_ts()
        )

    def accept(self, guild_id: int, loan_id: int, user_id: int) -> Outcome[str]:
        return self._loans.for_guild(guild_id).loan_accept(loan_id, user_id, self.clock.now_ts())

    def lend(self, guild_id: int, loan_id: int, user_id: int) -> Outcome[str]:
        return self._loans.for_guild(guild_id).loan_lend(loan_id, user_id, self.clock.now_ts())

    def repay(self, guild_id: int, loan_id: int, user_id: int, amount: Optional[int]) -> Outcome[int]:
        return self._loans.for_guild(guild_id).loan_repay(loan_id, user_id, amount, self.clock.now_ts())

    def process_tick(self, guild_id: int, now: Optional[float] = None) -> LoanTickOutcome:
        ts = self.clock.now_ts() if now is None else now
        repo = self._loans.for_guild(guild_id)
        return LoanTickOutcome(
            expired=repo.loan_expire_offers(ts),
            due_soon=repo.loan_due_soon(ts),
            collected=repo.loan_collect_due(ts),
        )
