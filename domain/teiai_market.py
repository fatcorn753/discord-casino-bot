"""帝愛の出品（#72）。差し押さえ品の競売とは別に、帝愛が期間限定でショップの品を出す。

- 1日（UTC）に MARKET_SLOTS_PER_DAY 回、ランダムな時刻に1件ずつ出す。その日の計画は、その日に初めて棚を見たときに決める
- 競売: 最初の値段（定価の MARKET_AUCTION_RATE）から、期間の終わりに MARKET_AUCTION_END 倍まで一定の速さで下がる。1つだけ
- 定価販売: 定価の MARKET_FIXED_RATE の値段で、期間中ずっと同じ。消耗品なら MARKET_FIXED_QTY 個
- 闇市品（MARKET_BLACK_PROB）: 中身は未鑑定。買うと、まれに別の品（安い品 MARKET_BLACK_WORSE・高い品 MARKET_BLACK_BETTER）になる。返品はできない
- 発掘品の競り: 1日に RELIC_AUCTION_PER_DAY 回、帝愛の競売（プレイヤー間の市場）に発掘品を1つ、競りで出す（pick_relic）

DB にも Discord にも触らない。乱数は呼び出し側から渡す。
"""

import random
from collections.abc import Callable
from dataclasses import dataclass

from domain import labor, teiai
from domain import shop as shop_mod

DAY = 86400
SAME, WORSE, BETTER = "same", "worse", "better"
AUCTION, FIXED = "auction", "fixed"


def pool(listable: Callable[[str], bool]) -> list[shop_mod.Item]:
    """帝愛が出せる品。差し押さえ品の競売と同じ条件（限定品・コレクション・称号は出さない）。"""
    return [it for it in shop_mod.BY_ID.values() if listable(it.id)]


@dataclass(frozen=True)
class Listing:
    """出品の計画（DB の teiai_market の1行に対応）。"""

    item_id: str
    mode: str
    qty: int
    start_price: int
    end_price: int
    black: bool
    listed_at: float
    expires_at: float


def day_start(now: float) -> float:
    return now - now % DAY


def plan_day(day: float, rng: random.Random, items: list[shop_mod.Item]) -> list[Listing]:
    """その日（day は UTC の0時の時刻）の出品の計画。時刻の早い順。"""
    if not items:
        return []
    out = []
    for _ in range(teiai.MARKET_SLOTS_PER_DAY):
        it = rng.choice(items)
        at = day + rng.uniform(0, DAY)
        if rng.random() < teiai.MARKET_AUCTION_PROB:
            start = max(1, round(it.price * rng.uniform(*teiai.MARKET_AUCTION_RATE)))
            end = max(1, round(start * teiai.MARKET_AUCTION_END))
            hours = rng.uniform(*teiai.MARKET_AUCTION_HOURS)
            mode, qty = AUCTION, 1
        else:
            start = end = max(1, round(it.price * rng.uniform(*teiai.MARKET_FIXED_RATE)))
            hours = teiai.MARKET_FIXED_HOURS
            lo, hi = teiai.MARKET_FIXED_QTY
            mode, qty = FIXED, 1 if it.unique else rng.randint(lo, hi)
        black = rng.random() < teiai.MARKET_BLACK_PROB
        out.append(Listing(it.id, mode, qty, start, end, black, at, at + hours * 3600))
    return sorted(out, key=lambda x: x.listed_at)


def price_at(start_price: int, end_price: int, listed_at: float, expires_at: float, now: float) -> int:
    """いまの1つあたりの値段。競売は期間中に一定の速さで下がる（定価販売は start == end なので変わらない）。"""
    span = max(1.0, expires_at - listed_at)
    t = min(1.0, max(0.0, (now - listed_at) / span))
    return max(1, round(start_price + (end_price - start_price) * t))


def reveal(item_id: str, rng: random.Random, items: list[shop_mod.Item]) -> tuple[str, str]:
    """闇市品を買ったときの、本当の中身。(品ID, SAME / WORSE / BETTER)。

    安い品・高い品は、同じ種類の中から選ぶ。同じ種類に無ければ、ほかの種類から。それも無ければ表示どおり。
    """
    shown = shop_mod.BY_ID[item_id]
    roll = rng.random()
    if roll < teiai.MARKET_BLACK_WORSE:
        outcome = WORSE
    elif roll < teiai.MARKET_BLACK_WORSE + teiai.MARKET_BLACK_BETTER:
        outcome = BETTER
    else:
        return item_id, SAME
    pick = _neighbor(shown, outcome, items, rng, same_kind=True) or _neighbor(
        shown, outcome, items, rng, same_kind=False
    )
    return (pick.id, outcome) if pick is not None else (item_id, SAME)


def _neighbor(
    shown: shop_mod.Item, outcome: str, items: list[shop_mod.Item], rng: random.Random, same_kind: bool
) -> shop_mod.Item | None:
    cands = [
        it
        for it in items
        if it.id != shown.id
        and (not same_kind or it.kind == shown.kind)
        and (it.price < shown.price if outcome == WORSE else it.price > shown.price)
    ]
    if not cands:
        return None
    # 「ダイヤのはずが銅」「ドリンクのはずが宝石」のような当たり外れも出るよう、候補から均等に選ぶ
    return rng.choice(sorted(cands, key=lambda it: it.id))


# ---- 発掘品の競り（帝愛の競売＝プレイヤー間の市場に、帝愛が出す）----
TEIAI_SELLER = 0  # 市場の出品者が帝愛のときの seller_id（Discord のユーザーID は 0 にならない）


def pick_relic(rng: random.Random) -> tuple[str, int]:
    """帝愛が競りに出す発掘品と、開始価格（目安の価値＝定価）。掘れる確率に比例して選ぶ（★なし）。"""
    rid = rng.choices([r[0] for r in labor.RELICS], weights=[r[3] for r in labor.RELICS])[0]
    item_id = labor.RELIC_ID[rid]
    return item_id, shop_mod.RELIC_VALUE[item_id]
