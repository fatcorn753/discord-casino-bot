"""信用取引・担保ガード・ランキング評価額のブリッジ契約（名目的基底クラスと NoOp / Composite 実装）。"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from domain.errors import Failure

AddAssetPart = Callable[[int, str, int], None]


class IMarginRatioReader:
    """対象ユーザーの信用取引維持率を参照する契約。"""

    def margin_ratio_for(self, guild_id: int, user_id: int) -> Optional[float]:
        raise NotImplementedError


class NoOpMarginRatioReader(IMarginRatioReader):
    """維持率未接続時に None を返す LSP 準拠 Null 実装。"""

    def margin_ratio_for(self, guild_id: int, user_id: int) -> Optional[float]:
        return None


class IMarginCollateralGuard:
    """現物株・債券を売るときに信用取引の維持率低下を防ぐガード契約。"""

    def check_spot_sell(
        self, guild_id: int, user_id: int, symbol: str, units: Optional[int], have: int, price: float
    ) -> Optional["Failure"]:
        raise NotImplementedError

    def check_bond_cancel(self, guild_id: int, user_id: int, kind: str, principal: int) -> Optional["Failure"]:
        raise NotImplementedError


class NoOpMarginCollateralGuard(IMarginCollateralGuard):
    """担保ガード未接続時に制約なし (None) を返す LSP 準拠 Null 実装。"""

    def check_spot_sell(
        self, guild_id: int, user_id: int, symbol: str, units: Optional[int], have: int, price: float
    ) -> Optional["Failure"]:
        return None

    def check_bond_cancel(self, guild_id: int, user_id: int, kind: str, principal: int) -> Optional["Failure"]:
        return None


class IWealthyReader:
    """サーバ内の富豪ユーザーID集合を取得する契約。"""

    def wealthy_ids_for(self, guild_id: int) -> set[int]:
        raise NotImplementedError


class NoOpWealthyReader(IWealthyReader):
    """富豪判定未接続時に空集合を返す LSP 準拠 Null 実装。"""

    def wealthy_ids_for(self, guild_id: int) -> set[int]:
        return set()


class IValuationSource:
    """ランキング純資産に合算する 1 種類の資産評価額ソース（Composite 対象）。"""

    def collect_values(self, guild_id: int, totals: dict[int, int]) -> None:
        raise NotImplementedError


class CompositeValuationSource(IValuationSource):
    """複数の IValuationSource を束ねて純資産ランキング評価額を集計する Composite。"""

    def __init__(self, sources: tuple[IValuationSource, ...]) -> None:
        self._sources = sources

    def collect_values(self, guild_id: int, totals: dict[int, int]) -> None:
        for source in self._sources:
            source.collect_values(guild_id, totals)


class IAssetCollector:
    """1 つの資産カテゴリを内訳辞書へ加算するコレクタ契約（OCP / Composite）。"""

    def collect(self, guild_id: int, add: AddAssetPart, now: float) -> None:
        raise NotImplementedError


class CompositeAssetCollector(IAssetCollector):
    """複数の IAssetCollector を束ねて順次適用する Composite。"""

    def __init__(self, collectors: tuple[IAssetCollector, ...]) -> None:
        self._collectors = collectors

    def collect(self, guild_id: int, add: AddAssetPart, now: float) -> None:
        for collector in self._collectors:
            collector.collect(guild_id, add, now)
