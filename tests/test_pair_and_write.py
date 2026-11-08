"""ペア・ワラント（Margrabe）と、オプションの裸売り（証拠金・プレミアム即時受取・満期の損失を借金へ）。"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from cogs.market_cog import MarketCog
from core import market, market_events, teiai
from core import options as opt
from core.economy import INITIAL_BALANCE, Economy
from core.session import Sessions

pytestmark = pytest.mark.real_scope

G, U, A = 10, 1, 2
START = {s.key: s.start for s in market.SYMBOLS}
HOURS = 3600


@pytest.fixture
def eco(tmp_path):
    e = Economy(str(tmp_path / "t.db"), strict=True).for_guild(G)
    now = time.time()
    for key, price in START.items():
        e.market_set(now, price, 0, key)
    return e


def fund_bank(eco, amount=50_000):
    """預金者を作り、銀行に、払い出せる現金を用意する。"""
    eco.credit(A, amount, "テスト")
    eco.bank_deposit(A, amount, time.time())


def set_price(eco, key, price, ts):
    eco.market_set(ts, price, 0, key)


# ----------------------------- ペア・ワラントの値付け ----------------------------- #
def test_pair_variance_is_lower_for_strongly_correlated_symbols():
    secs = 6 * HOURS
    tied = opt.pair_variance("cs", "teio", secs)  # 相関 0.85
    loose = opt.pair_variance("cs", "numa", secs)  # ほぼ無相関
    opposite = opt.pair_variance("cs", "gold", secs)  # 逆相関
    assert tied < loose and tied < opposite


def test_pair_variance_is_symmetric_and_zero_for_the_same_symbol():
    assert opt.pair_variance("cs", "teio", 6 * HOURS) == pytest.approx(opt.pair_variance("teio", "cs", 6 * HOURS))
    assert opt.pair_variance("cs", "cs", 6 * HOURS) == pytest.approx(0.0, abs=1e-12)


def test_pair_rate_matches_the_margrabe_formula_at_parity():
    v = opt.pair_variance("cs", "numa", 6 * HOURS)
    assert opt.pair_fair_rate(1.0, 1.0, v) == pytest.approx(2 * opt.phi(v**0.5 / 2) - 1)


def test_pair_value_moves_with_relative_performance():
    args = ("cs", "teio", 5 * HOURS)
    ahead = opt.pair_bid_rate(*args[:2], 1.05, 1.0, args[2])
    level = opt.pair_bid_rate(*args[:2], 1.0, 1.0, args[2])
    behind = opt.pair_bid_rate(*args[:2], 1.0, 1.05, args[2])
    assert ahead > level > behind >= 0


def test_pair_ask_is_above_bid_and_events_raise_the_price():
    ask = opt.pair_ask_rate("cs", "numa", 6 * HOURS)
    bid = opt.pair_bid_rate("cs", "numa", 1.0, 1.0, 6 * HOURS)
    assert ask / bid == pytest.approx((1 + opt.SPREAD) / (1 - opt.SPREAD))
    now = time.time()
    ev = {
        "symbol": "numa",
        "announce_ts": now,
        "start_ts": now + 900,
        "end_ts": now + 2100,
        "factor": 3.0,
    }
    assert opt.pair_ask_rate("cs", "numa", 6 * HOURS, now, [ev]) > ask


def test_pair_intrinsic():
    assert opt.pair_intrinsic(1_000, 100, 200, 110, 200) == 100  # A +10%、B 0% → 想定元本 × 10%
    assert opt.pair_intrinsic(1_000, 100, 200, 105, 220) == 0  # B のほうが上がった


# ----------------------------- ペア・ワラントの売買 ----------------------------- #
def test_pair_buy_settle_pays_the_outperformance(eco):
    fund_bank(eco)
    ok, res = eco.option_buy(U, opt.PAIR, "cs", 1_000, symbol2="teio")
    assert ok and res["spot2"] == START["teio"] and 900 < res["cost"] <= 1_000
    set_price(eco, "cs", START["cs"] * 1.10, res["expiry"] - 10)  # A +10%
    set_price(eco, "teio", START["teio"] * 1.02, res["expiry"] - 10)  # B +2%
    done = eco.option_settle(res["expiry"] + 1)
    assert done[0]["status"] == "exercised"
    assert done[0]["paid"] == min(int(res["units"] * (1.10 - 1.02)), res["cost"] * opt.PAYOUT_CAP_MULT)


def test_pair_pays_nothing_when_the_other_symbol_wins_even_if_both_rise(eco):
    fund_bank(eco)
    ok, res = eco.option_buy(U, opt.PAIR, "cs", 1_000, symbol2="teio")
    set_price(eco, "cs", START["cs"] * 1.05, res["expiry"] - 10)
    set_price(eco, "teio", START["teio"] * 1.20, res["expiry"] - 10)
    assert eco.option_settle(res["expiry"] + 1)[0]["status"] == "expired"


def test_pair_pays_even_when_the_whole_market_falls(eco):
    fund_bank(eco)
    ok, res = eco.option_buy(U, opt.PAIR, "cs", 1_000, symbol2="numa")
    set_price(eco, "cs", START["cs"] * 0.95, res["expiry"] - 10)  # 市場全体が下がっても…
    set_price(eco, "numa", START["numa"] * 0.80, res["expiry"] - 10)  # A が B よりましなら当たり
    assert eco.option_settle(res["expiry"] + 1)[0]["paid"] > 0


def test_pair_buy_rules(eco):
    assert not eco.option_buy(U, opt.PAIR, "cs", 1_000)[0]  # 相手がない
    assert not eco.option_buy(U, opt.PAIR, "cs", 1_000, symbol2="cs")[0]  # 同じ銘柄
    assert not eco.option_buy(U, opt.PAIR, "cs", 1_000, symbol2="nope")[0]


def test_pair_can_be_sold_before_expiry_and_gains_when_a_pulls_ahead(eco):
    fund_bank(eco)
    ok, res = eco.option_buy(U, opt.PAIR, "cs", 1_000, symbol2="teio")
    row = eco.option_list(U)["active"][0]
    now = time.time()
    base = eco.option_value(row, now, {"cs": START["cs"], "teio": START["teio"]})
    ahead = eco.option_value(row, now, {"cs": START["cs"] * 1.03, "teio": START["teio"]})
    assert ahead > base
    ok, got = eco.option_sell(U, res["id"])
    assert ok and 0 < got < res["cost"]


# ----------------------------- 裸売り ----------------------------- #
def test_write_gives_the_premium_now_and_locks_the_margin(eco):
    fund_bank(eco)
    before = eco.balance(U)
    ok, res = eco.option_write(U, opt.CALL, "cs", 1_000)
    assert ok and res["strike"] == START["cs"] * 1.05
    assert res["margin"] <= 1_000 and res["premium"] > 0
    assert eco.balance(U) == before - res["margin"] + res["premium"]
    row = eco.option_list(U)["active"][0]
    assert row["side"] == "short" and row["margin"] == res["margin"]


def test_write_needs_bank_cash_for_the_premium(eco):
    ok, why = eco.option_write(U, opt.CALL, "numa", 3_000)  # 銀行に現金がない
    assert not ok and "帝愛銀行" in why and eco.balance(U) == INITIAL_BALANCE


def test_write_rules(eco):
    fund_bank(eco)
    assert not eco.option_write(U, "pair", "cs", 1_000)[0]  # 裸売りは、コールとプットだけ
    assert not eco.option_write(U, opt.CALL, "cs", 50)[0]  # 最低額
    assert not eco.option_write(U, opt.CALL, "cs", INITIAL_BALANCE + 1)[0]  # 残高不足
    with eco._lock, eco._conn:
        eco._conn.execute("UPDATE users SET debt=100 WHERE user_id=?", (U,))
    assert "借金" in eco.option_write(U, opt.CALL, "cs", 1_000)[1]


def test_write_margin_cap(eco):
    fund_bank(eco, 200_000)
    eco.credit(U, 100_000, "テスト")
    assert eco.option_write(U, opt.PUT, "cs", 15_000)[0]
    ok, why = eco.option_write(U, opt.PUT, "cs", 10_000)
    assert not ok and f"{opt.MAX_SHORT_MARGIN:,}" in why


def test_write_expires_worthless_the_premium_is_the_profit(eco):
    fund_bank(eco)
    before = eco.balance(U)
    ok, res = eco.option_write(U, opt.CALL, "cs", 1_000)
    set_price(eco, "cs", START["cs"], res["expiry"] - 10)  # 動かなかった
    done = eco.option_settle(res["expiry"] + 1)
    assert done[0]["status"] == "expired" and done[0]["paid"] == res["margin"]
    assert eco.balance(U) == before + res["premium"]  # プレミアムが丸ごと利益


def test_write_loss_within_the_margin_is_paid_from_it(eco):
    fund_bank(eco)
    ok, res = eco.option_write(U, opt.CALL, "cs", 2_000)
    price = res["strike"] * 1.05  # 行使価格を 5% 超えた: 損失 = 差 × 口数
    set_price(eco, "cs", price, res["expiry"] - 10)
    loss = opt.intrinsic("call", res["units"], price, res["strike"])
    assert 0 < loss < res["margin"]
    done = eco.option_settle(res["expiry"] + 1)
    assert done[0]["status"] == "exercised" and done[0]["paid"] == res["margin"] - loss and eco.debt(U) == 0


def test_write_loss_beyond_the_margin_becomes_debt(eco):
    fund_bank(eco)
    eco.credit(U, 10_000, "テスト")
    ok, res = eco.option_write(U, opt.CALL, "cs", 3_000)
    eco.debit_up_to(U, eco.balance(U), "テスト")  # 手元の現金をなくす（差し押さえるものがない）
    set_price(eco, "cs", res["strike"] * 3, res["expiry"] - 10)  # 大暴騰
    loss = opt.intrinsic("call", res["units"], res["strike"] * 3, res["strike"])
    done = eco.option_settle(res["expiry"] + 1)
    assert done[0]["paid"] == 0 and loss > res["margin"]
    assert eco.debt(U) >= loss - res["margin"] and eco.status(U) != teiai.KATAGI  # 借金（地下行き）


def test_the_loss_flows_to_the_bank(eco):
    fund_bank(eco)
    ok, res = eco.option_write(U, opt.CALL, "cs", 2_000)
    before = eco.bank_status(res["expiry"])["cash"]
    set_price(eco, "cs", res["strike"] * 1.05, res["expiry"] - 10)
    loss = opt.intrinsic("call", res["units"], res["strike"] * 1.05, res["strike"])
    eco.option_settle(res["expiry"] + 1)
    assert eco.bank_status(res["expiry"])["cash"] >= before + loss - 1  # 損失が、銀行の収入になる


def test_write_can_be_bought_back_at_the_ask(eco):
    fund_bank(eco)
    before = eco.balance(U)
    ok, res = eco.option_write(U, opt.PUT, "cs", 2_000)
    ok, back = eco.option_sell(U, res["id"])  # すぐ買い戻す
    assert ok and 0 < back <= res["margin"]
    net = eco.balance(U) - before
    assert net < 0  # 売値と買値の差（手数料）の分だけ、すぐ戻すと損
    assert eco.option_list(U)["done"][0]["status"] == "sold"


def test_write_counts_margin_minus_liability_in_the_ranking_and_is_seized(eco):
    fund_bank(eco)
    ok, res = eco.option_write(U, opt.CALL, "cs", 2_000)
    total = dict((u, v) for _, u, v in eco.top(5))[U]
    assert INITIAL_BALANCE - 600 < total < INITIAL_BALANCE + 600  # 証拠金は資産、買い戻しの費用は負債
    eco.debit_up_to(U, eco.balance(U), "テスト")
    with eco._lock, eco._conn:
        eco._seize_assets(U, 10_000, "テスト")
    assert eco.option_list(U)["active"] == []  # 強制執行で、買い戻して閉じる


def test_long_premium_cap_does_not_count_written_options(eco):
    fund_bank(eco, 200_000)
    eco.credit(U, 100_000, "テスト")
    eco.option_write(U, opt.PUT, "cs", 15_000)
    assert eco.option_buy(U, opt.CALL, "cs", 15_000)[0]  # 買いの上限（掛け金 20,000）は、裸売りの証拠金とは別


# ----------------------------- コマンド ----------------------------- #
def choice(value):
    return SimpleNamespace(value=value)


def new_cog(root):
    """このファイルで読み込んだ MarketCog で作る（他のテストが cogs を読み込み直しても、クラスがずれない）。"""
    cog = MarketCog(SimpleNamespace(economy=root, sessions=Sessions(cooldown=0), config={}))
    cog._prices()
    return cog


@pytest.fixture
def cog(tmp_path):
    root = Economy(str(tmp_path / "c.db"), strict=True)
    root.for_guild(G).credit(U, 30_000, "テスト")
    root.for_guild(G).credit(A, 200_000, "テスト")
    root.for_guild(G).bank_deposit(A, 200_000, time.time())
    return new_cog(root)


def test_order_pair_needs_a_counterpart_and_shows_the_condition(cog):
    from tests.test_market import It

    it = It()
    asyncio.run(MarketCog.order.callback(cog, it, choice("cs"), 2_000, choice("pair"), None, None))
    assert it.sent[-1]["ephemeral"] and "2つの銘柄" in it.sent[-1]["embed"].title
    it = It()
    asyncio.run(MarketCog.order.callback(cog, it, choice("cs"), 2_000, choice("pair"), None, choice("teio")))
    embed = it.sent[-1]["embed"]
    assert "ペア・ワラント" in embed.title and "CSトークン vs TEIO重工" in embed.title
    assert "上昇率" in embed.description and "想定リターン" in embed.description
    assert cog.eco.for_guild(G).option_list(U)["active"][0]["symbol2"] == "teio"


def test_order_naked_sell_asks_for_confirmation_and_creates_the_position(cog, monkeypatch):
    from tests.test_market import It

    asked = []

    async def agree(interaction, needed, title, text, label):
        asked.append((needed, title, text))
        return interaction.response.send_message

    monkeypatch.setattr(MarketCog, "_confirmed_reply", staticmethod(agree))  # 確認ダイアログは、内容だけを記録して通す
    it = It()
    asyncio.run(MarketCog.order.callback(cog, it, choice("cs"), 2_000, choice("scall"), None, None))
    assert (
        asked and asked[0][0] is True and "借金になります" in asked[0][2]
    )  # 裸売りは、必ず確認を挟み、借金になりうることを伝える
    assert "コール裸売り" in it.sent[-1]["embed"].title
    row = cog.eco.for_guild(G).option_list(U)["active"][0]
    assert row["side"] == "short" and row["kind"] == "call"


def test_holdings_and_settle_for_a_naked_sell_and_a_pair(cog, monkeypatch):
    from tests.test_market import It

    async def agree(interaction, needed, title, text, label):
        return interaction.response.send_message

    monkeypatch.setattr(MarketCog, "_confirmed_reply", staticmethod(agree))
    asyncio.run(MarketCog.order.callback(cog, It(), choice("cs"), 2_000, choice("sput"), None, None))
    asyncio.run(MarketCog.order.callback(cog, It(), choice("cs"), 1_000, choice("pair"), None, choice("numa")))
    it = It()
    asyncio.run(MarketCog.holdings.callback(cog, it))
    text = it.sent[-1]["embed"].description
    assert "プット裸売り" in text and "いま買い戻すと" in text and "ペア・ワラント" in text and "vs" in text
    codes = dict(cog.positions(U, G))
    short_code = next(c for c, label in codes.items() if "買い戻し" in label)
    it = It()
    asyncio.run(MarketCog.settle.callback(cog, it, short_code, None))
    assert "買い戻しました" in it.sent[-1]["embed"].title
    pair_code = next(c for c, label in dict(cog.positions(U, G)).items() if "ペア" in label)
    it = It()
    asyncio.run(MarketCog.settle.callback(cog, it, pair_code, None))
    assert "売りました" in it.sent[-1]["embed"].title


def test_leverage_is_refused_for_pair_and_naked_sell(cog):
    from tests.test_market import It

    for kind in ("pair", "scall", "sput"):
        it = It()
        asyncio.run(MarketCog.order.callback(cog, it, choice("cs"), 1_000, choice(kind), choice(3), choice("teio")))
        assert it.sent[-1]["ephemeral"] and "❌" in it.sent[-1]["embed"].title
    assert cog.eco.for_guild(G).option_list(U)["active"] == []
    assert market_events and INITIAL_BALANCE  # 読み込みの確認
