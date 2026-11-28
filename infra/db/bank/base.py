"""帝愛銀行リポジトリの基礎部分: コンストラクタと手元現金の同期。"""

from domain import teiai
from domain.ports.bridges import (
    BankruptExecutor,
    ExcessLossSink,
    GraceEnterHook,
    INoticeSink,
    IStatusReader,
    MarginRatioReader,
    NoOpNoticeSink,
    NoOpStatusReader,
    noop_bankrupt_executor,
    noop_excess_loss_sink,
    noop_grace_enter_hook,
    noop_margin_ratio_reader,
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
        notices: INoticeSink | None = None,
        status_reader: IStatusReader | None = None,
        margin_ratio_reader: MarginRatioReader | None = None,
        grace_enter_hook: GraceEnterHook | None = None,
        bankrupt_executor: BankruptExecutor | None = None,
        excess_sink: ExcessLossSink | None = None,
        guild_id: int = 0,
        strict: bool = False,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._notices: INoticeSink = notices or NoOpNoticeSink()
        self._status_reader: IStatusReader = status_reader or NoOpStatusReader()
        self._margin_ratio_reader: MarginRatioReader = margin_ratio_reader or noop_margin_ratio_reader
        self._grace_enter_hook: GraceEnterHook = grace_enter_hook or noop_grace_enter_hook
        self._bankrupt_executor: BankruptExecutor = bankrupt_executor or noop_bankrupt_executor
        self._excess_sink: ExcessLossSink = excess_sink or noop_excess_loss_sink

    # 銀行の手元・預金・課税（IBankCashFlow / IHouseAccount / GainsTaxCollector / ValuationSource）

    def _ensure_bank_children(self, guild_id: int) -> None:
        self._conn.execute("INSERT OR IGNORE INTO teiai_thermostat (guild_id) VALUES (?)", (guild_id,))
        self._conn.execute("INSERT OR IGNORE INTO teiai_trust (guild_id) VALUES (?)", (guild_id,))

    def bank_sync_for(self, guild_id: int, now: float) -> dict[str, float]:
        """銀行の手元を取り出す（なければ作る）。地下融資の回収を、いまの時刻まで反映する。"""
        row = self._one("SELECT cash, loaned, ts FROM teiai_bank WHERE guild_id=?", (guild_id,))
        if row is None:
            self._conn.execute(
                "INSERT INTO teiai_bank (guild_id, cash, loaned, ts) VALUES (?, 0, 0, ?)", (guild_id, now)
            )
            self._ensure_bank_children(guild_id)
            return {"cash": 0.0, "loaned": 0.0}
        self._ensure_bank_children(guild_id)
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
