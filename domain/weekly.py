"""週間ランキング: 週の数え方・発表の文面。Discord と DB に依存しない純粋なロジック。

週は月曜0時（設定の時差）から日曜の終わりまで。先週の「ゲームの稼ぎ（損益の合計）」1位に、限定称号が贈られる。
"""

import datetime

from domain.ports import get_catalogs

_ECON = get_catalogs().economy()
MIN_PLAYS = _ECON.weekly.min_plays  # 王者になるのに必要な、その週のゲーム数（1回だけの運で決まらないように）
ANNOUNCE_TOP = _ECON.weekly.announce_top  # 発表に載せる人数
MEDALS = dict(_ECON.wallet.medals)


def week_start(day: datetime.date) -> datetime.date:
    """その日を含む週の月曜日。"""
    return day - datetime.timedelta(days=day.weekday())


def week_id(day: datetime.date) -> str:
    return week_start(day).isoformat()


def previous_week_id(day: datetime.date) -> str:
    return (week_start(day) - datetime.timedelta(days=7)).isoformat()


def week_label(week: str) -> str:
    """\"2026-10-26\" -> \"10/26〜11/1\"。"""
    start = datetime.date.fromisoformat(week)
    end = start + datetime.timedelta(days=6)
    return f"{start.month}/{start.day}〜{end.month}/{end.day}"
