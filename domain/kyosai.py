"""帝愛共済（#34）の数字と計算。調整は catalogs/teiai.yaml の kyosai。

帝愛銀行の多層資本の最下層（最後に損をかぶる持分）。
- 口: 口座を作った人に 1 口ずつ配る。破産・休眠で帝愛が回収し、帝愛の持ち口として売りに出す
- 配当: 銀行の準備率が目標を超えた分（余剰）の一部を、毎日、口数に応じて配る
- 超過損失 Δ: 帝愛信託でも払えず、熱浴も肩代わりしない分を、口数に応じて請求する（手元が足りなければ借金）
- 売買: 口は、ほかの人へ値段をつけて譲れる。準備率が低いとき・破産猶予中の人がいるときは止める（流動性ゲート）
- 動議: 口を持つ人が、帝愛信託の上乗せ金利を変える動議を出せる。賛成・反対を口数で数え、多いほうに決まる
"""

from domain.ports import get_catalogs

_CFG = get_catalogs().teiai().kyosai

HOUSE = 0  # 帝愛の持ち口（user_id=0 として記録する）
TARGET_RATIO = _CFG.target_ratio
DIVIDEND_SHARE = _CFG.dividend_share
DIVIDEND_CAP = _CFG.dividend_cap
GATE_RATIO = _CFG.gate_ratio
DORMANT_DAYS = _CFG.dormant_days
AUCTION_START = _CFG.auction_start
AUCTION_DECAY = _CFG.auction_decay
AUCTION_FLOOR = _CFG.auction_floor
OFFER_HOURS = _CFG.offer_hours
MOTION_HOURS = _CFG.motion_hours
PREMIUM_MAX = _CFG.premium_max


def split(amount: int, holdings: list[tuple[int, int]]) -> dict[int, int]:
    """amount を口数に応じて分ける（最大剰余法。合計はちょうど amount）。holdings は (user_id, 口数)。"""
    total = sum(u for _, u in holdings if u > 0)
    if amount <= 0 or total <= 0:
        return {}
    exact = [(uid, amount * u / total) for uid, u in holdings if u > 0]
    out = {uid: int(x) for uid, x in exact}
    left = amount - sum(out.values())
    for uid, x in sorted(exact, key=lambda p: (-(p[1] - int(p[1])), p[0]))[:left]:
        out[uid] += 1
    return out


def dividend_pool(available: float, liabilities: int) -> int:
    """その日に配る額: (利用可能現金 − 目標準備率 × 負債) の DIVIDEND_SHARE。上限 DIVIDEND_CAP。"""
    surplus = available - TARGET_RATIO * liabilities
    if surplus <= 0:
        return 0
    return min(DIVIDEND_CAP, int(surplus * DIVIDEND_SHARE))


def auction_price(since: float, now: float) -> int:
    """帝愛の持ち口 1 口の値段。回収した時刻 since から、1日ごとに始値の AUCTION_DECAY ずつ下がる（下限 AUCTION_FLOOR）。"""
    days = max(0.0, (now - since) / 86400)
    return max(AUCTION_FLOOR, int(AUCTION_START * (1 - AUCTION_DECAY * int(days))))


def gate_locked(theta: float | None, grace_debtors: int) -> bool:
    """口の売買を止めるか（流動性ゲート）。準備率が GATE_RATIO 未満か、破産猶予中の人がいるとき。"""
    return grace_debtors > 0 or (theta is not None and theta < GATE_RATIO)


def motion_passes(yes_units: int, no_units: int) -> bool:
    """賛成の口数が反対より多ければ可決（同数は否決）。"""
    return yes_units > no_units
