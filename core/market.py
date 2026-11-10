"""証券の市場（CSトークン・TEIO重工・NUMAバイオ・GOLD地金）。価格の動き・売買の計算・文字のチャート。Discord と DB に依存しない純粋なロジック。

価格は全サーバー共通。4銘柄が1分ごとに、相関をもって動く（分散共分散行列とコレスキー分解）。どの銘柄も期待値は変わらない（平均への引き戻しなどの偏りも入れない）ので、先を読んで儲けることはできない。
売買の手数料は今はかけていない（FEE_RATE で変えられる）。ペリカも銘柄も、現実のお金とは関係ない。
"""

import math
import random
from dataclasses import dataclass
from typing import Optional

MIN_PRICE = 20
MAX_PRICE = 50_000
EVENT_PROB = 1 / 720  # 1日に2回ほど、有事（全銘柄が同じ向きに動く急騰・急落）が起こる
EVENT_RANGE = (0.015, 0.035)  # 有事の大きさ（CSトークンで±1.5〜3.5%。ほかの銘柄は、値動きの大きさ σ の比をかける）
EVENT_SCALE_MAX = 1.5  # 有事の大きさにかける比の上限（NUMAバイオが、動きすぎないように）
UNIT = 1_000  # 1枚 = 1000口。小数で買えるよう、保有は口で数える
FEE_RATE = 0.0  # 買うときと売るときに、それぞれ取引額にかかる割合（今は無料）


@dataclass(frozen=True)
class Symbol:
    key: str
    name: str
    emoji: str
    start: float  # 開始価格
    sigma: float  # 1分あたりの値動きの大きさ


# 銘柄。値動きの大きさ σ と、銘柄どうしの相関 ρ は、ここの数字だけで決まる（調整はここを書き換える）
# 1日（1,440分）の標準偏差は σ × √1440 ≒ 38σ。現実の値動きに少しスリルを足した水準にしてある
CS = Symbol("cs", "CSトークン", "🪙", 1_000.0, 0.0012)  # 標準（1日の標準偏差 約4.6%。主要な暗号資産ほど）
TEIO = Symbol("teio", "TEIO重工", "🏭", 1_200.0, 0.0007)  # 大型株（1日 約2.7%）。CSとは正相関だが、連れ安しすぎない
NUMA = Symbol("numa", "NUMAバイオ", "🧪", 400.0, 0.0022)  # 仕手株（1日 約8.3%）。ほかとはほぼ無相関
GOLD = Symbol("gold", "GOLD地金", "🥇", 3_000.0, 0.0004)  # 避難先（1日 約1.5%）。CS・TEIOと逆相関
SYMBOLS = (CS, TEIO, NUMA, GOLD)
BY_KEY = {s.key: s for s in SYMBOLS}
DEFAULT = CS.key
CORRELATION = {
    ("cs", "teio"): 0.60,
    ("cs", "numa"): 0.05,
    ("teio", "numa"): 0.05,
    ("cs", "gold"): -0.6,
    ("teio", "gold"): -0.5,
    ("numa", "gold"): -0.1,
}

# 互換のための別名（CSトークン）
NAME = CS.name
START_PRICE = CS.start
SIGMA = CS.sigma


DAILY_LIMIT = 0.30  # 値幅制限。24時間前の価格から、上下にこの割合まで（ストップ高・ストップ安）


def limit_to_day(prices: dict, reference: dict, limit: float = DAILY_LIMIT) -> dict:
    """価格を、24時間前の価格（reference）の ±limit の範囲に収める。基準のない銘柄は、そのまま。"""
    out = {}
    for key, price in prices.items():
        ref = reference.get(key)
        out[key] = price if not ref else min(ref * (1 + limit), max(ref * (1 - limit), price))
    return out


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
    対数の補正（-Σ_ii/2）で、どの銘柄も価格の期待値がちょうど変わらない（マルチンゲール）ので、先を読んで儲けることはできない。
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
        factor = math.exp(-m * m * COVARIANCE[i][i] / 2 + m * shock[i]) * panic
        new = round(prices[sym.key] * factor, 2)  # 低い価格でも動きが止まらないよう、小数2桁まで持つ
        out[sym.key] = min(MAX_PRICE, max(MIN_PRICE, new))
    return out, event


# ---- クロスマージン（口座ぜんたいの証拠金）----
RISK_K = 20.0  # 必要証拠金 = RISK_K × √(wᵀΣw)。1分の値動きの約2.6σ を、約60分ぶん（2.6×√60≒20）見込んだ額
OPEN_RATIO = 1.3  # 新しく建てる（または担保を手放す）とき、この維持率を下回るなら受け付けない
LOSSCUT_RATIO = 1.0  # 維持率がこれを下回ったら、損失の大きい建玉から順に強制決済する
COLLATERAL_HAIRCUT = {"spot": 0.8, "normal": 0.95, "junk": 0.5}  # 現物・通常債・ジャンク債を、担保として数える掛目


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


def pel(price: float) -> str:
    return f"{fmt(price)}{UNIT_NAME}"


UNIT_NAME = "ペリカ"


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
