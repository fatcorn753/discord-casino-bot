"""会社（帝愛の子会社）の設立・出資・出荷・増築・定時精算のアプリケーションサービス。"""

from typing import Any, Optional

from domain.errors import Outcome
from domain.ports.clock import IClock
from domain.ports.repos import ICompanyRepo


class CompanyService:
    """会社のユースケース。時刻は注入された IClock から取り、Cog に時刻を扱わせない。"""

    def __init__(self, company: ICompanyRepo, clock: IClock) -> None:
        self._company = company
        self._clock = clock

    def _repo(self, guild_id: int) -> ICompanyRepo:
        return self._company.for_guild(guild_id)

    def get(self, guild_id: int, user_id: int) -> Optional[dict[str, Any]]:
        return self._repo(guild_id).company_get(user_id, self._clock.now_ts())

    def found(self, guild_id: int, user_id: int, name: str, kind: str) -> Outcome[dict[str, Any]]:
        return self._repo(guild_id).company_found(user_id, name, kind, self._clock.now_ts())

    def invest(self, guild_id: int, user_id: int, amount: int) -> Outcome[int]:
        return self._repo(guild_id).company_invest(user_id, amount, self._clock.now_ts())

    def withdraw(self, guild_id: int, user_id: int, amount: Optional[int]) -> Outcome[int]:
        """amount が None なら全額を引き出す。"""
        return self._repo(guild_id).company_withdraw(user_id, amount, self._clock.now_ts())

    def ship(self, guild_id: int, user_id: int) -> Outcome[dict[str, Any]]:
        return self._repo(guild_id).company_ship(user_id, self._clock.now_ts())

    def upgrade(self, guild_id: int, user_id: int) -> Outcome[int]:
        return self._repo(guild_id).company_upgrade(user_id, self._clock.now_ts())

    def change_kind(self, guild_id: int, user_id: int, kind: str) -> Outcome[str]:
        return self._repo(guild_id).company_change_kind(user_id, kind, self._clock.now_ts())

    # ------------------------------ ほかの人の会社への出資（#71） ------------------------------ #
    def stake_invest(self, guild_id: int, investor_id: int, owner_id: int, amount: int) -> Outcome[dict[str, Any]]:
        return self._repo(guild_id).stake_invest(investor_id, owner_id, amount, self._clock.now_ts())

    def stake_holdings(self, guild_id: int, investor_id: int) -> list[dict[str, Any]]:
        return self._repo(guild_id).stake_holdings(investor_id, self._clock.now_ts())

    def set_dividend(self, guild_id: int, user_id: int, rate: float) -> Outcome[float]:
        return self._repo(guild_id).company_set_dividend(user_id, rate, self._clock.now_ts())

    def settle_all(self, guild_id: int) -> list[dict[str, Any]]:
        """維持費の精算。倒産した会社の一覧を返す。"""
        return self._repo(guild_id).company_settle_all(self._clock.now_ts())
