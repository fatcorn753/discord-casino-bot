"""銀行・猶予・CDS・通知・価格・帝愛物産プール・破産実行のブリッジ契約。

住み分け:
- 単一メソッドの純粋コールバック契約は `Callable` 型エイリアス（形式的型付け）で表し、
  関数・クロージャ・束縛メソッドをそのまま渡せる。既定動作は `noop_*` 関数（LSP 準拠）、
  束ねは `compose_*` 関数（OCP）が担う。
- 複数メソッド・状態・不変条件を持つ契約（INoticeSink / IBankCashFlow / IHouseAccount /
  IBankCdsBridge）は名目的基底クラスのまま維持する。
"""

from collections.abc import Callable
from typing import Any

from domain.errors import Outcome, fail
from domain.ports.contracts import BankruptResult

# 単一コールバック契約（Callable 型エイリアス）: 静的型だけで LSP を 100% 保証できる純粋契約
MarketPriceReader = Callable[[], dict[str, float]]
GraceReader = Callable[[int, int], float | None]
GraceEnterHook = Callable[[int, int, float], None]
ExcessLossSink = Callable[[int, int, float], int]
# (guild_id, user_id, forced, escape) -> (成功か, 結果 | Failure)
BankruptExecutor = Callable[[int, int, bool, bool], Outcome[BankruptResult]]
GainsTaxCollector = Callable[[int, int, int, bool, str], int]


def noop_market_price_reader() -> dict[str, float]:
    """価格ソース未接続時に空辞書を返す LSP 準拠の既定動作。"""
    return {}


def noop_grace_reader(guild_id: int, user_id: int) -> float | None:
    """猶予読み取り未接続時に None を返す LSP 準拠の既定動作。"""
    return None


def noop_grace_enter_hook(guild_id: int, user_id: int, now: float) -> None:
    """猶予入りフック未接続時の LSP 準拠の既定動作。"""
    return None


def compose_grace_enter_hooks(hooks: tuple[GraceEnterHook, ...]) -> GraceEnterHook:
    """複数の猶予入りフックを順次適用する合成フックを返す（OCP: 既存を修正せず追加できる）。"""

    def run(guild_id: int, user_id: int, now: float) -> None:
        for hook in hooks:
            hook(guild_id, user_id, now)

    return run


def noop_excess_loss_sink(guild_id: int, amount: int, now: float) -> int:
    """負う先が無いとき: 何も集めない（超過損失は払われない）。"""
    return 0


def noop_bankrupt_executor(guild_id: int, user_id: int, forced: bool, escape: bool) -> Outcome[BankruptResult]:
    """破産実行未接続時に失敗を返す LSP 準拠の既定動作。"""
    return False, fail("unavailable")


class INoticeSink:
    """処理中に発生したユーザー向け一言通知を記録する書き込み契約。"""

    def note_for(self, guild_id: int, user_id: int, code: str, **params: Any) -> None:
        raise NotImplementedError


class NoOpNoticeSink(INoticeSink):
    """通知シンク未接続時に記録を無視する LSP 準拠 Null 実装。"""

    def note_for(self, guild_id: int, user_id: int, code: str, **params: Any) -> None:
        return None


class IBankCashFlow:
    """帝愛銀行の手元現金の同期・入金（配当分配・利子源泉）・払い出しの契約。"""

    def risk_free_per_day_for(self, guild_id: int, now: float) -> float:
        """いまの無リスク金利 r_f（1日あたり。#18）。"""
        raise NotImplementedError

    def bank_sync_for(self, guild_id: int, now: float) -> dict[str, float]:
        raise NotImplementedError

    def bank_set_for(self, guild_id: int, cash: float, loaned: float) -> None:
        raise NotImplementedError

    def bank_locks_for(self, guild_id: int, now: float) -> float:
        raise NotImplementedError

    def bank_income_for(
        self,
        guild_id: int,
        amount: int,
        now: float,
        *,
        thermo: bool = True,
    ) -> None:
        raise NotImplementedError

    def bank_pay_for(self, guild_id: int, amount: int, now: float, *, thermo: bool = True) -> int:
        raise NotImplementedError

    def bank_pay_batch_for(self, guild_id: int, amounts: list[int], now: float, *, offset: float = 0.0) -> list[int]:
        """バッチの支払いを、流入 offset との純額で1回のウォーターフォールに通す。払えた額をそれぞれ返す。"""
        raise NotImplementedError

    def bank_thermo_batch_for(self, guild_id: int, net: float, now: float) -> None:
        """入金と支払いが混ざるバッチの純額を、サーモスタットに1回だけ通す（個々は thermo=False で動かす）。"""
        raise NotImplementedError


class IHouseAccount:
    """帝愛物産（`/会社` の生産・出荷買取専用プール）と、日次買取枠の契約。"""

    def house_income_for(self, guild_id: int, amount: int) -> None:
        raise NotImplementedError

    def house_balance_for(self, guild_id: int) -> int:
        raise NotImplementedError

    def house_receive_for(self, guild_id: int, amount: int) -> None:
        raise NotImplementedError

    def house_pay_for(self, guild_id: int, amount: int) -> int:
        raise NotImplementedError

    def house_grant_left_for(self, guild_id: int, day: str, cap: int) -> int:
        raise NotImplementedError

    def house_grant_use_for(self, guild_id: int, day: str, amount: int) -> None:
        raise NotImplementedError


class IBankCdsBridge:
    """DebtRepo が BankRepo 具体型に依存せず猶予更新と CDS 精算を行う契約。"""

    def grace_until_for(self, guild_id: int, user_id: int) -> float | None:
        raise NotImplementedError

    def grace_update_for(self, guild_id: int, user_id: int, now: float) -> None:
        raise NotImplementedError

    def grace_clear_for(self, guild_id: int, user_id: int) -> None:
        raise NotImplementedError

    def cds_cover_self_for(self, guild_id: int, user_id: int, shortfall: int, now: float) -> int:
        raise NotImplementedError

    def cds_settle_default_for(self, guild_id: int, target_id: int, debt_before: int, now: float) -> int:
        raise NotImplementedError

    def cds_void_for(self, guild_id: int, target_id: int) -> None:
        raise NotImplementedError


class NoOpBankCdsBridge(IBankCdsBridge):
    """銀行 CDS ブリッジ未接続時の LSP 準拠 Null 実装。"""

    def grace_until_for(self, guild_id: int, user_id: int) -> float | None:
        return None

    def grace_update_for(self, guild_id: int, user_id: int, now: float) -> None:
        return None

    def grace_clear_for(self, guild_id: int, user_id: int) -> None:
        return None

    def cds_cover_self_for(self, guild_id: int, user_id: int, shortfall: int, now: float) -> int:
        return 0

    def cds_settle_default_for(self, guild_id: int, target_id: int, debt_before: int, now: float) -> int:
        return 0

    def cds_void_for(self, guild_id: int, target_id: int) -> None:
        return None
