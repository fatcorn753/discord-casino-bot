"""強制執行（ファーストロス差し押さえ）ステップ群（OCP）。"""

from domain import market as market_mod
from domain.ports.bridges import (
    IAssetSeizureStep,
    IBondHoldings,
    IMarketPriceReader,
    INoticeSink,
    IOptionHoldings,
)
from infra.db.account import AccountCore
from infra.db.bank import BankRepo
from infra.db.crypto import CryptoRepo
from infra.db.margin import MarginRepo

from .base import _take_cash


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
            if ok and isinstance(got, int) and got > 0:
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
