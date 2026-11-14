"""突発イベント。1日に1回、1つのゲームで30分だけ、勝ったときの勝ち分が増える。

いつ・どのゲームかは、日付とサーバーIDから決める（保存しなくても、再起動後に同じ結果になる）。
開始は日本時間の12時〜23:30の間で、5分刻み。お金が増えすぎないよう、1回のボーナスには上限がある。
"""

import datetime
import random
from dataclasses import dataclass
from typing import Optional

from core import catalogs

_CFG = catalogs.events()
DURATION_MIN = _CFG.duration_min
EARLIEST_MIN = _CFG.earliest_hour * 60  # 開始の最も早い時刻（12:00）
LATEST_MIN = 24 * 60 - DURATION_MIN  # 開始の最も遅い時刻（23:30。0時をまたがない）
BOOST = _CFG.boost  # 勝ち分（払い戻し - 賭け金）に上乗せする割合
BONUS_CAP = _CFG.bonus_cap  # 1回で上乗せできる上限

# ゲームごとの名前と、告知の一言
GAMES = {k: (v.title, v.blurb) for k, v in _CFG.games.items()}


@dataclass(frozen=True)
class Event:
    game: str
    start_min: int  # その日の0時からの分
    day: datetime.date

    @property
    def end_min(self) -> int:
        return self.start_min + DURATION_MIN

    @property
    def title(self) -> str:
        return GAMES[self.game][0]

    @property
    def blurb(self) -> str:
        return GAMES[self.game][1]

    def clock(self, minutes: int) -> str:
        return f"{minutes // 60}:{minutes % 60:02d}"


def schedule(guild_id: int, day: datetime.date) -> Event:
    """そのサーバーの、その日の突発イベント。"""
    rng = random.Random(f"event:{guild_id}:{day.isoformat()}")
    game = rng.choice(sorted(GAMES))
    start = rng.randrange(EARLIEST_MIN, LATEST_MIN + 1, 5)
    return Event(game, start, day)


def active(guild_id: int, now: datetime.datetime) -> Optional[Event]:
    """いま開催中のイベント（なければ None）。now は日本時間などの現地時刻。"""
    ev = schedule(guild_id, now.date())
    minutes = now.hour * 60 + now.minute
    return ev if ev.start_min <= minutes < ev.end_min else None


def bonus(bet: int, payout: int, boost: float = BOOST) -> int:
    """勝ったときの上乗せ額。負けや引き分けは 0。boost は勝ち分に上乗せする割合（持ち主が開く特別イベントでは変えられる）。"""
    return min(int((payout - bet) * boost), BONUS_CAP) if payout > bet else 0


def best_boost(game: str, scheduled: Optional[Event], manual: Optional[dict]) -> float:
    """そのゲームに今かかる上乗せの割合。定例の突発イベントと、持ち主が開いた特別イベントは重ねず、大きいほうだけ使う。"""
    boosts = [0.0]
    if scheduled is not None and scheduled.game == game:
        boosts.append(BOOST)
    if manual is not None and manual["game"] == game:
        boosts.append(manual["boost"] / 100)
    return max(boosts)
