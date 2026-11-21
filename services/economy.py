"""経済・デイリー・送金・週間ランキング補完のアプリケーションサービス。"""

import datetime
from dataclasses import dataclass

from domain import missions as missions_mod
from domain.earnings import MemoInfo
from domain.ports.clock import IClock
from domain.ports.contracts import (
    LedgerEntry,
    PlaySummary,
    ProfileView,
    RankEntry,
    UserRow,
    WeeklyAward,
)
from domain.ports.repos import (
    IBondRepo,
    ICryptoRepo,
    IEarningsRepo,
    INoticeRepo,
    IRecordRepo,
    IShopRepo,
    IWalletRepo,
)
from domain.types import INITIAL_BALANCE, DailyResult, TransferResult


@dataclass(frozen=True)
class BalanceCard:
    """`/ペリカ 残高` に出す値（表示の組み立ては cogs/economy/embeds.py）。"""

    stats: UserRow
    rank: RankEntry | None
    display_status: str
    profile: ProfileView
    has_frozen_equipment: bool  # 差し押さえ中の称号・カード・演出を、元は装備していたか
    inventory: dict[str, int]
    crypto_held: int
    bonds_held: int
    play: PlaySummary


@dataclass(frozen=True)
class MissionBoard:
    """`/ペリカ ミッション` に出す値。"""

    status: list[tuple[missions_mod.Mission, int, bool]]
    win_streak: int
    best_streak: int
    next_reset: datetime.datetime


@dataclass(frozen=True)
class DailyClaimOutcome:
    result: DailyResult
    memo: MemoInfo | None = None


class EconomyService:
    """デイリーボーナス・送金・週間集計補完のユースケース調整。"""

    def __init__(
        self,
        wallet: IWalletRepo,
        notices: INoticeRepo,
        earnings: IEarningsRepo,
        records: IRecordRepo,
        clock: IClock,
        shop: IShopRepo,
        crypto: ICryptoRepo,
        bonds: IBondRepo,
    ) -> None:
        self._wallet = wallet
        self._notices = notices
        self._earnings = earnings
        self._records = records
        self.clock = clock
        self._shop = shop
        self._crypto = crypto
        self._bonds = bonds

    # ------------------------------ 残高・履歴（読み取り） ------------------------------ #
    def balance_of(self, guild_id: int, user_id: int) -> int:
        return self._wallet.for_guild(guild_id).balance(user_id)

    def peek_balance(self, guild_id: int, user_id: int) -> int:
        """いまの残高。口座を作らずに読む（初めての人は、最初に遊ぶときに初期支給が入る額を返す）。"""
        stats = self._wallet.for_guild(guild_id).peek_stats(user_id)
        return stats["balance"] if stats else INITIAL_BALANCE

    def history(self, guild_id: int, user_id: int, limit: int) -> list[LedgerEntry]:
        return self._wallet.for_guild(guild_id).history(user_id, limit)

    def balance_card(self, guild_id: int, viewer_id: int, target_id: int) -> BalanceCard | None:
        """残高カードの値。本人なら口座を作って読み、他人は読み取り専用で調べる（見るだけで口座を作らない）。

        口座がなければ None。
        """
        wallet = self._wallet.for_guild(guild_id)
        shop_repo = self._shop.for_guild(guild_id)
        stats = wallet.stats(target_id) if target_id == viewer_id else wallet.peek_stats(target_id)
        if stats is None:
            return None
        rank = wallet.rank_of(target_id)
        profile = shop_repo.get_profile(target_id)  # カタギでないときは、装備が差し押さえ中で、空になって返る
        raw = shop_repo.get_profile(target_id, raw=True)
        return BalanceCard(
            stats=stats,
            rank=rank,
            display_status=wallet.display_status(target_id),
            profile=profile,
            has_frozen_equipment=bool(profile["frozen"]) and any(raw[slot] for slot in shop_repo.SLOTS),
            inventory=shop_repo.inventory(target_id),
            crypto_held=self._crypto.for_guild(guild_id).crypto_values().get(target_id, 0),
            bonds_held=self._bonds.for_guild(guild_id).bond_values().get(target_id, 0),
            play=self._records.for_guild(guild_id).play_summary(target_id),
        )

    def mission_board(self, guild_id: int, user_id: int) -> MissionBoard:
        now = self.clock.now()
        records = self._records.for_guild(guild_id)
        status = records.mission_status(user_id, now.date())
        streak, best = records.win_streak(user_id)
        tomorrow = datetime.datetime.combine(
            now.date() + datetime.timedelta(days=1), datetime.time(), tzinfo=now.tzinfo
        )
        return MissionBoard(status=status, win_streak=streak, best_streak=best, next_reset=tomorrow)

    def claim_daily(self, guild_id: int, user_id: int, now: datetime.datetime, slot: int) -> DailyClaimOutcome:
        """デイリー受取・一時通知の掃き出し・裏情報メモ抽選を 1 ユースケースとして実行する。"""
        result = self._wallet.for_guild(guild_id).claim_daily(user_id, now.date(), slot)
        if not result.ok:
            return DailyClaimOutcome(result=result)
        self._notices.for_guild(guild_id).take_notices(user_id)
        memo = self._earnings.for_guild(guild_id).earnings_memo(user_id, now.timestamp())
        return DailyClaimOutcome(result=result, memo=memo)

    def transfer(
        self,
        guild_id: int,
        sender_id: int,
        receiver_id: int,
        amount: int,
        fee_rate: float,
        daily_limit: int,
        min_games: int,
        now: datetime.datetime | None = None,
    ) -> TransferResult:
        now = self.clock.now() if now is None else now
        day_start = datetime.datetime.combine(now.date(), datetime.time(), tzinfo=now.tzinfo).timestamp()
        return self._wallet.for_guild(guild_id).transfer(
            sender_id, receiver_id, amount, fee_rate, daily_limit, day_start, min_games
        )

    def weekly_last_announced(self, guild_id: int) -> str | None:
        return self._records.for_guild(guild_id).weekly_last_announced()

    def set_weekly_announced(self, guild_id: int, week: str) -> None:
        self._records.for_guild(guild_id).set_weekly_announced(week)

    def weekly_award(self, guild_id: int, week: str) -> WeeklyAward | None:
        """その週の王者に限定称号を贈り、順位を返す（発表済みなら None）。"""
        return self._records.for_guild(guild_id).weekly_award(week)

    def backfill_weekly(self) -> int:
        return self._records.backfill_weekly(self.clock.offset_hours())
