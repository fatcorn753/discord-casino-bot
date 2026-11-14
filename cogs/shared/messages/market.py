"""証券・信用取引・オプション・債券領域の失敗メッセージ定義。"""

from typing import Any, Callable

from domain import market as market_mod

FailureFormatter = str | Callable[[dict[str, Any]], str]


def _pin_limit_down(p: dict[str, Any]) -> str:
    name = market_mod.BY_KEY[p["symbol"]].name
    act = market_mod.PIN_ACTION_TEXT[p["action"]]
    return (
        f"🚨 {name} は**ストップ安に張り付いています**。{act}は、買い手がいなくて約定しません"
        "（下げが止まるまで、待つしかありません）。"
    )


def _pin_limit_up(p: dict[str, Any]) -> str:
    name = market_mod.BY_KEY[p["symbol"]].name
    act = market_mod.PIN_ACTION_TEXT[p["action"]]
    return (
        f"🚀 {name} は**ストップ高に張り付いています**。{act}は、売り手がいなくて約定しません"
        "（上げが止まるまで、待つしかありません）。"
    )


MARKET_FAILURES: dict[str, FailureFormatter] = {
    "market_pin_limit_down": _pin_limit_down,
    "market_pin_limit_up": _pin_limit_up,
    "crypto_invalid_amount": "金額が正しくありません。",
    "crypto_insufficient_balance": lambda p: f"ペリカが足りません（残高 {p['balance']:,}ペリカ）。",
    "crypto_amount_too_small": "その金額では 0.001枚も買えません。金額を増やしてください。",
    "crypto_collateral_locked": lambda p: (
        f"信用取引の担保になっているので、売れません（売ると維持率が {p['ratio']:.0%}・{p['open_ratio']:.0%} 以上が必要）。"
        "先に建玉を決済してください。"
    ),
    "crypto_not_held": lambda p: f"{p['name']}を持っていません。",
    "crypto_sell_range": lambda p: f"売れるのは 0.001〜{p['max_lots']} です（持っている分）。",
    "crypto_proceeds_too_small": "その数量では受け取れる額が 1ペリカ未満です。もう少し多く売ってください。",
    "margin_debt_blocks": lambda p: f"借金が {p['debt']:,}ペリカ 残っているので、{p['label']}はできません。",
    "margin_opposite_open": lambda p: f"{p['name']}の{p['other_label']}を建てています。先に決済してください。",
    "margin_min_collateral": lambda p: f"担保が {p['min_margin']}ペリカ 未満になる量です。枚数を増やしてください。",
    "margin_insufficient_collateral": lambda p: (
        f"担保が足りません（{p['need']:,}ペリカ必要・残高 {p['balance']:,}ペリカ）。"
    ),
    "margin_ratio_too_low": lambda p: (
        f"口座の維持率が足りなくなります（建てると {p['ratio']:.0%}・{p['open_ratio']:.0%} 以上が必要）。"
        "枚数を減らすか、レバレッジを下げるか、現物や債券を担保に持ってください。"
    ),
    "margin_not_open": lambda p: f"{p['label']}を建てていません。",
    "margin_close_range": lambda p: f"決済できるのは 0.001〜{p['max_lots']} です（建てている分）。",
    "market_no_price": "まだ価格がありません。",
    "option_pair_same_symbol": "ペア・ワラントは、ちがう2つの銘柄を選んでください。",
    "option_target_pct_range": lambda p: f"目標割合は、1〜{p['max_pct']}% にしてください。",
    "option_min_premium": lambda p: f"掛け金は {p['min_premium']:,}ペリカ 以上からです。",
    "option_min_margin": lambda p: f"証拠金は {p['min_premium']:,}ペリカ 以上からです。",
    "option_buy_too_small": "その掛け金では 1口も買えません。掛け金を増やしてください。",
    "option_write_too_small": "その証拠金では 1口も売れません。証拠金を増やしてください。",
    "option_insufficient_balance": lambda p: (
        f"残高が足りません（{p['cost']:,}ペリカ必要・残高 {p['balance']:,}ペリカ）。"
    ),
    "option_max_open_premium": lambda p: (
        f"持てるオプションの掛け金は、合計 {p['max_open']:,}ペリカ までです（あと {p['left']:,}ペリカ）。"
    ),
    "option_max_short_margin": lambda p: (
        f"預けられる裸売りの証拠金は、合計 {p['max_margin']:,}ペリカ までです（あと {p['left']:,}ペリカ）。"
    ),
    "option_bank_no_cash": "いま帝愛銀行の手元の現金が足りず、プレミアムを払えません（準備率が低いとき）。",
    "option_not_found": "その番号のオプションは持っていません。",
    "bond_invalid_kind": "債券の種類が正しくありません。",
    "bond_min_amount": lambda p: f"{p['min_amount']:,}ペリカ以上で買ってください。",
    "bond_max_total": lambda p: (
        f"{p['name']}は元本の合計が {p['max_total']:,}ペリカ まで（いま {p['held']:,}ペリカ 保有）。"
        f"あと {p['room']:,}ペリカ まで買えます。"
    ),
    "bond_not_found": "その番号の債券は持っていません。",
    "bond_not_sellable": lambda p: f"{p['name']}は売れません（満期まで持つだけです）。",
}
