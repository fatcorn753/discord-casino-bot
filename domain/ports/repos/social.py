"""ショップ・競売・P2P・クイズ・抽選・地下作業・エスクローのリポジトリポート契約。"""

import random
from typing import TYPE_CHECKING, Literal, overload

from domain.errors import Outcome
from domain.ports.contracts import (
    AuctionListing,
    CompanyBankrupt,
    CompanyShipResult,
    CompanyView,
    LaborClick,
    LaborClickHourRow,
    LaborCollectDetail,
    LaborFindSummary,
    LaborState,
    LaborTop,
    ListingRow,
    P2PMine,
    PollRigResult,
    PollView,
    ProfileView,
    QuizAdminView,
    QuizAnswerResult,
    QuizImportItem,
    QuizSettleRow,
    QuizView,
    RaffleResult,
    RaffleView,
    ShopStockInfo,
    StakeHolding,
    StakeInvestResult,
    TeiaiMarketListing,
)
from domain.ports.repos.base import IGuildScopedRepo

if TYPE_CHECKING:
    from domain.types import MarketPurchase, PurchaseResult


class IRaffleRepo(IGuildScopedRepo):
    def raffle_open(self, host_id: int, fee: int, closes: float, channel_id: int) -> Outcome[str]:
        raise NotImplementedError

    def raffle_get(self) -> RaffleView | None:
        raise NotImplementedError

    def raffle_set_message(self, raffle_id: int, message_id: int, channel_id: int | None = None) -> None:
        raise NotImplementedError

    def raffle_join(self, user_id: int, now: float) -> Outcome[str]:
        raise NotImplementedError

    def raffle_close(self, rng: random.Random | None = None, cancel: bool = False) -> RaffleResult | None:
        raise NotImplementedError


class IPollRepo(IGuildScopedRepo):
    def poll_create(self, creator: int, title: str, closes: float, channel_id: int, now: float) -> Outcome[int]:
        raise NotImplementedError

    def poll_set_message(self, poll_id: int, channel_id: int, message_id: int) -> None:
        raise NotImplementedError

    def poll_get(self, poll_id: int) -> PollView | None:
        raise NotImplementedError

    def poll_vote(self, user_id: int, poll_id: int, yes: bool, now: float) -> Outcome[PollView]:
        raise NotImplementedError

    def poll_rig(self, user_id: int, poll_id: int, yes: int, no: int, now: float) -> Outcome[PollRigResult]:
        raise NotImplementedError

    def poll_close_due(self, now: float) -> list[PollView]:
        raise NotImplementedError


class IShopRepo(IGuildScopedRepo):
    SLOTS: tuple[str, ...]

    def shop_stock(self, user_id: int, item_id: str, now: float) -> ShopStockInfo | None:
        raise NotImplementedError

    def purchase(
        self,
        user_id: int,
        item_id: str,
        label: str,
        price: int,
        unique: bool,
        qty: int = 1,
        now: float | None = None,
    ) -> "PurchaseResult":
        raise NotImplementedError

    def inventory(self, user_id: int) -> dict[str, int]:
        raise NotImplementedError

    def get_profile(self, user_id: int, raw: bool = False) -> ProfileView:
        raise NotImplementedError

    def equip(self, user_id: int, slot: str, item_id: str | None) -> bool:
        raise NotImplementedError

    def arm(self, user_id: int, item_id: str) -> bool:
        raise NotImplementedError


class IAuctionRepo(IGuildScopedRepo):
    def auction_price(self, item_id: str, listed_at: float, now: float) -> int:
        raise NotImplementedError

    def auction_list(self, now: float) -> list[AuctionListing]:
        raise NotImplementedError

    def auction_buy(self, user_id: int, item_id: str, qty: int = 1, now: float | None = None) -> "PurchaseResult":
        raise NotImplementedError

    def market_list(self, now: float, rng: random.Random | None = None) -> list[TeiaiMarketListing]:
        raise NotImplementedError

    def market_buy(
        self,
        user_id: int,
        listing_id: int,
        qty: int = 1,
        now: float | None = None,
        rng: random.Random | None = None,
    ) -> "MarketPurchase":
        raise NotImplementedError


