"""証券の市場（CSトークン・TEIO重工・NUMAバイオ・GOLD地金）。価格の動き・売買の計算・文字のチャート。Discord と DB に依存しない純粋なロジック。

価格は全サーバー共通。4銘柄が1分ごとに、相関をもって動く（分散共分散行列とコレスキー分解）。値動きの向きは読めない（平均への引き戻しなどの偏りは入れない）。ただし、銘柄ごとに、ごく小さな期待成長率（drift。1日で GOLD 0.3%〜NUMA 1.0%、通常債の約1.3%/日より控えめ）がある。リスクを取って株を持つ理由になる。
売買の手数料は今はかけていない（FEE_RATE で変えられる）。ペリカも銘柄も、現実のお金とは関係ない。
"""

import math
import random
from dataclasses import dataclass
from typing import Optional

from infra import catalogs

_CFG = catalogs.market().market

MIN_PRICE = _CFG.min_price
MAX_PRICE = _CFG.max_price
EVENT_PROB = 1.0 / _CFG.event_interval_minutes  # 1日に2回ほど、有事（全銘柄が同じ向きに動く急騰・急落）が起こる
EVENT_RANGE = _CFG.event_range  # 有事の大きさ（CSトークンで±1.5〜3.5%。ほかの銘柄は、値動きの大きさ σ の比をかける）
EVENT_SCALE_MAX = _CFG.event_scale_max  # 有事の大きさにかける比の上限（NUMAバイオが、動きすぎないように）
UNIT = _CFG.unit  # 1枚 = 1000口。小数で買えるよう、保有は口で数える
FEE_RATE = _CFG.fee_rate  # 買うときと売るときに、それぞれ取引額にかかる割合（今は無料）
SIDE_NAME = dict(_CFG.side_name)
SHORT_LEVERAGES = (1, 2, 3, 5)


@dataclass(frozen=True)
class Symbol:
    key: str
    name: str
    emoji: str
    start: float  # 開始価格
    sigma: float  # 1分あたりの値動きの大きさ
    drift: float = 0.0  # 1日あたりの期待成長率（対数。リスクプレミアム）。0 なら、期待値が変わらない（マルチンゲール）


SYMBOLS = tuple(Symbol(s.key, s.name, s.emoji, s.start, s.sigma, s.drift) for s in _CFG.symbols)
BY_KEY = {s.key: s for s in SYMBOLS}
CS, TEIO, NUMA, GOLD = BY_KEY["cs"], BY_KEY["teio"], BY_KEY["numa"], BY_KEY["gold"]
DEFAULT = CS.key
CORRELATION = {c.pair: c.value for c in _CFG.correlations}

# 互換のための別名（CSトークン）
NAME = CS.name
START_PRICE = CS.start
SIGMA = CS.sigma


DRIFT_BAND = _CFG.drift_band


def drift_per_minute(sym: "Symbol", price: float) -> float:
    """いまの価格での、1分あたりの期待成長率（対数）。開始価格より高いほど弱まり、DRIFT_BAND 倍で 0（それ以上は、成長しない）。"""
    if sym.drift <= 0 or price <= sym.start:
        return sym.drift / 1440.0
    fade = max(0.0, 1.0 - math.log(price / sym.start) / math.log(DRIFT_BAND))
    return sym.drift * fade / 1440.0


DAILY_LIMIT = _CFG.daily_limit  # 値幅制限。24時間前の価格から、上下にこの割合まで（ストップ高・ストップ安）


def limit_to_day(prices: dict, reference: dict, limit: float = DAILY_LIMIT) -> dict:
    """価格を、24時間前の価格（reference）の ±limit の範囲に収める。基準のない銘柄は、そのまま。"""
    out = {}
    for key, price in prices.items():
        ref = reference.get(key)
        out[key] = price if not ref else min(ref * (1 + limit), max(ref * (1 - limit), price))
    return out


def pin_states(fair: dict, reference: dict, limit: float = DAILY_LIMIT) -> dict:
    """理論価格が値幅の外にある銘柄の、張り付きの向き {銘柄: 1（ストップ高）/ -1（ストップ安）/ 0}。基準のない銘柄は 0。"""
    out = {}
    for key, price in fair.items():
        ref = reference.get(key)
        out[key] = 0 if not ref else 1 if price >= ref * (1 + limit) else -1 if price <= ref * (1 - limit) else 0
    return out


# 張り付き中に、約定しなくなる売買（買い手・売り手がいないため）。逆張りの売買と、利益確定は通る
PIN_LOCKS = dict(_CFG.pin_locks)
PIN_ACTION_TEXT = dict(_CFG.pin_action_text)


