"""自己破産時の換金ステップ群（OCP）。"""

from domain import teiai
from domain.ports.bridges import (
    IBondHoldings,
    ILiquidationStep,
    IMarketPriceReader,
    IOptionHoldings,
)
from domain.ports.repos import IKyosaiRepo
from infra.db.account import AccountCore
from infra.db.bank import BankRepo
from infra.db.crypto import CryptoRepo
from infra.db.margin import MarginRepo


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
