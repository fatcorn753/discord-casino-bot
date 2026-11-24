"""配信（/配信）のアプリケーションサービス。スパチャ（送金と同じ規則）と、勝敗の賭け。"""

import datetime

from domain import stream
from domain.errors import Outcome, fail
from domain.ports import get_catalogs
from domain.ports.clock import IClock
from domain.ports.contracts import StreamSettleResult, StreamView
from domain.ports.repos import IStreamRepo, IWalletBalanceRepo
from domain.types import TransferResult

_TRANSFER = get_catalogs().economy().transfer


class StreamService:
    """配信パネル・スパチャ・勝敗の賭け。"""

    def __init__(self, streams: IStreamRepo, wallet: IWalletBalanceRepo, clock: IClock) -> None:
        self._streams = streams
        self._wallet = wallet
        self._clock = clock

    def _now(self) -> float:
        return self._clock.now_ts()

    def start(self, guild_id: int, streamer: int, title: str, url: str, channel_id: int) -> Outcome[int]:
        return self._streams.for_guild(guild_id).stream_start(streamer, title, url, channel_id, self._now())

    def set_message(self, guild_id: int, stream_id: int, channel_id: int, message_id: int) -> None:
        self._streams.for_guild(guild_id).stream_set_message(stream_id, channel_id, message_id)

    def get(self, guild_id: int, stream_id: int) -> StreamView | None:
        return self._streams.for_guild(guild_id).stream_get(stream_id)

    def balance_of(self, guild_id: int, user_id: int) -> int:
        return self._wallet.for_guild(guild_id).balance(user_id)

    def tip(self, guild_id: int, user_id: int, stream_id: int, amount: int) -> tuple[TransferResult, StreamView | None]:
        """スパチャ。配信者への送金として送る（手数料・1日の上限・遊んだ回数の条件は、送金と同じ）。"""
        s = self.get(guild_id, stream_id)
        if s is None or s["status"] != "live":
            return TransferResult(False, fail("stream_ended")), s
        if s["streamer"] == user_id:
            return TransferResult(False, fail("stream_self_tip")), s
        if amount < stream.TIP_MIN:
            return TransferResult(False, fail("stream_tip_min", min=stream.TIP_MIN)), s
        now = self._clock.now()
        day_start = datetime.datetime.combine(now.date(), datetime.time(), tzinfo=now.tzinfo).timestamp()
        res = self._wallet.for_guild(guild_id).transfer(
            user_id,
            s["streamer"],
            amount,
            _TRANSFER.fee_rate,
            _TRANSFER.daily_limit,
            day_start,
            _TRANSFER.min_games,
        )
        if not res.ok:
            return res, s
        self._streams.for_guild(guild_id).stream_add_tip(stream_id, amount)
        return res, self.get(guild_id, stream_id)

    def open_round(self, guild_id: int, streamer: int, stream_id: int, label: str) -> Outcome[StreamView]:
        return self._streams.for_guild(guild_id).stream_round_open(streamer, stream_id, label, self._now())

    def lock_round(self, guild_id: int, streamer: int, stream_id: int) -> Outcome[StreamView]:
        return self._streams.for_guild(guild_id).stream_round_lock(streamer, stream_id)

    def bet(self, guild_id: int, user_id: int, stream_id: int, side: str, amount: int) -> Outcome[StreamView]:
        return self._streams.for_guild(guild_id).stream_bet(user_id, stream_id, side, amount, self._now())

    def settle(self, guild_id: int, streamer: int, stream_id: int, winner: str | None) -> Outcome[StreamSettleResult]:
        return self._streams.for_guild(guild_id).stream_round_settle(streamer, stream_id, winner, self._now())

    def end(self, guild_id: int, streamer: int, stream_id: int) -> Outcome[StreamSettleResult | None]:
        return self._streams.for_guild(guild_id).stream_end(streamer, stream_id, self._now())

    def close_due(self, guild_id: int) -> list[tuple[StreamView, StreamSettleResult | None]]:
        return self._streams.for_guild(guild_id).stream_close_due(self._now())
