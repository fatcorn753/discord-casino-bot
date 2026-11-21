"""帝愛共済（#34）のアプリケーションサービス。"""

from typing import Any, Optional

from domain.errors import Outcome
from domain.ports.clock import IClock
from domain.ports.repos import IKyosaiRepo


class KyosaiService:
    """共済の口・配当・譲渡・動議。"""

    def __init__(self, kyosai: IKyosaiRepo, clock: IClock) -> None:
        self._kyosai = kyosai
        self._clock = clock

    def _now(self) -> float:
        return self._clock.now_ts()

    def status(self, guild_id: int, user_id: int) -> dict[str, Any]:
        return self._kyosai.for_guild(guild_id).kyosai_status(user_id, self._now())

    def offers(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        return self._kyosai.for_guild(guild_id).kyosai_offers_of(user_id, self._now())

    def bid(self, guild_id: int, user_id: int, amount: int) -> Outcome[dict[str, Any]]:
        return self._kyosai.for_guild(guild_id).kyosai_bid(user_id, amount, self._now())

    def offer(self, guild_id: int, seller: int, buyer: int, units: int, price: int) -> Outcome[int]:
        return self._kyosai.for_guild(guild_id).kyosai_offer(seller, buyer, units, price, self._now())

    def accept(self, guild_id: int, buyer: int, offer_id: int) -> Outcome[dict[str, Any]]:
        return self._kyosai.for_guild(guild_id).kyosai_accept(buyer, offer_id, self._now())

    def cancel(self, guild_id: int, user_id: int, offer_id: int) -> bool:
        return self._kyosai.for_guild(guild_id).kyosai_cancel(user_id, offer_id)

    def propose(self, guild_id: int, user_id: int, premium: float) -> Outcome[dict[str, Any]]:
        return self._kyosai.for_guild(guild_id).kyosai_propose(user_id, premium, self._now())

    def vote(self, guild_id: int, user_id: int, yes: bool, motion_id: Optional[int] = None) -> Outcome[int]:
        return self._kyosai.for_guild(guild_id).kyosai_vote(user_id, yes, self._now(), motion_id)

    def set_motion_message(self, guild_id: int, motion_id: int, channel_id: int, message_id: int) -> None:
        self._kyosai.for_guild(guild_id).kyosai_set_motion_message(motion_id, channel_id, message_id)

    def motion(self, guild_id: int, motion_id: int) -> Optional[dict[str, Any]]:

        return self._kyosai.for_guild(guild_id).kyosai_motion(motion_id, self._now())

    def tick(self, guild_id: int) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """毎分: 1日ごとの配当（と休眠の回収）、締切を過ぎた動議の締め、帝愛の持ち口の競り。

        (配当の結果, 締めた動議の結果, 競りで起きたこと)。
        """
        repo = self._kyosai.for_guild(guild_id)
        now = self._now()
        return repo.kyosai_daily(now), repo.kyosai_close_motions(now), repo.kyosai_auction_tick(now)
