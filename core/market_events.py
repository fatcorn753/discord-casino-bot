"""市場イベントの数字と計算（ボラティリティ予告・相関崩壊の警報）。Discord と DB に依存しない純粋なロジック。

どのイベントも、勝ち負けの八百長ではない。「大きく動く」ことは分かっても、上がるか下がるかは分からない。
- ボラティリティ予告: ある銘柄の値動きの大きさ σ が、予告から LEAD_MIN 分後に始まり、DURATION_MIN 分間だけ FACTOR 倍になる。
  価格の動きは、-m²Σ/2 の補正をつけて、期待値は変わらない（マルチンゲールのまま）。向きはランダム
- オプションの値段（core/options.py）には、予告を少しずつ織り込む。予告の直後は PRICED_AT_ANNOUNCE の割合だけ、開始に近づくほど全部になる。
  早く気づいて仕込めば得で、遅いほど値段が上がっている
- 相関崩壊の警報: 強く連動する2銘柄の、直近 ALERT_WINDOW 分の値動きの差が、ふだんの揺れの Z_ALERT 倍（σ）以上に開いたら、市況に出す
"""

import math
import random
from typing import Optional

from core import market

LEAD_MIN = 15  # 予告から、開始までの分
DURATION_MIN = 20  # 続く分
FACTOR = 2.0  # 値動きの大きさ σ の倍率
ANNOUNCE_PROB = 1 / 300  # 1分ごとに、新しい予告が出る確率（約5時間に1回）
COOLDOWN_MIN = 60  # 終わってから、次の予告が出るまでの最短の分
PRICED_AT_ANNOUNCE = 0.4  # 予告の直後に、オプションの値段が、イベントを織り込んでいる割合（開始までに 1.0 になる）
KEEP_DAYS = 7  # 終わったイベントの記録を残す日数

ALERT_WINDOW = 60  # 相関崩壊を調べる、直近の分
Z_ALERT = 2.5
MIN_ABS_CORR = 0.5  # 警報の対象にする、連動の強さ（相関の絶対値）


def plan_event(now: float, rng: random.Random) -> dict:
    """新しいボラティリティ予告（銘柄はランダム）。"""
    symbol = rng.choice([s.key for s in market.SYMBOLS])
    start = now + LEAD_MIN * 60
    return {
        "symbol": symbol,
        "announce_ts": now,
        "start_ts": start,
        "end_ts": start + DURATION_MIN * 60,
        "factor": FACTOR,
    }


def phase(ev: dict, now: float) -> str:
    """announced（予告中）/ active（発生中）/ done（終了）。"""
    if now >= ev["end_ts"]:
        return "done"
    return "active" if now >= ev["start_ts"] else "announced"


def multiplier(events: list, symbol: str, now: float) -> float:
    """いま、その銘柄の値動きの大きさにかかっている倍率（発生中のイベントだけ。なければ 1）。"""
    m = 1.0
    for ev in events:
        if ev["symbol"] == symbol and phase(ev, now) == "active":
            m = max(m, ev["factor"])
    return m


def priced_weight(ev: dict, now: float) -> float:
    """オプションの値段が、そのイベントを織り込んでいる割合（予告の直後 PRICED_AT_ANNOUNCE → 開始で 1）。"""
    if now >= ev["start_ts"]:
        return 1.0
    span = max(1.0, ev["start_ts"] - ev["announce_ts"])
    progress = max(0.0, min(1.0, (now - ev["announce_ts"]) / span))
    return PRICED_AT_ANNOUNCE + (1 - PRICED_AT_ANNOUNCE) * progress


def extra_variance_ticks(ev: dict, now: float, seconds: float, tick_seconds: float) -> float:
    """now から seconds 秒のあいだに、イベントの時間と重なる、1分ごとの刻みの数（織り込みの割合をかけたもの）。"""
    lo, hi = max(now, ev["start_ts"]), min(now + seconds, ev["end_ts"])
    if hi <= lo:
        return 0.0
    return (hi - lo) / tick_seconds * priced_weight(ev, now)


def spread_alerts(history: dict, window: int = ALERT_WINDOW) -> list:
    """相関崩壊の警報。history は {銘柄: 直近の価格の列（古い順）}。連動する2銘柄の値動きの差が Z_ALERT 以上に開いた組を、大きい順に返す。

    差 = rA − sign(ρ)·rB（rは対数の変化）。ふだんの揺れの大きさは、σ_diff = √(Σ_AA + Σ_BB − 2·sign(ρ)·Σ_AB) × √(刻みの数)。
    """
    out = []
    symbols = [s.key for s in market.SYMBOLS]
    for i, a in enumerate(symbols):
        for b in symbols[i + 1 :]:
            rho = market.correlation(a, b)
            pa, pb = history.get(a, [])[-(window + 1) :], history.get(b, [])[-(window + 1) :]
            n = min(len(pa), len(pb)) - 1
            if abs(rho) < MIN_ABS_CORR or n < 10:
                continue
            ra, rb = math.log(pa[-1] / pa[-1 - n]), math.log(pb[-1] / pb[-1 - n])
            sign = 1 if rho > 0 else -1
            ia, ib = symbols.index(a), symbols.index(b)
            var = market.COVARIANCE[ia][ia] + market.COVARIANCE[ib][ib] - 2 * sign * market.COVARIANCE[ia][ib]
            z = (ra - sign * rb) / math.sqrt(max(var, 1e-18) * n)
            if abs(z) >= Z_ALERT:
                out.append({"a": a, "b": b, "ra": ra, "rb": rb, "z": z, "n": n, "sign": sign})
    return sorted(out, key=lambda x: -abs(x["z"]))


def describe_alert(alert: dict) -> str:
    """警報の1行。どちらが先行して、どちらが取り残されているか。"""
    a, b = market.BY_KEY[alert["a"]], market.BY_KEY[alert["b"]]
    tie = "ふつう連動する" if alert["sign"] > 0 else "ふつう逆に動く"
    return (
        f"{a.emoji}{a.name} {alert['ra'] * 100:+.1f}%　{b.emoji}{b.name} {alert['rb'] * 100:+.1f}%"
        f"（直近{alert['n']}分。{tie}2銘柄の差が、ふだんの {abs(alert['z']):.1f}倍）"
    )


def event_text(ev: dict, now: float) -> str:
    sym = market.BY_KEY[ev["symbol"]]
    if phase(ev, now) == "active":
        return (
            f"⚡ **発生中**　{sym.emoji}{sym.name} の値動きが **{ev['factor']:g}倍**（<t:{int(ev['end_ts'])}:R> まで）。"
            "上がるか下がるかは分かりませんが、大きく動きます。"
        )
    return (
        f"📢 **予告**　{sym.emoji}{sym.name} の値動きが、<t:{int(ev['start_ts'])}:R> から {DURATION_MIN}分間、**{ev['factor']:g}倍**になります。"
        "上がるか下がるかは分かりませんが、大きく動きます。オプションの値段は、開始に近づくほど上がります。"
    )


def current(events: list, now: float) -> Optional[dict]:
    """終わっていないイベント（予告中か発生中）を1つ返す。"""
    live = [e for e in events if phase(e, now) != "done"]
    return min(live, key=lambda e: e["start_ts"]) if live else None
