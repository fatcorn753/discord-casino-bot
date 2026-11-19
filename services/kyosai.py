"""帝愛共済（#34）のアプリケーションサービス。"""

from typing import Any, Optional

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

    def buy_house(self, guild_id: int, user_id: int, units: int) -> tuple[Any, ...]:
        return self._kyosai.for_guild(guild_id).kyosai_buy_house(user_id, units, self._now())

    def offer(self, guild_id: int, seller: int, buyer: int, units: int, price: int) -> tuple[Any, ...]:
        return self._kyosai.for_guild(guild_id).kyosai_offer(seller, buyer, units, price, self._now())

    def accept(self, guild_id: int, buyer: int, offer_id: int) -> tuple[Any, ...]:
        return self._kyosai.for_guild(guild_id).kyosai_accept(buyer, offer_id, self._now())

    def cancel(self, guild_id: int, user_id: int, offer_id: int) -> bool:
        return self._kyosai.for_guild(guild_id).kyosai_cancel(user_id, offer_id)

    def propose(self, guild_id: int, user_id: int, premium: float) -> tuple[Any, ...]:
        return self._kyosai.for_guild(guild_id).kyosai_propose(user_id, premium, self._now())

    def vote(self, guild_id: int, user_id: int, yes: bool) -> tuple[Any, ...]:
        return self._kyosai.for_guild(guild_id).kyosai_vote(user_id, yes, self._now())

    def tick(self, guild_id: int) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
        """毎分: 1日ごとの配当（と休眠の回収）と、締切を過ぎた動議の締め。(配当の結果, 動議の結果)。"""
        repo = self._kyosai.for_guild(guild_id)
        now = self._now()
        return repo.kyosai_daily(now), repo.kyosai_close_motion(now)
