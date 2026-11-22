"""個人間貸付・代用有価証券のブリッジ契約（名目的基底クラスと NoOp 実装）。"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from domain.ports.contracts import LoanRow

if TYPE_CHECKING:
    from domain.types import CryptoResult


class IOverdueLoanCollector:
    """収入・償還・破産整理時に個人間貸付の状態参照と返済整理を行う契約。"""

    def collect_overdue_for(self, guild_id: int, user_id: int, budget: int) -> int:
        raise NotImplementedError

    def loan_remaining(self, loan: Mapping[str, Any], now: float) -> int:
        raise NotImplementedError

    def has_overdue_for(self, guild_id: int, user_id: int) -> bool:
        raise NotImplementedError

    def active_loans_for(self, guild_id: int, user_id: int) -> list[LoanRow]:
        raise NotImplementedError

    def record_bankrupt_payment_for(self, loan_id: int, paid: int) -> None:
        raise NotImplementedError

    def finalize_bankrupt_loan_for(self, loan_id: int, repaid: bool) -> None:
        raise NotImplementedError


class NoOpOverdueLoanCollector(IOverdueLoanCollector):
    """個人間貸付契約未接続時の LSP 準拠 Null 実装。"""

    def collect_overdue_for(self, guild_id: int, user_id: int, budget: int) -> int:
        return 0

    def loan_remaining(self, loan: Mapping[str, Any], now: float) -> int:
        return 0

    def has_overdue_for(self, guild_id: int, user_id: int) -> bool:
        return False

    def active_loans_for(self, guild_id: int, user_id: int) -> list[LoanRow]:
        return []

    def record_bankrupt_payment_for(self, loan_id: int, paid: int) -> None:
        return None

    def finalize_bankrupt_loan_for(self, loan_id: int, repaid: bool) -> None:
        return None


class ISpotCollateralReader:
    """信用取引の代用有価証券（現物株）読み取り契約。"""

    def holdings_for(self, guild_id: int, user_id: int) -> list[tuple[str, int, int]]:
        raise NotImplementedError

    def crypto_sell_locked_for(
        self,
        guild_id: int,
        user_id: int,
        units: int | None,
        price: float,
        symbol: str,
        taxable: bool = False,
    ) -> "CryptoResult":
        raise NotImplementedError


class NoOpSpotCollateralReader(ISpotCollateralReader):
    """代用有価証券契約未接続時の LSP 準拠 Null 実装。"""

    def holdings_for(self, guild_id: int, user_id: int) -> list[tuple[str, int, int]]:
        return []

    def crypto_sell_locked_for(
        self,
        guild_id: int,
        user_id: int,
        units: int | None,
        price: float,
        symbol: str,
        taxable: bool = False,
    ) -> "CryptoResult":
        from domain.types import CryptoResult

        return CryptoResult(ok=False)
