"""永続化・サービス層を疎結合に合成するための Bridge / Composite / Null-Object 契約（名目的基底クラス）。

- 契約基底クラス (`I*`) はすべて `raise NotImplementedError` で抽象契約のみを定義する。
- 省略可能な依存のフォールバックは、型・事後条件・不変条件を破らない `NoOp*` 実装（LSP 準拠）を用いる。
- 複数ステップの差し込み口は同一契約を実装する `Composite*` で束ね、呼び出し側の OCP を保つ。
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

from domain.ports.contracts import ActiveBond, SeizedBelongings

if TYPE_CHECKING:
    from domain.errors import Failure
    from domain.types import CryptoResult

AddAssetPart = Callable[[int, str, int], None]


# ------------------------------------------------------------------ #
# 台帳・借金ライフサイクル・返済（Account / Debt / Bonds / Games）     #
# ------------------------------------------------------------------ #
class IDebtLifecycleHook:
    """台帳更新に連動する借金利息・天引き・猶予更新のブリッジ契約。"""

    def accrue_interest(self, guild_id: int, user_id: int, now: Optional[float] = None) -> int:
        raise NotImplementedError

    def after_debt_change(self, guild_id: int, user_id: int) -> None:
        raise NotImplementedError

    def withhold_income(self, guild_id: int, user_id: int, income: int) -> int:
        raise NotImplementedError


class NoOpDebtLifecycleHook(IDebtLifecycleHook):
    """借金フック未接続時に安全に何もしない LSP 準拠実装。"""

    def accrue_interest(self, guild_id: int, user_id: int, now: Optional[float] = None) -> int:
        return 0

    def after_debt_change(self, guild_id: int, user_id: int) -> None:
        return None

    def withhold_income(self, guild_id: int, user_id: int, income: int) -> int:
        return 0


class IStatusReader:
    """ユーザーの借金・身分・ブラックリスト状態の読み取り契約。"""

    def status_for(self, guild_id: int, user_id: int, now: float) -> str:
        raise NotImplementedError

    def debt_for(self, guild_id: int, user_id: int) -> int:
        raise NotImplementedError

    def loan_blacklisted_for(self, guild_id: int, user_id: int, now: float) -> bool:
        raise NotImplementedError


class NoOpStatusReader(IStatusReader):
    """身分契約未接続時の安全デフォルト（カタギ・借金 0・制限なし）。"""

    def status_for(self, guild_id: int, user_id: int, now: float) -> str:
        return "katagi"

    def debt_for(self, guild_id: int, user_id: int) -> int:
        return 0

    def loan_blacklisted_for(self, guild_id: int, user_id: int, now: float) -> bool:
        return False


class IDebtOps:
    """信用取引・クイズ・預かり金・ウォレットが利用する借金・種銭操作契約。"""

    def borrow_seed_for(self, guild_id: int, user_id: int) -> tuple[Any, ...]:
        raise NotImplementedError

    def can_borrow_for_bet_for(self, guild_id: int, user_id: int, need: int) -> tuple[Any, ...]:
        raise NotImplementedError

    def seed_for(self, guild_id: int, user_id: int, amount: int) -> None:
        raise NotImplementedError

    def topup_seed_for(self, guild_id: int, user_id: int) -> int:
        raise NotImplementedError

    def owe_for(self, guild_id: int, user_id: int, shortfall: int, reason: str) -> int:
        raise NotImplementedError

    def accrue_debt_interest_for(self, guild_id: int, user_id: int, now: float) -> int:
        raise NotImplementedError


class NoOpDebtOps(IDebtOps):
    """借金操作契約未接続時の LSP 準拠 Null 実装。"""

    def borrow_seed_for(self, guild_id: int, user_id: int) -> tuple[Any, ...]:
        return False, ""

    def can_borrow_for_bet_for(self, guild_id: int, user_id: int, need: int) -> tuple[Any, ...]:
        return False, ""

    def seed_for(self, guild_id: int, user_id: int, amount: int) -> None:
        return None

    def topup_seed_for(self, guild_id: int, user_id: int) -> int:
        return 0

    def owe_for(self, guild_id: int, user_id: int, shortfall: int, reason: str) -> int:
        return 0

    def accrue_debt_interest_for(self, guild_id: int, user_id: int, now: float) -> int:
        return 0


class IDebtRepayHook:
    """債券償還など非収入の入金時に借金・延滞貸付へ全額充当する契約。"""

    def repay_all_of_for(self, guild_id: int, user_id: int, amount: int, label: str) -> int:
        raise NotImplementedError


class NoOpDebtRepayHook(IDebtRepayHook):
    """返済フック未接続時に 0 充当を返す LSP 準拠 Null 実装。"""

    def repay_all_of_for(self, guild_id: int, user_id: int, amount: int, label: str) -> int:
        return 0


# ------------------------------------------------------------------ #
# 銀行・猶予・CDS・通知・価格（Bank / Notices / Market）             #
# ------------------------------------------------------------------ #
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

    def bankrupt_for(self, guild_id: int, user_id: int, forced: bool = False, escape: bool = False) -> tuple[Any, ...]:
        raise NotImplementedError


class NoOpBankruptExecutor(IBankruptExecutor):
    """破産実行未接続時に失敗タプルを返す LSP 準拠 Null 実装。"""

    def bankrupt_for(self, guild_id: int, user_id: int, forced: bool = False, escape: bool = False) -> tuple[Any, ...]:
        return False, ""


# ------------------------------------------------------------------ #
# 信用取引・担保ガード・ランキング評価額（Margin / Assets / Wealthy）  #
# ------------------------------------------------------------------ #
class IMarginRatioReader:
    """対象ユーザーの信用取引維持率を参照する契約。"""

    def margin_ratio_for(self, guild_id: int, user_id: int) -> Optional[float]:
        raise NotImplementedError


class NoOpMarginRatioReader(IMarginRatioReader):
    """維持率未接続時に None を返す LSP 準拠 Null 実装。"""

    def margin_ratio_for(self, guild_id: int, user_id: int) -> Optional[float]:
        return None


class IMarginCollateralGuard:
    """現物株・債券を売るときに信用取引の維持率低下を防ぐガード契約。"""

    def check_spot_sell(
        self, guild_id: int, user_id: int, symbol: str, units: Optional[int], have: int, price: float
    ) -> Optional["Failure"]:
        raise NotImplementedError

    def check_bond_cancel(self, guild_id: int, user_id: int, kind: str, principal: int) -> Optional["Failure"]:
        raise NotImplementedError


class NoOpMarginCollateralGuard(IMarginCollateralGuard):
    """担保ガード未接続時に制約なし (None) を返す LSP 準拠 Null 実装。"""

    def check_spot_sell(
        self, guild_id: int, user_id: int, symbol: str, units: Optional[int], have: int, price: float
    ) -> Optional["Failure"]:
        return None

    def check_bond_cancel(self, guild_id: int, user_id: int, kind: str, principal: int) -> Optional["Failure"]:
        return None


class IWealthyReader:
    """サーバ内の富豪ユーザーID集合を取得する契約。"""

    def wealthy_ids_for(self, guild_id: int) -> set[int]:
        raise NotImplementedError


class NoOpWealthyReader(IWealthyReader):
    """富豪判定未接続時に空集合を返す LSP 準拠 Null 実装。"""

    def wealthy_ids_for(self, guild_id: int) -> set[int]:
        return set()


class IValuationSource:
    """ランキング純資産に合算する 1 種類の資産評価額ソース（Composite 対象）。"""

    def collect_values(self, guild_id: int, totals: dict[int, int]) -> None:
        raise NotImplementedError


class CompositeValuationSource(IValuationSource):
    """複数の IValuationSource を束ねて純資産ランキング評価額を集計する Composite。"""

    def __init__(self, sources: tuple[IValuationSource, ...]) -> None:
        self._sources = sources

    def collect_values(self, guild_id: int, totals: dict[int, int]) -> None:
        for source in self._sources:
            source.collect_values(guild_id, totals)


class IAssetCollector:
    """1 つの資産カテゴリを内訳辞書へ加算するコレクタ契約（OCP / Composite）。"""

    def collect(self, guild_id: int, add: AddAssetPart, now: float) -> None:
        raise NotImplementedError


class CompositeAssetCollector(IAssetCollector):
    """複数の IAssetCollector を束ねて順次適用する Composite。"""

    def __init__(self, collectors: tuple[IAssetCollector, ...]) -> None:
        self._collectors = collectors

    def collect(self, guild_id: int, add: AddAssetPart, now: float) -> None:
        for collector in self._collectors:
            collector.collect(guild_id, add, now)


# ------------------------------------------------------------------ #
# 強制執行・自己破産ステップ群（OCP / Composite / LSP）                 #
# ------------------------------------------------------------------ #
class IOptionHoldings:
    """差し押さえ・破産の換金で使う、オプション建玉の照会と決済の契約（options 表を外へ見せない）。"""

    def active_option_ids_for(self, guild_id: int, user_id: int) -> list[int]:
        raise NotImplementedError

    def option_close_locked_for(self, guild_id: int, user_id: int, option_id: int, now: float) -> tuple[Any, ...]:
        raise NotImplementedError


class IBondHoldings:
    """差し押さえ・破産の換金で使う、債券の照会と解約・償却の契約（bonds 表を外へ見せない）。"""

    def active_bonds_for(self, guild_id: int, user_id: int) -> list[ActiveBond]:
        raise NotImplementedError

    def bond_cancel_locked_for(
        self, guild_id: int, user_id: int, bond_id: int, now: Optional[float] = None
    ) -> tuple[Any, ...]:
        raise NotImplementedError

    def bond_write_off_for(self, guild_id: int, user_id: int, bond_id: int, payout: int, now: float) -> None:
        """解約できない債券を、payout だけ払い戻して解約済みにする（破産の換金用。払い戻しは帝愛銀行から）。"""
        raise NotImplementedError


class IShopBelongings:
    """破産で、ショップの持ち物・装備・消耗品をすべて没収する契約（inventory・profile 表を外へ見せない）。"""

    def seize_belongings_for(self, guild_id: int, user_id: int) -> SeizedBelongings:
        raise NotImplementedError


class IAssetSeizureStep:
    """借金返済不足時の強制執行（ファーストロス差し押さえ）の 1 ステップ（OCP）。"""

    def seize(self, guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
        raise NotImplementedError


class NoOpAssetSeizureStep(IAssetSeizureStep):
    """差し押さえステップ未設定時の LSP 準拠 Null 実装。"""

    def seize(self, guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
        return 0


class CompositeAssetSeizureStep(IAssetSeizureStep):
    """複数の IAssetSeizureStep を順に実行し、必要額に達した時点で停止する Composite。"""

    def __init__(self, steps: tuple[IAssetSeizureStep, ...]) -> None:
        self._steps = steps

    def seize(self, guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
        covered = 0
        for step in self._steps:
            if covered >= need:
                break
            covered += step.seize(guild_id, user_id, need - covered, reason, now)
        return covered


class ILiquidationStep:
    """自己破産時の保有金融資産の換金（口座現金への回収）の 1 ステップ（OCP）。"""

    def liquidate(self, guild_id: int, user_id: int, now: float) -> None:
        raise NotImplementedError


class NoOpLiquidationStep(ILiquidationStep):
    """換金ステップ未設定時の LSP 準拠 Null 実装。"""

    def liquidate(self, guild_id: int, user_id: int, now: float) -> None:
        return None


class CompositeLiquidationStep(ILiquidationStep):
    """複数の ILiquidationStep を順に実行する Composite。"""

    def __init__(self, steps: tuple[ILiquidationStep, ...]) -> None:
        self._steps = steps

    def liquidate(self, guild_id: int, user_id: int, now: float) -> None:
        for step in self._steps:
            step.liquidate(guild_id, user_id, now)


class ISeizureValueStep:
    """自己破産時の現物・馬などの競売出品と査定額算出の 1 ステップ（OCP）。"""

    def seize(self, guild_id: int, user_id: int, now: float) -> int:
        raise NotImplementedError


class NoOpSeizureValueStep(ISeizureValueStep):
    """競売査定ステップ未設定時の LSP 準拠 Null 実装。"""

    def seize(self, guild_id: int, user_id: int, now: float) -> int:
        return 0


class CompositeSeizureValueStep(ISeizureValueStep):
    """複数の ISeizureValueStep を順に実行し、合計換金額を返す Composite。"""

    def __init__(self, steps: tuple[ISeizureValueStep, ...]) -> None:
        self._steps = steps

    def seize(self, guild_id: int, user_id: int, now: float) -> int:
        return sum(step.seize(guild_id, user_id, now) for step in self._steps)


class IBankruptPrepHook:
    """自己破産前の資産引き戻しフック（例: P2P出品・入札の引き戻し）。"""

    def prepare(self, guild_id: int, user_id: int, now: float) -> None:
        raise NotImplementedError


class NoOpBankruptPrepHook(IBankruptPrepHook):
    """破産前フック未設定時の LSP 準拠 Null 実装。"""

    def prepare(self, guild_id: int, user_id: int, now: float) -> None:
        return None


class CompositeBankruptPrepHook(IBankruptPrepHook):
    """複数の IBankruptPrepHook を順次実行する Composite。"""

    def __init__(self, hooks: tuple[IBankruptPrepHook, ...]) -> None:
        self._hooks = hooks

    def prepare(self, guild_id: int, user_id: int, now: float) -> None:
        for hook in self._hooks:
            hook.prepare(guild_id, user_id, now)


# ------------------------------------------------------------------ #
# 個人間貸付・代用有価証券（Loans / Spot Collateral）                  #
# ------------------------------------------------------------------ #
class IOverdueLoanCollector:
    """収入・償還・破産整理時に個人間貸付の状態参照と返済整理を行う契約。"""

    def collect_overdue_for(self, guild_id: int, user_id: int, budget: int) -> int:
        raise NotImplementedError

    def loan_remaining(self, loan: dict[str, Any], now: float) -> int:
        raise NotImplementedError

    def has_overdue_for(self, guild_id: int, user_id: int) -> bool:
        raise NotImplementedError

    def active_loans_for(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        raise NotImplementedError

    def record_bankrupt_payment_for(self, loan_id: int, paid: int) -> None:
        raise NotImplementedError

    def finalize_bankrupt_loan_for(self, loan_id: int, repaid: bool) -> None:
        raise NotImplementedError


class NoOpOverdueLoanCollector(IOverdueLoanCollector):
    """個人間貸付契約未接続時の LSP 準拠 Null 実装。"""

    def collect_overdue_for(self, guild_id: int, user_id: int, budget: int) -> int:
        return 0

    def loan_remaining(self, loan: dict[str, Any], now: float) -> int:
        return 0

    def has_overdue_for(self, guild_id: int, user_id: int) -> bool:
        return False

    def active_loans_for(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        return []

    def record_bankrupt_payment_for(self, loan_id: int, paid: int) -> None:
        return None

    def finalize_bankrupt_loan_for(self, loan_id: int, repaid: bool) -> None:
        return None


class ISpotCollateralReader:
    """信用取引の代用有価証券（現物株）読み取り契約。"""

    def holdings_for(self, guild_id: int, user_id: int) -> list[tuple[str, int, int]]:
        raise NotImplementedError

    def crypto_sell_locked_for(
        self,
        guild_id: int,
        user_id: int,
        units: Optional[int],
        price: float,
        symbol: str,
        taxable: bool = False,
    ) -> "CryptoResult":
        raise NotImplementedError


class NoOpSpotCollateralReader(ISpotCollateralReader):
    """代用有価証券契約未接続時の LSP 準拠 Null 実装。"""

    def holdings_for(self, guild_id: int, user_id: int) -> list[tuple[str, int, int]]:
        return []

    def crypto_sell_locked_for(
        self,
        guild_id: int,
        user_id: int,
        units: Optional[int],
        price: float,
        symbol: str,
        taxable: bool = False,
    ) -> "CryptoResult":
        from domain.types import CryptoResult

        return CryptoResult(ok=False)
