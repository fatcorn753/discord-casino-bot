"""ユーザーが出題するクイズの数字。調整はここの定数を書き換える。

出題者は、期限が来たとき（または取り下げたとき）に、回答者が MIN_PARTICIPANTS 人以上なら、システムから次の3つを受け取る。
  基本報酬 BASE_REWARD×m ＋ 正答率ボーナス f×MAX_BONUS×m ＋ 参加報酬 f×PARTICIPATION_RATE×報酬×回答者数（上限なし。人気の問題ほど増える）
m は報酬に応じた倍率（√(報酬÷100)。報酬100で1、400で2、1000で約3.2）。報酬を高くするほど、基本・ボーナスも増える。
f は正答率が TARGET_RATE に近いほど大きい係数（ちょうどで 1、TARGET_RATE から TARGET_RATE だけ離れると 0。三角形に減る）。
正解者への報酬は、出題者本人のペリカから払う（預かりはなく、払うたびに出題者の残高から引く）。残高が足りなければ、足りない分は出題者の借金になる。
"""

import math

from core import catalogs

_CFG = catalogs.economy().quizpay
USER_MIN_MINUTES = _CFG.user_min_minutes  # 出題の有効期間（分）
USER_MAX_MINUTES = _CFG.user_max_minutes  # 7日
USER_DEFAULT_MINUTES = _CFG.user_default_minutes
MIN_PARTICIPANTS = _CFG.min_participants  # 出題者報酬が出る最低の回答者数
MIN_GAMES_FOR_REWARD = _CFG.min_games_for_reward  # 正解の報酬をもらえるのは、ゲームを3回以上遊んだ人だけ
OPEN_LIMIT = _CFG.open_limit  # 1人が同時に出せる問題の数
DAILY_LIMIT = _CFG.daily_limit  # 1人が24時間に出せる問題の数
BASE_REWARD = _CFG.base_reward
MAX_BONUS = _CFG.max_bonus
TARGET_RATE = _CFG.target_rate
REWARD_SCALE_BASE = _CFG.reward_scale_base  # 報酬がこの額のとき、基本・ボーナスの倍率が 1 になる
PARTICIPATION_RATE = _CFG.participation_rate  # 回答者1人あたり、報酬の何割を出題者が受け取るか


def factor(participants: int, correct: int) -> float:
    """正答率が TARGET_RATE に近いほど 1 に近い係数（0〜1）。"""
    if participants <= 0:
        return 0.0
    return max(0.0, 1 - abs(correct / participants - TARGET_RATE) / TARGET_RATE)


def scale(reward: int) -> float:
    """報酬が高いほど、基本報酬とボーナスも増やす倍率（√(報酬÷100)。増え方を抑えるため、比例にはしない）。"""
    return math.sqrt(max(reward, 0) / REWARD_SCALE_BASE)


def base_reward(reward: int) -> int:
    return round(BASE_REWARD * scale(reward))


def bonus(participants: int, correct: int, reward: int = REWARD_SCALE_BASE) -> int:
    return round(MAX_BONUS * factor(participants, correct) * scale(reward))


def participation(participants: int, correct: int, reward: int) -> int:
    return round(factor(participants, correct) * PARTICIPATION_RATE * reward * participants)


def author_payout(participants: int, correct: int, reward: int) -> tuple:
    """(基本報酬, 正答率ボーナス, 参加報酬)。回答者が MIN_PARTICIPANTS 人に満たなければ (0, 0, 0)。"""
    if participants < MIN_PARTICIPANTS:
        return 0, 0, 0
    return base_reward(reward), bonus(participants, correct, reward), participation(participants, correct, reward)
