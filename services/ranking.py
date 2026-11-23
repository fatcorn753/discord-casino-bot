"""ランキング（`/ペリカ ランキング`・掲示板・週間発表）の読み取り専用の問い合わせ。"""

from domain.ports.repos import IAssetRepo, IWalletRepo
from domain.types import asset_groups


class RankingService:
    """ランキングの描画に要る値だけを読む（書き込みは持たない）。for_guild でサーバーを絞る。"""

    def __init__(self, wallet: IWalletRepo, assets: IAssetRepo) -> None:
        self._wallet = wallet
        self._assets = assets

    def for_guild(self, guild_id: int) -> "RankingService":
        return RankingService(self._wallet.for_guild(guild_id), self._assets.for_guild(guild_id))

    def wealthy_ids(self) -> set[int]:
        return self._wallet.wealthy_ids()

    def display_status(self, user_id: int, wealthy: set[int] | None = None) -> str:
        return self._wallet.display_status(user_id, wealthy)

    def net_assets(self, user_ids: set[int] | None = None) -> list[tuple[int, int, dict[str, int]]]:
        """[(ユーザーID, 純資産, 内訳)] を多い順に。"""
        # 富豪の判定は、借金の利息（トイチ）を計上してから数える。先に呼んで、負債にも利息を反映させる
        self._wallet.wealthy_ids()
        return self._assets.net_assets(user_ids)

    def ranked_assets(self, measure: str, user_ids: set[int] | None = None) -> list[tuple[int, int, dict[str, int]]]:
        """[(ユーザーID, 金額, 区分の小計)] を多い順に（同額はユーザーID順）。

        measure は "net"（純資産 = 総資産 − 負債）か "gross"（総資産 = 流動資産 + 固定資産）。
        """
        rows = [(uid, asset_groups(parts)) for uid, _net, parts in self.net_assets(user_ids)]
        return sorted(((uid, g[measure], g) for uid, g in rows), key=lambda t: (-t[1], t[0]))
