"""還元率のレベル（1〜6）。持ち主がゲームごとに変えられる。レベル3が、いまの設定。

しくみ: 勝ったとき（払い戻しが賭け金より多いとき）の「勝ち分」だけを、一定の倍率で増減させる。
負け・引き分けは変えない。倍率は、1レベルあたり STEP（4ポイント）ぶん還元率が動くよう、ゲームごとに決める:

    倍率 = 1 + (レベル - 3) × STEP ÷ 勝ち分の平均（W）

W は「賭けた額あたり、勝ち分が平均いくらか」。各ゲームの RTP プロファイル（Strategy）で計算する。
"""

import itertools
from typing import Optional

from domain.games import pachinko, roulette, slot
from domain.ports import get_catalogs

_CFG = get_catalogs().games().rtplevel

MIN_BET = get_catalogs().games().common.min_bet
GAMES = _CFG.games
LEVELS = _CFG.levels
DEFAULT_LEVEL = _CFG.default_level
STEP = _CFG.step  # 1レベルあたりの還元率の動き（4ポイント）

# シミュレーションで求めた目安（tests/test_rtplevel.py で再計算して、近いことを確かめている）
CHINCHIRO_RTP, CHINCHIRO_W = _CFG.chinchiro_rtp, _CFG.chinchiro_w
BLACKJACK_RTP, BLACKJACK_W = _CFG.blackjack_rtp, _CFG.blackjack_w  # 「17まで引く」打ち方の場合
ROULETTE_DEFAULT_KIND = _CFG.roulette_default_kind


def _slot_stats() -> tuple[float, float]:
    total = sum(slot.WEIGHTS.values())
    rtp = share = 0.0
    for reels in itertools.product(slot.SYMBOLS, repeat=3):
        p = 1.0
        for s in reels:
            p *= slot.WEIGHTS[s] / total
        m = slot.multiplier(reels)
        rtp += p * m
        share += p * max(0.0, m - 1)
    return rtp, share


SLOT_RTP, SLOT_W = _slot_stats()


class IRtpProfile:
    """ゲームごとの基準還元率・平均勝ち分を計算する Strategy インターフェース。"""

    def stats(self, kind: Optional[str] = None) -> tuple[float, float]:
        raise NotImplementedError


class FixedRtpProfile(IRtpProfile):
    """事前シミュレーションで求めた固定の (RTP, W) を返すプロファイル。"""

    def __init__(self, rtp: float, win_share: float) -> None:
        self._rtp = rtp
        self._win_share = win_share

    def stats(self, kind: Optional[str] = None) -> tuple[float, float]:
        return self._rtp, self._win_share


class RouletteRtpProfile(IRtpProfile):
    def __init__(self, default_kind: str) -> None:
        self._default_kind = default_kind

    def stats(self, kind: Optional[str] = None) -> tuple[float, float]:
        mult = roulette.BETS[kind or self._default_kind][1]
        p = sum(1 for n in range(37) if roulette.wins(kind or self._default_kind, n, 17)) / 37
        return p * mult, p * (mult - 1)


class PachinkoRtpProfile(IRtpProfile):
    def stats(self, kind: Optional[str] = None) -> tuple[float, float]:
        rtp = pachinko.rtp()
        return rtp, rtp - pachinko.hit_rate()


PROFILES: dict[str, IRtpProfile] = {
    "スロット": FixedRtpProfile(SLOT_RTP, SLOT_W),
    "ルーレット": RouletteRtpProfile(ROULETTE_DEFAULT_KIND),
    "パチンコ": PachinkoRtpProfile(),
    "チンチロ": FixedRtpProfile(CHINCHIRO_RTP, CHINCHIRO_W),
    "ブラックジャック": FixedRtpProfile(BLACKJACK_RTP, BLACKJACK_W),
}


def base_stats(game: str, kind: str | None = None) -> tuple[float, float]:
    """(いまの還元率, 勝ち分の平均 W)。"""
    profile = PROFILES.get(game)
    if profile is None:
        raise KeyError(game)
    return profile.stats(kind)


def factor(game: str, level: int, kind: str | None = None) -> float:
    """勝ち分にかける倍率。"""
    _, w = base_stats(game, kind)
    return 1 + (level - DEFAULT_LEVEL) * STEP / w


def approx_rtp(game: str, level: int, kind: str | None = None) -> float:
    """そのレベルでの還元率の目安。"""
    rtp, _ = base_stats(game, kind)
    return rtp + (level - DEFAULT_LEVEL) * STEP


def adjust(game: str, level: int, bet: int, payout: int, kind: str | None = None) -> int:
    """勝ったときだけ、勝ち分（払い戻し - 賭け金）を倍率で増減した払い戻しを返す。負け・引き分け・レベル3は、そのまま。"""
    if level == DEFAULT_LEVEL or game not in GAMES or payout <= bet:
        return payout
    return bet + max(0, int((payout - bet) * factor(game, level, kind)))
