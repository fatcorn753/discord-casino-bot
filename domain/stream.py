"""配信（/配信）の数字と計算。Discord と DB に依存しない純粋なロジック。

- スパチャは、配信者への送金。手数料・1日の上限・遊んだ回数の条件は、送金と同じ（送金の抜け道にしない）
- 勝敗の賭けは山分け（パリミュチュエル）。負けた側の賭け金から帝愛が控除し、残りを当てた人で、賭けた額に比例して分ける
  - 当てた人には、自分の賭け金が必ず戻る（控除は負けた側からだけ引くので、当てて損することはない）
  - 片方にしか賭けがなければ、勝負にならないので全額返す。新しくペリカは作らない
- 勝敗は配信者が決める。配信者本人は自分の勝負に賭けられない。1人が1回の勝負に賭けられる額には上限がある
"""

import math
from dataclasses import dataclass

from domain.ports import get_catalogs

_CFG = get_catalogs().economy().stream
TITLE_MAX = _CFG.title_max
MAX_HOURS = _CFG.max_hours
TIP_MIN = _CFG.tip_min
TIP_MESSAGE_MAX = _CFG.tip_message_max
BET_MIN = _CFG.bet_min
BET_MAX = _CFG.bet_max
RAKE = _CFG.rake
ROUND_LABEL_MAX = _CFG.round_label_max

WIN, LOSE = "win", "lose"  # 賭ける側（配信者が勝つ / 負ける）
SIDES = (WIN, LOSE)

# スパチャの色（YouTube のように、額が大きいほど目立つ色）。(この額から, 色)
TIP_TIERS: tuple[tuple[int, int], ...] = (
    (20000, 0xE62117),  # 赤
    (10000, 0xE91E63),  # 赤紫
    (5000, 0xF57C00),  # 橙
    (2000, 0xFFCA28),  # 黄
    (1000, 0x1DE9B6),  # 緑
    (500, 0x00E5FF),  # 水色
    (0, 0x1E88E5),  # 青
)


def tip_color(amount: int) -> int:
    """スパチャの額に応じた色。"""
    return next(color for floor, color in TIP_TIERS if amount >= floor)


def transfer_fee(amount: int, fee_rate: float) -> int:
    """スパチャの手数料（送金と同じ。切り上げで、少額でも最低1）。"""
    return math.ceil(round(amount * fee_rate, 6)) if fee_rate else 0


@dataclass(frozen=True)
class Settlement:
    """勝負の精算の結果。payouts は、その人に返すペリカ（賭け金を含む）。"""

    payouts: dict[int, int]
    rake: int  # 帝愛銀行の収入（控除と、山分けの端数）
    refunded: bool  # 勝負にならず、全額を返したか


def settle(bets: dict[int, tuple[str, int]], winner: str | None) -> Settlement:
    """賭け（user_id → (側, 額)）を精算する。winner が None なら無効（全額返す）。

    負けた側の合計から RAKE を引いた残りを、当てた人で賭けた額に比例して分ける。端数は帝愛銀行へ。
    """
    totals = {side: sum(a for s, a in bets.values() if s == side) for side in SIDES}
    if winner is None or totals[WIN] == 0 or totals[LOSE] == 0:
        return Settlement({uid: amount for uid, (_s, amount) in bets.items()}, 0, True)
    loser = LOSE if winner == WIN else WIN
    pot = totals[loser] - math.floor(totals[loser] * RAKE)
    payouts = {
        uid: amount + math.floor(pot * amount / totals[winner])
        for uid, (side, amount) in bets.items()
        if side == winner
    }
    paid = sum(payouts.values())
    return Settlement(payouts, totals[WIN] + totals[LOSE] - paid, False)


def odds(win_total: int, lose_total: int, side: str) -> float | None:
    """いまの賭けで、その側が当たったときの倍率の目安（賭け金を含む）。相手側に賭けがなければ None。"""
    mine, other = (win_total, lose_total) if side == WIN else (lose_total, win_total)
    if mine == 0 or other == 0:
        return None
    return 1 + (other - math.floor(other * RAKE)) / mine
