"""競馬のレース進行・オッズ計算・スケジュール管理のアプリケーションサービス。"""

import datetime
from typing import Any, Optional

from domain import horse as horse_mod
from domain.errors import Outcome
from domain.ports.clock import IClock
from domain.ports.repos import IHorseRepo, IRaceRepo, IShopRepo


class HorseDesk:
    """1 つのサーバーの競馬窓口（出走表・馬券・厩舎）。Cog は Repository ではなく、これを通して読み書きする。

    時刻を引数に取る操作には、呼び出し側が clock.now_ts() を渡す（Cog は time.time() を直接呼ばない）。
    """

    def __init__(self, horses: IHorseRepo, races: IRaceRepo, shop: IShopRepo, clock: IClock) -> None:
        self.clock = clock
        self._horses = horses
        self._races = races
        self._shop = shop

    # ------------------------------ レース・馬券 ------------------------------ #
    def race_get(self, race_id: int) -> Optional[dict[str, Any]]:
        return self._races.race_get(race_id)

    def race_number(self, race_id: int) -> int:
        return self._races.race_number(race_id)

    def race_open(self) -> list[dict[str, Any]]:
        return self._races.race_open()

    def race_entries(self, race_id: int) -> list[dict[str, Any]]:
        return self._races.race_entries(race_id)

    def race_odds(self, race_id: int, kind: str, lanes: list[int]) -> Optional[float]:
        return self._races.race_odds(race_id, kind, lanes)

    def race_bets(self, user_id: int, race_id: int) -> list[dict[str, Any]]:
        return self._races.race_bets(user_id, race_id)

    def race_bet(
        self, user_id: int, race_id: int, kind: str, lane: int | list[int], stake: int, now: float
    ) -> Outcome[dict[str, Any]]:
        return self._races.race_bet(user_id, race_id, kind, lane, stake, now)

    def race_bet_many(
        self, user_id: int, race_id: int, kind: str, lanes: list[int], stake: int, now: float
    ) -> Outcome[list[dict[str, Any]]]:
        return self._races.race_bet_many(user_id, race_id, kind, lanes, stake, now)

    def race_set_message(self, race_id: int, channel_id: int, message_id: int) -> None:
        self._races.race_set_message(race_id, channel_id, message_id)

    def race_create_special(self, now: float, entry_min: int, bet_min: int, channel_id: int = 0) -> int:
        return self._races.race_create_special(now, entry_min, bet_min, channel_id)

    def race_register(self, user_id: int, horse_id: int, race_id: int, now: float) -> Outcome[str]:
        return self._races.race_register(user_id, horse_id, race_id, now)

    def race_withdraw(self, user_id: int, horse_id: int, race_id: int, now: float) -> Outcome[str]:
        return self._races.race_withdraw(user_id, horse_id, race_id, now)

    # ------------------------------ 厩舎 ------------------------------ #
    def horse_get(self, horse_id: int) -> Optional[dict[str, Any]]:
        return self._horses.horse_get(horse_id)

    def horse_list(self, user_id: int) -> list[dict[str, Any]]:
        return self._horses.horse_list(user_id)

    def horse_create(self, user_id: int, name: str, coat: str, silks: str) -> Outcome[int]:
        return self._horses.horse_create(user_id, name, coat, silks)

    def horse_set_style(self, user_id: int, horse_id: int, style: Optional[str]) -> Outcome[str]:
        return self._horses.horse_set_style(user_id, horse_id, style)

    def horse_carrot(self, user_id: int, horse_id: int, src: str, dst: str) -> Outcome[dict[str, int]]:
        return self._horses.horse_carrot(user_id, horse_id, src, dst)

    def horse_gold_carrot(self, user_id: int, horse_id: int, first: str, second: str) -> Outcome[dict[str, int]]:
        return self._horses.horse_gold_carrot(user_id, horse_id, first, second)

    def horse_retire(self, user_id: int, horse_id: int) -> Outcome[int]:
        return self._horses.horse_retire(user_id, horse_id)

    def is_major_race(self, race: dict[str, Any]) -> bool:
        """重賞か（発走時刻を、設定の時差の現地時間で見る）。"""
        return horse_mod.is_major_race(race, self.clock.offset_hours())

    def local_time(self, ts: float) -> datetime.datetime:
        """ts を、設定の時差（utc_offset_hours）の現地時刻にする。"""
        return datetime.datetime.fromtimestamp(
            ts, datetime.timezone(datetime.timedelta(hours=self.clock.offset_hours()))
        )

    def inventory(self, user_id: int) -> dict[str, Any]:
        """にんじんなど、馬に使う持ち物を見るための持ち物一覧。"""
        return self._shop.inventory(user_id)


class HorseService:
    """定時レースのスケジュール作成・締切オッズ確定・発走精算の調整役。"""

    def __init__(
        self,
        races: IRaceRepo,
        clock: IClock,
        horses: Optional[IHorseRepo] = None,
        shop: Optional[IShopRepo] = None,
    ) -> None:
        self._races = races
        self.clock = clock
        self._horses = horses
        self._shop = shop

    def desk(self, guild_id: int) -> HorseDesk:
        """そのサーバーの競馬窓口。"""
        if self._horses is None or self._shop is None:
            raise RuntimeError("HorseService に horses・shop が渡されていません")
        return HorseDesk(
            self._horses.for_guild(guild_id),
            self._races.for_guild(guild_id),
            self._shop.for_guild(guild_id),
            self.clock,
        )

    def next_slots(self, guild_id: int, now: Optional[float] = None) -> list[float]:
        now = self.clock.now_ts() if now is None else now
        return self._races.for_guild(guild_id).race_slots(now, self.clock.offset_hours())

    def ensure_scheduled(self, guild_id: int, now: float, channel_id: int) -> Optional[int]:
        return self._races.for_guild(guild_id).race_ensure_scheduled(now, self.clock.offset_hours(), channel_id)

    def close_entries_and_set_odds(self, guild_id: int, race_id: int) -> Optional[dict[str, Any]]:
        """出走申込を締め切ってオッズを計算・保存し、最新の race 辞書を返す。"""
        races = self._races.for_guild(guild_id)
        field = races.race_close_entries(race_id)
        if field is None:
            return None
        races.race_set_odds(race_id, horse_mod.make_all_odds(field["runners"], field["track"]))
        return races.race_get(race_id)

    def run_race(self, guild_id: int, race_id: int) -> Optional[dict[str, Any]]:
        """レースを実行して賞金・馬券を精算し、演出結果辞書を返す。"""
        return self._races.for_guild(guild_id).race_run(race_id, self.clock.offset_hours())
