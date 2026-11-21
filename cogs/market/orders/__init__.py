"""`/証券` の注文・決済フロー（cogs/market/cog.py から使う）。

現物・信用・オプション・債券の売買実行と確認ダイアログの進行を、取引種別ごとのサブモジュールに分け、
ここから従来どおりの名前で再エクスポートする（読み込まれる Cog ではない）。
"""

from .bond import bond_buy, bond_cancel
from .common import (
    CONFIRM_FROM,
    MAX_MARGIN_NOTIONAL,
    RNG,
    default_confirmed_reply,
    margin_quote,
    run_trade,
    units_from,
    worth,
)
from .margin import margin_close, margin_order
from .option import option_buy, option_sell, option_write
from .spot import buy_spot, sell_spot

__all__ = [
    "CONFIRM_FROM",
    "MAX_MARGIN_NOTIONAL",
    "RNG",
    "bond_buy",
    "bond_cancel",
    "buy_spot",
    "default_confirmed_reply",
    "margin_close",
    "margin_order",
    "margin_quote",
    "option_buy",
    "option_sell",
    "option_write",
    "run_trade",
    "sell_spot",
    "units_from",
    "worth",
]
