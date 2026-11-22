"""同じ原因のログを、毎分ずっと出し続けないための間引き。"""

import logging
import time
from collections.abc import Hashable

_LOG = logging.getLogger(__name__)
_last: dict[Hashable, float] = {}


def warn(key: Hashable, message: str, interval: float = 1800, logger: logging.Logger | None = None) -> None:
    """key ごとに、interval 秒に1回だけ警告を記録する（権限不足などが直るまで、毎分のログで埋まらないように）。"""
    now = time.monotonic()
    if now - _last.get(key, -interval) >= interval:
        _last[key] = now
        (logger or _LOG).warning(message)
