"""共有ジャックポット（サーバーごと）。Discord と DB に依存しない純粋なロジック。

負けた額の一部（SHARE）を積み立て、ゲームを精算するたびに抽選する。当たる確率は賭け金に比例する
（たくさん賭けた人ほど当たりやすく、そのぶん積み立てにも多く払っている）。積み立ては負けて消える分から回すので、
新しくペリカは作らない。設定は catalogs/games.yaml の jackpot。
"""

import random

from domain.ports import get_catalogs

_CFG = get_catalogs().games().jackpot
SHARE = _CFG.share
PELICA_PER_CERTAIN = _CFG.pelica_per_certain
MAX_CHANCE = _CFG.max_chance
MIN_POOL = _CFG.min_pool


def contribution(lost: int) -> int:
    """負けた額 lost から、積み立てに回す額。"""
    return int(max(0, lost) * SHARE)


def chance(bet: int, pool: int) -> float:
    """賭け金 bet で、いまの積み立て pool を当てる確率。"""
    if bet <= 0 or pool < MIN_POOL:
        return 0.0
    return min(MAX_CHANCE, bet / PELICA_PER_CERTAIN)


def draw(rng: random.Random, bet: int, pool: int) -> bool:
    """当たったか。"""
    p = chance(bet, pool)
    return p > 0 and rng.random() < p
