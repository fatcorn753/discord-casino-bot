"""週間ランキング: 週の数え方・発表の文面。Discord と DB に依存しない純粋なロジック。

週は月曜0時（設定の時差）から日曜の終わりまで。先週の「ゲームの稼ぎ（損益の合計）」1位に、限定称号が贈られる。
"""

import datetime

MIN_PLAYS = 5            # 王者になるのに必要な、その週のゲーム数（1回だけの運で決まらないように）
ANNOUNCE_TOP = 5         # 発表に載せる人数
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


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


def standings_lines(rows: list, viewer_id: int = None) -> list:
    """[(user_id, 損益, ゲーム数), ...]（順位順）から、表示の行を作る。"""
    lines = []
    for rank, (uid, net, plays) in enumerate(rows, 1):
        mark = MEDALS.get(rank, f"`{rank:>2}`")
        me = "　◀ あなた" if viewer_id == uid else ""
        lines.append(f"{mark} <@{uid}>　**{net:+,}ペリカ**（{plays}戦）{me}")
    return lines
