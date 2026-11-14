"""デイリーミッションと連勝・大勝ちの演出。Discord に依存しない純粋なロジック。

ミッションは毎日、日付から全員共通の3つが決まる。達成すると自動でペリカがもらえ、3つそろえるとボーナス。
報酬は小さめ（デイリーボーナスの半分程度）にして、遊ぶきっかけになっても経済を壊さないようにしている。
"""

import random
from dataclasses import dataclass
from typing import Optional

from domain.ports import get_catalogs

_CFG = get_catalogs().missions()
BLACKJACK = _CFG.blackjack_game
GAMES = list(_CFG.games)
MIN_BET = _CFG.min_bet  # これ未満の賭けは、ミッションの進み具合に数えない（小さい賭けの量産で報酬を稼がせない）
COMPLETE_BONUS = _CFG.complete_bonus  # 3つ全部達成したときの追加ボーナス
PER_DAY = _CFG.per_day


@dataclass(frozen=True)
class Mission:
    id: str
    name: str
    kind: str  # play / win / wager / streak / big_win / play_game / game_win / tag
    target: int
    reward: int
    game: Optional[str] = None
    tag: Optional[str] = None  # kind が tag のとき、ゲームが付ける印（例: ブラックジャックのダブル勝ち）


UNIVERSAL = [
    Mission(m.id, m.name, m.kind, m.target, m.reward, m.game, m.tag) for m in _CFG.universal
]  # どのゲームでも進む
OTHER_GAMES = [
    Mission(f"game_{g}", f"{g}を3回遊ぶ", "play_game", _CFG.other_game_play_target, _CFG.other_game_play_reward, g)
    for g in GAMES
    if g != BLACKJACK
] + [
    Mission(f"gwin_{g}", f"{g}で2回勝つ", "game_win", _CFG.other_game_win_target, _CFG.other_game_win_reward, g)
    for g in GAMES
    if g != BLACKJACK
]  # 特定のゲーム向け
BLACKJACK_MISSIONS = [
    Mission(m.id, m.name, m.kind, m.target, m.reward, m.game, m.tag) for m in _CFG.blackjack_missions
]  # ブラックジャック向け
BIG_WIN = Mission(
    _CFG.big_win.id,
    _CFG.big_win.name,
    _CFG.big_win.kind,
    _CFG.big_win.target,
    _CFG.big_win.reward,
    _CFG.big_win.game,
    _CFG.big_win.tag,
)  # ブラックジャックでは出ない（最大でも1.5倍）

POOL = UNIVERSAL + OTHER_GAMES + BLACKJACK_MISSIONS + [BIG_WIN]
BY_ID = {m.id: m for m in POOL}


def today_missions(day) -> list:
    """その日のミッション（全員共通）。日付が同じなら何度呼んでも同じ。

    毎日、(1) どのゲームでも進むもの、(2) ブラックジャック向け、(3) 残り全部からランダムに1つ。
    ブラックジャックだけで遊ぶ人も、毎日少なくとも2つは達成できる。
    """
    rng = random.Random(f"mission:{day.isoformat()}")
    first, second = rng.choice(UNIVERSAL), rng.choice(BLACKJACK_MISSIONS)
    third = rng.choice([m for m in POOL if m.id not in (first.id, second.id)])
    return [first, second, third]


def progress_after(m: Mission, current: int, game: str, bet: int, net: int, streak: int, tags=()) -> int:
    """1回遊んだあとの、そのミッションの進み具合。"""
    if m.kind == "play":
        return current + 1
    if m.kind == "win":
        return current + (1 if net > 0 else 0)
    if m.kind == "wager":
        return current + bet
    if m.kind == "streak":
        return max(current, streak)
    if m.kind == "big_win":
        return max(current, net // bet if net > 0 and bet > 0 else 0)
    if m.kind == "play_game":
        return current + (1 if game == m.game else 0)
    if m.kind == "game_win":
        return current + (1 if game == m.game and net > 0 else 0)
    if m.kind == "tag":
        return current + (1 if m.tag in tags else 0)
    return current


# ----------------------------- 演出の文言 ----------------------------- #
def streak_line(streak: int, new_best: bool) -> str:
    """連勝の表示。2連勝から出す。"""
    if streak < 2:
        return ""
    fire = "🔥" * min(streak // 2 + 1, 4)
    line = f"{fire} **{streak}連勝中！**"
    if new_best and streak >= 3:
        line += "　🏅 自己ベスト更新！"
    return line


def big_win_line(bet: int, net: int) -> str:
    """賭け金に対する利益の大きさに応じた、勝ちの派手な一言。"""
    if net <= 0 or bet <= 0:
        return ""
    ratio = net / bet
    for tier in _CFG.win_tiers:
        if ratio >= tier.min_ratio:
            return tier.message
    return ""


JACKPOT_ART = _CFG.jackpot_art


def is_jackpot(bet: int, net: int) -> bool:
    """賭け金の50倍以上の利益。たまにしか出ないので、特別な演出を付ける。"""
    return bet > 0 and net >= bet * _CFG.jackpot_multiplier
