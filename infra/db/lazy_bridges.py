"""Composition Root で循環依存を 1 パスの Constructor DI へ解決するための遅延デリゲート群。"""

from collections.abc import Callable
from typing import Any, Optional

from domain.errors import Failure, Outcome
from domain.ports.bridges import (
    IAssetSeizureStep,
    IBankruptExecutor,
    IBankruptPrepHook,
    IDebtLifecycleHook,
    IExcessLossSink,
    IGraceEnterHook,
    ILiquidationStep,
    IMarginCollateralGuard,
    IMarginRatioReader,
    IOverdueLoanCollector,
    ISeizureValueStep,
    IStatusReader,
    IWealthyReader,
)


class LazyDebtLifecycleHook(IDebtLifecycleHook):
    def __init__(self, target: Callable[[], IDebtLifecycleHook]) -> None:
        self._target = target

    def accrue_interest(self, guild_id: int, user_id: int, now: Optional[float] = None) -> int:
        return self._target().accrue_interest(guild_id, user_id, now)

    def after_debt_change(self, guild_id: int, user_id: int) -> None:
        self._target().after_debt_change(guild_id, user_id)

    def withhold_income(self, guild_id: int, user_id: int, income: int) -> int:
        return self._target().withhold_income(guild_id, user_id, income)


class LazyStatusReader(IStatusReader):
    def __init__(self, target: Callable[[], IStatusReader]) -> None:
        self._target = target

    def status_for(self, guild_id: int, user_id: int, now: float) -> str:
        return self._target().status_for(guild_id, user_id, now)

    def debt_for(self, guild_id: int, user_id: int) -> int:
        return self._target().debt_for(guild_id, user_id)

    def loan_blacklisted_for(self, guild_id: int, user_id: int, now: float) -> bool:
        return self._target().loan_blacklisted_for(guild_id, user_id, now)


class LazyBankruptExecutor(IBankruptExecutor):
    def __init__(self, target: Callable[[], IBankruptExecutor]) -> None:
        self._target = target

    def bankrupt_for(
        self, guild_id: int, user_id: int, forced: bool = False, escape: bool = False
    ) -> Outcome[dict[str, Any]]:
        return self._target().bankrupt_for(guild_id, user_id, forced=forced, escape=escape)


class LazyMarginRatioReader(IMarginRatioReader):
    def __init__(self, target: Callable[[], IMarginRatioReader]) -> None:
        self._target = target

    def margin_ratio_for(self, guild_id: int, user_id: int) -> Optional[float]:
        return self._target().margin_ratio_for(guild_id, user_id)


class LazyGraceEnterHook(IGraceEnterHook):
    def __init__(self, target: Callable[[], IGraceEnterHook]) -> None:
        self._target = target

    def on_grace_enter(self, guild_id: int, user_id: int, now: float) -> None:
        self._target().on_grace_enter(guild_id, user_id, now)


class LazyMarginCollateralGuard(IMarginCollateralGuard):
    def __init__(self, target: Callable[[], IMarginCollateralGuard]) -> None:
        self._target = target

    def check_spot_sell(
        self, guild_id: int, user_id: int, symbol: str, units: Optional[int], have: int, price: float
    ) -> Optional[Failure]:
        return self._target().check_spot_sell(guild_id, user_id, symbol, units, have, price)

    def check_bond_cancel(self, guild_id: int, user_id: int, kind: str, principal: int) -> Optional[Failure]:
        return self._target().check_bond_cancel(guild_id, user_id, kind, principal)


class LazyOverdueLoanCollector(IOverdueLoanCollector):
    def __init__(self, target: Callable[[], IOverdueLoanCollector]) -> None:
        self._target = target

    def collect_overdue_for(self, guild_id: int, user_id: int, budget: int) -> int:
        return self._target().collect_overdue_for(guild_id, user_id, budget)

    def loan_remaining(self, loan: dict[str, Any], now: float) -> int:
        return self._target().loan_remaining(loan, now)

    def has_overdue_for(self, guild_id: int, user_id: int) -> bool:
        return self._target().has_overdue_for(guild_id, user_id)

    def active_loans_for(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        return self._target().active_loans_for(guild_id, user_id)

    def record_bankrupt_payment_for(self, loan_id: int, paid: int) -> None:
        self._target().record_bankrupt_payment_for(loan_id, paid)

    def finalize_bankrupt_loan_for(self, loan_id: int, repaid: bool) -> None:
        self._target().finalize_bankrupt_loan_for(loan_id, repaid)


class LazyAssetSeizureStep(IAssetSeizureStep):
    def __init__(self, target: Callable[[], IAssetSeizureStep]) -> None:
        self._target = target

    def seize(self, guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
        return self._target().seize(guild_id, user_id, need, reason, now)


class LazyLiquidationStep(ILiquidationStep):
    def __init__(self, target: Callable[[], ILiquidationStep]) -> None:
        self._target = target

    def liquidate(self, guild_id: int, user_id: int, now: float) -> None:
        self._target().liquidate(guild_id, user_id, now)


class LazySeizureValueStep(ISeizureValueStep):
    def __init__(self, target: Callable[[], ISeizureValueStep]) -> None:
        self._target = target

    def seize(self, guild_id: int, user_id: int, now: float) -> int:
        return self._target().seize(guild_id, user_id, now)


class LazyBankruptPrepHook(IBankruptPrepHook):
    def __init__(self, target: Callable[[], IBankruptPrepHook]) -> None:
        self._target = target

    def prepare(self, guild_id: int, user_id: int, now: float) -> None:
        self._target().prepare(guild_id, user_id, now)


class LazyWealthyReader(IWealthyReader):
    def __init__(self, target: Callable[[], IWealthyReader]) -> None:
        self._target = target

    def wealthy_ids_for(self, guild_id: int) -> set[int]:
        return self._target().wealthy_ids_for(guild_id)


class LazyExcessLossSink(IExcessLossSink):
    """帝愛共済（#34）は銀行より後に組み立てるので、呼ばれたときに解決する。"""

    def __init__(self, target: Callable[[], IExcessLossSink]) -> None:
        self._target = target

    def charge_excess_for(self, guild_id: int, amount: int, now: float) -> int:
        return self._target().charge_excess_for(guild_id, amount, now)
