"""強制執行・自己破産のステップ群のブリッジ契約（名目的基底クラスと NoOp / Composite 実装）。"""

from typing import Optional

from domain.errors import Outcome
from domain.ports.contracts import ActiveBond, SeizedBelongings


class IOptionHoldings:
    """差し押さえ・破産の換金で使う、オプション建玉の照会と決済の契約（options 表を外へ見せない）。"""

    def active_option_ids_for(self, guild_id: int, user_id: int) -> list[int]:
        raise NotImplementedError

    def option_close_locked_for(self, guild_id: int, user_id: int, option_id: int, now: float) -> Outcome[int]:
        raise NotImplementedError


class IBondHoldings:
    """差し押さえ・破産の換金で使う、債券の照会と解約・償却の契約（bonds 表を外へ見せない）。"""

    def active_bonds_for(self, guild_id: int, user_id: int) -> list[ActiveBond]:
        raise NotImplementedError

    def bond_cancel_locked_for(
        self, guild_id: int, user_id: int, bond_id: int, now: Optional[float] = None
    ) -> Outcome[int]:
        raise NotImplementedError

    def bond_write_off_for(self, guild_id: int, user_id: int, bond_id: int, payout: int, now: float) -> None:
        """解約できない債券を、payout だけ払い戻して解約済みにする（破産の換金用。払い戻しは帝愛銀行から）。"""
        raise NotImplementedError


class IShopBelongings:
    """破産で、ショップの持ち物・装備・消耗品をすべて没収する契約（inventory・profile 表を外へ見せない）。"""

    def seize_belongings_for(self, guild_id: int, user_id: int) -> SeizedBelongings:
        raise NotImplementedError


class IAssetSeizureStep:
    """借金返済不足時の強制執行（ファーストロス差し押さえ）の 1 ステップ（OCP）。"""

    def seize(self, guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
        raise NotImplementedError


class NoOpAssetSeizureStep(IAssetSeizureStep):
    """差し押さえステップ未設定時の LSP 準拠 Null 実装。"""

    def seize(self, guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
        return 0


class CompositeAssetSeizureStep(IAssetSeizureStep):
    """複数の IAssetSeizureStep を順に実行し、必要額に達した時点で停止する Composite。"""

    def __init__(self, steps: tuple[IAssetSeizureStep, ...]) -> None:
        self._steps = steps

    def seize(self, guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
        covered = 0
        for step in self._steps:
            if covered >= need:
                break
            covered += step.seize(guild_id, user_id, need - covered, reason, now)
        return covered


class ILiquidationStep:
    """自己破産時の保有金融資産の換金（口座現金への回収）の 1 ステップ（OCP）。"""

    def liquidate(self, guild_id: int, user_id: int, now: float) -> None:
        raise NotImplementedError


class NoOpLiquidationStep(ILiquidationStep):
    """換金ステップ未設定時の LSP 準拠 Null 実装。"""

    def liquidate(self, guild_id: int, user_id: int, now: float) -> None:
        return None


class CompositeLiquidationStep(ILiquidationStep):
    """複数の ILiquidationStep を順に実行する Composite。"""

    def __init__(self, steps: tuple[ILiquidationStep, ...]) -> None:
        self._steps = steps

    def liquidate(self, guild_id: int, user_id: int, now: float) -> None:
        for step in self._steps:
            step.liquidate(guild_id, user_id, now)


class ISeizureValueStep:
    """自己破産時の現物・馬などの競売出品と査定額算出の 1 ステップ（OCP）。"""

    def seize(self, guild_id: int, user_id: int, now: float) -> int:
        raise NotImplementedError


class NoOpSeizureValueStep(ISeizureValueStep):
    """競売査定ステップ未設定時の LSP 準拠 Null 実装。"""

    def seize(self, guild_id: int, user_id: int, now: float) -> int:
        return 0


class CompositeSeizureValueStep(ISeizureValueStep):
    """複数の ISeizureValueStep を順に実行し、合計換金額を返す Composite。"""

    def __init__(self, steps: tuple[ISeizureValueStep, ...]) -> None:
        self._steps = steps

    def seize(self, guild_id: int, user_id: int, now: float) -> int:
        return sum(step.seize(guild_id, user_id, now) for step in self._steps)


class IBankruptPrepHook:
    """自己破産前の資産引き戻しフック（例: P2P出品・入札の引き戻し）。"""

    def prepare(self, guild_id: int, user_id: int, now: float) -> None:
        raise NotImplementedError


class NoOpBankruptPrepHook(IBankruptPrepHook):
    """破産前フック未設定時の LSP 準拠 Null 実装。"""

    def prepare(self, guild_id: int, user_id: int, now: float) -> None:
        return None


class CompositeBankruptPrepHook(IBankruptPrepHook):
    """複数の IBankruptPrepHook を順次実行する Composite。"""

    def __init__(self, hooks: tuple[IBankruptPrepHook, ...]) -> None:
        self._hooks = hooks

    def prepare(self, guild_id: int, user_id: int, now: float) -> None:
        for hook in self._hooks:
            hook.prepare(guild_id, user_id, now)
