"""定期決算イベント（毎週火曜 13:30）と、デイリーでもらえる「インサイダー裏情報メモ」。Discord と DB に依存しない純粋なロジック。

- 決算の対象は、TEIO と NUMA のどちらか。月曜 9:00 に、対象と発表の時刻を、みんなに知らせる。
- 発表（火曜 13:30）で、裏の理論価格が、好決算なら上へ・悪決算なら下へ、一気に動く。ときどき、値幅制限を突き抜ける大サプライズ（#15 の張り付き・2連ストップへつながる）。
- 裏情報メモ: デイリーを受け取ると、一定の確率で、決算の方向についての噂が、本人にだけ届く。噂が当たる確率は MEMO_TRUTH（外れは、ガセネタ）。
"""

import datetime
import math
from dataclasses import dataclass

from domain.ports import get_catalogs

_CFG = get_catalogs().market().earnings

SYMBOLS = _CFG.symbols  # 決算の対象になる銘柄
RELEASE_WEEKDAY, RELEASE_TIME = _CFG.release_weekday, _CFG.release_time  # 火曜 13:30
ANNOUNCE_WEEKDAY, ANNOUNCE_TIME = _CFG.announce_weekday, _CFG.announce_time  # 月曜 9:00（予告）
SIZE_RANGE = _CFG.size_range  # ふつうの決算の、理論価格の動き（±）
SURPRISE_PROB = _CFG.surprise_prob  # 大サプライズの確率
SURPRISE_RANGE = _CFG.surprise_range  # 大サプライズの動き（値幅制限 ±30% を突き抜ける）
MEMO_PROB = _CFG.memo_prob  # デイリー1回あたりの、裏情報メモの確率
MEMO_TRUTH = _CFG.memo_truth  # 噂が、決算の方向と合っている確率（デイリーでもらうもの）
SHOP_TRUTH = _CFG.shop_truth  # 帝愛市で買うメモの確度（お金を払うぶん、少し高い）
SHOP_STOCK = _CFG.shop_stock  # 1つの決算に、1つのサーバーで売るメモの数


def _local(ts: float, tz_hours: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone(datetime.timedelta(hours=tz_hours)))


def _at(day: datetime.date, hm: tuple, tz_hours: float) -> float:
    tz = datetime.timezone(datetime.timedelta(hours=tz_hours))
    return datetime.datetime.combine(day, datetime.time(*hm), tzinfo=tz).timestamp()


def next_release(now: float, tz_hours: float) -> float:
    """now より後の、いちばん近い決算発表（火曜 13:30）の時刻。"""
    today = _local(now, tz_hours).date()
    for d in range(0, 8):
        day = today + datetime.timedelta(days=d)
        if day.weekday() == RELEASE_WEEKDAY:
            t = _at(day, RELEASE_TIME, tz_hours)
            if t > now:
                return t
    raise AssertionError("unreachable")


def announce_time(release_at: float, tz_hours: float) -> float:
    """その発表の、予告の時刻（直前の月曜 9:00）。"""
    day = _local(release_at, tz_hours).date() - datetime.timedelta(days=(RELEASE_WEEKDAY - ANNOUNCE_WEEKDAY) % 7)
    return _at(day, ANNOUNCE_TIME, tz_hours)


def plan(now: float, tz_hours: float, rng) -> dict:
    """次の決算を決める。{symbol, announce_at, release_at, direction（1 好決算 / -1 悪決算）, size}。結果は、ここで決まっている。"""
    release = next_release(now, tz_hours)
    surprise = rng.random() < SURPRISE_PROB
    return {
        "symbol": rng.choice(SYMBOLS),
        "announce_at": announce_time(release, tz_hours),
        "release_at": release,
        "direction": 1 if rng.random() < 0.5 else -1,
        "size": round(rng.uniform(*(SURPRISE_RANGE if surprise else SIZE_RANGE)), 3),
    }


def factor(direction: int, size: float) -> float:
    """理論価格にかける倍率。上がる分と下がる分が、同じ対数の大きさ（期待値が偏らない）。"""
    return math.exp(direction * math.log1p(size))


def jump_variance() -> float:
    """決算の発表で、対数価格が動く大きさの2乗の期待値 E[ln(1+size)²]（ふつうの決算と大サプライズを、確率で混ぜたもの）。

    オプションの値段に、決算を含む期間の分として足す（足さないと、決算をまたぐオプションが、ただ同然になる）。
    """

    def mean_sq(lo: float, hi: float, n: int = 200) -> float:
        return sum(math.log1p(lo + (hi - lo) * (i + 0.5) / n) ** 2 for i in range(n)) / n

    return (1 - SURPRISE_PROB) * mean_sq(*SIZE_RANGE) + SURPRISE_PROB * mean_sq(*SURPRISE_RANGE)


def memo_direction(direction: int, rng, truth: float = MEMO_TRUTH) -> int:
    """噂が言う方向。truth の確率で本当の方向、それ以外はガセ（逆）。"""
    return direction if rng.random() < truth else -direction


@dataclass(frozen=True)
class MemoInfo:
    """裏情報メモの構造化データ（Discord 記法による文面組み立ては cogs 側で行う）。"""

    symbol_name: str
    hint: int
    release_at: float
    truth: float


def build_memo(symbol_name: str, hint: int, release_at: float, truth: float = MEMO_TRUTH) -> MemoInfo:
    return MemoInfo(symbol_name=symbol_name, hint=hint, release_at=release_at, truth=truth)