class IEscrowRepo(IGuildScopedRepo):
    def place_bet(self, user_id: int, amount: int, game: str) -> int | None:
        raise NotImplementedError

    def add_to_escrow(self, escrow_id: int, extra: int, credit: bool = False) -> bool:
        raise NotImplementedError

    def escrow_amount(self, escrow_id: int) -> int:
        raise NotImplementedError

    def settle(self, escrow_id: int, payout: int, game: str) -> int | None:
        raise NotImplementedError

    def settle_with_penalty(self, escrow_id: int, payout: int, game: str, penalty: int) -> tuple[int, int, int] | None:
        raise NotImplementedError

    def refund(self, escrow_id: int) -> bool:
        raise NotImplementedError

    def jackpot_pool(self) -> int:
        raise NotImplementedError

    def jackpot_claim(self, user_id: int) -> int:
        raise NotImplementedError

    def refund_all_escrows(self) -> int:
        raise NotImplementedError


class IQuizAdminRepo(IGuildScopedRepo):
    """クイズ問題の登録・一覧・削除（管理用）。"""

    def quiz_add(
        self,
        question: str,
        choices: list[str],
        explanation: str,
        reward: int,
        expires: float | None,
        now: float,
        creator_name: str = "",
        msg_right: str = "",
        msg_wrong: str = "",
    ) -> int:
        raise NotImplementedError

    def quiz_add_many(self, items: list[QuizImportItem], now: float, creator_name: str = "") -> list[int]:
        raise NotImplementedError

    def quiz_existing_questions(self) -> set[str]:
        raise NotImplementedError

    def quiz_get(self, quiz_id: int) -> QuizView | None:
        raise NotImplementedError

    def quiz_list(self) -> list[QuizAdminView]:
        raise NotImplementedError

    def quiz_delete(self, quiz_id: int) -> bool:
        raise NotImplementedError


class IQuizPlayRepo(IGuildScopedRepo):
    """クイズの出題・回答・投稿・締め切り（プレイ用）。"""

    def quiz_next(self, user_id: int, now: float) -> tuple[QuizView | None, int]:
        raise NotImplementedError

    def quiz_answer(self, user_id: int, quiz_id: int, chosen: str, now: float) -> Outcome[QuizAnswerResult]:
        raise NotImplementedError

    def quiz_post(
        self,
        author_id: int,
        question: str,
        choices: list[str],
        explanation: str,
        reward: int,
        minutes: int,
        channel_id: int,
        now: float,
        creator_name: str = "",
        msg_right: str = "",
        msg_wrong: str = "",
    ) -> Outcome[int]:
        raise NotImplementedError

    def quiz_own_open(self, user_id: int, now: float) -> list[QuizView]:
        raise NotImplementedError

    def quiz_set_message(self, quiz_id: int, message_id: int) -> None:
        raise NotImplementedError

    def quiz_settle(self, now: float) -> list[QuizSettleRow]:
        raise NotImplementedError

    def quiz_withdraw(self, user_id: int, quiz_id: int, now: float) -> QuizSettleRow | None:
        raise NotImplementedError


class IQuizRepo(IQuizAdminRepo, IQuizPlayRepo):
    """クイズの全契約。利用側は必要な部分契約だけに依存する。"""


class ILaborRepo(IGuildScopedRepo):
    def labor_donated_total(self) -> int:
        raise NotImplementedError

    def labor_finds(self, user_id: int, limit: int = 6) -> list[LaborFindSummary]:
        raise NotImplementedError

    def labor_state(self, user_id: int, now: float, day: str = "") -> LaborState:
        raise NotImplementedError

    def labor_top(self, limit: int = 10, day: str | None = None, user_id: int | None = None) -> LaborTop:
        raise NotImplementedError

    def labor_click(self, user_id: int, now: float, day: str = "", rng: random.Random | None = None) -> LaborClick:
        raise NotImplementedError

    def labor_click_hours(self, user_id: int, since: float) -> list[LaborClickHourRow]:
        raise NotImplementedError

    @overload
    def labor_collect(self, user_id: int, now: float, day: str = "", detail: Literal[False] = False) -> int: ...
    @overload
    def labor_collect(self, user_id: int, now: float, day: str, detail: Literal[True]) -> LaborCollectDetail: ...
    def labor_collect(self, user_id: int, now: float, day: str = "", detail: bool = False) -> int | LaborCollectDetail:
        raise NotImplementedError

    def labor_drink(self, user_id: int, now: float, day: str = "") -> Outcome[str]:
        raise NotImplementedError

    def labor_upgrade(self, user_id: int, tool: str, now: float, day: str = "") -> Outcome[str]:
        raise NotImplementedError

    def labor_hire(self, user_id: int, kind: str, now: float, day: str = "") -> Outcome[str]:
        raise NotImplementedError

    def labor_dismiss(self, user_id: int, kind: str, now: float, day: str = "") -> Outcome[str]:
        raise NotImplementedError

    def labor_dorm(self, user_id: int, now: float, day: str = "") -> Outcome[str]:
        raise NotImplementedError

    def labor_set_renew(self, user_id: int, on: bool, now: float, day: str = "") -> Outcome[str]:
        raise NotImplementedError


