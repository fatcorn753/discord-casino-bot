"""オプション（爆騰コール・暴落プット）の値付けと数字。Discord と DB に依存しない純粋なロジック。

- 満期は OPTION_HOURS 時間後（固定）、権利行使価格は、買った時点の価格の ±STRIKE_OTM（固定。コールは上、プットは下）。
- 値付けは Black-Scholes（ヨーロピアン）。この市場は、どの銘柄も期待値が変わらない（マルチンゲール）ので、金利は 0 で、ちょうど公正な値段になる。
  C = S·Φ(d1) − K·Φ(d2)、P = K·Φ(−d2) − S·Φ(−d1)、d1 = (ln(S/K) + v/2) / √v、d2 = d1 − √v（v は満期までの分散の合計）。
- 分散 v は、1分ごとの揺れ Σ_ii の合計に、有事（急騰・急落）の分（EVENT_PROB と大きさ）を足したもの。有事を入れないと、安く売りすぎる。
- 買値は公正価格より SPREAD 高く、売値は SPREAD 安い（この差が銀行の収入）。
- 配当は、買った掛け金の PAYOUT_CAP_MULT 倍までに抑える（銀行が、1件の大当たりで崩れないように）。
"""

import math

from core import market, market_events

OPTION_HOURS = 6  # 満期までの時間（固定）
STRIKE_OTM = 0.05  # 権利行使価格のずれ（コール = 現在値 ×1.05、プット = 現在値 ×0.95）
TICK_SECONDS = 60  # 価格が動く間隔（cogs/market_cog.py の TICK_MINUTES と同じ）
SPREAD = 0.015  # 買値は公正価格の +1.5%、売値は −1.5%（往復で約3%。ルーレット・パチンコ並みの控除）
PAYOUT_CAP_MULT = 50  # 配当の上限（掛け金の何倍までか）
MIN_PREMIUM = 100  # 1回の掛け金の下限
MAX_OPEN_PREMIUM = 20_000  # 1人が同時に持てる、有効なオプションの掛け金の合計の上限
YIELD_SHARE = 0.04  # 掛け金のうち、預金者の利回りへ回す割合（残りは、配当に備えて銀行の手元に残る）

MARGIN_RATE = 0.20  # 裸売りの証拠金（行使価格 × 口数 の、この割合を、売るときに預ける）
MAX_SHORT_MARGIN = 20_000  # 1人が同時に預けられる、裸売りの証拠金の合計の上限

CALL, PUT, PAIR = "call", "put", "pair"
KINDS = {
    CALL: ("爆騰コール", "🚀", "上がると儲かる"),
    PUT: ("暴落プット", "📉", "下がると儲かる"),
    PAIR: ("ペア・ワラント", "⚖️", "相手の銘柄より上がると儲かる"),
}
SHORT_NAMES = {CALL: "コール裸売り", PUT: "プット裸売り"}  # 売る側（帝愛に売って、プレミアムを先にもらう）


def phi(x: float) -> float:
    """標準正規分布の累積分布関数 Φ(x)。"""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def jump_variance_per_tick(symbol: str) -> float:
    """有事（全銘柄が同じ向きに急騰・急落）による、1分あたりの分散の増え分。大きさは一様分布 EVENT_RANGE の2乗平均。"""
    lo, hi = market.EVENT_RANGE
    mean_sq = (hi**3 - lo**3) / (3 * (hi - lo))
    scale = min(market.EVENT_SCALE_MAX, market.BY_KEY[symbol].sigma / market.CS.sigma)
    return market.EVENT_PROB * mean_sq * scale * scale


def total_variance(symbol: str, seconds: float, now: float = 0.0, events=()) -> float:
    """いまから seconds 秒後までの、対数価格の分散の合計（1分ごとの揺れ + 有事 + 市場イベント）。

    events（core/market_events.py のボラティリティ予告）と時間が重なる分は、揺れが factor² 倍になる。
    その増え分は、予告の直後は一部（PRICED_AT_ANNOUNCE）だけ、開始に近づくほど全部が、値段に織り込まれる。
    """
    ticks = max(0.0, seconds) / TICK_SECONDS
    index = [s.key for s in market.SYMBOLS].index(symbol)
    base = market.COVARIANCE[index][index]
    total = ticks * (base + jump_variance_per_tick(symbol))
    for ev in events:
        if ev["symbol"] == symbol:
            total += market_events.extra_variance_ticks(ev, now, seconds, TICK_SECONDS) * base * (ev["factor"] ** 2 - 1)
    return total


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


