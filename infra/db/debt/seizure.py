"""強制執行（ファーストロス差し押さえ）ステップ群（OCP）。

各ステップは注入された依存を束ねるファクトリ関数が `AssetSeizureStep`（Callable）を返す。
種類を増やすときは既存を修正せず、`compose_asset_seizure_steps` の束ねに追加するだけでよい。
"""

from domain import market as market_mod
from domain.ports.bridges import (
    AssetSeizureStep,
    IBondHoldings,
    INoticeSink,
    IOptionHoldings,
    MarketPriceReader,
)
from infra.db.account import AccountCore
from infra.db.bank import BankRepo
from infra.db.crypto import CryptoRepo
from infra.db.margin import MarginRepo

from .base import _take_cash


def deposit_seizure_step(account: AccountCore, bank: BankRepo, notices: INoticeSink) -> AssetSeizureStep:
    def seize(guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
        taken = bank.deposit_take_for(guild_id, user_id, need, now)
        if taken:
            account.ledger(
                guild_id,
                user_id,
                0,
                account.raw_balance(guild_id, user_id),
                f"強制執行:預金の差し押さえ（{taken:,}ペリカ）",
            )
            notices.note_for(guild_id, user_id, "seize_deposit", taken=taken)
        return taken

    return seize


def margin_seizure_step(
    account: AccountCore, market: MarketPriceReader, margin: MarginRepo, notices: INoticeSink
) -> AssetSeizureStep:
    def seize(guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
        covered = 0
        prices = market()
        for side, symbol, *_ in margin.margin_positions_for(guild_id, user_id):
            if covered >= need or symbol not in prices:
                continue
            name = margin.SIDE_NAME[side]
            result = margin.margin_close_locked_for(
                guild_id, user_id, side, None, prices[symbol], f"強制執行:{name}決済", symbol
            )
            notices.note_for(
                guild_id,
                user_id,
                "seize_margin",
                symbol_name=market_mod.BY_KEY[symbol].name,
                side_label=name,
                money=result.money,
            )
            covered += _take_cash(account, guild_id, user_id, need - covered, reason)
        return covered

    return seize


def crypto_seizure_step(
    account: AccountCore, market: MarketPriceReader, crypto: CryptoRepo, notices: INoticeSink
) -> AssetSeizureStep:
    def seize(guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
        covered = 0
        prices = market()
        for symbol, _units, _ in crypto.holdings_for(guild_id, user_id):
            if covered >= need or symbol not in prices:
                continue
            result = crypto.crypto_sell_locked_for(guild_id, user_id, None, prices[symbol], symbol)
            notices.note_for(
                guild_id,
                user_id,
                "seize_spot",
                symbol_name=market_mod.BY_KEY[symbol].name,
                money=result.money,
            )
            covered += _take_cash(account, guild_id, user_id, need - covered, reason)
        return covered

    return seize


def option_seizure_step(account: AccountCore, options: IOptionHoldings, notices: INoticeSink) -> AssetSeizureStep:
    def seize(guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
        covered = 0
        for option_id in options.active_option_ids_for(guild_id, user_id):
            if covered >= need:
                break
            ok, got = options.option_close_locked_for(guild_id, user_id, option_id, now)
            if ok and isinstance(got, int) and got > 0:
                notices.note_for(guild_id, user_id, "seize_option", got=got)
                covered += _take_cash(account, guild_id, user_id, need - covered, reason)
        return covered

    return seize


def bond_seizure_step(account: AccountCore, bonds: IBondHoldings, notices: INoticeSink) -> AssetSeizureStep:
    def seize(guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
        covered = 0
        for bond in bonds.active_bonds_for(guild_id, user_id):
            if covered >= need:
                break
            ok, refund = bonds.bond_cancel_locked_for(guild_id, user_id, bond.id, now)
            if ok:
                notices.note_for(guild_id, user_id, "seize_bond", refund=refund)
                covered += _take_cash(account, guild_id, user_id, need - covered, reason)
        return covered

    return seize
