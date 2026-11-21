"""サービス層（Constructor DI / Bridge パターン）の単体テスト。

巨大ファサードに依存せず、狭小な名目的基底ポート（domain.ports.repos / bridges）を
実装したクライアント選択のスタブだけで各サービスを組み立てて検証できることを確認する。
"""

import datetime
import random
from typing import Any, Optional, Self, cast

from domain import shop as shop_mod
from domain.games.rtplevel import DEFAULT_RTP_POLICY
from domain.ports.clock import IClock
from domain.ports.contracts import (
    ArenaTournamentDetailRow,
    ManualEventRow,
    PlayRecord,
    ProfileView,
    RaceCloseResult,
    RaceRow,
    ShopStockInfo,
    TeiaiMarketListing,
    UserRow,
)
from domain.ports.repos import (
    IArenaRepo,
    IAuctionRepo,
    IBondRepo,
    ICryptoRepo,
    IDebtRepo,
    IEarningsRepo,
    IEscrowRepo,
    IEventRepo,
    IGuildRepo,
    IHorseRepo,
    ILaborRepo,
    ILoanRepo,
    INoticeRepo,
    IP2PRepo,
    IRaceRepo,
    IRecordRepo,
    IRtpRepo,
    IShopRepo,
    IWalletRepo,
)
from domain.ports.session import ISessionGuard
from domain.types import DailyResult, TransferResult
from services.arena import ArenaService
from services.casino import CasinoService
from services.economy import EconomyService
from services.horse import HorseService
from services.labor import LaborService
from services.loan import LoanService
from services.shop import ShopService


class FakeClock(IClock):
    def __init__(self, dt: datetime.datetime, offset: float = 9.0) -> None:
        self._dt = dt
        self._offset = offset

    def now(self) -> datetime.datetime:
        return self._dt

    def today(self) -> datetime.date:
        return self._dt.date()

    def offset_hours(self) -> float:
        return self._offset


class FakeSessions(ISessionGuard):
    cooldown = 0.0

    def __init__(self) -> None:
        self.active: set[int] = set()

    def try_start(self, user_id: int, now: Optional[float] = None) -> Optional[str]:
        if user_id in self.active:
            return "プレイ中"
        self.active.add(user_id)
        return None

    def end(self, user_id: int, now: Optional[float] = None) -> None:
        self.active.discard(user_id)

    def is_active(self, user_id: int) -> bool:
        return user_id in self.active


class StubWallet(IWalletRepo):
    def __init__(self, balances: dict[int, int]) -> None:
        self._balances = balances
        self.transfers: list[tuple[Any, ...]] = []

    def for_guild(self, guild_id: int) -> Self:
        return self

    def balance(self, user_id: int) -> int:
        return self._balances.get(user_id, 0)

    def peek_stats(self, user_id: int) -> Optional[UserRow]:
        return cast("UserRow", {"balance": self._balances.get(user_id, 0)}) if user_id in self._balances else None

    def claim_daily(self, user_id: int, today: datetime.date, slot: Optional[int] = None) -> DailyResult:
        return DailyResult(True, 500, 1, self._balances.get(user_id, 0) + 500)

    def transfer(
        self,
        sender: int,
        receiver: int,
        amount: int,
        fee_rate: float,
        daily_limit: int,
        day_start_ts: float,
        min_games: int = 0,
    ) -> TransferResult:
        self.transfers.append((sender, receiver, amount, day_start_ts))
        return TransferResult(True, received=amount, fee=0, sender_balance=1000)


class StubEscrow(IEscrowRepo):
    def __init__(self) -> None:
        self.bets: dict[int, int] = {}
        self.refunded: list[int] = []

    def for_guild(self, guild_id: int) -> Self:
        return self

    def place_bet(self, user_id: int, amount: int, game: str) -> Optional[int]:
        eid = len(self.bets) + 1
        self.bets[eid] = amount
        return eid

    def escrow_amount(self, escrow_id: int) -> int:
        return self.bets.get(escrow_id, 0)

    def settle_with_penalty(self, escrow_id: int, payout: int, game: str, penalty: int) -> Optional[tuple[Any, ...]]:
        bet = self.bets.pop(escrow_id, None)
        if bet is None:
            return None
        return (payout - bet - penalty, penalty, 0)

    def refund(self, escrow_id: int) -> bool:
        self.refunded.append(escrow_id)
        return self.bets.pop(escrow_id, None) is not None


class StubShop(IShopRepo):
    SLOTS = ()

    def for_guild(self, guild_id: int) -> Self:
        return self

    def get_profile(self, user_id: int, raw: bool = False) -> ProfileView:
        return cast("ProfileView", {"card_color": None, "card_icon": None, "title": None})


