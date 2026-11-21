"""帝愛銀行リポジトリの基礎部分: コンストラクタと手元現金の同期。"""

from typing import Optional

from domain import teiai
from domain.ports.bridges import (
    IBankruptExecutor,
    IExcessLossSink,
    IGraceEnterHook,
    IMarginRatioReader,
    INoticeSink,
    IStatusReader,
    NoOpBankruptExecutor,
    NoOpExcessLossSink,
    NoOpGraceEnterHook,
    NoOpMarginRatioReader,
    NoOpNoticeSink,
    NoOpStatusReader,
)
from domain.ports.repos import IBankRepo
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo


class BankRepo(ScopedRepo, IBankRepo):
    """帝愛銀行・破産猶予・CDS。"""

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        notices: Optional[INoticeSink] = None,
        status_reader: Optional[IStatusReader] = None,
        margin_ratio_reader: Optional[IMarginRatioReader] = None,
        grace_enter_hook: Optional[IGraceEnterHook] = None,
        bankrupt_executor: Optional[IBankruptExecutor] = None,
        excess_sink: Optional[IExcessLossSink] = None,
        guild_id: int = 0,
        strict: bool = False,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._notices: INoticeSink = notices or NoOpNoticeSink()
        self._status_reader: IStatusReader = status_reader or NoOpStatusReader()
        self._margin_ratio_reader: IMarginRatioReader = margin_ratio_reader or NoOpMarginRatioReader()
        self._grace_enter_hook: IGraceEnterHook = grace_enter_hook or NoOpGraceEnterHook()
        self._bankrupt_executor: IBankruptExecutor = bankrupt_executor or NoOpBankruptExecutor()
        self._excess_sink: IExcessLossSink = excess_sink or NoOpExcessLossSink()

    # ------------------------------------------------------------------ #
    # 銀行の手元・預金・課税（IBankCashFlow / IHouseAccount / IGainsTaxCollector / IValuationSource）
    # ------------------------------------------------------------------ #
    def bank_sync_for(self, guild_id: int, now: float) -> dict[str, float]:
        """銀行の手元を取り出す（なければ作る）。地下融資の回収を、いまの時刻まで反映する。"""
        row = self._one("SELECT cash, loaned, ts FROM teiai_bank WHERE guild_id=?", (guild_id,))
        if row is None:
            self._conn.execute(
                "INSERT INTO teiai_bank (guild_id, cash, loaned, ts) VALUES (?, 0, 0, ?)", (guild_id, now)
            )
            return {"cash": 0.0, "loaned": 0.0}
        cash, loaned, hours = row["cash"], row["loaned"], (now - row["ts"]) / 3600
        if hours > 0:
            if loaned > 0:
                moved = loaned * (1 - (1 - teiai.LOAN_RECOVERY_PER_HOUR) ** hours)
                cash, loaned = cash + moved, loaned - moved
            self._conn.execute(
                "UPDATE teiai_bank SET cash=?, loaned=?, ts=? WHERE guild_id=?", (cash, loaned, now, guild_id)
            )
        return {"cash": cash, "loaned": loaned}

    def bank_set_for(self, guild_id: int, cash: float, loaned: float) -> None:
        self._conn.execute("UPDATE teiai_bank SET cash=?, loaned=? WHERE guild_id=?", (cash, loaned, guild_id))
