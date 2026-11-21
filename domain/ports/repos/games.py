"""カジノ・イベント・競馬・地下闘技場・遺物のリポジトリポート契約。"""

import datetime
import random
from typing import TYPE_CHECKING

from domain.errors import Outcome
from domain.ports.contracts import (
    ArenaBetRow,
    ArenaFighterRow,
    ArenaFinalResult,
    ArenaSemiResult,
    ArenaTournamentDetailRow,
    AuctionHorseRow,
    HorseRow,
    ManualEventRow,
    PachinkoStats,
    RaceBetResult,
    RaceBetRow,
    RaceCloseResult,
    RaceEntryRow,
    RaceRow,
    RaceRunResult,
)
from domain.ports.repos.base import IGuildScopedRepo

if TYPE_CHECKING:
    from domain.games.pachinko import SetResult

if TYPE_CHECKING:
    from domain.types import PurchaseResult


class IRtpRepo(IGuildScopedRepo):
    def rtp_level_get(self, game: str) -> int:
        raise NotImplementedError

    def rtp_levels(self) -> dict[str, int]:
        raise NotImplementedError

    def rtp_level_set(self, game: str | None, level: int) -> list[str]:
        raise NotImplementedError


class IEventRepo(IGuildScopedRepo):
    def event_phase(self, day: datetime.date) -> int:
        raise NotImplementedError

    def set_event_phase(self, day: datetime.date, phase: int) -> None:
        raise NotImplementedError

    def manual_event_start(self, game: str, boost_pct: int, starts: float, ends: float, channel_id: int = 0) -> None:
        raise NotImplementedError

    def manual_event_get(self, now: float) -> ManualEventRow | None:
        raise NotImplementedError

    def manual_event_stop(self, now: float) -> ManualEventRow | None:
        raise NotImplementedError

    def manual_event_finish_due(self, now: float) -> ManualEventRow | None:
        raise NotImplementedError


class IPachinkoRepo(IGuildScopedRepo):
    def pachinko_record_set(
        self, user_id: int, bet: int, balls: int, result: "SetResult", now: float | None = None
    ) -> None:
        raise NotImplementedError

    def pachinko_stats(self, guild_id: int | None = None, since: float | None = None) -> PachinkoStats:
        raise NotImplementedError


class IRelicRepo(IGuildScopedRepo):
    def relic_buffs_for(self, guild_id: int, user_id: int) -> dict[str, float]:
        raise NotImplementedError

    def relic_stock_for(self, guild_id: int, user_id: int) -> dict[str, int]:
        raise NotImplementedError

    def relic_stock(self, user_id: int) -> dict[str, int]:
        raise NotImplementedError

    def relic_merge(self, user_id: int, item_id: str) -> Outcome[str]:
        raise NotImplementedError


class IHorseRepo(IGuildScopedRepo):
    HORSE_PRICE: int

    def horse_create(self, user_id: int, name: str, coat: str, silks: str) -> Outcome[int]:
        raise NotImplementedError

    def horse_list(self, user_id: int) -> list[HorseRow]:
        raise NotImplementedError

    def horse_get(self, horse_id: int) -> HorseRow | None:
        raise NotImplementedError

    def horse_carrot(self, user_id: int, horse_id: int, src: str, dst: str) -> Outcome[dict[str, int]]:
        raise NotImplementedError

    def horse_retire(self, user_id: int, horse_id: int) -> Outcome[int]:
        raise NotImplementedError

    def horse_set_style(self, user_id: int, horse_id: int, style: str | None) -> Outcome[str]:
        raise NotImplementedError

    def horse_gold_carrot(self, user_id: int, horse_id: int, first: str, second: str) -> Outcome[dict[str, int]]:
        raise NotImplementedError

    def horse_auction_price(self, listed_at: float, now: float) -> int:
        raise NotImplementedError

    def auction_horses(self, now: float) -> list[AuctionHorseRow]:
        raise NotImplementedError

    def auction_buy_horse(self, user_id: int, horse_id: int, now: float) -> "PurchaseResult":
        raise NotImplementedError


