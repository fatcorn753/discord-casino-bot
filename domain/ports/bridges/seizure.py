"""強制執行・自己破産のステップ群のブリッジ契約。

住み分け:
- 単一メソッドの純粋コールバック契約（AssetSeizureStep / LiquidationStep / SeizureValueStep /
  BankruptPrepHook / ShopBelongings）は `Callable` 型エイリアス＋ `noop_*` / `compose_*` 関数で表す。
- 複数メソッド・表の隠蔽という不変条件を持つ契約（IOptionHoldings / IBondHoldings）は名目的基底クラスのまま維持する。
"""

from collections.abc import Callable

from domain.errors import Outcome
from domain.ports.contracts import ActiveBond, SeizedBelongings

# 単一コールバック契約（Callable 型エイリアス）: 静的型だけで LSP を 100% 保証できる純粋契約
ShopBelongings = Callable[[int, int], SeizedBelongings]
AssetSeizureStep = Callable[[int, int, int, str, float], int]
LiquidationStep = Callable[[int, int, float], None]
SeizureValueStep = Callable[[int, int, float], int]
BankruptPrepHook = Callable[[int, int, float], None]


def noop_asset_seizure_step(guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
    """差し押さえステップ未設定時の LSP 準拠の既定動作。"""
    return 0


def compose_asset_seizure_steps(steps: tuple[AssetSeizureStep, ...]) -> AssetSeizureStep:
    """複数の差し押さえステップを順に実行し、必要額に達した時点で停止する合成ステップを返す（OCP）。"""

    def seize(guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
        covered = 0
        for step in steps:
            if covered >= need:
                break
            covered += step(guild_id, user_id, need - covered, reason, now)
        return covered

    return seize


def noop_liquidation_step(guild_id: int, user_id: int, now: float) -> None:
    """換金ステップ未設定時の LSP 準拠の既定動作。"""
    return None


def compose_liquidation_steps(steps: tuple[LiquidationStep, ...]) -> LiquidationStep:
    """複数の換金ステップを順に実行する合成ステップを返す（OCP）。"""

    def liquidate(guild_id: int, user_id: int, now: float) -> None:
        for step in steps:
            step(guild_id, user_id, now)

    return liquidate


def noop_seizure_value_step(guild_id: int, user_id: int, now: float) -> int:
    """競売査定ステップ未設定時の LSP 準拠の既定動作。"""
    return 0


def compose_seizure_value_steps(steps: tuple[SeizureValueStep, ...]) -> SeizureValueStep:
    """複数の競売査定ステップを順に実行し、合計換金額を返す合成ステップを返す（OCP）。"""

    def seize(guild_id: int, user_id: int, now: float) -> int:
        return sum(step(guild_id, user_id, now) for step in steps)

    return seize


def noop_bankrupt_prep_hook(guild_id: int, user_id: int, now: float) -> None:
    """破産前フック未設定時の LSP 準拠の既定動作。"""
    return None


def compose_bankrupt_prep_hooks(hooks: tuple[BankruptPrepHook, ...]) -> BankruptPrepHook:
    """複数の破産前フックを順次実行する合成フックを返す（OCP）。"""

    def prepare(guild_id: int, user_id: int, now: float) -> None:
        for hook in hooks:
            hook(guild_id, user_id, now)

    return prepare


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
        self, guild_id: int, user_id: int, bond_id: int, now: float | None = None
    ) -> Outcome[int]:
        raise NotImplementedError

    def bond_write_off_for(self, guild_id: int, user_id: int, bond_id: int, payout: int, now: float) -> None:
        """解約できない債券を、payout だけ払い戻して解約済みにする（破産の換金用。払い戻しは帝愛銀行から）。"""
        raise NotImplementedError