def ask_per_unit(
    kind: str, symbol: str, spot: float, strike: float, seconds: float, now: float = 0.0, events=()
) -> float:
    """買うときの、1口あたりの値段（ペリカ）。公正価格 +SPREAD。1口 = 1枚の 1/UNIT。"""
    fair = fair_price(kind, spot, strike, total_variance(symbol, seconds, now, events))
    return fair * (1 + SPREAD) / market.UNIT


def bid_per_unit(
    kind: str, symbol: str, spot: float, strike: float, seconds: float, now: float = 0.0, events=()
) -> float:
    """売るときの、1口あたりの値段（ペリカ）。公正価格 −SPREAD。"""
    fair = fair_price(kind, spot, strike, total_variance(symbol, seconds, now, events))
    return fair * (1 - SPREAD) / market.UNIT


def intrinsic(kind: str, units: int, spot: float, strike: float) -> int:
    """満期の配当（上限をかける前）。"""
    per = max(0.0, spot - strike) if kind == CALL else max(0.0, strike - spot)
    return int(per * units / market.UNIT)


# ---- ペア・ワラント（銘柄 A が、銘柄 B より上がると当たる。Margrabe の交換オプション）----
def pair_variance(a: str, b: str, seconds: float, now: float = 0.0, events=()) -> float:
    """A と B の値動きの差（相対ボラティリティ）の、満期までの分散の合計。

    1分ごとは Σ_AA + Σ_BB − 2Σ_AB（連動が強いほど小さい）。有事（同じ向きに、銘柄ごとの大きさで動く）の差と、
    市場イベント（その銘柄の揺れが m 倍）の分も足す。
    """
    ticks = max(0.0, seconds) / TICK_SECONDS
    symbols = [s.key for s in market.SYMBOLS]
    ia, ib = symbols.index(a), symbols.index(b)
    cov = market.COVARIANCE
    saa, sbb, sab = cov[ia][ia], cov[ib][ib], cov[ia][ib]
    lo, hi = market.EVENT_RANGE
    mean_sq = (hi**3 - lo**3) / (3 * (hi - lo))
    scale = lambda k: min(market.EVENT_SCALE_MAX, market.BY_KEY[k].sigma / market.CS.sigma)  # noqa: E731
    jump = market.EVENT_PROB * mean_sq * (scale(a) - scale(b)) ** 2
    total = ticks * (saa + sbb - 2 * sab + jump)
    for ev in events:
        m, weight = ev["factor"], market_events.extra_variance_ticks(ev, now, seconds, TICK_SECONDS)
        if ev["symbol"] == a:
            total += weight * ((m * m - 1) * saa - 2 * (m - 1) * sab)
        elif ev["symbol"] == b:
            total += weight * ((m * m - 1) * sbb - 2 * (m - 1) * sab)
    return max(total, 0.0)


def pair_fair_rate(rel_a: float, rel_b: float, variance: float) -> float:
    """ペア・ワラントの公正価格（想定元本 1 あたり）。rel_a・rel_b は、買った時点を 1 とした、いまの A・B の価格。

    交換オプション max(0, A_T − B_T) の Margrabe の式。比 A/B に対する、金利 0 のコールと同じ形になる。
    """
    return fair_price(CALL, rel_a, rel_b, variance)


def pair_ask_rate(a: str, b: str, seconds: float, now: float = 0.0, events=()) -> float:
    """買うときの、想定元本 1 ペリカあたりの値段（買った直後。A も B も 1）。公正価格 +SPREAD。"""
    return pair_fair_rate(1.0, 1.0, pair_variance(a, b, seconds, now, events)) * (1 + SPREAD)


def pair_bid_rate(a: str, b: str, rel_a: float, rel_b: float, seconds: float, now: float = 0.0, events=()) -> float:
    """売るときの、想定元本 1 ペリカあたりの値段（いまの相対価格で）。公正価格 −SPREAD。"""
    return pair_fair_rate(rel_a, rel_b, pair_variance(a, b, seconds, now, events)) * (1 - SPREAD)


def pair_intrinsic(notional: int, a0: float, b0: float, a_t: float, b_t: float) -> int:
    """満期の配当（上限をかける前）: 想定元本 × max(0, A の上昇率 − B の上昇率)。"""
    return int(notional * max(0.0, a_t / a0 - b_t / b0))


def margin_per_unit(strike: float) -> float:
    """裸売りの、1口あたりの証拠金（ペリカ）。"""
    return MARGIN_RATE * strike / market.UNIT
