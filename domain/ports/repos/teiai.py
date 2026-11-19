"""帝愛ファイナンス・銀行・個人間貸付のリポジトリポート契約。"""

from typing import Any, Optional

from domain.ports.bridges import (
    IBankCashFlow,
    IBankCdsBridge,
    IBankruptExecutor,
    IDebtLifecycleHook,
    IDebtOps,
    IDebtRepayHook,
    IGainsTaxCollector,
    IHouseAccount,
    IOverdueLoanCollector,
    IStatusReader,
    IValuationSource,
)
from domain.ports.contracts import BankStatus, CdsHoldings, CdsQuote, HouseAccount, LoanBook, LoanCredit
from domain.ports.repos.base import IGuildScopedRepo


class ILoanRepo(IGuildScopedRepo, IOverdueLoanCollector):
    def loan_credit(self, user_id: int, now: float) -> LoanCredit:
        raise NotImplementedError

    def loan_request(
        self, borrower_id: int, principal: int, rate_tenths: int, term_days: int, channel_id: int, now: float
    ) -> tuple[Any, ...]:
        raise NotImplementedError

    def loan_lend(self, loan_id: int, lender_id: int, now: float) -> tuple[Any, ...]:
        raise NotImplementedError

    def loan_offer(
        self,
        lender_id: int,
        borrower_id: int,
        principal: int,
        rate_tenths: int,
        term_days: int,
        channel_id: int,
        now: float,
    ) -> tuple[Any, ...]:
        raise NotImplementedError

    def loan_set_message(self, loan_id: int, message_id: int) -> None:
        raise NotImplementedError

    def loan_get(self, loan_id: int) -> Optional[dict[str, Any]]:
        raise NotImplementedError

    def loan_accept(self, loan_id: int, user_id: int, now: float) -> tuple[Any, ...]:
        raise NotImplementedError

    def loan_decline(self, loan_id: int, user_id: int) -> tuple[Any, ...]:
        raise NotImplementedError

    def loan_cancel(self, loan_id: int, user_id: int) -> tuple[Any, ...]:
        raise NotImplementedError

    def loan_repay(self, loan_id: int, user_id: int, amount: Optional[int], now: float) -> tuple[Any, ...]:
        raise NotImplementedError

    def loan_collect_due(self, now: float) -> list[Any]:
        raise NotImplementedError

    def loan_due_soon(self, now: float) -> list[Any]:
        raise NotImplementedError

    def loan_expire_offers(self, now: float) -> list[Any]:
        raise NotImplementedError

    def loan_list(self, user_id: int) -> LoanBook:
        raise NotImplementedError


class IBankRepo(IGuildScopedRepo, IBankCashFlow, IHouseAccount, IGainsTaxCollector, IBankCdsBridge, IValuationSource):
    def deposit_of(self, user_id: int) -> int:
        raise NotImplementedError

    def bank_values(self) -> dict[int, int]:
        raise NotImplementedError

    def bank_status(self, now: float) -> BankStatus:
        raise NotImplementedError

    def house_account(self) -> HouseAccount:
        raise NotImplementedError

    def bank_deposit(self, user_id: int, amount: int, now: float) -> tuple[Any, ...]:
        raise NotImplementedError

    def bank_withdraw(self, user_id: int, amount: Optional[int] = None, now: Optional[float] = None) -> tuple[Any, ...]:
        raise NotImplementedError

    def grace_until(self, user_id: int) -> Optional[float]:
        raise NotImplementedError

    def grace_list(self) -> list[Any]:
        raise NotImplementedError

    def grace_expire(self, now: float) -> list[Any]:
        raise NotImplementedError

    def bank_trust_accrue(self, now: float) -> int:
        raise NotImplementedError

    def cds_quote(self, target_id: int, now: float) -> CdsQuote:
        raise NotImplementedError

    def cds_buy(self, buyer_id: int, target_id: int, stake: int, now: float) -> tuple[Any, ...]:
        raise NotImplementedError

    def cds_list(self, user_id: int) -> CdsHoldings:
        raise NotImplementedError

    def cds_total_claims(self) -> int:
        raise NotImplementedError


class IDebtRepo(
    IGuildScopedRepo,
    IDebtLifecycleHook,
    IStatusReader,
    IBankruptExecutor,
    IDebtOps,
    IDebtRepayHook,
):
    def status(self, user_id: int, now: Optional[float] = None) -> str:
        raise NotImplementedError

    def loan_blacklisted(self, user_id: int, now: Optional[float] = None) -> bool:
        raise NotImplementedError

    def debt(self, user_id: int) -> int:
        raise NotImplementedError

    def borrow_seed(self, user_id: int) -> tuple[Any, ...]:
        raise NotImplementedError

    def can_borrow_for_bet(self, user_id: int, need: int) -> tuple[Any, ...]:
        raise NotImplementedError

    def repay_debt(self, user_id: int, amount: Optional[int] = None) -> tuple[Any, ...]:
        raise NotImplementedError

    def escape_left(self, user_id: int) -> int:
        raise NotImplementedError

    def escape_attempt(self, user_id: int, now: float, rng: Any = None) -> tuple[Any, ...]:
        raise NotImplementedError

    def bankrupt(self, user_id: int, forced: bool = False, escape: bool = False) -> tuple[Any, ...]:
        raise NotImplementedError
