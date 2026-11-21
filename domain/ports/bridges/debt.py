"""台帳・借金ライフサイクル・返済のブリッジ契約（名目的基底クラスと NoOp 実装）。"""

from typing import Any, Optional


class IDebtLifecycleHook:
    """台帳更新に連動する借金利息・天引き・猶予更新のブリッジ契約。"""

    def accrue_interest(self, guild_id: int, user_id: int, now: Optional[float] = None) -> int:
        raise NotImplementedError

    def after_debt_change(self, guild_id: int, user_id: int) -> None:
        raise NotImplementedError

    def withhold_income(self, guild_id: int, user_id: int, income: int) -> int:
        raise NotImplementedError


class NoOpDebtLifecycleHook(IDebtLifecycleHook):
    """借金フック未接続時に安全に何もしない LSP 準拠実装。"""

    def accrue_interest(self, guild_id: int, user_id: int, now: Optional[float] = None) -> int:
        return 0

    def after_debt_change(self, guild_id: int, user_id: int) -> None:
        return None

    def withhold_income(self, guild_id: int, user_id: int, income: int) -> int:
        return 0


class IStatusReader:
    """ユーザーの借金・身分・ブラックリスト状態の読み取り契約。"""

    def status_for(self, guild_id: int, user_id: int, now: float) -> str:
        raise NotImplementedError

    def debt_for(self, guild_id: int, user_id: int) -> int:
        raise NotImplementedError

    def loan_blacklisted_for(self, guild_id: int, user_id: int, now: float) -> bool:
        raise NotImplementedError


class NoOpStatusReader(IStatusReader):
    """身分契約未接続時の安全デフォルト（カタギ・借金 0・制限なし）。"""

    def status_for(self, guild_id: int, user_id: int, now: float) -> str:
        return "katagi"

    def debt_for(self, guild_id: int, user_id: int) -> int:
        return 0

    def loan_blacklisted_for(self, guild_id: int, user_id: int, now: float) -> bool:
        return False


class IDebtOps:
    """信用取引・クイズ・預かり金・ウォレットが利用する借金・種銭操作契約。"""

    def borrow_seed_for(self, guild_id: int, user_id: int) -> tuple[Any, ...]:
        raise NotImplementedError

    def can_borrow_for_bet_for(self, guild_id: int, user_id: int, need: int) -> tuple[Any, ...]:
        raise NotImplementedError

    def seed_for(self, guild_id: int, user_id: int, amount: int) -> None:
        raise NotImplementedError

    def topup_seed_for(self, guild_id: int, user_id: int) -> int:
        raise NotImplementedError

    def owe_for(self, guild_id: int, user_id: int, shortfall: int, reason: str) -> int:
        raise NotImplementedError

    def accrue_debt_interest_for(self, guild_id: int, user_id: int, now: float) -> int:
        raise NotImplementedError


class NoOpDebtOps(IDebtOps):
    """借金操作契約未接続時の LSP 準拠 Null 実装。"""

    def borrow_seed_for(self, guild_id: int, user_id: int) -> tuple[Any, ...]:
        return False, ""

    def can_borrow_for_bet_for(self, guild_id: int, user_id: int, need: int) -> tuple[Any, ...]:
        return False, ""

    def seed_for(self, guild_id: int, user_id: int, amount: int) -> None:
        return None

    def topup_seed_for(self, guild_id: int, user_id: int) -> int:
        return 0

    def owe_for(self, guild_id: int, user_id: int, shortfall: int, reason: str) -> int:
        return 0

    def accrue_debt_interest_for(self, guild_id: int, user_id: int, now: float) -> int:
        return 0


class IDebtRepayHook:
    """債券償還など非収入の入金時に借金・延滞貸付へ全額充当する契約。"""

    def repay_all_of_for(self, guild_id: int, user_id: int, amount: int, label: str) -> int:
        raise NotImplementedError


class NoOpDebtRepayHook(IDebtRepayHook):
    """返済フック未接続時に 0 充当を返す LSP 準拠 Null 実装。"""

    def repay_all_of_for(self, guild_id: int, user_id: int, amount: int, label: str) -> int:
        return 0
