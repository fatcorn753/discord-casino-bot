"""ドメイン・永続化層が返す構造化エラー・通知イベント（プレゼンテーション非依存）。"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Failure:
    """失敗理由のコードと文脈値（表示文言・Discord 記法の組み立ては cogs 層が担う）。"""

    code: str
    params: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        if not self.params:
            return self.code
        args = ", ".join(f"{k}={v!r}" for k, v in self.params.items())
        return f"{self.code}({args})"


@dataclass(frozen=True)
class Notice:
    """ユーザー向け通知イベントのコードと文脈値（表示文言は cogs 層で整形する）。"""

    code: str
    params: dict[str, Any] = field(default_factory=dict)


def fail(code: str, **params: Any) -> Failure:
    return Failure(code, params)


def notice(code: str, **params: Any) -> Notice:
    return Notice(code, params)
