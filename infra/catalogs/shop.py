"""ショップ・スキン・発掘品カタログの Pydantic スキーマ。"""

from functools import lru_cache
from typing import Self

from pydantic import Field, model_validator

from infra.catalogs.loader import StrictModel, load_yaml_model


# ------------------------------------------------------------------ #
# shop.yaml
# ------------------------------------------------------------------ #
class ShopItemConfig(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    price: int = Field(ge=0)
    value: int | str | None = None
    desc: str = ""
    limited: bool = False
    mark: str = ""


class StockRuleConfig(StrictModel):
    max: int = Field(gt=0)
    per_hour: float = Field(gt=0)


class ShopTabConfig(StrictModel):
    label: str = Field(min_length=1)
    kinds: tuple[str, ...] = ()


class ShopTierConfig(StrictModel):
    max_price: int | None = None
    label: str = Field(min_length=1)


class P2pFilterConfig(StrictModel):
    label: str = Field(min_length=1)
    kind: str | None = None
    mode: str | None = None


class ShopUiConfig(StrictModel):
    slot_of_kind: dict[str, str]
    unequip_names: dict[str, str]
    tabs: dict[str, ShopTabConfig]
    tab_notes: dict[str, str]
    tiers: tuple[ShopTierConfig, ...] = Field(min_length=1)
    section_names: dict[str, str]
    p2p_filters: dict[str, P2pFilterConfig]
    p2p_orders: dict[str, str]


class ShopCatalog(StrictModel):
    kind_labels: dict[str, str]
    role_prefix: str = Field(min_length=1)
    insurance_cap: int = Field(gt=0)
    auto_used: tuple[str, ...]
    items: tuple[ShopItemConfig, ...]
    stock_rules: dict[str, StockRuleConfig]
    confirm_from: int = Field(ge=0)
    status_nick_minutes: int = Field(gt=0)
    status_nick_per_guild: int = Field(gt=0)
    ui: ShopUiConfig

    @model_validator(mode="after")
    def _validate_items(self) -> Self:
        ids = [item.id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("shop.yaml: duplicate item id")
        item_set = set(ids)
        for item in self.items:
            if item.kind not in self.kind_labels:
                raise ValueError(f"shop.yaml: unknown kind {item.kind!r} on {item.id!r}")
        for key in self.auto_used:
            if key not in item_set:
                raise ValueError(f"shop.yaml: auto_used references unknown item {key!r}")
        for key in self.stock_rules:
            if key not in item_set:
                raise ValueError(f"shop.yaml: stock_rules references unknown item {key!r}")
        return self


# ------------------------------------------------------------------ #
# skins.yaml
# ------------------------------------------------------------------ #
class SkinConfig(StrictModel):
    item_id: str = Field(min_length=1)
    games: tuple[str, ...] = Field(min_length=1)
    title_prefix: str
    start_line: str
    win_line: str
    lose_line: str
    dice_mark: str = ""
    role_win: dict[str, str] | None = None
    role_lose: dict[str, str] | None = None
    roll_comment: str = ""
    banner: str = ""


class SkinsCatalog(StrictModel):
    slot_of_game: dict[str, str]
    skins: tuple[SkinConfig, ...]


# ------------------------------------------------------------------ #
# relics.yaml
# ------------------------------------------------------------------ #
class RelicBuffConfig(StrictModel):
    effect: str = Field(min_length=1)
    description: str = Field(min_length=1)


class RelicsCatalog(StrictModel):
    buff_rate: float = Field(gt=0)
    merge_count: int = Field(ge=2)
    star_max: int = Field(ge=1)
    buffs: dict[str, RelicBuffConfig]


@lru_cache(maxsize=1)
def shop() -> ShopCatalog:
    return load_yaml_model("shop.yaml", ShopCatalog)


@lru_cache(maxsize=1)
def skins() -> SkinsCatalog:
    return load_yaml_model("skins.yaml", SkinsCatalog)


@lru_cache(maxsize=1)
def relics() -> RelicsCatalog:
    return load_yaml_model("relics.yaml", RelicsCatalog)
