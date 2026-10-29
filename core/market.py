"""CSトークンの市場。価格の動き・売買の計算・文字のチャート。Discord と DB に依存しない純粋なロジック。

価格は全サーバー共通の1つ。1分ごとにランダムに動き、期待値は変わらない（平均への引き戻しなどの偏りも入れない）ので、先を読んで儲けることはできない。
売買の手数料は今はかけていない（FEE_RATE で変えられる）。ペリカもトークンも、現実のお金とは関係ない。
"""

import math
import random
from typing import Optional

NAME = "CSトークン"
START_PRICE = 1_000.0
MIN_PRICE = 20
MAX_PRICE = 50_000
SIGMA = 0.004                # 1分あたりの値動きの大きさ（約±0.4%。1日で±15〜20%ほど）
EVENT_PROB = 1 / 720         # 1日に2回ほど、急騰・急落が起こる
EVENT_RANGE = (0.03, 0.08)   # 急騰・急落の大きさ（±3〜8%）
UNIT = 1_000                 # 1枚 = 1000口。小数で買えるよう、保有は口で数える
FEE_RATE = 0.0               # 買うときと売るときに、それぞれ取引額にかかる割合（今は無料）


def next_price(price: float, rng: random.Random) -> tuple:
    """1分後の価格と、起きた出来事（+1 急騰 / -1 急落 / 0 なし）。

    価格の期待値がちょうど変わらない（マルチンゲール）ように作っている。
    ふつうの揺れは対数の補正（-σ²/2）、急騰・急落は ×(1+x) と ×(1-x) を同じ確率にして打ち消す。
    平均への引き戻しなどの偏りを入れると、「安いときに買えば得」という必勝法が生まれてしまう。
    """
    factor = math.exp(-SIGMA * SIGMA / 2 + SIGMA * rng.gauss(0, 1))
    event = 0
    if rng.random() < EVENT_PROB:
        event = 1 if rng.random() < 0.5 else -1
        factor *= 1 + event * rng.uniform(*EVENT_RANGE)
    new = round(price * factor, 2)                    # 低い価格でも動きが止まらないよう、小数2桁まで持つ
    return min(MAX_PRICE, max(MIN_PRICE, new)), event


def fmt(price: float) -> str:
    """価格の表示。100未満は小数2桁、それ以上は整数。"""
    return f"{price:,.2f}" if price < 100 else f"{price:,.0f}"


def _fee(gross: int) -> int:
    return math.ceil(gross * FEE_RATE)


def buy_quote(amount: int, price: float) -> tuple:
    """amount ペリカの範囲で買える最大の口数。(口数, 支払い総額, うち手数料)。買えなければ (0, 0, 0)。"""
    units = int(amount * UNIT // (price * (1 + FEE_RATE)))
    while units > 0:
        gross = math.ceil(round(units * price / UNIT, 6))
        fee = _fee(gross)
        if gross + fee <= amount:
            return units, gross + fee, fee
        units -= 1
    return 0, 0, 0


def sell_quote(units: int, price: float) -> tuple:
    """units 口を売ったときの (受け取る額, 手数料)。"""
    gross = int(round(units * price / UNIT, 6) // 1)         # 小数の誤差で1ペリカずれないよう丸めてから切り捨てる
    fee = _fee(gross)
    return max(0, gross - fee), fee


def avg_price(units: int, cost: int) -> float:
    """取得時の1枚あたりの価格（買った総額 ÷ 枚数）。"""
    return cost * UNIT / units if units > 0 else 0.0


def pnl_percent(price: float, units: int, cost: int) -> float:
    """取得時の価格に対して、いまの価格が何％前後しているか。"""
    avg = avg_price(units, cost)
    return (price - avg) / avg * 100 if avg else 0.0


def pel(price: float) -> str:
    return f"{fmt(price)}{UNIT_NAME}"


UNIT_NAME = "ペリカ"


def lots(units: int) -> str:
    return f"{units / UNIT:,.3f}枚"


# ----------------------------- 文字のチャート ----------------------------- #
LEVELS = " ▁▂▃▄▅▆▇█"
PERIODS = {"1h": ("1時間", 3600), "6h": ("6時間", 6 * 3600), "24h": ("24時間", 24 * 3600), "7d": ("7日", 7 * 24 * 3600)}


def downsample(prices: list, width: int) -> list:
    """価格の列を width 個にまとめる（各区間の最後の値）。足りなければそのまま。"""
    if len(prices) <= width:
        return list(prices)
    return [prices[min(len(prices) - 1, (i + 1) * len(prices) // width - 1)] for i in range(width)]


def render_chart(prices: list, width: int = 32, height: int = 8) -> str:
    """棒の高さで価格を表す面グラフ（コードブロックの中に入れて使う）。縦は height 行 × 8段階。"""
    if not prices:
        return "（まだデータがありません）"
    pts = downsample(prices, width)
    lo, hi = min(pts), max(pts)
    span = hi - lo
    total = height * 8
    levels = [total // 2 if span == 0 else 1 + round((p - lo) / span * (total - 1)) for p in pts]
    label_w = max(len(fmt(hi)), len(fmt(lo)))
    rows = []
    for r in range(height):               # r=0 が一番上
        floor = (height - 1 - r) * 8
        cells = "".join(LEVELS[max(0, min(8, lv - floor))] for lv in levels)
        label = fmt(hi) if r == 0 else (fmt(lo) if r == height - 1 else "")
        rows.append(f"{label:>{label_w}} ┤{cells}")
    rows.append(" " * label_w + " └" + "─" * len(pts))
    return "\n".join(rows)


def summary(prices: list) -> Optional[dict]:
    if not prices:
        return None
    first, last = prices[0], prices[-1]
    return {"last": last, "change": (last - first) / first * 100 if first else 0.0, "high": max(prices), "low": min(prices)}
