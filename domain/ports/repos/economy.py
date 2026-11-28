"""経済・通知・履歴・ギルド設定・資産集計のリポジトリポート契約。"""

import datetime
from collections.abc import Iterable
from typing import TYPE_CHECKING

from domain.ports.bridges import INoticeSink
from domain.ports.contracts import (
    GuildStatusRow,
    LeaderboardRow,
    LedgerEntry,
    PlayRecord,
    PlaySummary,
    ProfileRow,
    RankEntry,
    UserRow,
    WeeklyAward,
    WeeklyRankEntry,
)
from domain.ports.repos.base import IGuildScopedRepo

if TYPE_CHECKING:
    from domain.errors import Notice
    from domain.missions import Mission
    from domain.types import DailyResult, TransferResult


class INoticeRepo(IGuildScopedRepo, INoticeSink):
    def take_notices(self, user_id: int) -> list["Notice"]:
        raise NotImplementedError


class IRecordRepo(IGuildScopedRepo):
    def record_play(
        self, user_id: int, game: str, bet: int, net: int, day: datetime.date, tags: Iterable[str] = ()
    ) -> PlayRecord:
        raise NotImplementedError

    def mission_status(self, user_id: int, day: datetime.date) -> list[tuple["Mission", int, bool]]:
        raise NotImplementedError

    def weekly_top(self, week: str, limit: int = 10) -> list[tuple[int, int, int]]:
        raise NotImplementedError

    def weekly_rank(self, week: str, user_id: int) -> WeeklyRankEntry | None:
        raise NotImplementedError

    def weekly_last_announced(self) -> str | None:
        raise NotImplementedError

    def set_weekly_announced(self, week: str) -> None:
        raise NotImplementedError

    def weekly_champion_count(self, user_id: int) -> int:
        raise NotImplementedError

    def weekly_award(self, week: str) -> WeeklyAward | None:
        raise NotImplementedError

    def backfill_weekly(self, utc_offset_hours: float = 9) -> int:
        raise NotImplementedError

    def play_summary(self, user_id: int) -> PlaySummary:
        raise NotImplementedError

    def win_streak(self, user_id: int) -> tuple[int, int]:
        raise NotImplementedError


class IGuildApprovalRepo(IGuildScopedRepo):
    """サーバーの承認状態の読み書き。"""

    def set_guild_status(self, guild_id: int, status: str, name: str | None = None, by: int | None = None) -> None:
        raise NotImplementedError

    def get_guild_status(self, guild_id: int) -> GuildStatusRow | None:
        raise NotImplementedError

    def list_guild_status(self) -> list[GuildStatusRow]:
        raise NotImplementedError

    def mark_guild_notified(self, guild_id: int) -> None:
        raise NotImplementedError


class ILeaderboardConfigRepo(IGuildScopedRepo):
    """ランキング掲示板（チャンネル・メッセージ）の設定。"""

    def set_leaderboard(self, guild_id: int, channel_id: int, message_id: int) -> None:
        raise NotImplementedError

    def get_leaderboard(self, guild_id: int) -> LeaderboardRow | None:
        raise NotImplementedError

    def list_leaderboards(self) -> list[LeaderboardRow]:
        raise NotImplementedError

    def delete_leaderboard(self, guild_id: int) -> None:
        raise NotImplementedError


class INickPrefRepo(IGuildScopedRepo):
    """身分ニックネームの管理（希望・元の名前・付けた名前）。"""

    def set_nick_pref(self, guild_id: int, user_id: int, enabled: bool) -> None:
        raise NotImplementedError

    def ensure_nick_row(self, guild_id: int, user_id: int) -> None:
        raise NotImplementedError

    def nick_managed(self, guild_id: int, user_id: int) -> bool:
        raise NotImplementedError

    def status_nick_targets(self) -> list[int]:
        raise NotImplementedError

    def get_nick_state(self, guild_id: int, user_id: int) -> tuple[str | None, str] | None:
        raise NotImplementedError

    def set_nick_state(self, guild_id: int, user_id: int, original: str | None, applied: str | None) -> None:
        raise NotImplementedError

    def delete_guild_nick_prefs(self, guild_id: int) -> None:
        raise NotImplementedError

    def nick_pref_guilds(self, user_id: int) -> list[int]:
        raise NotImplementedError

    def nick_pref(self, guild_id: int, user_id: int) -> bool:
        raise NotImplementedError