class IRaceRepo(IGuildScopedRepo):
    def race_get(self, race_id: int) -> RaceRow | None:
        raise NotImplementedError

    def race_number(self, race_id: int) -> int:
        raise NotImplementedError

    def race_open(self) -> list[RaceRow]:
        raise NotImplementedError

    def race_slots(self, now: float, tz_hours: float) -> list[float]:
        raise NotImplementedError

    def race_ensure_scheduled(self, now: float, tz_hours: float, channel_id: int = 0) -> int | None:
        raise NotImplementedError

    def race_create_special(self, now: float, entry_min: int, bet_min: int, channel_id: int = 0) -> int:
        raise NotImplementedError

    def race_set_message(self, race_id: int, channel_id: int, message_id: int) -> None:
        raise NotImplementedError

    def race_entries(self, race_id: int, registered_only: bool = False) -> list[RaceEntryRow]:
        raise NotImplementedError

    def race_register(self, user_id: int, horse_id: int, race_id: int, now: float) -> Outcome[str]:
        raise NotImplementedError

    def race_withdraw(self, user_id: int, horse_id: int, race_id: int, now: float) -> Outcome[str]:
        raise NotImplementedError

    def race_close_entries(self, race_id: int, rng: random.Random | None = None) -> RaceCloseResult | None:
        raise NotImplementedError

    def race_set_odds(self, race_id: int, odds: dict[str, dict[str, float]] | list[tuple[float, float]]) -> None:
        raise NotImplementedError

    def race_odds(self, race_id: int, kind: str, lanes: list[int]) -> float | None:
        raise NotImplementedError

    def race_bets(self, user_id: int, race_id: int) -> list[RaceBetRow]:
        raise NotImplementedError

    def race_bet(
        self, user_id: int, race_id: int, kind: str, lane: int | list[int], stake: int, now: float
    ) -> Outcome[RaceBetResult]:
        raise NotImplementedError

    def race_bet_many(
        self, user_id: int, race_id: int, kind: str, lanes: list[int], stake: int, now: float
    ) -> Outcome[list[RaceBetResult]]:
        raise NotImplementedError

    def race_run(self, race_id: int, tz_hours: float, rng: random.Random | None = None) -> RaceRunResult | None:
        raise NotImplementedError


class IArenaRepo(IGuildScopedRepo):
    def arena_today(self, now: float, tz_hours: float) -> str:
        raise NotImplementedError

    def arena_tournament_ensure(
        self, fmt: str, now: float, tz_hours: float, rng: random.Random | None = None
    ) -> ArenaTournamentDetailRow:
        raise NotImplementedError

    def arena_tournaments_ensure_today(
        self, now: float, tz_hours: float, rng: random.Random | None = None
    ) -> list[ArenaTournamentDetailRow]:
        raise NotImplementedError

    def arena_tournament_get_by_day(self, day: str, fmt: str) -> ArenaTournamentDetailRow | None:
        raise NotImplementedError

    def arena_fighters(self, tournament_id: int) -> list[ArenaFighterRow]:
        raise NotImplementedError

    def arena_bet(self, user_id: int, fighter_id: int, amount: int, now: float) -> Outcome[str]:
        raise NotImplementedError

    def arena_run_semis(
        self, tournament_id: int, rng: random.Random | None = None, now: float | None = None
    ) -> ArenaSemiResult | None:
        raise NotImplementedError

    def arena_run_final(
        self, tournament_id: int, rng: random.Random | None = None, now: float | None = None
    ) -> ArenaFinalResult | None:
        raise NotImplementedError

    def arena_my_bets(self, tournament_id: int, user_id: int) -> list[ArenaBetRow]:
        raise NotImplementedError

    def arena_bets_breakdown(self, tournament_id: int) -> dict[int, list[ArenaBetRow]]:
        raise NotImplementedError