class StubRtp(IRtpRepo):
    def for_guild(self, guild_id: int) -> Self:
        return self

    def rtp_level_get(self, game: str) -> int:
        return DEFAULT_RTP_POLICY.default_level


class StubEvents(IEventRepo):
    def for_guild(self, guild_id: int) -> Self:
        return self

    def manual_event_get(self, now: float) -> Optional[ManualEventRow]:
        return None


class StubNotices(INoticeRepo):
    def for_guild(self, guild_id: int) -> Self:
        return self

    def take_notices(self, user_id: int) -> list[Any]:
        return []


class StubRecords(IRecordRepo):
    def for_guild(self, guild_id: int) -> Self:
        return self

    def record_play(self, user_id: int, game: str, bet: int, net: int, day: datetime.date, tags=()) -> PlayRecord:
        return cast("PlayRecord", {"streak": 1, "new_best": True, "completed": [], "bonus": 0})

    def backfill_weekly(self, utc_offset_hours: float = 9) -> int:
        return 7


class StubEarnings(IEarningsRepo):
    def for_guild(self, guild_id: int) -> Self:
        return self

    def earnings_memo(self, user_id: int, now: Optional[float] = None, rng: Optional[random.Random] = None):
        return None


class StubRaces(IRaceRepo):
    def __init__(self) -> None:
        self.odds_set: list[tuple[int, Any]] = []

    def for_guild(self, guild_id: int) -> Self:
        return self

    def race_close_entries(self, race_id: int, rng=None) -> Optional[RaceCloseResult]:
        from domain.horse import DISTANCES, GOINGS, Runner, Track

        result = {
            "track": Track(DISTANCES[0], GOINGS[0]),
            "runners": [
                Runner(
                    lane=i + 1,
                    name=f"H{i + 1}",
                    speed=50,
                    stamina=50,
                    power=50,
                    guts=50,
                    races=5,
                    wins=1,
                    places=3,
                    form=0,
                )
                for i in range(6)
            ],
        }
        return cast("RaceCloseResult", result)

    def race_set_odds(self, race_id: int, odds: Any) -> None:
        self.odds_set.append((race_id, odds))

    def race_get(self, race_id: int) -> Optional[RaceRow]:
        return cast("RaceRow", {"id": race_id, "status": "betting"})


class StubLabor(ILaborRepo):
    def for_guild(self, guild_id: int) -> Self:
        return self

    def labor_click_hours(self, user_id: int, since: float) -> list[Any]:
        return [
            {
                "hour": 100,
                "clicks": 3,
                "pay": 300,
                "gaps": 2,
                "sum_gap": 10.0,
                "sum_gap2": 50.0,
                "min_gap": 5.0,
            }
        ]


def test_casino_service_works_with_narrow_bridge_stubs():
    clock = FakeClock(datetime.datetime(2026, 11, 15, 12, 0))
    sessions = FakeSessions()
    svc = CasinoService(
        wallet=StubWallet({1: 5000}),
        escrow=StubEscrow(),
        shop=StubShop(),
        rtp=StubRtp(),
        events=StubEvents(),
        notices=StubNotices(),
        records=StubRecords(),
        sessions=sessions,
        clock=clock,
    )
    begin = svc.begin_game(10, 1, 1000, "スロット")
    assert begin.ok and sessions.is_active(1)
    settle = svc.settle_game(10, 1, begin.escrow_id, "スロット", 1000, 2500)
    assert not settle.duplicate and settle.net == 1500 and not sessions.is_active(1)


def test_economy_horse_and_labor_services_work_with_narrow_stubs():
    clock = FakeClock(datetime.datetime(2026, 11, 15, 12, 0))
    wallet = StubWallet({1: 5000})
    econ_svc = EconomyService(
        wallet,
        StubNotices(),
        StubEarnings(),
        StubRecords(),
        clock,
        shop=cast(IShopRepo, None),
        crypto=cast(ICryptoRepo, None),
        bonds=cast(IBondRepo, None),
    )
    assert econ_svc.claim_daily(10, 1, clock.now(), 0).result.ok
    assert econ_svc.backfill_weekly() == 7
    assert econ_svc.transfer(10, 1, 2, 1000, 0.1, 10_000, 3).ok and wallet.transfers

    races = StubRaces()
    horse_svc = HorseService(races, clock)
    assert horse_svc.close_entries_and_set_odds(10, 42) == {"id": 42, "status": "betting"}
    assert len(races.odds_set) == 1

    labor_svc = LaborService(StubLabor(), clock)
    assert labor_svc.today_key() == "2026-11-15"
    assert labor_svc.click_report(10, 1, 24)["clicks"] == 3


