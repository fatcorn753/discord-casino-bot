"""経済基盤の水平インターフェース（ISP / DIP）。

巨大な単一具象（Economy）へ直接依存するのではなく、利用側の関心事ごとに
小さな契約を定義し、永続化層（infra/db/）がこれらを実装する。
"""

import datetime
from typing import Optional, Self

from domain.types import DailyResult, TransferResult


class IGuildScopable:
    """サーバー（guild_id）単位のビューを切り出す契約。"""

    def for_guild(self, guild_id: int) -> Self:
        raise NotImplementedError


class IGuildApprovalStore:
    """サーバー承認状態の読み取り契約。"""

    def get_guild_status(self, guild_id: int) -> Optional[dict]:
        raise NotImplementedError

    def list_guild_status(self) -> list:
        raise NotImplementedError


class IRankingReader:
    """ランキング表示・総資産内訳の読み取り契約。"""

    def top(self, limit: int = 10, user_ids: Optional[set] = None) -> list:
        raise NotImplementedError

    def rank_of(self, user_id: int, user_ids: Optional[set] = None) -> Optional[tuple]:
        raise NotImplementedError

    def crypto_values(self) -> dict[int, int]:
        raise NotImplementedError

    def bond_values(self) -> dict[int, int]:
        raise NotImplementedError

    def wealthy_ids(self) -> set[int]:
        raise NotImplementedError

    def display_status(self, user_id: int, wealthy: Optional[set] = None) -> str:
        raise NotImplementedError

    def final_assets(self, user_ids: Optional[set] = None, now: Optional[float] = None) -> list:
        raise NotImplementedError


class IEscrowPort:
    """ゲームの賭け金預かり・精算の契約。"""

    def place_bet(self, user_id: int, amount: int, game: str) -> Optional[int]:
        raise NotImplementedError

    def add_to_escrow(self, escrow_id: int, extra: int, credit: bool = False) -> bool:
        raise NotImplementedError

    def escrow_amount(self, escrow_id: int) -> int:
        raise NotImplementedError

    def settle(self, escrow_id: int, payout: int, game: str) -> Optional[int]:
        raise NotImplementedError

    def settle_with_penalty(self, escrow_id: int, payout: int, game: str, penalty: int) -> Optional[tuple]:
        raise NotImplementedError

    def refund(self, escrow_id: int) -> bool:
        raise NotImplementedError

    def refund_all_escrows(self) -> int:
        raise NotImplementedError


class IWalletPort:
    """基本的な残高照会・付与・デイリー・送金の契約。"""

    def balance(self, user_id: int) -> int:
        raise NotImplementedError

    def credit(self, user_id: int, amount: int, reason: str) -> int:
        raise NotImplementedError

    def claim_daily(self, user_id: int, today: datetime.date, slot: Optional[int] = None) -> DailyResult:
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
    ) -> TransferResult:
        raise NotImplementedError


class IEconomy(
    IGuildScopable,
    IGuildApprovalStore,
    IRankingReader,
    IEscrowPort,
    IWalletPort,
):
    """暫定の統合ポート契約（#49 でサブサービスへ水平分解予定）。"""