def pin_lock_reason(symbol: str, pin: int, action: str) -> str:
    """張り付き中に action が約定しないなら、その理由。通るなら空文字。"""
    if action not in PIN_LOCKS.get(pin, ()):
        return ""
    name = BY_KEY[symbol].name
    if pin < 0:
        return f"🚨 {name} は**ストップ安に張り付いています**。{PIN_ACTION_TEXT[action]}は、買い手がいなくて約定しません（下げが止まるまで、待つしかありません）。"
    return f"🚀 {name} は**ストップ高に張り付いています**。{PIN_ACTION_TEXT[action]}は、売り手がいなくて約定しません（上げが止まるまで、待つしかありません）。"


PIN_MARK = dict(_CFG.pin_mark)


# 利益への帝愛の税（キャピタルゲイン税）。現物の売却と信用建玉の決済で、利益が出たときだけ、その利益から天引きする。超過累進（段ごとに税率がかわる）
GAINS_TAX_BRACKETS = tuple((b.limit, b.rate) for b in _CFG.gains_tax_brackets)  # (段の上限, 税率)


def capital_gains_tax(profit: int) -> int:
    """1回の決済の利益 profit にかかる税。小口（1万ペリカまで）は非課税。段ごとに、その段にかかる分だけ、段の税率をかける（超過累進）。"""
    if profit <= 0:
        return 0
    tax, low = 0.0, 0
    for limit, rate in GAINS_TAX_BRACKETS:
        top = profit if limit is None else min(profit, limit)
        if top > low:
            tax += (top - low) * rate
        if limit is None or profit <= limit:
            break
        low = limit
    return int(tax)


BORROW_RATE_PER_DAY = dict(_CFG.borrow_rate_per_day)
INTEREST_MIN_CHARGE = (
    _CFG.interest_min_charge
)  # 引く金利が、この額に貯まるまでは、数えるだけで、担保から引かない（履歴を細かくしすぎない）


def margin_interest_base(side: str, entry_value: float, margin: int) -> float:
    """金利をかける元の額。信用買いは借りている額、空売りは建てた総額（貸株料）。"""
    return max(0.0, entry_value - margin) if side == "long" else max(0.0, entry_value)


def correlation(a: str, b: str) -> float:
    """銘柄 a と b の相関。同じ銘柄は 1。"""
    if a == b:
        return 1.0
    return CORRELATION.get((a, b), CORRELATION.get((b, a), 0.0))


def covariance_matrix() -> list:
    """分散共分散行列 Σ（Σ_ij = σ_i σ_j ρ_ij）。SYMBOLS の並びの順。"""
    return [[a.sigma * b.sigma * correlation(a.key, b.key) for b in SYMBOLS] for a in SYMBOLS]


def cholesky(matrix: list) -> list:
    """コレスキー分解 Σ = L Lᵀ の下三角行列 L。正定値でなければ ValueError。"""
    n = len(matrix)
    low = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            total = sum(low[i][k] * low[j][k] for k in range(j))
            if i == j:
                diag = matrix[i][i] - total
                if diag <= 0:
                    raise ValueError("相関の数字が正しくありません（行列が正定値ではありません）")
                low[i][j] = math.sqrt(diag)
            else:
                low[i][j] = (matrix[i][j] - total) / low[j][j]
    return low


COVARIANCE = covariance_matrix()
CHOLESKY = cholesky(COVARIANCE)


def next_prices(prices: dict, rng: random.Random, vol: Optional[dict] = None) -> tuple:
    """1分後の全銘柄の価格と、有事の出来事（+1 急騰 / -1 急落 / 0 なし）。

    独立な標準正規乱数 z から、相関のある揺れ ε = L z を作り、P_i ← P_i · exp(-Σ_ii/2 + ε_i) と動かす（Σ = L Lᵀ）。
    対数の補正（-Σ_ii/2）で、揺れだけでは価格の期待値が変わらない。そこに、銘柄ごとの期待成長率（drift_per_minute）を足す。
    市場イベント（vol = {銘柄: 倍率}）で、その銘柄の揺れだけが m 倍になる（-m²Σ/2 の補正で、期待値は変わらない）。
    有事は、全銘柄が同じ向きに動く（逆相関のヘッジが効かなくなる）。大きさは、CSトークンの ±3〜8% に、銘柄の値動きの大きさの比をかけたもの。急騰と急落は同じ確率で、打ち消し合う。
    """
    z = [rng.gauss(0, 1) for _ in SYMBOLS]
    shock = [sum(CHOLESKY[i][k] * z[k] for k in range(i + 1)) for i in range(len(SYMBOLS))]
    event, size = 0, 0.0
    if rng.random() < EVENT_PROB:
        event = 1 if rng.random() < 0.5 else -1
        size = rng.uniform(*EVENT_RANGE)
    out = {}
    for i, sym in enumerate(SYMBOLS):
        panic = 1 + event * size * min(
            EVENT_SCALE_MAX, sym.sigma / CS.sigma
        )  # 急騰と急落は同じ確率なので、打ち消し合う
        m = (vol or {}).get(
            sym.key, 1.0
        )  # 市場イベントで、この銘柄の値動きが m 倍（補正も m² 倍にして、期待値は変えない）
        factor = math.exp(drift_per_minute(sym, prices[sym.key]) - m * m * COVARIANCE[i][i] / 2 + m * shock[i]) * panic
        new = round(prices[sym.key] * factor, 2)  # 低い価格でも動きが止まらないよう、小数2桁まで持つ
        out[sym.key] = min(MAX_PRICE, max(MIN_PRICE, new))
    return out, event


