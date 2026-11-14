"""YAML カタログの共通ローダーと厳格基底モデル。"""

from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ConfigDict

from infra.paths import CATALOGS_DIR

CATALOG_DIR = CATALOGS_DIR
T = TypeVar("T", bound=BaseModel)


class StrictModel(BaseModel):
    """未定義キーを禁止し、読み込み後は書き換え不可にする基底モデル。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


def load_yaml_model(name: str, schema: type[T], *, root: Path | None = None) -> T:
    """YAML ファイルを読み込み、Pydantic スキーマで検証して返す。"""
    path = (root or CATALOG_DIR) / name
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if raw is None:
        raw = {}
    return schema.model_validate(raw)
