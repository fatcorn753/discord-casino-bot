"""信用取引・担保ガード・ランキング評価額のブリッジ契約。

住み分け:
- 単一メソッドの純粋コールバック契約（MarginRatioReader / WealthyReader / ValuationSource /
  AssetCollector）は `Callable` 型エイリアス＋ `noop_*` / `compose_*` 関数で表す。
- 複数メソッド・不変条件を持つ契約（IMarginCollateralGuard）は名目的基底クラスのまま維持する。
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from domain.errors import Failure

AddAssetPart = Callable[[int, str, int], None]

# 単一コールバック契約（Callable 型エイリアス）: 静的型だけで LSP を 100% 保証できる純粋契約
MarginRatioReader = Callable[[int, int], float | None]
WealthyReader = Callable[[int], set[int]]
ValuationSource = Callable[[int, dict[int, int]], None]
AssetCollector = Callable[[int, AddAssetPart, float], None]


def noop_margin_ratio_reader(guild_id: int, user_id: int) -> float | None:
    """維持率未接続時に None を返す LSP 準拠の既定動作。"""
    return None


def noop_wealthy_reader(guild_id: int) -> set[int]:
    """富豪判定未接続時に空集合を返す LSP 準拠の既定動作。"""
    return set()


def compose_valuation_sources(sources: tuple[ValuationSource, ...]) -> ValuationSource:
    """複数の評価額ソースを順次束ねて純資産ランキング評価額を集計する合成ソースを返す（OCP）。"""

    def collect(guild_id: int, totals: dict[int, int]) -> None:
        for source in sources:
            source(guild_id, totals)

    return collect


def compose_asset_collectors(collectors: tuple[AssetCollector, ...]) -> AssetCollector:
    """複数の資産コレクタを順次適用する合成コレクタを返す（OCP）。"""

    def collect(guild_id: int, add: AddAssetPart, now: float) -> None:
        for collector in collectors:
            collector(guild_id, add, now)

    return collect


class IMarginCollateralGuard:
    """現物株・債券を売るときに信用取引の維持率低下を防ぐガード契約。"""

    def check_spot_sell(
        self, guild_id: int, user_id: int, symbol: str, units: int | None, have: int, price: float
    ) -> Optional["Failure"]:
        raise NotImplementedError

    def check_bond_cancel(self, guild_id: int, user_id: int, kind: str, principal: int) -> Optional["Failure"]:
        raise NotImplementedError


class NoOpMarginCollateralGuard(IMarginCollateralGuard):
    """担保ガード未接続時に制約なし (None) を返す LSP 準拠 Null 実装。"""

    def check_spot_sell(
        self, guild_id: int, user_id: int, symbol: str, units: int | None, have: int, price: float
    ) -> Optional["Failure"]:
        return None

    def check_bond_cancel(self, guild_id: int, user_id: int, kind: str, principal: int) -> Optional["Failure"]:
        return None
