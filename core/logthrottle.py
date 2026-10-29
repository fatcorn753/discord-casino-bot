"""同じ原因のログを、毎分ずっと出し続けないための間引き。"""

import time

_last: dict = {}


def warn(key, message: str, interval: float = 1800) -> None:
    """key ごとに、interval 秒に1回だけ表示する（権限不足などが直るまで、毎分のログで埋まらないように）。"""
    now = time.monotonic()
    if now - _last.get(key, -interval) >= interval:
        _last[key] = now
        print(message)
