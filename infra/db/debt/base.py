"""帝愛ファイナンスリポジトリの基礎部分: コンストラクタと身分・借金の読み取り。"""

from domain import teiai
from domain.ports.bridges import (
    AssetSeizureStep,
    BankruptPrepHook,
    IBankCdsBridge,
    INoticeSink,
    IOverdueLoanCollector,
    LiquidationStep,
    NoOpBankCdsBridge,
    NoOpNoticeSink,
    NoOpOverdueLoanCollector,
    SeizureValueStep,
    noop_asset_seizure_step,
    noop_bankrupt_prep_hook,
    noop_liquidation_step,
    noop_seizure_value_step,
)
from domain.ports.repos import IDebtRepo
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo


def _take_cash(account: AccountCore, guild_id: int, user_id: int, need: int, reason: str) -> int:
    """手元の現金から need まで差し押さえる。取れた額を返す。"""
    take = min(account.raw_balance(guild_id, user_id), need)
    if take > 0:
        account.apply(guild_id, user_id, -take, reason)
    return take


class DebtRepo(ScopedRepo, IDebtRepo):
    """借金・強制執行・種銭・身分・自己破産。"""

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        notices: INoticeSink | None = None,
        bank: IBankCdsBridge | None = None,
        loans: IOverdueLoanCollector | None = None,
        *,
        seizure_step: AssetSeizureStep | None = None,
        liquidation_step: LiquidationStep | None = None,
        seizure_value_step: SeizureValueStep | None = None,
        bankrupt_prep_hook: BankruptPrepHook | None = None,
        guild_id: int = 0,
        strict: bool = False,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._notices: INoticeSink = notices or NoOpNoticeSink()
        self._bank: IBankCdsBridge = bank or NoOpBankCdsBridge()
        self._loans: IOverdueLoanCollector = loans or NoOpOverdueLoanCollector()
        self._seizure_step: AssetSeizureStep = seizure_step or noop_asset_seizure_step
        self._liquidation_step: LiquidationStep = liquidation_step or noop_liquidation_step
        self._seizure_value_step: SeizureValueStep = seizure_value_step or noop_seizure_value_step
        self._bankrupt_prep_hook: BankruptPrepHook = bankrupt_prep_hook or noop_bankrupt_prep_hook

    # 身分・読み取り契約（IStatusReader）

    def status_for(self, guild_id: int, user_id: int, now: float) -> str:
        """身分。読み取り専用で、口座は作らない。"""
        with self._lock:
            row = self._one(
                "SELECT COALESCE(d.debt, 0) AS debt, b.bankrupt_at, b.blacklist_until "
                "FROM accounts a "
                "LEFT JOIN teiai_debts d ON d.guild_id=a.guild_id AND d.user_id=a.user_id "
                "LEFT JOIN teiai_bankruptcies b ON b.guild_id=a.guild_id AND b.user_id=a.user_id "
                "WHERE a.guild_id=? AND a.user_id=?",
                (guild_id, user_id),
            )
            if row is None:
                return teiai.KATAGI
            if row["bankrupt_at"] is not None and row["blacklist_until"] is None and row["debt"] > 0:
                return teiai.BANKRUPT
            if row["debt"] > 0 or self._loans.has_overdue_for(guild_id, user_id):
                return teiai.UNDERGROUND
            return teiai.KATAGI

    def status(self, user_id: int, now: float | None = None) -> str:
        return self.status_for(self._g(), user_id, self._now() if now is None else now)

    def loan_blacklisted_for(self, guild_id: int, user_id: int, now: float) -> bool:
        """破産のブラックリストの間か（貸付・種銭・帝愛の信用を使えない）。"""
        with self._lock:
            row = self._one(
                "SELECT bankrupt_at, blacklist_until FROM teiai_bankruptcies WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            )
        if row is None or row["bankrupt_at"] is None:
            return False
        if row["blacklist_until"] is None:
            return True
        return now < row["blacklist_until"]

    def loan_blacklisted(self, user_id: int, now: float | None = None) -> bool:
        return self.loan_blacklisted_for(self._g(), user_id, self._now() if now is None else now)

    def debt_for(self, guild_id: int, user_id: int) -> int:
        return self._account.debt_readonly(guild_id, user_id)

    def debt(self, user_id: int) -> int:
        """借金の残り。口座が無ければ 0（口座は作らない）。"""
        return self._account.debt_readonly(self._g(), user_id)

    def _seize_assets_for(
        self, guild_id: int, user_id: int, need: int, reason: str, now: float | None = None
    ) -> int:
        """現金と流動資産を順に差し押さえ、確保できた合計額を返す。"""
        ts = self._now() if now is None else now
        covered = _take_cash(self._account, guild_id, user_id, need, reason)
        if covered >= need:
            return covered
        return covered + self._seizure_step(guild_id, user_id, need - covered, reason, ts)
