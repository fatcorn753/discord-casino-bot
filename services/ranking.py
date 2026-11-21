"""ランキング（`/ペリカ ランキング`・掲示板・週間発表）の読み取り専用の問い合わせ。"""


from domain.ports.contracts import RankEntry
from domain.ports.repos import IAssetRepo, IBondRepo, ICryptoRepo, IWalletRepo


class RankingService:
    """ランキングの描画に要る値だけを読む（書き込みは持たない）。for_guild でサーバーを絞る。"""

    def __init__(self, wallet: IWalletRepo, crypto: ICryptoRepo, bonds: IBondRepo, assets: IAssetRepo) -> None:
        self._wallet = wallet
        self._crypto = crypto
        self._bonds = bonds
        self._assets = assets

    def for_guild(self, guild_id: int) -> "RankingService":
        return RankingService(
            self._wallet.for_guild(guild_id),
            self._crypto.for_guild(guild_id),
            self._bonds.for_guild(guild_id),
            self._assets.for_guild(guild_id),
        )

    def top(self, limit: int, user_ids: set[int] | None = None) -> list[RankEntry]:
        return self._wallet.top(limit, user_ids)

    def rank_of(self, user_id: int, user_ids: set[int] | None = None) -> RankEntry | None:
        return self._wallet.rank_of(user_id, user_ids)

    def wealthy_ids(self) -> set[int]:
        return self._wallet.wealthy_ids()

    def display_status(self, user_id: int, wealthy: set[int] | None = None) -> str:
        return self._wallet.display_status(user_id, wealthy)

    def crypto_values(self) -> dict[int, int]:
        return self._crypto.crypto_values()

    def bond_values(self) -> dict[int, int]:
        return self._bonds.bond_values()

    def final_assets(self, user_ids: set[int] | None = None) -> list[tuple[int, int, dict[str, int]]]:
        return self._assets.final_assets(user_ids)
