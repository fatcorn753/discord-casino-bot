"""地下作業のアプリケーションサービス（日付キー解決・クリック不正検知レポート）。"""

from domain import labor as labor_mod
from domain.errors import Outcome
from domain.ports.clock import IClock
from domain.ports.contracts import (
    LaborClick,
    LaborClickReport,
    LaborCollectDetail,
    LaborState,
    LaborTop,
)
from domain.ports.repos import ILaborRepo, IRelicRepo


class LaborService:
    """地下作業の日付キー・クリック分析レポート取得を担う。"""

    def __init__(self, labor: ILaborRepo, clock: IClock, relics: IRelicRepo | None = None) -> None:
        self._labor = labor
        self.clock = clock
        self._relics = relics

    def today_key(self) -> str:
        return self.clock.today().isoformat()

    def click_report(self, guild_id: int, user_id: int, hours: int) -> LaborClickReport:
        rows = self._labor.for_guild(guild_id).labor_click_hours(user_id, self.clock.now_ts() - hours * 3600)
        return labor_mod.click_report(rows)

    # ------------------------------ 作業場の操作（時刻は、画面の操作の時刻を受け取る） ------------------------------ #
    def _repo(self, guild_id: int) -> ILaborRepo:
        return self._labor.for_guild(guild_id)

    def state(self, guild_id: int, user_id: int, now: float) -> LaborState:
        return self._repo(guild_id).labor_state(user_id, now, self.today_key())

    def click(self, guild_id: int, user_id: int, now: float) -> LaborClick:
        return self._repo(guild_id).labor_click(user_id, now, self.today_key())

    def drink(self, guild_id: int, user_id: int, now: float) -> Outcome[str]:
        return self._repo(guild_id).labor_drink(user_id, now, self.today_key())

    def collect(self, guild_id: int, user_id: int, now: float) -> LaborCollectDetail:
        """稼ぎ・鉱石・発掘品を受け取る（内訳つき）。"""
        # detail=True なら必ず内訳の辞書が返る
        return self._repo(guild_id).labor_collect(user_id, now, self.today_key(), detail=True)

    def hire(self, guild_id: int, user_id: int, kind: str, now: float) -> Outcome[str]:
        return self._repo(guild_id).labor_hire(user_id, kind, now, self.today_key())

    def dismiss(self, guild_id: int, user_id: int, kind: str, now: float) -> Outcome[str]:
        return self._repo(guild_id).labor_dismiss(user_id, kind, now, self.today_key())

    def set_renew(self, guild_id: int, user_id: int, on: bool, now: float) -> Outcome[str]:
        return self._repo(guild_id).labor_set_renew(user_id, on, now, self.today_key())

    def dorm(self, guild_id: int, user_id: int, now: float) -> Outcome[str]:
        return self._repo(guild_id).labor_dorm(user_id, now, self.today_key())

    def upgrade(self, guild_id: int, user_id: int, tool: str, now: float) -> Outcome[str]:
        return self._repo(guild_id).labor_upgrade(user_id, tool, now, self.today_key())

    def top(self, guild_id: int, daily: bool, user_id: int, limit: int = 10) -> LaborTop:
        """採掘ランキング。daily なら今日の分、そうでなければ通算。"""
        return self._repo(guild_id).labor_top(limit, self.today_key() if daily else None, user_id)

    def merge_relic(self, guild_id: int, user_id: int, item_id: str) -> Outcome[str]:
        """同じ発掘品を集めて、★が1つ増えた1個に合体する。"""
        if self._relics is None:
            raise RuntimeError("LaborService に relics が渡されていません")
        return self._relics.for_guild(guild_id).relic_merge(user_id, item_id)
