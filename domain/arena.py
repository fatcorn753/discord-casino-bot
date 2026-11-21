"""地下闘技場（NPC戦士どうしの賭け対戦）の数字と計算。Discord と DB に依存しない純粋なロジック。

- 戦士はプレイヤーが所有しない、観戦・賭け専用のNPC。1日に、1on1とバトロワの**両方**を同時開催する
  （別々の大会として、それぞれ生成・進行・精算する）。
  - 1on1: 準決勝2試合（各2人）→ 決勝1試合（準決勝の勝者2人、1on1）。
  - バトロワ: 準決勝4試合（各4人）→ 決勝1試合（準決勝の勝者4人による、4人同時の戦い）。
- 戦士への賭け総額（大会を通じて累積）に応じて、その戦士は「武器」で強くなる（上限あり。必ず勝てるわけではない）。
- 配当は、戦士の強さ（武器込み）だけから見積もった勝率にもとづくオッズ制（horse.pyの単勝オッズと同方式）。
  賭け総額の人数には左右されない。ただし、その試合に集まった賭け金（負けた側の賭け＋観客の初期ランダム分）を
  超えて配当しない（新しくペリカを作らない。原資が足りなければ、配当は比例して減る）。
- 決勝は、準決勝より賭けたくなるよう、オッズに FINAL_ODDS_BOOST をかけて高くする（控除は同じ。上限は変わらない）。
"""

import math
import random
from typing import Any

from domain.ports import get_catalogs

_CFG = get_catalogs().arena()

SEMI_HOUR = _CFG.semi_hour
FINAL_HOUR = _CFG.final_hour

FIGHTER_NAMES = tuple(_CFG.fighter_names)
BASE_POWER_MIN = _CFG.base_power_min
BASE_POWER_MAX = _CFG.base_power_max

WEAPON_CAP = _CFG.weapon_cap  # 武器による強さの上乗せの上限（1.0 なら、最大で素の強さの2倍）
WEAPON_SCALE = _CFG.weapon_scale  # この賭け総額で、上限に達する

TAKEOUT = _CFG.takeout  # オッズから引く控除
MAX_ODDS = _CFG.max_odds  # 単勝オッズの上限
FINAL_ODDS_BOOST = _CFG.final_odds_boost  # 決勝のオッズに掛けるブースト（1.0 なら準決勝と同じ）
LUCK = _CFG.luck  # 試合ごとの運（強さだけで決まらないようにする）

MIN_STAKE = _CFG.min_stake
MAX_STAKE = _CFG.max_stake

SPECTATOR_COUNT = (_CFG.spectator_count_min, _CFG.spectator_count_max)
SPECTATOR_BET = (_CFG.spectator_min_bet, _CFG.spectator_max_bet)
HOUSE_TOPUP_MAX = _CFG.house_topup_max  # 観客の初期ランダム分を、帝愛の手元現金から補う上限（1大会あたり）
MATCH_SUBSIDY_MAX = _CFG.match_subsidy_max  # 1試合で、配当の利益分の不足を帝愛銀行の手元現金から補う上限

FORMATS = ("duel", "royale")
GROUP_SIZE = {"duel": 2, "royale": 4}  # 1試合（準決勝1組・決勝）の人数
SEMI_GROUPS = {"duel": 2, "royale": 4}  # 準決勝の組数（＝決勝に進む人数）
NPC_SPECTATOR = -5  # 観客（プレイヤー以外）のNPC賭けを表す user_id


def group_size(fmt: str) -> int:
    return GROUP_SIZE[fmt]


def semi_groups(fmt: str) -> int:
    return SEMI_GROUPS[fmt]


def win_weights(fighters: list[dict[str, Any]]) -> list[float]:
    """純粋な強さ（武器込み）だけから見積もる、各戦士の勝つ確率（合計1）。賭け総額の人数には左右されない。"""
    weights = [effective_power(f["base_power"], f.get("bet_total", 0)) for f in fighters]
    total = sum(weights) or 1
    return [w / total for w in weights]


def _odds(prob: float, takeout: float, cap: float) -> float:
    """確率から、控除を引いた倍率（小数1桁に切り捨て・1.0倍以上・cap以下）。"""
    if prob <= 0:
        return cap
    return max(1.0, min(cap, math.floor((1 - takeout) / prob * 10) / 10))


