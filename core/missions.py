"""デイリーミッションと連勝・大勝ちの演出。Discord に依存しない純粋なロジック。

ミッションは毎日、日付から全員共通の3つが決まる。達成すると自動でペリカがもらえ、3つそろえるとボーナス。
報酬は小さめ（デイリーボーナスの半分程度）にして、遊ぶきっかけになっても経済を壊さないようにしている。
"""

import random
from dataclasses import dataclass
from typing import Optional

GAMES = ["スロット", "コイン", "ルーレット", "チンチロ", "ブラックジャック", "パチンコ"]
MIN_BET = 100                # これ未満の賭けは、ミッションの進み具合に数えない（小さい賭けの量産で報酬を稼がせない）
COMPLETE_BONUS = 500         # 3つ全部達成したときの追加ボーナス
PER_DAY = 3


@dataclass(frozen=True)
class Mission:
    id: str
    name: str
    kind: str        # play / win / wager / streak / big_win / play_game
    target: int
    reward: int
    game: Optional[str] = None


def _pool() -> list:
    pool = [
        Mission("play5", "ゲームを5回遊ぶ", "play", 5, 300),
        Mission("play10", "ゲームを10回遊ぶ", "play", 10, 500),
        Mission("win3", "3回勝つ", "win", 3, 400),
        Mission("win5", "5回勝つ", "win", 5, 600),
        Mission("wager5k", "合計5,000ペリカ賭ける", "wager", 5_000, 400),
        Mission("wager20k", "合計20,000ペリカ賭ける", "wager", 20_000, 700),
        Mission("streak2", "2連勝する", "streak", 2, 400),
        Mission("streak3", "3連勝する", "streak", 3, 700),
        Mission("big3x", "賭け金の3倍以上の利益を1回で出す", "big_win", 3, 600),
    ]
    pool += [Mission(f"game_{g}", f"{g}を3回遊ぶ", "play_game", 3, 300, g) for g in GAMES]
    return pool


POOL = _pool()
BY_ID = {m.id: m for m in POOL}


def today_missions(day) -> list:
    """その日のミッション（全員共通）。日付が同じなら何度呼んでも同じ。"""
    return random.Random(f"mission:{day.isoformat()}").sample(POOL, PER_DAY)


def progress_after(m: Mission, current: int, game: str, bet: int, net: int, streak: int) -> int:
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
    if ratio >= 50:
        return "🎰💥 **超特大ジャックポット級！！** 💥🎰"
    if ratio >= 10:
        return "💎✨ **特大勝ち！！** ✨💎"
    if ratio >= 3:
        return "🎉 **大勝ち！** 🎉"
    return ""


JACKPOT_ART = """```
 *  .  *  .  *  .  *  .  *
 +=======================+
 |   J A C K P O T !!!   |
 |      7   7   7        |
 +=======================+
 *  .  *  .  *  .  *  .  *
```"""


def is_jackpot(bet: int, net: int) -> bool:
    """賭け金の50倍以上の利益。たまにしか出ないので、特別な演出を付ける。"""
    return bet > 0 and net >= bet * 50
