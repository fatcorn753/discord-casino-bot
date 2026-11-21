"""帝愛ファイナンス（借金・返済・自己破産・海外逃亡・帝愛銀行・CDS・種銭）のアプリケーションサービス。"""

import random
from dataclasses import dataclass
from typing import Any, Optional

from domain import teiai
from domain.ports.clock import IClock
from domain.ports.contracts import BankStatus, CdsHoldings, CdsQuote, HouseAccount
from domain.ports.repos import IBankRepo, IDebtRepo, ILoanRepo, INoticeRepo, IWalletRepo


@dataclass(frozen=True)
class TeiaiPanelSnapshot:
    """`/ペリカ 帝愛` の画面に出す値（表示の組み立ては cogs/teiai/panel.py）。"""

    bank: BankStatus
    house: HouseAccount
    graced: list[tuple[int, int, float]]
    claims: int
    status: str
    display_status: str
    deposit: int
    debt: int
    grace_until: Optional[float]
    cds: CdsHoldings
    uid: int


class TeiaiService:
    """帝愛まわりのユースケース。Cog は Repository に触れず、ここを通して状態を変える。"""

    def __init__(
        self,
        bank: IBankRepo,
        debt: IDebtRepo,
        wallet: IWalletRepo,
        notices: INoticeRepo,
        loans: ILoanRepo,
        clock: IClock,
    ) -> None:
        self._bank = bank
        self._debt = debt
        self._wallet = wallet
        self._notices = notices
        self._loans = loans
        self._clock = clock

    # ------------------------------ 借金 ------------------------------ #
    def debt_of(self, guild_id: int, user_id: int) -> int:
        return self._debt.for_guild(guild_id).debt(user_id)

    def status_of(self, guild_id: int, user_id: int) -> str:
        return self._debt.for_guild(guild_id).status(user_id)

    def can_borrow_for_bet(self, guild_id: int, user_id: int, need: int) -> tuple[Any, ...]:
        return self._debt.for_guild(guild_id).can_borrow_for_bet(user_id, need)

    def repay(self, guild_id: int, user_id: int, amount: Optional[int]) -> tuple[Any, ...]:
        """帝愛への返済。amount が None なら返せる限り全額。"""
        return self._debt.for_guild(guild_id).repay_debt(user_id, amount)

    def borrow_seed(self, guild_id: int, user_id: int) -> tuple[Any, ...]:
        """種銭を借りる。結果にかかわらず、溜まった一時通知は掃き出す。"""
        result = self._debt.for_guild(guild_id).borrow_seed(user_id)
        self._notices.for_guild(guild_id).take_notices(user_id)
        return result

    def can_go_bankrupt(self, guild_id: int, user_id: int) -> Optional[str]:
        """自己破産できないときの理由（"already" / "nothing"）。できるなら None。"""
        if self._debt.for_guild(guild_id).status(user_id) == teiai.BANKRUPT:
            return "already"
        if self.debt_of(guild_id, user_id) <= 0 and not self._loans.for_guild(guild_id).loan_list(user_id)["borrowed"]:
            return "nothing"
        return None

    def bankrupt(self, guild_id: int, user_id: int) -> tuple[Any, ...]:
        return self._debt.for_guild(guild_id).bankrupt(user_id)

    def take_notices(self, guild_id: int, user_id: int) -> None:
        self._notices.for_guild(guild_id).take_notices(user_id)

    # ------------------------------ 海外逃亡 ------------------------------ #
    def escape_left(self, guild_id: int, user_id: int) -> int:
        return self._debt.for_guild(guild_id).escape_left(user_id)

    def escape_attempt(self, guild_id: int, user_id: int) -> tuple[Any, ...]:
        return self._debt.for_guild(guild_id).escape_attempt(user_id, self._clock.now_ts())

    # ------------------------------ 帝愛銀行・CDS ------------------------------ #
    def panel(self, guild_id: int, user_id: int) -> TeiaiPanelSnapshot:
        now = self._clock.now_ts()
        bank, debt = self._bank.for_guild(guild_id), self._debt.for_guild(guild_id)
        status = debt.status(user_id, now)
        return TeiaiPanelSnapshot(
            bank=bank.bank_status(now),
            house=bank.house_account(),
            graced=bank.grace_list(),
            claims=bank.cds_total_claims(),
            status=status,
            display_status=(
                self._wallet.for_guild(guild_id).display_status(user_id) if status == teiai.KATAGI else status
            ),
            deposit=bank.deposit_of(user_id),
            debt=debt.debt(user_id),
            grace_until=bank.grace_until(user_id),
            cds=bank.cds_list(user_id),
            uid=user_id,
        )

    def deposit(self, guild_id: int, user_id: int, amount: int) -> tuple[Any, ...]:
        return self._bank.for_guild(guild_id).bank_deposit(user_id, amount, self._clock.now_ts())

    def withdraw(self, guild_id: int, user_id: int, amount: Optional[int]) -> tuple[Any, ...]:
        """amount が None なら全額。"""
        return self._bank.for_guild(guild_id).bank_withdraw(user_id, amount, self._clock.now_ts())

    def cds_quote(self, guild_id: int, target_id: int) -> CdsQuote:
        return self._bank.for_guild(guild_id).cds_quote(target_id, self._clock.now_ts())

    def cds_buy(self, guild_id: int, user_id: int, target_id: int, stake: int) -> tuple[Any, ...]:
        return self._bank.for_guild(guild_id).cds_buy(user_id, target_id, stake, self._clock.now_ts())

    def grace_expire(self, guild_id: int, now: float) -> list[tuple[int, Any]]:
        """破産猶予の時間切れ（強制破産と、CDS の配当）。(user_id, 結果) の一覧。"""
        return self._bank.for_guild(guild_id).grace_expire(now)

    def rf_tick(self, guild_id: int, now: float, rng: Optional[random.Random] = None) -> float:
        """無リスク金利を、準備率に応じて進める（毎分。#18）。"""
        return self._bank.for_guild(guild_id).bank_rf_tick(now, rng)

    def trust_accrue(self, guild_id: int, now: float) -> int:
        """帝愛信託の利息を払う（1時間ごと。払った額を返す）。"""
        return self._bank.for_guild(guild_id).bank_trust_accrue(now)
