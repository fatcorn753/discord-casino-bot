"""銀行・猶予・CDS・通知・価格・胴元・破産実行のブリッジ契約（名目的基底クラスと NoOp / Composite 実装）。"""

from typing import Any, Optional

from domain.errors import Outcome, fail
from domain.ports.contracts import BankruptResult


class INoticeSink:
    """処理中に発生したユーザー向け一言通知を記録する書き込み契約。"""

    def note_for(self, guild_id: int, user_id: int, code: str, **params: Any) -> None:
        raise NotImplementedError


class NoOpNoticeSink(INoticeSink):
    """通知シンク未接続時に記録を無視する LSP 準拠 Null 実装。"""

    def note_for(self, guild_id: int, user_id: int, code: str, **params: Any) -> None:
        return None


class IMarketPriceReader:
    """保有資産の評価額算出・強制決済に使う最新市場価格の読み取り契約。"""

    def latest_prices(self) -> dict[str, float]:
        raise NotImplementedError


class NoOpMarketPriceReader(IMarketPriceReader):
    """価格ソース未接続時に空辞書を返す LSP 準拠 Null 実装。"""

    def latest_prices(self) -> dict[str, float]:
        return {}


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
    """胴元口座（ピンハネの受け入れ・払い出し）と、日次支給枠の契約。"""

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


class IGainsTaxCollector:
    """キャピタルゲイン課税の徴収契約。"""

    def charge_gains_tax_for(self, guild_id: int, user_id: int, profit: int, taxable: bool, what: str) -> int:
        raise NotImplementedError


class IGraceReader:
    """破産猶予の終了時刻を参照する読み取り契約。"""

    def grace_until_for(self, guild_id: int, user_id: int) -> Optional[float]:
        raise NotImplementedError


class NoOpGraceReader(IGraceReader):
    """猶予読み取り未接続時に None を返す LSP 準拠 Null 実装。"""

    def grace_until_for(self, guild_id: int, user_id: int) -> Optional[float]:
        return None


class IBankCdsBridge(IGraceReader):
    """DebtRepo が BankRepo 具体型に依存せず猶予更新と CDS 精算を行う契約。"""

    def grace_update_for(self, guild_id: int, user_id: int, now: float) -> None:
        raise NotImplementedError

    def cds_cover_self_for(self, guild_id: int, user_id: int, shortfall: int, now: float) -> int:
        raise NotImplementedError

    def cds_settle_default_for(self, guild_id: int, target_id: int, debt_before: int, now: float) -> int:
        raise NotImplementedError

    def cds_void_for(self, guild_id: int, target_id: int) -> None:
        raise NotImplementedError


class NoOpBankCdsBridge(IBankCdsBridge):
    """銀行 CDS ブリッジ未接続時の LSP 準拠 Null 実装。"""

    def grace_until_for(self, guild_id: int, user_id: int) -> Optional[float]:
        return None

    def grace_update_for(self, guild_id: int, user_id: int, now: float) -> None:
        return None

    def cds_cover_self_for(self, guild_id: int, user_id: int, shortfall: int, now: float) -> int:
        return 0

    def cds_settle_default_for(self, guild_id: int, target_id: int, debt_before: int, now: float) -> int:
        return 0

    def cds_void_for(self, guild_id: int, target_id: int) -> None:
        return None


class IGraceEnterHook:
    """破産猶予入り時に連動して実行するフック契約（例: P2P出品引き戻し）。"""

    def on_grace_enter(self, guild_id: int, user_id: int, now: float) -> None:
        raise NotImplementedError


class IExcessLossSink:
    """帝愛信託でも受け止めきれない超過損失のうち、熱浴が肩代わりしない分を負う先（#34 の帝愛共済）。"""

    def charge_excess_for(self, guild_id: int, amount: int, now: float) -> int:
        """amount を請求し、集められた額を返す（銀行の手元には入れず、そのまま支払いに充てる）。"""
        raise NotImplementedError


class NoOpExcessLossSink(IExcessLossSink):
    """負う先が無いとき: 何も集めない（超過損失は払われない）。"""

    def charge_excess_for(self, guild_id: int, amount: int, now: float) -> int:
        return 0


class NoOpGraceEnterHook(IGraceEnterHook):
    """猶予入りフック未接続時の LSP 準拠 Null 実装。"""

    def on_grace_enter(self, guild_id: int, user_id: int, now: float) -> None:
        return None


class CompositeGraceEnterHook(IGraceEnterHook):
    """複数の IGraceEnterHook を束ねて順次適用する Composite。"""

    def __init__(self, hooks: tuple[IGraceEnterHook, ...]) -> None:
        self._hooks = hooks

    def on_grace_enter(self, guild_id: int, user_id: int, now: float) -> None:
        for hook in self._hooks:
            hook.on_grace_enter(guild_id, user_id, now)


class IBankruptExecutor:
    """猶予期限切れなどから強制破産を実行する契約。"""

    def bankrupt_for(
        self, guild_id: int, user_id: int, forced: bool = False, escape: bool = False
    ) -> Outcome[BankruptResult]:
        raise NotImplementedError


class NoOpBankruptExecutor(IBankruptExecutor):
    """破産実行未接続時に失敗タプルを返す LSP 準拠 Null 実装。"""

    def bankrupt_for(
        self, guild_id: int, user_id: int, forced: bool = False, escape: bool = False
    ) -> Outcome[BankruptResult]:
        return False, fail("unavailable")
