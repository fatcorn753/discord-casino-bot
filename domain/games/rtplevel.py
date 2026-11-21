"""還元率のレベル（1〜6）。持ち主がゲームごとに変えられる。レベル3が、いまの設定。

しくみ: 勝ったとき（払い戻しが賭け金より多いとき）の「勝ち分」だけを、一定の倍率で増減させる。
負け・引き分けは変えない。倍率は、1レベルあたり STEP（4ポイント）ぶん還元率が動くよう、ゲームごとに決める:

    倍率 = 1 + (レベル - 3) × STEP ÷ 勝ち分の平均（W）

W は「賭けた額あたり、勝ち分が平均いくらか」。各ゲームの RTP プロファイル（Strategy）で計算する。
"""

import itertools
from collections.abc import Callable, Mapping
from typing import Optional

from domain.games import pachinko, roulette, slot
from domain.ports import get_catalogs

_CFG = get_catalogs().games().rtplevel

MIN_BET = get_catalogs().games().common.min_bet
GAMES = _CFG.games
LEVELS = _CFG.levels
DEFAULT_LEVEL = _CFG.default_level
STEP = _CFG.step  # 1レベルあたりの還元率の動き（4ポイント）

# シミュレーションで求めた目安（tests/domain/games/test_rtplevel.py で再計算して、近いことを確かめている）
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


# ゲームごとの基準還元率・平均勝ち分を計算する純粋戦略（静的型だけで LSP が保証できるので Callable エイリアス）
RtpProfile = Callable[[Optional[str]], tuple[float, float]]


def fixed_rtp_profile(rtp: float, win_share: float) -> RtpProfile:
    """事前シミュレーションで求めた固定の (RTP, W) を返すプロファイル。"""

    def stats(kind: Optional[str] = None) -> tuple[float, float]:
        return rtp, win_share

    return stats


def roulette_rtp_profile(default_kind: str) -> RtpProfile:
    def stats(kind: Optional[str] = None) -> tuple[float, float]:
        mult = roulette.BETS[kind or default_kind][1]
        p = sum(1 for n in range(37) if roulette.wins(kind or default_kind, n, 17)) / 37
        return p * mult, p * (mult - 1)

    return stats


def pachinko_rtp_profile() -> RtpProfile:
    def stats(kind: Optional[str] = None) -> tuple[float, float]:
        rtp = pachinko.rtp()
        return rtp, rtp - pachinko.hit_rate()

    return stats


PROFILES: dict[str, RtpProfile] = {
    "スロット": fixed_rtp_profile(SLOT_RTP, SLOT_W),
    "ルーレット": roulette_rtp_profile(ROULETTE_DEFAULT_KIND),
    "パチンコ": pachinko_rtp_profile(),
    "チンチロ": fixed_rtp_profile(CHINCHIRO_RTP, CHINCHIRO_W),
    "ブラックジャック": fixed_rtp_profile(BLACKJACK_RTP, BLACKJACK_W),
}


class IRtpPolicy:
    """還元率調整ポリシーの契約（Bridge Abstraction）。

    不変条件:
    - `default_level in levels`
    - `step > 0`
    """

    games: tuple[str, ...]
    levels: tuple[int, ...]
    default_level: int
    step: float
    min_bet: int

    def base_stats(self, game: str, kind: Optional[str] = None) -> tuple[float, float]:
        raise NotImplementedError

    def factor(self, game: str, level: int, kind: Optional[str] = None) -> float:
        raise NotImplementedError

    def approx_rtp(self, game: str, level: int, kind: Optional[str] = None) -> float:
        raise NotImplementedError

    def adjust(self, game: str, level: int, bet: int, payout: int, kind: Optional[str] = None) -> int:
        raise NotImplementedError


class RtpPolicy(IRtpPolicy):
    """ゲーム別 `IRtpProfile` 群をコンストラクタ DI で束ねる還元率ポリシー実装。"""

    def __init__(
        self,
        profiles: Mapping[str, RtpProfile],
        *,
        games: tuple[str, ...] = GAMES,
        levels: tuple[int, ...] = LEVELS,
        default_level: int = DEFAULT_LEVEL,
        step: float = STEP,
        min_bet: int = MIN_BET,
    ) -> None:
        if default_level not in levels:
            raise ValueError(f"default_level {default_level} must be in {levels}")
        self._profiles = profiles
        self.games = games
        self.levels = levels
        self.default_level = default_level
        self.step = step
        self.min_bet = min_bet

    def base_stats(self, game: str, kind: Optional[str] = None) -> tuple[float, float]:
        profile = self._profiles.get(game)
        if profile is None:
            raise KeyError(game)
        return profile(kind)

    def factor(self, game: str, level: int, kind: Optional[str] = None) -> float:
        _, w = self.base_stats(game, kind)
        return 1 + (level - self.default_level) * self.step / w

    def approx_rtp(self, game: str, level: int, kind: Optional[str] = None) -> float:
        rtp, _ = self.base_stats(game, kind)
        return rtp + (level - self.default_level) * self.step

    def adjust(self, game: str, level: int, bet: int, payout: int, kind: Optional[str] = None) -> int:
        if level == self.default_level or game not in self.games or payout <= bet:
            return payout
        return bet + max(0, int((payout - bet) * self.factor(game, level, kind)))


DEFAULT_RTP_POLICY = RtpPolicy(PROFILES)


def base_stats(game: str, kind: str | None = None) -> tuple[float, float]:
    """(いまの還元率, 勝ち分の平均 W)。"""
    return DEFAULT_RTP_POLICY.base_stats(game, kind)


def factor(game: str, level: int, kind: str | None = None) -> float:
    """勝ち分にかける倍率。"""
    return DEFAULT_RTP_POLICY.factor(game, level, kind)


def approx_rtp(game: str, level: int, kind: str | None = None) -> float:
    """そのレベルでの還元率の目安。"""
    return DEFAULT_RTP_POLICY.approx_rtp(game, level, kind)


def adjust(game: str, level: int, bet: int, payout: int, kind: str | None = None) -> int:
    """勝ったときだけ、勝ち分（払い戻し - 賭け金）を倍率で増減した払い戻しを返す。負け・引き分け・レベル3は、そのまま。"""
    return DEFAULT_RTP_POLICY.adjust(game, level, bet, payout, kind)
