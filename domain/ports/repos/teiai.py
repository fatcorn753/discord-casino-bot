"""帝愛ファイナンス・銀行・個人間貸付のリポジトリポート契約。"""

import random
from typing import Any, Optional

from domain.errors import Outcome
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
    ) -> Outcome[int]:
        raise NotImplementedError

    def loan_lend(self, loan_id: int, lender_id: int, now: float) -> Outcome[str]:
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
    ) -> Outcome[int]:
        raise NotImplementedError

    def loan_set_message(self, loan_id: int, message_id: int) -> None:
        raise NotImplementedError

    def loan_get(self, loan_id: int) -> Optional[dict[str, Any]]:
        raise NotImplementedError

    def loan_accept(self, loan_id: int, user_id: int, now: float) -> Outcome[str]:
        raise NotImplementedError

    def loan_decline(self, loan_id: int, user_id: int) -> Outcome[str]:
        raise NotImplementedError

    def loan_cancel(self, loan_id: int, user_id: int) -> Outcome[str]:
        raise NotImplementedError

    def loan_repay(self, loan_id: int, user_id: int, amount: Optional[int], now: float) -> Outcome[int]:
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

    def bank_deposit(self, user_id: int, amount: int, now: float) -> Outcome[int]:
        raise NotImplementedError

    def bank_withdraw(self, user_id: int, amount: Optional[int] = None, now: Optional[float] = None) -> Outcome[int]:
        raise NotImplementedError

    def grace_until(self, user_id: int) -> Optional[float]:
        raise NotImplementedError

    def grace_list(self) -> list[tuple[int, int, float]]:
        raise NotImplementedError

    def grace_expire(self, now: float) -> list[tuple[int, Any]]:
        raise NotImplementedError

    def bank_trust_accrue(self, now: float) -> int:
        raise NotImplementedError

    def bank_rf_tick(self, now: float, rng: Optional[random.Random] = None) -> float:
        raise NotImplementedError

    def cds_quote(self, target_id: int, now: float) -> CdsQuote:
        raise NotImplementedError

    def cds_buy(self, buyer_id: int, target_id: int, stake: int, now: float) -> Outcome[float]:
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

    def borrow_seed(self, user_id: int) -> Outcome[int]:
        raise NotImplementedError

    def can_borrow_for_bet(self, user_id: int, need: int) -> Outcome[str]:
        raise NotImplementedError

    def repay_debt(self, user_id: int, amount: Optional[int] = None) -> Outcome[int]:
        raise NotImplementedError

    def escape_left(self, user_id: int) -> int:
        raise NotImplementedError

    def escape_attempt(self, user_id: int, now: float, rng: Optional[random.Random] = None) -> Outcome[dict[str, Any]]:
        raise NotImplementedError

    def bankrupt(self, user_id: int, forced: bool = False, escape: bool = False) -> Outcome[dict[str, Any]]:
        raise NotImplementedError


class IKyosaiRepo(IGuildScopedRepo):
    """帝愛共済（#34）: 口・配当・回収と帝愛の持ち口の競り・譲渡・動議。"""

    def units_of_for(self, guild_id: int, user_id: int) -> int:
        raise NotImplementedError

    def recover_units_for(self, guild_id: int, user_id: int, now: float) -> int:
        raise NotImplementedError

    def kyosai_daily(self, now: float) -> Optional[dict[str, Any]]:
        raise NotImplementedError

    def kyosai_auction_tick(self, now: float) -> list[dict[str, Any]]:
        raise NotImplementedError

    def kyosai_bid(self, user_id: int, amount: int, now: float) -> Outcome[dict[str, Any]]:
        raise NotImplementedError

    def kyosai_offer(self, seller: int, buyer: int, units: int, price: int, now: float) -> Outcome[int]:
        raise NotImplementedError

    def kyosai_accept(self, buyer: int, offer_id: int, now: float) -> Outcome[dict[str, Any]]:
        raise NotImplementedError

    def kyosai_cancel(self, user_id: int, offer_id: int) -> bool:
        raise NotImplementedError

    def kyosai_offers_of(self, user_id: int, now: float) -> list[dict[str, Any]]:
        raise NotImplementedError

    def kyosai_propose(self, user_id: int, premium: float, now: float) -> Outcome[dict[str, Any]]:
        raise NotImplementedError

    def kyosai_vote(self, user_id: int, yes: bool, now: float, motion_id: Optional[int] = None) -> Outcome[int]:
        raise NotImplementedError

    def kyosai_close_motions(self, now: float) -> list[dict[str, Any]]:
        raise NotImplementedError

    def kyosai_set_motion_message(self, motion_id: int, channel_id: int, message_id: int) -> None:
        raise NotImplementedError

    def kyosai_motion(self, motion_id: int, now: float) -> Optional[dict[str, Any]]:
        raise NotImplementedError

    def kyosai_status(self, user_id: int, now: float) -> dict[str, Any]:
        raise NotImplementedError
