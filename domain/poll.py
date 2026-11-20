"""投票（/投票）の数字と、表示する票数の計算。"""

from domain.ports import get_catalogs

_CFG = get_catalogs().economy().poll
RIG_COST = _CFG.rig_cost  # 票数を書き換える代金
DEFAULT_MINUTES = _CFG.default_minutes
MAX_MINUTES = _CFG.max_minutes
MAX_OPEN_PER_USER = _CFG.max_open_per_user
TITLE_MAX = _CFG.title_max
MAX_COUNT = _CFG.max_count


def shown(real: int, adj: int) -> int:
    """表示する票数。書き換えのあとで本物の票が減っても、マイナスにはしない。"""
    return max(0, real + adj)