class IProfileDisplayReader(IGuildScopedRepo):
    """ランキングの称号表示に使う、プロフィールのまとめ読み。"""

    def profiles_of(self, user_ids: Iterable[int]) -> dict[int, ProfileRow]:
        raise NotImplementedError


class IFeatureSwitchRepo(IGuildScopedRepo):
    """機能のオン・オフ（サーバーごと。止めた機能のキーを持つ）。"""

    def features_off(self, guild_id: int) -> set[str]:
        raise NotImplementedError

    def set_feature(self, guild_id: int, feature: str, on: bool) -> None:
        raise NotImplementedError


class IGuildRepo(IGuildApprovalRepo, ILeaderboardConfigRepo, INickPrefRepo, IProfileDisplayReader, IFeatureSwitchRepo):
    """サーバー単位の設定（承認・掲示板・ニックネーム・表示・機能のオン・オフ）をまとめた契約。利用側は必要な部分契約だけに依存する。"""


class IWalletBalanceRepo(IGuildScopedRepo):
    """手元残高の照会と入出金・デイリー・送金（取引系）。"""

    def balance(self, user_id: int) -> int:
        raise NotImplementedError

    def credit(self, user_id: int, amount: int, reason: str) -> int:
        raise NotImplementedError

    def set_balance(self, user_id: int, amount: int, reason: str) -> tuple[int, int, int, int]:
        raise NotImplementedError

    def debit_up_to(self, user_id: int, amount: int, reason: str) -> int:
        raise NotImplementedError

    def claim_daily(self, user_id: int, today: datetime.date, slot: int | None = None) -> "DailyResult":
        raise NotImplementedError

    def transfer(
        self,
        sender: int,
        receiver: int,
        amount: int,
        fee_rate: float,
        daily_limit: int,
        day_start_ts: float,
        min_games: int = 0,
    ) -> "TransferResult":
        raise NotImplementedError


class IWalletRankingReader(IGuildScopedRepo):
    """サーバーの参加者・身分表示・金融純資産の順位（読み取り）。"""

    def wealthy_ids(self) -> set[int]:
        raise NotImplementedError

    def display_status(self, user_id: int, wealthy: set[int] | None = None) -> str:
        raise NotImplementedError

    def top(self, limit: int = 10, user_ids: set[int] | None = None) -> list[RankEntry]:
        raise NotImplementedError

    def rank_of(self, user_id: int, user_ids: set[int] | None = None) -> RankEntry | None:
        raise NotImplementedError


class IWalletStatsReader(IGuildScopedRepo):
    """成績と取引履歴（読み取り）。"""

    def peek_stats(self, user_id: int) -> UserRow | None:
        raise NotImplementedError

    def stats(self, user_id: int) -> UserRow:
        raise NotImplementedError

    def history(self, user_id: int, limit: int = 10) -> list[LedgerEntry]:
        raise NotImplementedError


class IWalletRepo(IWalletBalanceRepo, IWalletRankingReader, IWalletStatsReader):
    """ウォレットの全契約。利用側は必要な部分契約だけに依存する。"""


class IAssetRepo(IGuildScopedRepo):
    def horse_worth(self, races: int, wins: int, places: int) -> int:
        raise NotImplementedError

    def asset_parts(self, now: float) -> dict[int, dict[str, int]]:
        raise NotImplementedError

    def net_assets(
        self, user_ids: set[int] | None = None, now: float | None = None
    ) -> list[tuple[int, int, dict[str, int]]]:
        raise NotImplementedError

    def net_top(self, limit: int = 10, user_ids: set[int] | None = None) -> list[RankEntry]:
        raise NotImplementedError

    def net_rank_of(self, user_id: int, user_ids: set[int] | None = None) -> RankEntry | None:
        raise NotImplementedError