# ---- クロスマージン（口座ぜんたいの証拠金）----
RISK_K = _CFG.risk_k  # 必要証拠金 = RISK_K × √(wᵀΣw)。1分の値動きの約2.6σ を、約60分ぶん（2.6×√60≒20）見込んだ額
OPEN_RATIO = _CFG.open_ratio  # 新しく建てる（または担保を手放す）とき、この維持率を下回るなら受け付けない
LOSSCUT_RATIO = _CFG.losscut_ratio  # 維持率がこれを下回ったら、損失の大きい建玉から順に強制決済する
COLLATERAL_HAIRCUT = dict(_CFG.collateral_haircut)  # 現物・通常債・ジャンク債を、担保として数える掛目


def required_margin(exposure: dict) -> float:
    """必要証拠金。exposure は {銘柄: 符号つきの建玉額（買い建てが＋、空売りが−）}。

    RISK_K × √(wᵀΣw)。相関のある建玉（CSの買いとTEIOの売りなど）は、互いの値動きを打ち消すので、必要額が小さくなる。
    """
    w = [exposure.get(s.key, 0.0) for s in SYMBOLS]
    var = sum(w[i] * COVARIANCE[i][j] * w[j] for i in range(len(w)) for j in range(len(w)))
    return RISK_K * math.sqrt(max(0.0, var))


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
    gross = int(round(units * price / UNIT, 6) // 1)  # 小数の誤差で1ペリカずれないよう丸めてから切り捨てる
    fee = _fee(gross)
    return max(0, gross - fee), fee


def avg_price(units: int, cost: int) -> float:
    """取得時の1枚あたりの価格（買った総額 ÷ 枚数）。"""
    return cost * UNIT / units if units > 0 else 0.0


def pnl_percent(price: float, units: int, cost: int) -> float:
    """取得時の価格に対して、いまの価格が何％前後しているか。"""
    avg = avg_price(units, cost)
    return (price - avg) / avg * 100 if avg else 0.0


def margin_pnl(side: str, units: int, entry_value: float, price: float) -> int:
    """建玉の含み損益。空売りは価格が下がるとプラス、信用買いは上がるとプラス。"""
    now = units * price / UNIT
    return round(entry_value - now) if side == "short" else round(now - entry_value)


def short_pnl(units: int, entry_value: float, price: float) -> int:
    """空売りの含み損益（価格が下がるとプラス）。"""
    return margin_pnl("short", units, entry_value, price)


def pel(price: float) -> str:
    return f"{fmt(price)}{UNIT_NAME}"


UNIT_NAME = _CFG.unit_name


def lots(units: int) -> str:
    return f"{units / UNIT:,.3f}枚"


def correlation_from_history(hist_a: list, hist_b: list) -> Optional[float]:
    """2つの銘柄の価格の履歴 [(時刻, 価格, 出来事), ...] から、1分ごとの値動き（対数の変化）の相関を求める。足りなければ None。"""
    a = {t: p for t, p, _ in hist_a}
    pairs = [(a[t], p) for t, p, _ in hist_b if t in a]  # 同じ時刻の価格だけを使う
    if len(pairs) < 30:
        return None
    ra = [math.log(y[0] / x[0]) for x, y in zip(pairs, pairs[1:])]
    rb = [math.log(y[1] / x[1]) for x, y in zip(pairs, pairs[1:])]
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    va, vb = sum((x - ma) ** 2 for x in ra), sum((x - mb) ** 2 for x in rb)
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(ra, rb)) / math.sqrt(va * vb)


def describe_correlation(value: Optional[float]) -> str:
    """相関を、ひと目で分かる言葉にする。"""
    if value is None:
        return "—"
    if value >= 0.6:
        return f"🔗 強く連動（{value:+.2f}）"
    if value >= 0.3:
        return f"↗️ やや連動（{value:+.2f}）"
    if value > -0.3:
        return f"➖ ほぼ無関係（{value:+.2f}）"
    if value > -0.6:
        return f"↘️ やや逆に動く（{value:+.2f}）"
    return f"🔀 逆に動く（{value:+.2f}）"


# ----------------------------- 文字のチャート ----------------------------- #
LEVELS = _CFG.levels
PERIODS = {k: (v.label, v.seconds) for k, v in _CFG.periods.items()}


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
    for r in range(height):  # r=0 が一番上
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
    return {
        "last": last,
        "change": (last - first) / first * 100 if first else 0.0,
        "high": max(prices),
        "low": min(prices),
    }
