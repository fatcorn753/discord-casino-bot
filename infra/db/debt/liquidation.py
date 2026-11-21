"""自己破産時の換金ステップ群（OCP）。

各ステップは注入された依存を束ねるファクトリ関数が `LiquidationStep`（Callable）を返す。
"""

from domain import teiai
from domain.ports.bridges import (
    IBondHoldings,
    IOptionHoldings,
    LiquidationStep,
    MarketPriceReader,
)
from domain.ports.repos import IKyosaiRepo
from infra.db.account import AccountCore
from infra.db.bank import BankRepo
from infra.db.crypto import CryptoRepo
from infra.db.margin import MarginRepo


def margin_liquidation_step(market: MarketPriceReader, margin: MarginRepo) -> LiquidationStep:
    def liquidate(guild_id: int, user_id: int, now: float) -> None:
        prices = market()
        for side, symbol, *_ in margin.margin_positions_for(guild_id, user_id):
            if symbol in prices:
                margin.margin_close_locked_for(
                    guild_id, user_id, side, None, prices[symbol], f"破産:{margin.SIDE_NAME[side]}決済", symbol
                )

    return liquidate


def crypto_liquidation_step(market: MarketPriceReader, crypto: CryptoRepo) -> LiquidationStep:
    def liquidate(guild_id: int, user_id: int, now: float) -> None:
        prices = market()
        for symbol, _, _ in crypto.holdings_for(guild_id, user_id):
            if symbol in prices:
                crypto.crypto_sell_locked_for(guild_id, user_id, None, prices[symbol], symbol)

    return liquidate


def bond_liquidation_step(account: AccountCore, bonds: IBondHoldings) -> LiquidationStep:
    def liquidate(guild_id: int, user_id: int, now: float) -> None:
        for bond in bonds.active_bonds_for(guild_id, user_id):
            ok, _ = bonds.bond_cancel_locked_for(guild_id, user_id, bond.id, now)
            if not ok:  # 解約できない債券は、元本の一部で換金して、差し押さえる
                payout = int(bond.principal * teiai.SEIZE_RATE)
                bonds.bond_write_off_for(guild_id, user_id, bond.id, payout, now)

    return liquidate


def option_liquidation_step(options: IOptionHoldings) -> LiquidationStep:
    def liquidate(guild_id: int, user_id: int, now: float) -> None:
        for option_id in options.active_option_ids_for(guild_id, user_id):
            options.option_close_locked_for(guild_id, user_id, option_id, now)

    return liquidate


def deposit_liquidation_step(account: AccountCore, bank: BankRepo) -> LiquidationStep:
    def liquidate(guild_id: int, user_id: int, now: float) -> None:
        saved = bank.deposit_take_for(guild_id, user_id, bank.deposit_of_for(guild_id, user_id), now)
        if saved:
            account.apply(guild_id, user_id, saved, "破産:預金の差し押さえ")

    return liquidate


def kyosai_liquidation_step(kyosai: IKyosaiRepo) -> LiquidationStep:
    """自己破産で、帝愛共済の口を帝愛の持ち口へ回収する（#34。値段はつけず、破産財団には入れない）。"""

    def liquidate(guild_id: int, user_id: int, now: float) -> None:
        kyosai.recover_units_for(guild_id, user_id, now)

    return liquidate
