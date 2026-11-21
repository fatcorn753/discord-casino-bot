"""Composition Root で循環依存を 1 パスの Constructor DI へ解決するための遅延デリゲート群。

住み分け:
- 複数メソッド・不変条件を持つ名目的ブリッジ契約（IDebtLifecycleHook / IStatusReader /
  IMarginCollateralGuard / IOverdueLoanCollector）は `LazyBridgeBase` を多重継承した `Lazy*` クラスのまま維持し、
  各メソッドは明示的に定義して _resolve() 経由で実装へ委譲する（__getattr__ にはしない。
  Pyright のオーバーライド検証と IDE 補完を落とさないため）。
- 単一メソッドの純粋コールバック契約（Callable 型エイリアス）は `lazy_*` ファクトリ関数で済む。
  解決した束縛メソッドを呼ぶだけのクロージャを返す（例: `lazy_excess_sink(lambda: kyosai.charge_excess_for)`）。
"""

from collections.abc import Callable, Mapping
from typing import Any, Generic, Optional, TypeVar

from domain.errors import Failure
from domain.ports.bridges import (
    AssetSeizureStep,
    BankruptExecutor,
    BankruptPrepHook,
    ExcessLossSink,
    GraceEnterHook,
    IDebtLifecycleHook,
    IMarginCollateralGuard,
    IOverdueLoanCollector,
    IStatusReader,
    LiquidationStep,
    MarginRatioReader,
    SeizureValueStep,
    WealthyReader,
)
from domain.ports.contracts import LoanRow

T = TypeVar("T")


class LazyBridgeBase(Generic[T]):
    """遅延解決ターゲットを保持する共通基底。

    具象の Lazy* クラスは名目的ブリッジ基底（isinstance 検証の対象）とこの基底を多重継承し、
    各メソッドは明示的に定義して _resolve() 経由で実装へ委譲する（__getattr__ にはしない。
    Pyright のオーバーライド検証と IDE 補完を落とさないため）。
    """

    def __init__(self, target: Callable[[], T]) -> None:
        self._target = target

    def _resolve(self) -> T:
        return self._target()


class LazyDebtLifecycleHook(LazyBridgeBase[IDebtLifecycleHook], IDebtLifecycleHook):
    def accrue_interest(self, guild_id: int, user_id: int, now: Optional[float] = None) -> int:
        return self._resolve().accrue_interest(guild_id, user_id, now)

    def after_debt_change(self, guild_id: int, user_id: int) -> None:
        self._resolve().after_debt_change(guild_id, user_id)

    def withhold_income(self, guild_id: int, user_id: int, income: int) -> int:
        return self._resolve().withhold_income(guild_id, user_id, income)


class LazyStatusReader(LazyBridgeBase[IStatusReader], IStatusReader):
    def status_for(self, guild_id: int, user_id: int, now: float) -> str:
        return self._resolve().status_for(guild_id, user_id, now)

    def debt_for(self, guild_id: int, user_id: int) -> int:
        return self._resolve().debt_for(guild_id, user_id)

    def loan_blacklisted_for(self, guild_id: int, user_id: int, now: float) -> bool:
        return self._resolve().loan_blacklisted_for(guild_id, user_id, now)


class LazyMarginCollateralGuard(LazyBridgeBase[IMarginCollateralGuard], IMarginCollateralGuard):
    def check_spot_sell(
        self, guild_id: int, user_id: int, symbol: str, units: Optional[int], have: int, price: float
    ) -> Optional[Failure]:
        return self._resolve().check_spot_sell(guild_id, user_id, symbol, units, have, price)

    def check_bond_cancel(self, guild_id: int, user_id: int, kind: str, principal: int) -> Optional[Failure]:
        return self._resolve().check_bond_cancel(guild_id, user_id, kind, principal)


class LazyOverdueLoanCollector(LazyBridgeBase[IOverdueLoanCollector], IOverdueLoanCollector):
    def collect_overdue_for(self, guild_id: int, user_id: int, budget: int) -> int:
        return self._resolve().collect_overdue_for(guild_id, user_id, budget)

    def loan_remaining(self, loan: Mapping[str, Any], now: float) -> int:
        return self._resolve().loan_remaining(loan, now)

    def has_overdue_for(self, guild_id: int, user_id: int) -> bool:
        return self._resolve().has_overdue_for(guild_id, user_id)

    def active_loans_for(self, guild_id: int, user_id: int) -> list[LoanRow]:
        return self._resolve().active_loans_for(guild_id, user_id)

    def record_bankrupt_payment_for(self, loan_id: int, paid: int) -> None:
        self._resolve().record_bankrupt_payment_for(loan_id, paid)

    def finalize_bankrupt_loan_for(self, loan_id: int, repaid: bool) -> None:
        self._resolve().finalize_bankrupt_loan_for(loan_id, repaid)


def lazy_bankrupt_executor(resolve: Callable[[], BankruptExecutor]) -> BankruptExecutor:
    return lambda guild_id, user_id, forced, escape: resolve()(guild_id, user_id, forced, escape)


def lazy_margin_ratio_reader(resolve: Callable[[], MarginRatioReader]) -> MarginRatioReader:
    return lambda guild_id, user_id: resolve()(guild_id, user_id)


def lazy_grace_enter_hook(resolve: Callable[[], GraceEnterHook]) -> GraceEnterHook:
    return lambda guild_id, user_id, now: resolve()(guild_id, user_id, now)


def lazy_asset_seizure_step(resolve: Callable[[], AssetSeizureStep]) -> AssetSeizureStep:
    return lambda guild_id, user_id, need, reason, now: resolve()(guild_id, user_id, need, reason, now)


def lazy_liquidation_step(resolve: Callable[[], LiquidationStep]) -> LiquidationStep:
    return lambda guild_id, user_id, now: resolve()(guild_id, user_id, now)


def lazy_seizure_value_step(resolve: Callable[[], SeizureValueStep]) -> SeizureValueStep:
    return lambda guild_id, user_id, now: resolve()(guild_id, user_id, now)


def lazy_bankrupt_prep_hook(resolve: Callable[[], BankruptPrepHook]) -> BankruptPrepHook:
    return lambda guild_id, user_id, now: resolve()(guild_id, user_id, now)


def lazy_wealthy_reader(resolve: Callable[[], WealthyReader]) -> WealthyReader:
    return lambda guild_id: resolve()(guild_id)


def lazy_excess_loss_sink(resolve: Callable[[], ExcessLossSink]) -> ExcessLossSink:
    """帝愛共済（#34）は銀行より後に組み立てるので、呼ばれたときに解決する。"""
    return lambda guild_id, amount, now: resolve()(guild_id, amount, now)
