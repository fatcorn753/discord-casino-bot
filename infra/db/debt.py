"""帝愛ファイナンス: 借金・強制執行・種銭・身分・自己破産。数字と考え方は domain/teiai.py。

表の定義は infra/db/schema/tables.py（users の debt・bankrupt_at・blacklist_until）。
差し押さえ（IAssetSeizureStep）・換金（ILiquidationStep）・競売査定（ISeizureValueStep）は
拡張に開き修正に閉じるため（OCP）独立戦略クラスとして構成する。
"""

import random
from typing import Any, Optional, cast

from domain import market as market_mod
from domain import shop as shop_mod
from domain import teiai
from domain.errors import fail
from domain.ports.bridges import (
    IAssetSeizureStep,
    IBankCdsBridge,
    IBankruptPrepHook,
    IBondHoldings,
    ILiquidationStep,
    IMarketPriceReader,
    INoticeSink,
    IOptionHoldings,
    IOverdueLoanCollector,
    ISeizureValueStep,
    IShopBelongings,
    NoOpAssetSeizureStep,
    NoOpBankCdsBridge,
    NoOpBankruptPrepHook,
    NoOpLiquidationStep,
    NoOpNoticeSink,
    NoOpOverdueLoanCollector,
    NoOpSeizureValueStep,
)
from domain.ports.repos import IDebtRepo, IKyosaiRepo
from infra.db.account import AccountCore
from infra.db.auction import AuctionRepo
from infra.db.bank import BankRepo
from infra.db.base import EcoDb, ScopedRepo
from infra.db.crypto import CryptoRepo
from infra.db.horse import HorseRepo
from infra.db.margin import MarginRepo


def _take_cash(account: AccountCore, guild_id: int, user_id: int, need: int, reason: str) -> int:
    """手元の現金から need まで差し押さえる。取れた額を返す。"""
    take = min(account.raw_balance(guild_id, user_id), need)
    if take > 0:
        account.apply(guild_id, user_id, -take, reason)
    return take


