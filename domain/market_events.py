"""市場イベントの数字と計算（ボラティリティ予告・相関崩壊の警報）。Discord と DB に依存しない純粋なロジック。

どのイベントも、勝ち負けの八百長ではない。「大きく動く」ことは分かっても、上がるか下がるかは分からない。
- ボラティリティ予告: ある銘柄の値動きの大きさ σ が、予告から LEAD_MIN 分後に始まり、DURATION_MIN 分間だけ FACTOR 倍になる。
  価格の動きは、-m²Σ/2 の補正をつけて、期待値は変わらない（マルチンゲールのまま）。向きはランダム
- オプションの値段（core/options.py）は、予告が出た瞬間に、イベントを全部織り込む（公知の情報なので、先回りの両建て（ストラドル）が儲からない）。
  予告が出ていない平時も、「これから予告が出る」期待の分（expected_event_ticks）を、最初から値段に織り込む（無裁定）
- 相関崩壊の警報: 強く連動する2銘柄の、直近 ALERT_WINDOW 分の値動きの差が、ふだんの揺れの Z_ALERT 倍（σ）以上に開いたら、市況に出す
"""

import math
import random
from typing import Optional

from domain import market
from domain.ports import get_catalogs

_CFG = get_catalogs().market().events

LEAD_MIN = _CFG.lead_min  # 予告から、開始までの分
DURATION_MIN = _CFG.duration_min  # 続く分
FACTOR = _CFG.factor  # 値動きの大きさ σ の倍率
ANNOUNCE_PROB = 1.0 / _CFG.announce_interval_minutes  # 1分ごとに、新しい予告が出る確率（約5時間に1回）
COOLDOWN_MIN = _CFG.cooldown_min  # 終わってから、次の予告が出るまでの最短の分
PRICED_AT_ANNOUNCE = _CFG.priced_at_announce  # 予告の直後に、オプションの値段が、イベントを織り込んでいる割合
KEEP_DAYS = _CFG.keep_days  # 終わったイベントの記録を残す日数

ALERT_WINDOW = _CFG.alert_window  # 相関崩壊を調べる、直近の分
Z_ALERT = _CFG.z_alert
MIN_ABS_CORR = _CFG.min_abs_corr  # 警報の対象にする、連動の強さ（相関の絶対値）


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
    """オプションの値段が、そのイベントを織り込んでいる割合（予告の直後から PRICED_AT_ANNOUNCE。いまは 1 で、予告の瞬間に全部）。"""
    if now >= ev["start_ts"]:
        return 1.0
    span = max(1.0, ev["start_ts"] - ev["announce_ts"])
    progress = max(0.0, min(1.0, (now - ev["announce_ts"]) / span))
    return PRICED_AT_ANNOUNCE + (1 - PRICED_AT_ANNOUNCE) * progress


def expected_event_ticks(seconds: float, tick_seconds: float) -> float:
    """平時に、満期までの seconds 秒のあいだに、ある1銘柄が（まだ予告のない）イベントに当たる、1分ごとの刻みの数の期待値。

    予告は、1分ごとに ANNOUNCE_PROB の確率で出る（ただし、前のイベントが終わって COOLDOWN_MIN 分たつまでは出ない）。
    1回の周期は、予告の待ち 1/ANNOUNCE_PROB + 予告から終了まで（LEAD_MIN + DURATION_MIN）+ 冷却 COOLDOWN_MIN 分。
    イベントの対象は、全銘柄から一様に選ばれるので、1銘柄あたりは、その 1/銘柄数。
    """
    cycle = 1 / ANNOUNCE_PROB + LEAD_MIN + DURATION_MIN + COOLDOWN_MIN
    return seconds / tick_seconds * DURATION_MIN / cycle / len(market.SYMBOLS)


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


def current(events: list, now: float) -> Optional[dict]:
    """終わっていないイベント（予告中か発生中）を1つ返す。"""
    live = [e for e in events if phase(e, now) != "done"]
    return min(live, key=lambda e: e["start_ts"]) if live else None
