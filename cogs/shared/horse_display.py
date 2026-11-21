"""競馬の能力値表示パーツ（horse と p2p の画面間で共有）。"""

from collections.abc import Mapping
from typing import Any

from domain import horse as horse_mod


def bar(value: int) -> str:
    n = max(0, min(8, value // 10))
    return "█" * n + "░" * (8 - n)


def stat_lines(stats: Mapping[str, Any]) -> str:
    return "\n".join(f"`{horse_mod.STAT_NAMES[k]:<4}` {stats[k]:>2} {bar(stats[k])}" for k in horse_mod.STAT_KEYS)
