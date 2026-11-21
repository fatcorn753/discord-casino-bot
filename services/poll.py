"""投票（/投票）のアプリケーションサービス。"""

from typing import Optional

from domain.errors import Outcome
from domain.ports.clock import IClock
from domain.ports.contracts import PollRigResult, PollView
from domain.ports.repos import IPollRepo


class PollService:
    """誰でも作れる賛成・反対の投票。"""

    def __init__(self, polls: IPollRepo, clock: IClock) -> None:
        self._polls = polls
        self._clock = clock

    def _now(self) -> float:
        return self._clock.now_ts()

    def create(self, guild_id: int, creator: int, title: str, minutes: int, channel_id: int) -> Outcome[int]:
        now = self._now()
        return self._polls.for_guild(guild_id).poll_create(creator, title, now + minutes * 60, channel_id, now)

    def set_message(self, guild_id: int, poll_id: int, channel_id: int, message_id: int) -> None:
        self._polls.for_guild(guild_id).poll_set_message(poll_id, channel_id, message_id)

    def get(self, guild_id: int, poll_id: int) -> Optional[PollView]:
        return self._polls.for_guild(guild_id).poll_get(poll_id)

    def vote(self, guild_id: int, user_id: int, poll_id: int, yes: bool) -> Outcome[PollView]:
        return self._polls.for_guild(guild_id).poll_vote(user_id, poll_id, yes, self._now())

    def rig(self, guild_id: int, user_id: int, poll_id: int, yes: int, no: int) -> Outcome[PollRigResult]:
        return self._polls.for_guild(guild_id).poll_rig(user_id, poll_id, yes, no, self._now())

    def close_due(self, guild_id: int) -> list[PollView]:
        return self._polls.for_guild(guild_id).poll_close_due(self._now())