def match_odds(fighters: list[dict[str, Any]], boost: float = 1.0) -> dict[str, float]:
    """試合に出る戦士ごとの単勝オッズ（{名前: 倍率}）。boost は、決勝で賭けたくなるよう高くする倍率。"""
    probs = win_weights(fighters)
    return {f["name"]: min(MAX_ODDS, _odds(p, TAKEOUT, MAX_ODDS) * boost) for f, p in zip(fighters, probs)}


def generate_fighters(fmt: str, rng: random.Random) -> list[dict[str, Any]]:
    """その日の戦士を生成する。[{name, base_power}]。人数は、形式の準決勝ぶん（1on1=4人・バトロワ=16人）。"""
    n = group_size(fmt) * semi_groups(fmt)
    names = rng.sample(FIGHTER_NAMES, min(n, len(FIGHTER_NAMES)))
    while len(names) < n:  # 名前の在庫が足りなければ、連番で埋める
        names.append(f"無名の戦士{len(names) + 1}")
    return [{"name": name, "base_power": rng.randint(BASE_POWER_MIN, BASE_POWER_MAX)} for name in names]


def initial_spectator_bets(rng: random.Random) -> int:
    """観客1人分の、ランダムな初期賭け額。"""
    return rng.randint(*SPECTATOR_BET)


def weapon_bonus(bet_total: int) -> float:
    """賭け総額による、強さの上乗せ（0 〜 WEAPON_CAP）。"""
    if WEAPON_SCALE <= 0:
        return 0.0
    return min(WEAPON_CAP, max(0.0, bet_total) / WEAPON_SCALE)


def effective_power(base_power: int, bet_total: int) -> float:
    """武器込みの、いまの強さ。"""
    return base_power * (1 + weapon_bonus(bet_total))


def simulate_match(fighters: list[dict[str, Any]], bet_totals: dict[str, int], rng: random.Random) -> int:
    """1試合の結果（勝者の添字）を決める。fighters は [{name, base_power}]、bet_totals は {名前: 賭け総額}。

    強さが高いほど勝ちやすいが、運（LUCK）で必ずしも勝てるとは限らない。
    """
    weights = [
        max(0.01, effective_power(f["base_power"], bet_totals.get(f["name"], 0)) * rng.gauss(1.0, LUCK))
        for f in fighters
    ]
    return rng.choices(range(len(fighters)), weights=weights, k=1)[0]


def battle_frames(count: int, winner_idx: int, rng: random.Random, frames: int = 6) -> list[list[int]]:
    """表示専用のHP(0〜100)の推移。勝敗はすでに決まっている前提で、見た目の攻防だけを作る（ゲーム性には関わらない）。

    [HP配列]をフレーム数+1個返す（先頭は全員100）。負けた側は最後に必ず0、勝った側は必ず残る。
    """
    hp = [100.0] * count
    out = [[round(x) for x in hp]]
    for _ in range(frames):
        for j in range(count):
            if j == winner_idx:
                hp[j] = max(20.0, hp[j] - rng.uniform(0, 10))
            else:
                hp[j] = max(0.0, hp[j] - rng.uniform(12, 25))
        out.append([round(x) for x in hp])
    for j in range(count):
        if j != winner_idx:
            hp[j] = 0.0
    out[-1] = [round(x) for x in hp]
    return out


def settle_payouts(stakes: list[int], rate: float, pool: int, subsidy_cap: int) -> tuple[list[int], int, int]:
    """勝者の払い戻し(#27)。(各人の払い戻し, 帝愛の補填額, 余った原資)。

    - 勝てば、賭け金(元本)は必ず戻す
    - 利益(賭け金 × (オッズ − 1))の原資は、負けた側の賭け金 pool と、足りない分だけ帝愛の補填(上限 subsidy_cap)
    - それでも足りなければ、利益の分だけを比例して減らす(元本は減らさない)。新しいペリカは作らない
    """
    profits = [max(0, round(a * rate) - a) for a in stakes]
    due = sum(profits)
    subsidy = min(max(0, due - pool), max(0, subsidy_cap))
    scale = payout_scale(pool + subsidy, due)
    paid = [round(p * scale) for p in profits]
    leftover = max(0, pool + subsidy - sum(paid))
    return [a + p for a, p in zip(stakes, paid)], subsidy, leftover


def payout_scale(losers_pool: int, winners_due: int) -> float:
    """配当の原資が足りないとき、比例して減らす倍率（1.0 が満額）。原資を超えて新しいペリカは作らない。"""
    if winners_due <= 0:
        return 1.0
    return min(1.0, max(0.0, losers_pool) / winners_due)
