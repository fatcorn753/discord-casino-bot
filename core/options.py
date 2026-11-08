"""オプション（爆騰コール・暴落プット）の値付けと数字。Discord と DB に依存しない純粋なロジック。

- 満期は OPTION_HOURS 時間後（固定）、権利行使価格は、買った時点の価格の ±STRIKE_OTM（固定。コールは上、プットは下）。
- 値付けは Black-Scholes（ヨーロピアン）。この市場は、どの銘柄も期待値が変わらない（マルチンゲール）ので、金利は 0 で、ちょうど公正な値段になる。
  C = S·Φ(d1) − K·Φ(d2)、P = K·Φ(−d2) − S·Φ(−d1)、d1 = (ln(S/K) + v/2) / √v、d2 = d1 − √v（v は満期までの分散の合計）。
- 分散 v は、1分ごとの揺れ Σ_ii の合計に、有事（急騰・急落）の分（EVENT_PROB と大きさ）を足したもの。有事を入れないと、安く売りすぎる。
- 買値は公正価格より SPREAD 高く、売値は SPREAD 安い（この差が銀行の収入）。
- 配当は、買った掛け金の PAYOUT_CAP_MULT 倍までに抑える（銀行が、1件の大当たりで崩れないように）。
"""

import math

from core import market

OPTION_HOURS = 6  # 満期までの時間（固定）
STRIKE_OTM = 0.05  # 権利行使価格のずれ（コール = 現在値 ×1.05、プット = 現在値 ×0.95）
TICK_SECONDS = 60  # 価格が動く間隔（cogs/market_cog.py の TICK_MINUTES と同じ）
SPREAD = 0.05  # 買値は公正価格の +5%、売値は −5%
PAYOUT_CAP_MULT = 50  # 配当の上限（掛け金の何倍までか）
MIN_PREMIUM = 100  # 1回の掛け金の下限
MAX_OPEN_PREMIUM = 20_000  # 1人が同時に持てる、有効なオプションの掛け金の合計の上限
YIELD_SHARE = 0.04  # 掛け金のうち、預金者の利回りへ回す割合（残りは、配当に備えて銀行の手元に残る）

CALL, PUT = "call", "put"
KINDS = {
    CALL: ("爆騰コール", "🚀", "上がると儲かる"),
    PUT: ("暴落プット", "📉", "下がると儲かる"),
}


def phi(x: float) -> float:
    """標準正規分布の累積分布関数 Φ(x)。"""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def jump_variance_per_tick(symbol: str) -> float:
    """有事（全銘柄が同じ向きに急騰・急落）による、1分あたりの分散の増え分。大きさは一様分布 EVENT_RANGE の2乗平均。"""
    lo, hi = market.EVENT_RANGE
    mean_sq = (hi**3 - lo**3) / (3 * (hi - lo))
    scale = min(market.EVENT_SCALE_MAX, market.BY_KEY[symbol].sigma / market.CS.sigma)
    return market.EVENT_PROB * mean_sq * scale * scale


def total_variance(symbol: str, seconds: float) -> float:
    """いまから seconds 秒後までの、対数価格の分散の合計（1分ごとの揺れ + 有事）。"""
    ticks = max(0.0, seconds) / TICK_SECONDS
    index = [s.key for s in market.SYMBOLS].index(symbol)
    return ticks * (market.COVARIANCE[index][index] + jump_variance_per_tick(symbol))


def fair_price(kind: str, spot: float, strike: float, variance: float) -> float:
    """1口あたりでなく「価格の単位」での公正価格（1枚あたり）。variance = 0（満期）なら、本源的価値そのもの。"""
    if variance <= 1e-12:
        return max(0.0, spot - strike) if kind == CALL else max(0.0, strike - spot)
    root = math.sqrt(variance)
    d1 = (math.log(spot / strike) + variance / 2) / root
    d2 = d1 - root
    if kind == CALL:
        return spot * phi(d1) - strike * phi(d2)
    return strike * phi(-d2) - spot * phi(-d1)


def strike_for(kind: str, spot: float) -> float:
    """買った時点の価格から決まる、権利行使価格（小数2桁）。"""
    return round(spot * (1 + STRIKE_OTM if kind == CALL else 1 - STRIKE_OTM), 2)


def ask_per_unit(kind: str, symbol: str, spot: float, strike: float, seconds: float) -> float:
    """買うときの、1口あたりの値段（ペリカ）。公正価格 +SPREAD。1口 = 1枚の 1/UNIT。"""
    fair = fair_price(kind, spot, strike, total_variance(symbol, seconds))
    return fair * (1 + SPREAD) / market.UNIT


def bid_per_unit(kind: str, symbol: str, spot: float, strike: float, seconds: float) -> float:
    """売るときの、1口あたりの値段（ペリカ）。公正価格 −SPREAD。"""
    fair = fair_price(kind, spot, strike, total_variance(symbol, seconds))
    return fair * (1 - SPREAD) / market.UNIT


def intrinsic(kind: str, units: int, spot: float, strike: float) -> int:
    """満期の配当（上限をかける前）。"""
    per = max(0.0, spot - strike) if kind == CALL else max(0.0, strike - spot)
    return int(per * units / market.UNIT)
