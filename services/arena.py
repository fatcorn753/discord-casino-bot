"""地下闘技場の大会進行・賭け・状況照会のアプリケーションサービス。"""

import datetime
import random
from typing import Any, Optional

from domain.ports.clock import IClock
from domain.ports.repos import IArenaRepo


class ArenaService:
    """地下闘技場の大会時刻・生成・賭け実行の調停役。"""

    def __init__(self, arena: IArenaRepo, clock: IClock) -> None:
        self._arena = arena
        self.clock = clock

    def local_hour(self, now: Optional[float] = None) -> int:
        """現地時間（設定の utc_offset_hours）での「時」。準決勝・決勝の定時進行に使う。"""
        ts = self.clock.now_ts() if now is None else now
        tz = datetime.timezone(datetime.timedelta(hours=self.clock.offset_hours()))
        return datetime.datetime.fromtimestamp(ts, tz).hour

    def ensure_tournament(self, guild_id: int, fmt: str, now: Optional[float] = None) -> dict[str, Any]:
        ts = self.clock.now_ts() if now is None else now
        return self._arena.for_guild(guild_id).arena_tournament_ensure(fmt, ts, self.clock.offset_hours())

    def current_tournament(self, guild_id: int, fmt: str, now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """今日の大会（無ければ作らずに None）。闘技場を止めているあいだの見回り用。"""
        ts = self.clock.now_ts() if now is None else now
        repo = self._arena.for_guild(guild_id)
        return repo.arena_tournament_get_by_day(repo.arena_today(ts, self.clock.offset_hours()), fmt)

    def place_bet(
        self, guild_id: int, user_id: int, fighter_id: int, stake: int, now: Optional[float] = None
    ) -> tuple[Any, ...]:
        ts = self.clock.now_ts() if now is None else now
        return self._arena.for_guild(guild_id).arena_bet(user_id, fighter_id, stake, ts)

    def run_semis(
        self, guild_id: int, tournament_id: int, rng: Optional[random.Random] = None
    ) -> Optional[dict[str, Any]]:
        return self._arena.for_guild(guild_id).arena_run_semis(tournament_id, rng, self.clock.now_ts())

    def run_final(
        self, guild_id: int, tournament_id: int, rng: Optional[random.Random] = None
    ) -> Optional[dict[str, Any]]:
        return self._arena.for_guild(guild_id).arena_run_final(tournament_id, rng, self.clock.now_ts())

    def tournament_by_day(self, guild_id: int, day: str, fmt: str) -> Optional[dict[str, Any]]:
        return self._arena.for_guild(guild_id).arena_tournament_get_by_day(day, fmt)

    def bets_breakdown(self, guild_id: int, tournament_id: int) -> dict[int, list[dict[str, Any]]]:
        return self._arena.for_guild(guild_id).arena_bets_breakdown(tournament_id)
