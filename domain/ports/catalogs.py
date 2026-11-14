"""カタログ提供の抽象契約（DIP: domain が infra.catalogs や YAML I/O に直接依存しないため）。"""

from domain.ports.catalog_types import (
    BondsCatalogProto,
    EconomyCatalogProto,
    EventsCatalogProto,
    GamesCatalogProto,
    HorseCatalogProto,
    LaborCatalogProto,
    MarketCatalogProto,
    MissionsCatalogProto,
    RelicsCatalogProto,
    ShopCatalogProto,
    SkinsCatalogProto,
    TeiaiCatalogProto,
)


class ICatalogs:
    """ドメイン層が必要とするカタログ設定の読み取りインターフェース。"""

    def shop(self) -> ShopCatalogProto:
        raise NotImplementedError

    def skins(self) -> SkinsCatalogProto:
        raise NotImplementedError

    def relics(self) -> RelicsCatalogProto:
        raise NotImplementedError

    def bonds(self) -> BondsCatalogProto:
        raise NotImplementedError

    def missions(self) -> MissionsCatalogProto:
        raise NotImplementedError

    def events(self) -> EventsCatalogProto:
        raise NotImplementedError

    def labor(self) -> LaborCatalogProto:
        raise NotImplementedError

    def horse(self) -> HorseCatalogProto:
        raise NotImplementedError

    def market(self) -> MarketCatalogProto:
        raise NotImplementedError

    def teiai(self) -> TeiaiCatalogProto:
        raise NotImplementedError

    def economy(self) -> EconomyCatalogProto:
        raise NotImplementedError

    def games(self) -> GamesCatalogProto:
        raise NotImplementedError


_PROVIDER: ICatalogs | None = None


def configure_catalogs(provider: ICatalogs) -> None:
    """Composition Root（main.py / manage.py / tests）から具象カタログ実装を注入する。"""
    global _PROVIDER
    _PROVIDER = provider


def get_catalogs() -> ICatalogs:
    """設定済みのカタログプロバイダを返す。未設定時は明示的にエラーを出す。"""
    if _PROVIDER is None:
        raise RuntimeError(
            "カタログプロバイダが未設定です。起動点（main.py / manage.py / conftest.py）で "
            "infra.catalogs.bind_catalogs() を呼んでから domain モジュールを読み込んでください。"
        )
    return _PROVIDER