# ------------------------------------------------------------------ #
# 強制執行（ファーストロス差し押さえ）ステップ（OCP）                 #
# ------------------------------------------------------------------ #
class DepositSeizureStep(IAssetSeizureStep):
    def __init__(self, account: AccountCore, bank: BankRepo, notices: INoticeSink) -> None:
        self._account = account
        self._bank = bank
        self._notices = notices

    def seize(self, guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
        taken = self._bank.deposit_take_for(guild_id, user_id, need, now)
        if taken:
            self._account.ledger(
                guild_id,
                user_id,
                0,
                self._account.raw_balance(guild_id, user_id),
                f"強制執行:預金の差し押さえ（{taken:,}ペリカ）",
            )
            self._notices.note_for(guild_id, user_id, "seize_deposit", taken=taken)
        return taken


class MarginSeizureStep(IAssetSeizureStep):
    def __init__(
        self, account: AccountCore, market: IMarketPriceReader, margin: MarginRepo, notices: INoticeSink
    ) -> None:
        self._account = account
        self._market = market
        self._margin = margin
        self._notices = notices

    def seize(self, guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
        covered = 0
        prices = self._market.latest_prices()
        for side, symbol, *_ in self._margin.margin_positions_for(guild_id, user_id):
            if covered >= need or symbol not in prices:
                continue
            name = self._margin.SIDE_NAME[side]
            result = self._margin.margin_close_locked_for(
                guild_id, user_id, side, None, prices[symbol], f"強制執行:{name}決済", symbol
            )
            self._notices.note_for(
                guild_id,
                user_id,
                "seize_margin",
                symbol_name=market_mod.BY_KEY[symbol].name,
                side_label=name,
                money=result.money,
            )
            covered += _take_cash(self._account, guild_id, user_id, need - covered, reason)
        return covered


class CryptoSeizureStep(IAssetSeizureStep):
    def __init__(
        self, account: AccountCore, market: IMarketPriceReader, crypto: CryptoRepo, notices: INoticeSink
    ) -> None:
        self._account = account
        self._market = market
        self._crypto = crypto
        self._notices = notices

    def seize(self, guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
        covered = 0
        prices = self._market.latest_prices()
        for symbol, _units, _ in self._crypto.holdings_for(guild_id, user_id):
            if covered >= need or symbol not in prices:
                continue
            result = self._crypto.crypto_sell_locked_for(guild_id, user_id, None, prices[symbol], symbol)
            self._notices.note_for(
                guild_id,
                user_id,
                "seize_spot",
                symbol_name=market_mod.BY_KEY[symbol].name,
                money=result.money,
            )
            covered += _take_cash(self._account, guild_id, user_id, need - covered, reason)
        return covered


class OptionSeizureStep(IAssetSeizureStep):
    def __init__(self, account: AccountCore, options: IOptionHoldings, notices: INoticeSink) -> None:
        self._account = account
        self._options = options
        self._notices = notices

    def seize(self, guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
        covered = 0
        for option_id in self._options.active_option_ids_for(guild_id, user_id):
            if covered >= need:
                break
            ok, got = self._options.option_close_locked_for(guild_id, user_id, option_id, now)
            if ok and got > 0:
                self._notices.note_for(guild_id, user_id, "seize_option", got=got)
                covered += _take_cash(self._account, guild_id, user_id, need - covered, reason)
        return covered


class BondSeizureStep(IAssetSeizureStep):
    def __init__(self, account: AccountCore, bonds: IBondHoldings, notices: INoticeSink) -> None:
        self._account = account
        self._bonds = bonds
        self._notices = notices

    def seize(self, guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
        covered = 0
        for bond in self._bonds.active_bonds_for(guild_id, user_id):
            if covered >= need:
                break
            ok, refund = self._bonds.bond_cancel_locked_for(guild_id, user_id, bond.id, now)
            if ok:
                self._notices.note_for(guild_id, user_id, "seize_bond", refund=refund)
                covered += _take_cash(self._account, guild_id, user_id, need - covered, reason)
        return covered


# ------------------------------------------------------------------ #
# 自己破産時の換金ステップ（OCP）                                      #
# ------------------------------------------------------------------ #
class MarginLiquidationStep(ILiquidationStep):
    def __init__(self, market: IMarketPriceReader, margin: MarginRepo) -> None:
        self._market = market
        self._margin = margin

    def liquidate(self, guild_id: int, user_id: int, now: float) -> None:
        prices = self._market.latest_prices()
        for side, symbol, *_ in self._margin.margin_positions_for(guild_id, user_id):
            if symbol in prices:
                self._margin.margin_close_locked_for(
                    guild_id, user_id, side, None, prices[symbol], f"破産:{self._margin.SIDE_NAME[side]}決済", symbol
                )


class CryptoLiquidationStep(ILiquidationStep):
    def __init__(self, market: IMarketPriceReader, crypto: CryptoRepo) -> None:
        self._market = market
        self._crypto = crypto

    def liquidate(self, guild_id: int, user_id: int, now: float) -> None:
        prices = self._market.latest_prices()
        for symbol, _, _ in self._crypto.holdings_for(guild_id, user_id):
            if symbol in prices:
                self._crypto.crypto_sell_locked_for(guild_id, user_id, None, prices[symbol], symbol)


class BondLiquidationStep(ILiquidationStep):
    def __init__(self, account: AccountCore, bonds: IBondHoldings) -> None:
        self._account = account
        self._bonds = bonds

    def liquidate(self, guild_id: int, user_id: int, now: float) -> None:
        for bond in self._bonds.active_bonds_for(guild_id, user_id):
            ok, _ = self._bonds.bond_cancel_locked_for(guild_id, user_id, bond.id, now)
            if not ok:  # 解約できない債券は、元本の一部で換金して、差し押さえる
                payout = int(bond.principal * teiai.SEIZE_RATE)
                self._bonds.bond_write_off_for(guild_id, user_id, bond.id, payout, now)


class OptionLiquidationStep(ILiquidationStep):
    def __init__(self, options: IOptionHoldings) -> None:
        self._options = options

    def liquidate(self, guild_id: int, user_id: int, now: float) -> None:
        for option_id in self._options.active_option_ids_for(guild_id, user_id):
            self._options.option_close_locked_for(guild_id, user_id, option_id, now)


class DepositLiquidationStep(ILiquidationStep):
    def __init__(self, account: AccountCore, bank: BankRepo) -> None:
        self._account = account
        self._bank = bank

    def liquidate(self, guild_id: int, user_id: int, now: float) -> None:
        saved = self._bank.deposit_take_for(guild_id, user_id, self._bank.deposit_of_for(guild_id, user_id), now)
        if saved:
            self._account.apply(guild_id, user_id, saved, "破産:預金の差し押さえ")


class KyosaiLiquidationStep(ILiquidationStep):
    """自己破産で、帝愛共済の口を帝愛の持ち口へ回収する（#34。値段はつけず、破産財団には入れない）。"""

    def __init__(self, kyosai: IKyosaiRepo) -> None:
        self._kyosai = kyosai

    def liquidate(self, guild_id: int, user_id: int, now: float) -> None:
        self._kyosai.recover_units_for(guild_id, user_id, now)


class ShopAndHorseSeizureStep(ISeizureValueStep):
    """ショップの持ち物・装備・持ち馬を没収して競売へ出品し、換金額（購入額の SEIZE_RATE）を返す。"""

    def __init__(self, shop: IShopBelongings, auction: AuctionRepo, horses: HorseRepo) -> None:
        self._shop = shop
        self._auction = auction
        self._horses = horses

    def seize(self, guild_id: int, user_id: int, now: float) -> int:
        value = 0
        for item_id, qty in self._shop.seize_belongings_for(guild_id, user_id).items:
            item = shop_mod.BY_ID.get(item_id)
            value += (item.price if item else 0) * qty
            self._auction.auction_list_locked_for(guild_id, item_id, qty, now)
        value += self._horses.seize_horses_for(guild_id, user_id, now)
        return int(value * teiai.SEIZE_RATE)


# ------------------------------------------------------------------ #
# DebtRepo                                                            #
# ------------------------------------------------------------------ #
class DebtRepo(ScopedRepo, IDebtRepo):
    """借金・強制執行・種銭・身分・自己破産。"""

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        notices: Optional[INoticeSink] = None,
        bank: Optional[IBankCdsBridge] = None,
        loans: Optional[IOverdueLoanCollector] = None,
        *,
        seizure_step: Optional[IAssetSeizureStep] = None,
        liquidation_step: Optional[ILiquidationStep] = None,
        seizure_value_step: Optional[ISeizureValueStep] = None,
        bankrupt_prep_hook: Optional[IBankruptPrepHook] = None,
        guild_id: int = 0,
        strict: bool = False,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._notices: INoticeSink = notices or NoOpNoticeSink()
        self._bank: IBankCdsBridge = bank or NoOpBankCdsBridge()
        self._loans: IOverdueLoanCollector = loans or NoOpOverdueLoanCollector()
        self._seizure_step: IAssetSeizureStep = seizure_step or NoOpAssetSeizureStep()
        self._liquidation_step: ILiquidationStep = liquidation_step or NoOpLiquidationStep()
        self._seizure_value_step: ISeizureValueStep = seizure_value_step or NoOpSeizureValueStep()
        self._bankrupt_prep_hook: IBankruptPrepHook = bankrupt_prep_hook or NoOpBankruptPrepHook()

    # ------------------------------------------------------------------ #
    # 身分・読み取り契約（IStatusReader）                                 #
    # ------------------------------------------------------------------ #
    def status_for(self, guild_id: int, user_id: int, now: float) -> str:
        """身分。読み取り専用で、口座は作らない。"""
        with self._lock:
            row = self._one(
                "SELECT debt, bankrupt_at, blacklist_until FROM users WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            )
            if row is None:
                return teiai.KATAGI
            if row["bankrupt_at"] is not None and row["debt"] > 0:
                return teiai.BANKRUPT
            if row["debt"] > 0 or self._loans.has_overdue_for(guild_id, user_id):
                return teiai.UNDERGROUND
            return teiai.KATAGI

    def status(self, user_id: int, now: Optional[float] = None) -> str:
        return self.status_for(self._g(), user_id, self._now() if now is None else now)

    def loan_blacklisted_for(self, guild_id: int, user_id: int, now: float) -> bool:
        """破産のブラックリストの間か（貸付を借りられない）。"""
        with self._lock:
            row = self._one(
                "SELECT debt, bankrupt_at, blacklist_until FROM users WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            )
        if row is None or row["bankrupt_at"] is None:
            return False
        return row["debt"] > 0 or row["blacklist_until"] is None or now < row["blacklist_until"]

    def loan_blacklisted(self, user_id: int, now: Optional[float] = None) -> bool:
        return self.loan_blacklisted_for(self._g(), user_id, self._now() if now is None else now)

    def debt_for(self, guild_id: int, user_id: int) -> int:
        return self._account.debt_readonly(guild_id, user_id)

    def debt(self, user_id: int) -> int:
        """借金の残り。口座が無ければ 0（口座は作らない）。"""
        return self._account.debt_readonly(self._g(), user_id)

    # ------------------------------------------------------------------ #
    # 利息・後始末・天引き（IDebtLifecycleHook / IDebtRepayHook）        #
    # ------------------------------------------------------------------ #
    def accrue_debt_interest_for(self, guild_id: int, user_id: int, now: float) -> int:
        """帝愛の利息（トイチ）を、借金に加える。加えた額を返す。呼び出し側のトランザクションの中で使う。"""
        row = self._one("SELECT debt, debt_interest_at FROM users WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        if row is None:
            return 0
        debt, since = row["debt"], row["debt_interest_at"]
        if debt <= 0:
            if since:
                self._conn.execute(
                    "UPDATE users SET debt_interest_at=0 WHERE guild_id=? AND user_id=?", (guild_id, user_id)
                )
            return 0
        if not since:
            self._conn.execute(
                "UPDATE users SET debt_interest_at=? WHERE guild_id=? AND user_id=?", (now, guild_id, user_id)
            )
            return 0
        days = int(max(0.0, now - since) // 86400)
        if days <= 0:
            return 0
        counted = min(days, teiai.DEBT_INTEREST_MAX_DAYS)
        add = max(0, min(int(debt * teiai.DEBT_RATE_PER_DAY * counted), teiai.DEBT_INTEREST_CEILING - debt))
        new_since = now if days > counted else since + days * 86400.0
        self._conn.execute(
            "UPDATE users SET debt=debt+?, debt_interest_at=? WHERE guild_id=? AND user_id=?",
            (add, new_since, guild_id, user_id),
        )
        if add > 0:
            balance = self._account.raw_balance(guild_id, user_id)
            self._account.ledger(
                guild_id,
                user_id,
                0,
                balance,
                f"帝愛の利息（トイチ・日利{teiai.DEBT_RATE_PER_DAY:.0%}・{counted}日分）+{add:,}",
            )
            self._notices.note_for(guild_id, user_id, "debt_interest", add=add, rate=teiai.DEBT_RATE_PER_DAY)
        return add

    def accrue_interest(self, guild_id: int, user_id: int, now: Optional[float] = None) -> int:
        return self.accrue_debt_interest_for(guild_id, user_id, self._now() if now is None else now)

    def after_debt_change(self, guild_id: int, user_id: int) -> None:
        """借金が増減したあとの後始末。破産猶予の出入りを直し、破産者が完済したら、そこから BLACKLIST_DAYS 日が始まる。"""
        self._bank.grace_update_for(guild_id, user_id, self._now())
        if self._account.debt(guild_id, user_id) > 0:
            return
        self._conn.execute(
            "UPDATE users SET blacklist_until=? WHERE guild_id=? AND user_id=? AND bankrupt_at IS NOT NULL "
            "AND blacklist_until IS NULL",
            (self._now() + teiai.BLACKLIST_DAYS * 86400, guild_id, user_id),
        )

    def withhold_income(self, guild_id: int, user_id: int, income: int) -> int:
        """収入 income の REPAY_RATE を、借金の返済へ天引きする。先に帝愛、残りを延滞中の貸付へ。天引きした合計を返す。"""
        amount = teiai.withhold(income)
        if amount <= 0:
            return 0
        pay = min(self._account.debt(guild_id, user_id), amount, self._account.raw_balance(guild_id, user_id))
        if pay > 0:
            self._conn.execute(
                "UPDATE users SET balance=balance-?, debt=debt-? WHERE guild_id=? AND user_id=?",
                (pay, pay, guild_id, user_id),
            )
            self._account.ledger(
                guild_id, user_id, -pay, self._account.raw_balance(guild_id, user_id), "帝愛への返済（収入から天引き）"
            )
            self.after_debt_change(guild_id, user_id)
        return pay + self._loans.collect_overdue_for(guild_id, user_id, amount - pay)

    def repay_all_of_for(self, guild_id: int, user_id: int, amount: int, label: str) -> int:
        """入ってきた amount（元本など、収入ではないお金）を、借金があれば全額、返済に充てる。返済した合計を返す。"""
        pay = min(self._account.debt(guild_id, user_id), amount, self._account.raw_balance(guild_id, user_id))
        if pay > 0:
            self._conn.execute(
                "UPDATE users SET balance=balance-?, debt=debt-? WHERE guild_id=? AND user_id=?",
                (pay, pay, guild_id, user_id),
            )
            self._account.ledger(guild_id, user_id, -pay, self._account.raw_balance(guild_id, user_id), label)
            self.after_debt_change(guild_id, user_id)
        return pay + self._loans.collect_overdue_for(
            guild_id, user_id, min(amount - pay, self._account.raw_balance(guild_id, user_id))
        )

    # ------------------------------------------------------------------ #
    # 強制執行と、借金の発生（IDebtOps）                                  #
    # ------------------------------------------------------------------ #
    def _seize_assets_for(self, guild_id: int, user_id: int, need: int, reason: str) -> int:
        now = self._now()
        covered = _take_cash(self._account, guild_id, user_id, need, reason)
        if covered >= need:
            return covered
        return covered + self._seizure_step.seize(guild_id, user_id, need - covered, reason, now)

    def seed_for(self, guild_id: int, user_id: int, amount: int) -> None:
        """帝愛が種銭を貸し付ける。手元に入り、同じ額が借金に加わる（天引きの対象にはならない）。"""
        self._account.apply(guild_id, user_id, amount, "帝愛の種銭貸付")
        self._conn.execute(
            "UPDATE users SET debt = debt + ? WHERE guild_id=? AND user_id=?", (amount, guild_id, user_id)
        )
        self._bank.grace_update_for(guild_id, user_id, self._now())
        self._notices.note_for(guild_id, user_id, "seed_loan", amount=amount)

    def borrow_seed_for(self, guild_id: int, user_id: int) -> tuple[Any, ...]:
        """（いつでも）手元が種銭の枠に満たなければ、枠まで帝愛から借りる。(成功か, 理由か借りた額)。"""
        with self._db.tx():
            self._account.ensure(guild_id, user_id)
            if self.status_for(guild_id, user_id, self._now()) == teiai.BANKRUPT:
                return False, fail("seed_bankrupt_blocked")
            if self._bank.grace_until_for(guild_id, user_id) is not None:
                return False, fail("seed_grace_blocked")
            short = teiai.SEED - self._account.raw_balance(guild_id, user_id)
            if short <= 0:
                return False, fail("seed_not_needed", seed=teiai.SEED)
            self.seed_for(guild_id, user_id, short)
            return True, short

    def borrow_seed(self, user_id: int) -> tuple[Any, ...]:
        return self.borrow_seed_for(self._g(), user_id)

    def can_borrow_for_bet_for(self, guild_id: int, user_id: int, need: int) -> tuple[Any, ...]:
        """手元が足りない need を、帝愛の信用で借りて賭けられるか。(可否, Failure | "")。"""
        with self._lock:
            if need <= 0:
                return True, ""
            if self.status_for(guild_id, user_id, self._now()) == teiai.BANKRUPT:
                return False, fail("credit_bankrupt_blocked")
            if self._bank.grace_until_for(guild_id, user_id) is not None:
                return False, fail("credit_grace_blocked")
            if need > teiai.SEED:
                return False, fail("credit_limit_exceeded", seed=teiai.SEED, need=need)
            return True, ""

    def can_borrow_for_bet(self, user_id: int, need: int) -> tuple[Any, ...]:
        return self.can_borrow_for_bet_for(self._g(), user_id, need)

    def _seed_if_empty_for(self, guild_id: int, user_id: int) -> int:
        if self._account.debt(guild_id, user_id) > 0 and self._account.raw_balance(guild_id, user_id) == 0:
            self.seed_for(guild_id, user_id, teiai.SEED)
            return teiai.SEED
        return 0

    def topup_seed_for(self, guild_id: int, user_id: int) -> int:
        short = teiai.SEED - self._account.raw_balance(guild_id, user_id)
        if self._account.debt(guild_id, user_id) > 0 and short > 0:
            self.seed_for(guild_id, user_id, short)
            return short
        return 0

    def owe_for(self, guild_id: int, user_id: int, shortfall: int, reason: str) -> int:
        """払えなかった shortfall を、強制執行したうえで、残りを帝愛の借金にする。借金に加えた額を返す。"""
        if shortfall <= 0:
            return 0
        need = shortfall - self._seize_assets_for(guild_id, user_id, shortfall, f"強制執行:{reason}")
        if need <= 0:
            return 0
        need -= self._bank.cds_cover_self_for(guild_id, user_id, need, self._now())
        if need <= 0:
            return 0
        self._conn.execute("UPDATE users SET debt = debt + ? WHERE guild_id=? AND user_id=?", (need, guild_id, user_id))
        self._account.ledger(
            guild_id, user_id, 0, self._account.raw_balance(guild_id, user_id), f"借金:{reason}（{need:,}ペリカ）"
        )
        self._seed_if_empty_for(guild_id, user_id)
        self._bank.grace_update_for(guild_id, user_id, self._now())
        return need

    # ------------------------------------------------------------------ #
    # 返済（自分から）                                                    #
    # ------------------------------------------------------------------ #
    def repay_debt(self, user_id: int, amount: Optional[int] = None) -> tuple[Any, ...]:
        """手元の現金から、帝愛の借金を返す（amount が None なら、返せる限り全額）。(成功か, 理由か返した額)。"""
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            debt, balance = self._account.debt(gid, user_id), self._account.raw_balance(gid, user_id)
            if debt <= 0:
                return False, fail("repay_no_debt")
            pay = min(debt, balance) if amount is None else amount
            if pay <= 0:
                return False, fail("repay_no_cash", balance=balance)
            if pay > debt:
                return False, fail("repay_exceeds_debt", debt=debt)
            if pay > balance:
                return False, fail("insufficient_balance", balance=balance)
            self._conn.execute(
                "UPDATE users SET balance=balance-?, debt=debt-? WHERE guild_id=? AND user_id=?",
                (pay, pay, gid, user_id),
            )
            self._account.ledger(gid, user_id, -pay, self._account.raw_balance(gid, user_id), "帝愛への返済")
            self.after_debt_change(gid, user_id)
            return True, pay

    # ------------------------------------------------------------------ #
    # 自己破産                                                            #
    # ------------------------------------------------------------------ #
    def _liquidate_everything_for(self, guild_id: int, user_id: int, now: float) -> int:
        self._liquidation_step.liquidate(guild_id, user_id, now)
        return _take_cash(
            self._account,
            guild_id,
            user_id,
            self._account.raw_balance(guild_id, user_id),
            "破産:現金の差し押さえ",
        )

    def _seize_valuables_for(self, guild_id: int, user_id: int, now: float) -> int:
        return self._seizure_value_step.seize(guild_id, user_id, now)

    def escape_left(self, user_id: int) -> int:
        """海外逃亡を、あと何回試せるか。読み取り専用。"""
        with self._lock:
            v = self._scalar("SELECT escape_tries FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id))
        return max(0, teiai.ESCAPE_MAX_TRIES - (v if v is not None else 0))

    def escape_attempt(self, user_id: int, now: float, rng: Optional[random.Random] = None) -> tuple[Any, ...]:
        """海外逃亡。(成功か, 理由か結果の辞書) を返す。"""
        rng = rng or cast("random.Random", random)
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            if self.status_for(gid, user_id, now) == teiai.BANKRUPT:
                return False, fail("escape_bankrupt_blocked")
            debt = self._account.debt(gid, user_id)
            if debt < teiai.ESCAPE_MIN_DEBT:
                return False, fail("escape_min_debt", min_debt=teiai.ESCAPE_MIN_DEBT)
            row = self._one("SELECT escape_at, escape_tries FROM users WHERE guild_id=? AND user_id=?", (gid, user_id))
            assert row is not None  # 直前に ensure 済み
            if row["escape_tries"] >= teiai.ESCAPE_MAX_TRIES:
                return False, fail("escape_max_tries", max_tries=teiai.ESCAPE_MAX_TRIES)
            ready = (row["escape_at"] or 0) + teiai.ESCAPE_COOLDOWN_HOURS * 3600
            if now < ready:
                return False, fail("escape_cooldown", ready=ready)
            self._conn.execute(
                "UPDATE users SET escape_at=?, escape_tries=escape_tries+1 WHERE guild_id=? AND user_id=?",
                (now, gid, user_id),
            )
            if rng.random() < teiai.ESCAPE_CHANCE:
                ok, result = self.bankrupt_for(gid, user_id, escape=True)
                if not ok:
                    return False, result
                return True, {
                    "escaped": True,
                    "fine": 0,
                    "debt": self._account.debt(gid, user_id),
                    "bankrupt": result,
                }
            fine = max(teiai.ESCAPE_FINE_MIN, int(debt * teiai.ESCAPE_FINE_RATE))
            self._conn.execute("UPDATE users SET debt = debt + ? WHERE guild_id=? AND user_id=?", (fine, gid, user_id))
            self._account.ledger(
                gid,
                user_id,
                0,
                self._account.raw_balance(gid, user_id),
                f"海外逃亡に失敗（罰金 {fine:,}ペリカが借金に加わる）",
            )
            self.after_debt_change(gid, user_id)
            return True, {"escaped": False, "fine": fine, "debt": self._account.debt(gid, user_id), "bankrupt": None}

    def bankrupt_for(self, guild_id: int, user_id: int, forced: bool = False, escape: bool = False) -> tuple[Any, ...]:
        """自己破産。全財産とショップの全品を差し押さえて換金し、帝愛 → 貸付の順に配当する。"""
        with self._db.tx():
            self._account.ensure(guild_id, user_id)
            if self.status_for(guild_id, user_id, self._now()) == teiai.BANKRUPT:
                return False, fail("bankrupt_already")
            loans = self._loans.active_loans_for(guild_id, user_id)
            if self._account.debt(guild_id, user_id) <= 0 and not loans:
                return False, fail("bankrupt_no_debt")
            debt_before = self._account.debt(guild_id, user_id)
            now = self._now()
            self._bankrupt_prep_hook.prepare(guild_id, user_id, now)
            in_grace = self._bank.grace_until_for(guild_id, user_id) is not None
            pool = self._liquidate_everything_for(guild_id, user_id, now)
            shop_value = self._seize_valuables_for(guild_id, user_id, now)
            pool += shop_value
            total_pool = pool
            to_senior = min(pool, self._account.debt(guild_id, user_id))
            self._conn.execute(
                "UPDATE users SET debt=debt-? WHERE guild_id=? AND user_id=?", (to_senior, guild_id, user_id)
            )
            pool -= to_senior
            junior_paid, forgiven = 0, 0
            for loan in loans:
                pay = min(pool, self._loans.loan_remaining(loan, now))
                if pay > 0:
                    self._account.ensure(guild_id, loan["lender_id"])
                    self._account.apply(
                        guild_id,
                        loan["lender_id"],
                        pay,
                        f"貸付の配当（借り手の自己破産）←{user_id}（#{loan['id']}）",
                    )
                    loan["paid"] += pay
                    self._loans.record_bankrupt_payment_for(loan["id"], loan["paid"])
                    pool -= pay
                    junior_paid += pay
                left = self._loans.loan_remaining(loan, now)
                forgiven += left
                self._loans.finalize_bankrupt_loan_for(loan["id"], left <= 0)
            if pool > 0:
                self._account.apply(guild_id, user_id, pool, "破産の整理のあまり")
            compressed = max(0, self._account.debt(guild_id, user_id) - (0 if escape else teiai.BANKRUPT_FLOOR))
            self._conn.execute(
                "UPDATE users SET debt=debt-?, bankrupt_at=?, blacklist_until=NULL WHERE guild_id=? AND user_id=?",
                (compressed, now, guild_id, user_id),
            )
            self.after_debt_change(guild_id, user_id)
            seed = self._seed_if_empty_for(guild_id, user_id)
            self._conn.execute("UPDATE users SET grace_until=NULL WHERE guild_id=? AND user_id=?", (guild_id, user_id))
            payable = forced or in_grace
            cds_paid = self._bank.cds_settle_default_for(guild_id, user_id, debt_before, now) if payable else 0
            if not payable:
                self._bank.cds_void_for(guild_id, user_id)
            return True, {
                "forced": forced,
                "cds_paid": cds_paid,
                "seized": total_pool,
                "shop_value": shop_value,
                "senior_paid": to_senior,
                "junior_paid": junior_paid,
                "forgiven": forgiven,
                "compressed": compressed,
                "debt": self._account.debt(guild_id, user_id),
                "seed": seed,
            }

    def bankrupt(self, user_id: int, forced: bool = False, escape: bool = False) -> tuple[Any, ...]:
        return self.bankrupt_for(self._g(), user_id, forced=forced, escape=escape)