class IP2PRepo(IGuildScopedRepo):
    def p2p_list(
        self,
        user_id: int,
        kind: str,
        ref: str,
        qty: int,
        mode: str,
        price: int,
        hours: int | None = None,
        now: float | None = None,
    ) -> Outcome[int]:
        raise NotImplementedError

    def p2p_label(self, row: ListingRow) -> str:
        raise NotImplementedError

    def p2p_get(self, listing_id: int) -> ListingRow | None:
        raise NotImplementedError

    def p2p_set_post(self, listing_id: int, channel_id: int, message_id: int) -> None:
        raise NotImplementedError

    def p2p_buy(self, buyer_id: int, listing_id: int, now: float) -> Outcome[int]:
        raise NotImplementedError

    def p2p_bid(self, bidder_id: int, listing_id: int, amount: int, now: float) -> Outcome[float]:
        raise NotImplementedError

    def p2p_cancel(self, user_id: int, listing_id: int, now: float) -> Outcome[str]:
        raise NotImplementedError

    def p2p_pull_back(self, user_id: int, now: float | None = None) -> dict[str, int]:
        raise NotImplementedError

    def p2p_unsynced_posts(self) -> list[ListingRow]:
        raise NotImplementedError

    def p2p_mark_post_done(self, listing_id: int) -> None:
        raise NotImplementedError

    def p2p_settle(self, now: float) -> list[tuple[ListingRow, str]]:
        raise NotImplementedError

    def p2p_teiai_daily(self, now: float, rng: random.Random) -> list[int]:
        raise NotImplementedError

    def p2p_active(
        self,
        kind: str | None = None,
        now: float | None = None,
        limit: int = 25,
        mode: str | None = None,
        order: str = "end",
    ) -> list[ListingRow]:
        raise NotImplementedError

    def p2p_count(self, now: float) -> int:
        raise NotImplementedError

    def p2p_mine(self, user_id: int, now: float | None = None) -> P2PMine:
        raise NotImplementedError


class ICompanyRepo(IGuildScopedRepo):
    def company_get(self, user_id: int, now: float) -> CompanyView | None:
        raise NotImplementedError

    def company_found(self, user_id: int, name: str, kind: str, now: float) -> Outcome[CompanyView]:
        raise NotImplementedError

    def company_invest(self, user_id: int, amount: int, now: float) -> Outcome[int]:
        raise NotImplementedError

    def company_withdraw(self, user_id: int, amount: int | None = None, now: float | None = None) -> Outcome[int]:
        raise NotImplementedError

    def company_ship(self, user_id: int, now: float) -> Outcome[CompanyShipResult]:
        raise NotImplementedError

    def company_upgrade(self, user_id: int, now: float) -> Outcome[int]:
        raise NotImplementedError

    def company_change_kind(self, user_id: int, kind: str, now: float) -> Outcome[str]:
        raise NotImplementedError

    def stake_invest(self, investor_id: int, owner_id: int, amount: int, now: float) -> Outcome[StakeInvestResult]:
        raise NotImplementedError

    def stake_holdings(self, investor_id: int, now: float) -> list[StakeHolding]:
        raise NotImplementedError

    def company_set_dividend(self, user_id: int, rate: float, now: float) -> Outcome[float]:
        raise NotImplementedError

    def company_settle_all(self, now: float) -> list[CompanyBankrupt]:
        raise NotImplementedError