class StubArena(IArenaRepo):
    def for_guild(self, guild_id: int) -> Self:
        return self

    def arena_tournament_ensure(self, fmt, now=None, tz_hours=9.0, rng=None) -> ArenaTournamentDetailRow:
        return cast(
            "ArenaTournamentDetailRow", {"id": 1, "day": "2026-11-15", "status": "semi_betting", "fighters": []}
        )

    def arena_bet(self, user_id: int, fighter_id: int, amount: int, now=None) -> tuple[Any, ...]:
        return (True, amount)


class StubLoan(ILoanRepo):
    def for_guild(self, guild_id: int) -> Self:
        return self

    def loan_expire_offers(self, now: float) -> list[Any]:
        return [{"id": 1}]

    def loan_due_soon(self, now: float) -> list[Any]:
        return [{"id": 2}]

    def loan_collect_due(self, now: float) -> list[Any]:
        return [{"id": 3, "status": "repaid"}]


class StubP2P(IP2PRepo):
    def for_guild(self, guild_id: int) -> Self:
        return self

    def p2p_label(self, row: Any) -> str:
        return "にんじん"

    def p2p_active(self, kind=None, now=None, limit=25, mode=None, order="end") -> list[Any]:
        return [{"id": 1}]

    def p2p_count(self, now=None) -> int:
        return 1


class StubHorse(IHorseRepo):
    HORSE_PRICE = 10000

    def for_guild(self, guild_id: int) -> Self:
        return self

    def auction_horses(self, now=None) -> list[Any]:
        return []


class StubAuction(IAuctionRepo):
    def for_guild(self, guild_id: int) -> Self:
        return self

    def auction_list(self, now=None) -> list[Any]:
        return []

    def market_list(self, now=None, rng=None) -> list[TeiaiMarketListing]:
        return []


class StubDebt(IDebtRepo):
    def for_guild(self, guild_id: int) -> Self:
        return self

    def status(self, user_id: int, now=None) -> str:
        return "katagi"


class StubGuild(IGuildRepo):
    def for_guild(self, guild_id: int) -> Self:
        return self

    def nick_pref(self, guild_id: int, user_id: int) -> bool:
        return False


class StubShopForService(StubShop):
    def inventory(self, user_id: int) -> dict[str, Any]:
        return {"title_rookie": 1}

    def shop_stock(self, user_id: int, item_id: str, now=None) -> Optional[ShopStockInfo]:
        return None

    def arm(self, user_id: int, item_id: str) -> bool:
        return item_id == shop_mod.INSURANCE

    def get_profile(self, user_id: int, raw: bool = False) -> ProfileView:
        return cast("ProfileView", {"card_color": None, "card_icon": None, "title": None, "insurance": 2})


def test_arena_loan_and_shop_services_work_with_narrow_stubs():
    clock = FakeClock(datetime.datetime(2026, 11, 15, 20, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=9))))

    arena_svc = ArenaService(StubArena(), clock)
    assert arena_svc.ensure_tournament(1, "duel")["status"] == "semi_betting"
    assert arena_svc.local_hour() == 20
    assert arena_svc.place_bet(1, 10, 1, 500) == (True, 500)

    loan_svc = LoanService(StubLoan(), clock)
    outcome = loan_svc.process_tick(1)
    assert len(outcome.expired) == 1 and len(outcome.due_soon) == 1 and len(outcome.collected) == 1

    shop_svc = ShopService(
        StubShopForService(),
        StubAuction(),
        StubHorse(),
        StubEarnings(),
        StubDebt(),
        StubP2P(),
        StubWallet({1: 5000}),
        StubGuild(),
        clock,
    )
    owned = shop_mod.BY_ID["title_rookie"]
    assert shop_svc.precheck_buy(1, 1, owned, 1).status == "owned"
    carrot = shop_mod.BY_ID[shop_mod.CARROT]
    assert shop_svc.precheck_buy(1, 1, carrot, 1).ok
    assert shop_svc.arm_consumable(1, 1, shop_mod.INSURANCE) == 2
    assert shop_svc.arm_consumable(1, 1, carrot.id) is None
    cat = shop_svc.catalog_snapshot(1, 1, "home", [])
    assert cat["balance"] == 5000 and cat["waiting"] == 0
    auc = shop_svc.auction_snapshot(1, 1, None, True, None, None, "end", 25)
    assert auc["tab"] == "players" and auc["p2p_total"] == 1 and auc["p2p_rows"] == [({"id": 1}, "にんじん")]
