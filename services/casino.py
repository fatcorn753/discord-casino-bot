"""カジノゲームのユースケース調整（開始検証・escrow預かり・RTP/イベント計算・精算・通知・成績記録）。"""

import datetime
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Optional

from domain import events, skins
from domain.games import rtplevel
from domain.ports import ICasinoPort, IClock, ISessionGuard


@dataclass(frozen=True)
class BeginGameResult:
    ok: bool
    reason_kind: str = ""  # "session" | "min_bet" | "balance_factor" | "balance" | "place_bet"
    reason: str = ""
    balance: int = 0
    escrow_id: int = 0
    skin: Optional[skins.Skin] = None


@dataclass(frozen=True)
class SettleGameResult:
    duplicate: bool
    net: int = 0
    penalty_taken: int = 0
    insured_refund: int = 0
    event_bonus: int = 0
    rtp_shift: int = 0
    notices: tuple[Any, ...] = ()
    events: dict[str, Any] | None = None


class CasinoService:
    """カジノ 1 プレイの開始〜精算〜中断のアプリケーションサービス。"""

    def __init__(self, economy: ICasinoPort, sessions: ISessionGuard, clock: IClock) -> None:
        self._economy = economy
        self._sessions = sessions
        self._clock = clock

    def for_guild(self, guild_id: int) -> ICasinoPort:
        return self._economy.for_guild(guild_id)

    def today(self) -> datetime.date:
        return self._clock.today()

    def now(self) -> datetime.datetime:
        return self._clock.now()

    def begin_game(
        self,
        guild_id: int,
        user_id: int,
        bet: int,
        game: str,
        balance_factor: int = 1,
    ) -> BeginGameResult:
        """排他セッションを取得し、賭け金を検証して預かる。失敗時はセッションを解放して返す。"""
        eco = self._economy.for_guild(guild_id)
        reason = self._sessions.try_start(user_id)
        if reason:
            return BeginGameResult(False, reason_kind="session", reason=reason)
        escrow: Optional[int] = None
        try:
            if bet < rtplevel.MIN_BET:
                return BeginGameResult(False, reason_kind="min_bet")
            balance = eco.balance(user_id)
            if balance < bet * balance_factor:
                kind = "balance_factor" if (balance_factor > 1 and balance >= bet) else "balance"
                return BeginGameResult(False, reason_kind=kind, balance=balance)
            escrow = eco.place_bet(user_id, bet, game)
            if escrow is None:
                return BeginGameResult(False, reason_kind="place_bet", balance=eco.balance(user_id))
            skin = skins.active_skin(eco.get_profile(user_id), game)
            return BeginGameResult(True, escrow_id=escrow, skin=skin, balance=balance)
        finally:
            if escrow is None:
                self._sessions.end(user_id)

    def settle_game(
        self,
        guild_id: int,
        user_id: int,
        escrow_id: int,
        game: str,
        bet: int,
        payout: int,
        *,
        penalty: int = 0,
        keep_session: bool = False,
        tags: Iterable[Any] = (),
        rtp_kind: Optional[str] = None,
        rtp_done: bool = False,
        now: Optional[datetime.datetime] = None,
    ) -> SettleGameResult:
        """RTP・突発イベント補正を適用して預かり金を精算し、通知とミッション/連勝記録を更新する。"""
        eco = self._economy.for_guild(guild_id)
        current_now = self._clock.now() if now is None else now
        rtp_shift = 0
        level = eco.rtp_level_get(game)
        if level != rtplevel.DEFAULT_LEVEL and not rtp_done:
            adjusted = rtplevel.adjust(game, level, eco.escrow_amount(escrow_id), payout, rtp_kind)
            rtp_shift, payout = adjusted - payout, adjusted
        event_bonus = 0
        boost = events.best_boost(game, events.active(guild_id, current_now), eco.manual_event_get(time.time()))
        if boost:
            event_bonus = events.bonus(bet, payout, boost)
            payout += event_bonus
        try:
            result = eco.settle_with_penalty(escrow_id, payout, game, penalty)
        except Exception:
            self._sessions.end(user_id)
            raise
        if not keep_session:
            self._sessions.end(user_id)
        if result is None:
            print(f"警告: 精算済みの預かり金を再度精算しようとしました（{game}, escrow={escrow_id}）")
            return SettleGameResult(duplicate=True, event_bonus=event_bonus, rtp_shift=rtp_shift)
        net, penalty_taken, insured_refund = result
        notices = tuple(eco.take_notices(user_id))
        ev_result: dict[str, Any] = {}
        try:
            ev_result = eco.record_play(user_id, game, bet, net, current_now.date(), tags)
        except Exception as exc:
            print(f"連勝・ミッションの記録に失敗: {game}: {exc!r}")
        return SettleGameResult(
            duplicate=False,
            net=net,
            penalty_taken=penalty_taken,
            insured_refund=insured_refund,
            event_bonus=event_bonus,
            rtp_shift=rtp_shift,
            notices=notices,
            events=ev_result,
        )

    def release_session(self, user_id: int) -> None:
        self._sessions.end(user_id)

    def abort_game(self, guild_id: int, user_id: int, escrow_id: int) -> None:
        eco = self._economy.for_guild(guild_id)
        try:
            eco.refund(escrow_id)
        finally:
            self._sessions.end(user_id)
