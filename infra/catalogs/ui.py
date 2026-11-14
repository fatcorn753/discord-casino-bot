"""プレゼンテーション専用（/help 文面等）の YAML カタログスキーマ。"""

from functools import lru_cache
from typing import Self

from pydantic import Field, model_validator

from infra.catalogs.loader import StrictModel, load_yaml_model


# ------------------------------------------------------------------ #
# help.yaml
# ------------------------------------------------------------------ #
class HelpHomeConfig(StrictModel):
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    footer_template: str = Field(min_length=1)


class HelpButtonConfig(StrictModel):
    key: str = Field(min_length=1)
    label: str = Field(min_length=1)


class HelpPageConfig(StrictModel):
    title: str = Field(min_length=1)
    lines: tuple[str, ...] = Field(min_length=1)


class HelpCatalog(StrictModel):
    home: HelpHomeConfig
    buttons: tuple[HelpButtonConfig, ...] = Field(min_length=1)
    pages: dict[str, HelpPageConfig]

    @model_validator(mode="after")
    def _validate_buttons(self) -> Self:
        for btn in self.buttons:
            if btn.key != "home" and btn.key not in self.pages:
                raise ValueError(f"help.yaml: button references unknown page {btn.key!r}")
        return self


@lru_cache(maxsize=1)
def help_pages() -> HelpCatalog:
    return load_yaml_model("help.yaml", HelpCatalog)
