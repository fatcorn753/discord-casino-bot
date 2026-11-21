"""カジノゲームのユースケース調整（開始検証・escrow預かり・RTP/イベント計算・精算・通知・成績記録）。"""

import datetime
import logging
import random
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Optional

from domain import events as events_mod
from domain import skins
from domain.errors import Notice
from domain.games import jackpot
from domain.games.pachinko import SetResult
from domain.games.rtplevel import DEFAULT_RTP_POLICY, IRtpPolicy
from domain.ports.clock import IClock
from domain.ports.repos import (
    IEscrowRepo,
    IEventRepo,
    INoticeRepo,
    IPachinkoRepo,
    IRecordRepo,
    IRtpRepo,
    IShopRepo,
    IWalletRepo,
)
from domain.ports.session import ISessionGuard

_log = logging.getLogger(__name__)


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
    notices: tuple[Notice, ...] = ()
    events: dict[str, Any] | None = None
    jackpot_won: int = 0  # 共有ジャックポットを当てた額（当たらなければ 0）
    jackpot_pool: int = 0  # 精算のあとの、ジャックポットの積み立て


class CasinoService:
    """カジノ 1 プレイの開始〜精算〜中断のアプリケーションサービス。"""

    def __init__(
        self,
        wallet: IWalletRepo,
        escrow: IEscrowRepo,
        shop: IShopRepo,
        rtp: IRtpRepo,
        events: IEventRepo,
        notices: INoticeRepo,
        records: IRecordRepo,
        sessions: ISessionGuard,
        clock: IClock,
        rtp_policy: IRtpPolicy = DEFAULT_RTP_POLICY,
        pachinko: Optional[IPachinkoRepo] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        self._wallet = wallet
        self._escrow = escrow
        self._shop = shop
        self._rtp = rtp
        self._events = events
        self._notices = notices
        self._records = records
        self.sessions = sessions
        self.clock = clock
        self._rtp_policy = rtp_policy
        self._pachinko = pachinko
        self._rng = rng or random.Random()  # ジャックポットの抽選

    def begin_game(
        self,
        guild_id: int,
        user_id: int,
        bet: int,
        game: str,
        balance_factor: int = 1,
    ) -> BeginGameResult:
        """排他セッションを取得し、賭け金を検証して預かる。失敗時はセッションを解放して返す。"""
        wallet = self._wallet.for_guild(guild_id)
        escrow_repo = self._escrow.for_guild(guild_id)
        shop_repo = self._shop.for_guild(guild_id)
        reason = self.sessions.try_start(user_id)
        if reason:
            return BeginGameResult(False, reason_kind="session", reason=reason)
        escrow_id: Optional[int] = None
        try:
            if bet < self._rtp_policy.min_bet:
                return BeginGameResult(False, reason_kind="min_bet")
            balance = wallet.balance(user_id)
            if balance < bet * balance_factor:
                kind = "balance_factor" if (balance_factor > 1 and balance >= bet) else "balance"
                return BeginGameResult(False, reason_kind=kind, balance=balance)
            escrow_id = escrow_repo.place_bet(user_id, bet, game)
            if escrow_id is None:
                return BeginGameResult(False, reason_kind="place_bet", balance=wallet.balance(user_id))
            skin = skins.skin_for(shop_repo.get_profile(user_id), game, self.clock.now())
            return BeginGameResult(True, escrow_id=escrow_id, skin=skin, balance=balance)
        finally:
            if escrow_id is None:
                self.sessions.end(user_id)

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
        tags: Iterable[str] = (),
        rtp_kind: Optional[str] = None,
        rtp_done: bool = False,
        now: Optional[datetime.datetime] = None,
    ) -> SettleGameResult:
        """RTP・突発イベント補正を適用して預かり金を精算し、通知とミッション/連勝記録を更新する。"""
        rtp_repo = self._rtp.for_guild(guild_id)
        escrow_repo = self._escrow.for_guild(guild_id)
        event_repo = self._events.for_guild(guild_id)
        notice_repo = self._notices.for_guild(guild_id)
        record_repo = self._records.for_guild(guild_id)
        current_now = self.clock.now() if now is None else now
        rtp_shift = 0
        level = rtp_repo.rtp_level_get(game)
        if level != self._rtp_policy.default_level and not rtp_done:
            adjusted = self._rtp_policy.adjust(game, level, escrow_repo.escrow_amount(escrow_id), payout, rtp_kind)
            rtp_shift, payout = adjusted - payout, adjusted
        event_bonus = 0
        boost = events_mod.best_boost(
            game, events_mod.active(guild_id, current_now), event_repo.manual_event_get(current_now.timestamp())
        )
        if boost:
            event_bonus = events_mod.bonus(bet, payout, boost)
            payout += event_bonus
        try:
            result = escrow_repo.settle_with_penalty(escrow_id, payout, game, penalty)
        except Exception:
            self.sessions.end(user_id)
            raise
        if not keep_session:
            self.sessions.end(user_id)
        if result is None:
            _log.warning("精算済みの預かり金を再度精算しようとしました（%s, escrow=%s）", game, escrow_id)
            return SettleGameResult(duplicate=True, event_bonus=event_bonus, rtp_shift=rtp_shift)
        net, penalty_taken, insured_refund = result
        notices = tuple(notice_repo.take_notices(user_id))
        ev_result: dict[str, Any] = {}
        try:
            ev_result = record_repo.record_play(user_id, game, bet, net, current_now.date(), tags)
        except Exception:
            _log.exception("連勝・ミッションの記録に失敗: %s", game)
        won, pool = self._draw_jackpot(escrow_repo, user_id, bet)
        return SettleGameResult(
            duplicate=False,
            net=net,
            penalty_taken=penalty_taken,
            insured_refund=insured_refund,
            event_bonus=event_bonus,
            rtp_shift=rtp_shift,
            notices=notices,
            events=ev_result,
            jackpot_won=won,
            jackpot_pool=pool,
        )

    def _draw_jackpot(self, escrow_repo: IEscrowRepo, user_id: int, bet: int) -> tuple[int, int]:
        """共有ジャックポットの抽選。(当てた額, 抽選のあとの積み立て)。失敗しても、精算そのものは済んでいる。"""
        try:
            pool = escrow_repo.jackpot_pool()
            if jackpot.draw(self._rng, bet, pool):
                return escrow_repo.jackpot_claim(user_id), escrow_repo.jackpot_pool()
            return 0, pool
        except Exception:
            _log.exception("ジャックポットの抽選に失敗")
            return 0, 0

    def abort_game(self, guild_id: int, user_id: int, escrow_id: int) -> None:
        escrow_repo = self._escrow.for_guild(guild_id)
        try:
            escrow_repo.refund(escrow_id)
        finally:
            self.sessions.end(user_id)

    # ------------------------------ ゲーム中の参照・追加の賭け ------------------------------ #
    def balance_of(self, guild_id: int, user_id: int) -> int:
        return self._wallet.for_guild(guild_id).balance(user_id)

    def escrow_amount(self, guild_id: int, escrow_id: int) -> int:
        return self._escrow.for_guild(guild_id).escrow_amount(escrow_id)

    def add_to_escrow(self, guild_id: int, escrow_id: int, extra: int, credit: bool = False) -> bool:
        """預かり金を足す（ブラックジャックのダブル）。credit なら足りない分を帝愛から借りる。"""
        return self._escrow.for_guild(guild_id).add_to_escrow(escrow_id, extra, credit=credit)

    def rtp_level(self, guild_id: int, game: str) -> int:
        return self._rtp.for_guild(guild_id).rtp_level_get(game)

    def record_pachinko_set(self, guild_id: int, user_id: int, bet: int, balls: int, result: SetResult) -> None:
        """パチンコの統計用の記録（記録先が無い構成では何もしない）。"""
        if self._pachinko is not None:
            self._pachinko.for_guild(guild_id).pachinko_record_set(user_id, bet, balls, result)
