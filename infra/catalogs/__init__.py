"""YAML カタログパッケージ（責務別スキーマと ICatalogs 実装）。"""

from domain.ports.catalogs import ICatalogs, configure_catalogs
from infra.catalogs.economy import BondsCatalog, EconomyCatalog, TeiaiCatalog, bonds, economy, teiai
from infra.catalogs.games import (
    EventsCatalog,
    GamesCatalog,
    HorseCatalog,
    LaborCatalog,
    MissionsCatalog,
    events,
    games,
    horse,
    labor,
    missions,
)
from infra.catalogs.loader import CATALOG_DIR, StrictModel, load_yaml_model
from infra.catalogs.market import MarketCatalog, market
from infra.catalogs.shop import RelicsCatalog, ShopCatalog, SkinsCatalog, relics, shop, skins
from infra.catalogs.ui import HelpCatalog, help_pages


class YamlCatalogs(ICatalogs):
    """YAML ファイルから読み込み・キャッシュするカタログ実装。"""

    def shop(self) -> ShopCatalog:
        return shop()

    def skins(self) -> SkinsCatalog:
        return skins()

    def relics(self) -> RelicsCatalog:
        return relics()

    def bonds(self) -> BondsCatalog:
        return bonds()

    def missions(self) -> MissionsCatalog:
        return missions()

    def events(self) -> EventsCatalog:
        return events()

    def labor(self) -> LaborCatalog:
        return labor()

    def horse(self) -> HorseCatalog:
        return horse()

    def market(self) -> MarketCatalog:
        return market()

    def teiai(self) -> TeiaiCatalog:
        return teiai()

    def economy(self) -> EconomyCatalog:
        return economy()

    def games(self) -> GamesCatalog:
        return games()


def bind_catalogs() -> ICatalogs:
    """YAML カタログプロバイダをドメイン境界へ登録する。"""
    provider = YamlCatalogs()
    configure_catalogs(provider)
    return provider


__all__ = [
    "CATALOG_DIR",
    "BondsCatalog",
    "EconomyCatalog",
    "EventsCatalog",
    "GamesCatalog",
    "HelpCatalog",
    "HorseCatalog",
    "LaborCatalog",
    "MarketCatalog",
    "MissionsCatalog",
    "RelicsCatalog",
    "ShopCatalog",
    "SkinsCatalog",
    "StrictModel",
    "TeiaiCatalog",
    "YamlCatalogs",
    "bind_catalogs",
    "bonds",
    "economy",
    "events",
    "games",
    "help_pages",
    "horse",
    "labor",
    "load_yaml_model",
    "market",
    "missions",
    "relics",
    "shop",
    "skins",
    "teiai",
]
