"""帝愛共済（#34）の数字と計算。調整は catalogs/teiai.yaml の kyosai。

帝愛銀行の多層資本の最下層（最後に損をかぶる持分）。
- 口: サーバーごとに総口数 TOTAL_UNITS で固定（新しく発行も消却もしない。#93）。新しい人は、譲渡か帝愛の競りで手に入れる。
  破産・休眠で帝愛が回収し、帝愛の持ち口として入札の競りに出す（値段は入札で決まる）
- 配当: 銀行の準備率が目標を超えた分（余剰）の一部を、毎日、口数に応じて配る
- 超過損失 Δ: 帝愛信託でも払えず、熱浴も肩代わりしない分を、口数に応じて請求する（手元が足りなければ借金）
- 売買: 口は、ほかの人へ値段をつけて譲れる。準備率が低いとき・破産猶予中の人がいるときは止める（流動性ゲート）
- 動議: 口を一定以上持つ人が、帝愛信託の上乗せ金利を変える動議を出せる（いくつも並行できる。#94）。
  賛成・反対を口数で数え、賛成が反対より多く、かつ賛成が流通口数の一定割合以上なら可決。締めた順に後勝ちで決まる
"""

from domain.ports import get_catalogs

_CFG = get_catalogs().teiai().kyosai

HOUSE = 0  # 帝愛の持ち口（user_id=0 として記録する）
TARGET_RATIO = _CFG.target_ratio
DIVIDEND_SHARE = _CFG.dividend_share
DIVIDEND_CAP = _CFG.dividend_cap
GATE_RATIO = _CFG.gate_ratio
DORMANT_DAYS = _CFG.dormant_days
TOTAL_UNITS = _CFG.total_units
AUCTION_LOT = _CFG.auction_lot
AUCTION_HOURS = _CFG.auction_hours
AUCTION_MIN_BID = _CFG.auction_min_bid
OFFER_HOURS = _CFG.offer_hours
MOTION_HOURS = _CFG.motion_hours
PREMIUM_LIMIT = _CFG.premium_limit
MOTION_MIN_YES_SHARE = _CFG.motion_min_yes_share
MOTION_MIN_UNITS = _CFG.motion_min_units
MOTION_MAX_OPEN_PER_USER = _CFG.motion_max_open_per_user
MOTION_COOLDOWN_HOURS = _CFG.motion_cooldown_hours


def split(amount: int, holdings: list[tuple[int, int]]) -> dict[int, int]:
    """amount を口数に応じて分ける（最大剰余法。合計はちょうど amount）。holdings は (user_id, 口数)。"""
    total = sum(u for _, u in holdings if u > 0)
    if amount <= 0 or total <= 0:
        return {}
    exact = [(uid, amount * u / total) for uid, u in holdings if u > 0]
    out = {uid: int(x) for uid, x in exact}
    left = amount - sum(out.values())
    for uid, _ in sorted(exact, key=lambda p: (-(p[1] - int(p[1])), p[0]))[:left]:
        out[uid] += 1
    return out


def dividend_pool(available: float, liabilities: int) -> int:
    """その日に配る額: (利用可能現金 − 目標準備率 × 負債) の DIVIDEND_SHARE。上限 DIVIDEND_CAP。"""
    surplus = available - TARGET_RATIO * liabilities
    if surplus <= 0:
        return 0
    return min(DIVIDEND_CAP, int(surplus * DIVIDEND_SHARE))


def gate_locked(theta: float | None, grace_debtors: int) -> bool:
    """口の売買を止めるか（流動性ゲート）。準備率が GATE_RATIO 未満か、破産猶予中の人がいるとき。"""
    return grace_debtors > 0 or (theta is not None and theta < GATE_RATIO)


def motion_passes(yes_units: int, no_units: int, circulating: int) -> bool:
    """可決か。賛成が反対より多く（同数は否決）、かつ賛成が流通口数 circulating の MOTION_MIN_YES_SHARE 以上。

    最低「投票率」ではなく最低「賛成率」にするのは、反対票を入れると定足数を満たして可決を助けてしまう
    （棄権したほうが否決できる）逆転を起こさないため。流通口数は帝愛の持ち口を除く（帝愛は投票しないため）。
    """
    return yes_units > no_units and yes_units >= MOTION_MIN_YES_SHARE * circulating


def premium_ok(premium: float) -> bool:
    """動議で出せる上乗せ金利か（0 より大きい。上限は計算の桁あふれを防ぐためだけ）。

    0 以下を認めないのは、元本保証のない信託が、元本保証の通常債（r_f）と同じ利率にならないようにするため。
    """
    return 0 < premium <= PREMIUM_LIMIT
