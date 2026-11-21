"""ショップ・スキン・発掘品カタログの Pydantic スキーマ。"""

import datetime
from collections.abc import Mapping
from functools import lru_cache
from typing import Self

from pydantic import Field, model_validator
from pydantic.dataclasses import dataclass as pydantic_dataclass

from domain.ports.catalog_types.shop import (
    RelicBuffContract,
    RelicsCatalogContract,
    SeasonContract,
    ShopCatalogContract,
    ShopItemContract,
    SkinContract,
    SkinsCatalogContract,
    StockRuleContract,
)
from infra.catalogs.loader import STRICT_CONFIG, load_yaml_model


# ------------------------------------------------------------------ #
# shop.yaml
# ------------------------------------------------------------------ #
@pydantic_dataclass(frozen=True, config=STRICT_CONFIG, kw_only=True)
class ShopItemConfig(ShopItemContract):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    price: int = Field(ge=0)
    value: int | str | None = None
    desc: str = ""
    limited: bool = False
    mark: str = ""


@pydantic_dataclass(frozen=True, config=STRICT_CONFIG, kw_only=True)
class StockRuleConfig(StockRuleContract):
    max: int = Field(gt=0)
    per_hour: float = Field(gt=0)


@pydantic_dataclass(frozen=True, config=STRICT_CONFIG, kw_only=True)
class ShopCatalog(ShopCatalogContract):
    kind_labels: Mapping[str, str]
    role_prefix: str = Field(min_length=1)
    insurance_cap: int = Field(gt=0)
    auto_used: tuple[str, ...]
    items: tuple[ShopItemConfig, ...]
    stock_rules: Mapping[str, StockRuleConfig]
    confirm_from: int = Field(ge=0)
    status_nick_minutes: int = Field(gt=0)
    status_nick_per_guild: int = Field(gt=0)

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
@pydantic_dataclass(frozen=True, config=STRICT_CONFIG, kw_only=True)
class SkinConfig(SkinContract):
    item_id: str = Field(min_length=1)
    games: tuple[str, ...] = Field(min_length=1)
    title_prefix: str
    start_line: str
    win_line: str
    lose_line: str
    dice_mark: str = ""
    role_win: Mapping[str, str] | None = None
    role_lose: Mapping[str, str] | None = None
    roll_speaker: str = ""
    roll_mark: str = ""
    banner: str = ""


def _aware(text: str) -> datetime.datetime:
    at = datetime.datetime.fromisoformat(text)
    if at.tzinfo is None:
        raise ValueError(f"タイムゾーンをつけてください（例: +09:00）: {text}")
    return at


@pydantic_dataclass(frozen=True, config=STRICT_CONFIG, kw_only=True)
class SeasonConfig(SeasonContract):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    start: str
    end: str
    skins: tuple[SkinConfig, ...] = Field(min_length=1)
    daily_title: str = ""
    daily_line: str = ""

    @model_validator(mode="after")
    def _check(self) -> Self:
        if _aware(self.end) <= _aware(self.start):
            raise ValueError(f"季節演出 {self.id}: end は start より後にしてください")
        games = [g for s in self.skins for g in s.games]
        if len(games) != len(set(games)):
            raise ValueError(f"季節演出 {self.id}: 同じゲームに2つの見た目があります")
        return self


@pydantic_dataclass(frozen=True, config=STRICT_CONFIG, kw_only=True)
class SkinsCatalog(SkinsCatalogContract):
    slot_of_game: Mapping[str, str]
    skins: tuple[SkinConfig, ...]
    seasons: tuple[SeasonConfig, ...] = ()


# ------------------------------------------------------------------ #
# relics.yaml
# ------------------------------------------------------------------ #
@pydantic_dataclass(frozen=True, config=STRICT_CONFIG, kw_only=True)
class RelicBuffConfig(RelicBuffContract):
    effect: str = Field(min_length=1)
    description: str = Field(min_length=1)


@pydantic_dataclass(frozen=True, config=STRICT_CONFIG, kw_only=True)
class RelicsCatalog(RelicsCatalogContract):
    buff_rate: float = Field(gt=0)
    merge_count: int = Field(ge=2)
    star_max: int = Field(ge=1)
    buffs: Mapping[str, RelicBuffConfig]


@lru_cache(maxsize=1)
def shop() -> ShopCatalog:
    return load_yaml_model("shop.yaml", ShopCatalog)


@lru_cache(maxsize=1)
def skins() -> SkinsCatalog:
    return load_yaml_model("skins.yaml", SkinsCatalog)


@lru_cache(maxsize=1)
def relics() -> RelicsCatalog:
    return load_yaml_model("relics.yaml", RelicsCatalog)
