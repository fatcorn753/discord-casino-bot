"""信用取引: 信用買い・クロスマージン（維持率・代用有価証券・連鎖ロスカット）。"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from cogs.market_cog import MarketCog
from core import market
from core.economy import INITIAL_BALANCE, Economy

pytestmark = pytest.mark.real_scope

G, U = 10, 1
START = {s.key: s.start for s in market.SYMBOLS}


@pytest.fixture
def eco(tmp_path):
    e = Economy(str(tmp_path / "t.db"), strict=True)
    for key, price in START.items():
        e.market_set(time.time(), price, 0, key)
    return e


def set_price(eco, key, price):
    eco.market_set(time.time() + set_price.n, price, 0, key)
    set_price.n += 1


set_price.n = 1


OLD_NUMA_RISK_K = 20.0 * 0.012 / market.NUMA.sigma  # NUMA の σ が 0.012 だった頃と、同じ大きさの必要証拠金になる倍率


# ----------------------------- 必要証拠金の計算 ----------------------------- #
def test_required_margin_of_one_symbol_is_risk_k_times_sigma_times_notional():
    got = market.required_margin({"cs": 1000.0})
    assert got == pytest.approx(market.RISK_K * market.CS.sigma * 1000.0)
    assert market.required_margin({"cs": -1000.0}) == pytest.approx(got)  # 売りでも買いでも同じ
    assert market.required_margin({}) == 0.0


def test_hedged_positions_need_less_margin_than_the_sum_of_each():
    single = market.required_margin({"cs": 1000.0}) + market.required_margin({"teio": 1000.0})
    hedged = market.required_margin({"cs": 1000.0, "teio": -1000.0})  # 相関0.6: 値動きが打ち消し合う
    same_side = market.required_margin({"cs": 1000.0, "teio": 1000.0})
    assert hedged < 0.6 * single and same_side < single and hedged < same_side


# ----------------------------- 信用買い ----------------------------- #
def test_long_profits_when_price_rises_and_returns_margin(eco):
    a = eco.for_guild(G)
    set_price(eco, "cs", 100.0)
    res = a.long_open(U, 10_000, 100.0, 2)  # 10枚 = 1,000ペリカ分、2倍 → 担保500
    assert res.ok and res.money == 500 and a.balance(U) == INITIAL_BALANCE - 500
    close = a.long_close(U, None, 120.0)  # 20%上がった: 利益 200
    assert close.ok and close.profit == 200 and close.money == 700
    assert a.balance(U) == INITIAL_BALANCE + 200 and a.long_position(U) == (0, 0.0, 0)


def test_long_loses_when_price_falls_and_partial_close_is_proportional(eco):
    a = eco.for_guild(G)
    assert a.long_open(U, 10_000, 100.0, 1).ok  # 担保1000
    half = a.long_close(U, 5_000, 90.0)  # 半分を10%下がったところで: 損失50
    assert half.profit == -50 and a.long_position(U)[0] == 5_000 and a.long_position(U)[2] == 500
    assert a.balance(U) == INITIAL_BALANCE - 1000 + 450


def test_long_and_short_of_the_same_symbol_cannot_coexist_but_other_symbols_can(eco):
    a = eco.for_guild(G)
    assert a.long_open(U, 2_000, 1000.0, 2, "cs").ok
    refused = a.short_open(U, 2_000, 1000.0, 2, "cs")
    assert not refused.ok and "信用買い" in refused.reason
    assert a.short_open(U, 1_000, 1000.0, 2, "gold").ok
    assert sorted((side, sym) for side, sym, *_ in a.margin_positions(U)) == [("long", "cs"), ("short", "gold")]


def test_margin_equity_counts_longs_in_the_ranking(eco):
    a = eco.for_guild(G)
    set_price(eco, "cs", 100.0)
    a.long_open(U, 10_000, 100.0, 2)
    assert dict((u, v) for _, u, v in a.top(5))[U] == INITIAL_BALANCE  # 担保 + 含み損益（0）
    set_price(eco, "cs", 110.0)
    assert dict((u, v) for _, u, v in a.top(5))[U] == INITIAL_BALANCE + 100


# ----------------------------- 維持率と建玉の受付 ----------------------------- #
def test_a_position_that_leaves_the_ratio_below_the_open_limit_is_refused(eco, monkeypatch):
    monkeypatch.setattr(market, "RISK_K", OLD_NUMA_RISK_K)  # σ を下げる前の、NUMA の必要証拠金の大きさに合わせる
    a = eco.for_guild(G)
    a.credit(U, 20_000, "テスト")
    price = START["numa"]  # σ 1.2%: 必要証拠金が大きい
    refused = a.long_open(U, 10_000, price, 5, "numa")  # 担保20% < 必要24%
    assert not refused.ok and "維持率" in refused.reason and a.long_position(U, "numa")[0] == 0
    assert a.long_open(U, 10_000, price, 3, "numa").ok  # 3倍なら 139% で通る
    assert a.margin_status(U)["ratio"] >= market.OPEN_RATIO


def test_spot_and_bonds_count_as_collateral_with_haircuts(eco, monkeypatch):
    monkeypatch.setattr(market, "RISK_K", OLD_NUMA_RISK_K)
    a = eco.for_guild(G)
    a.credit(U, 20_000, "テスト")
    price = START["numa"]
    assert not a.long_open(U, 10_000, price, 5, "numa").ok  # 担保だけでは足りない
    assert a.crypto_buy(U, 1_000, START["cs"], "cs").ok  # 現物 1,000ペリカ分 → 担保評価 800
    assert a.bond_buy(U, "normal", 1_000, time.time())[0] and a.bond_buy(U, "junk", 1_000, time.time())[0]
    assert a.margin_status(U)["collateral"] == pytest.approx(800 + 950 + 500, abs=2)
    assert a.long_open(U, 10_000, price, 5, "numa").ok  # 代用有価証券が加わって通る


def test_collateral_cannot_be_sold_if_the_ratio_would_fall_below_the_open_limit(eco, monkeypatch):
    monkeypatch.setattr(market, "RISK_K", OLD_NUMA_RISK_K)
    a = eco.for_guild(G)
    a.credit(U, 20_000, "テスト")
    price = START["numa"]
    a.crypto_buy(U, 2_000, START["cs"], "cs")  # 担保評価 1,600
    a.bond_buy(U, "normal", 1_000, time.time())  # 担保評価 950
    assert a.long_open(U, 40_000, price, 5, "numa").ok  # 16,000ペリカ分・担保3,200・必要3,840 → 維持率 150%
    sold = a.crypto_sell(U, None, START["cs"], "cs")  # 売ると 108%
    assert not sold.ok and "担保" in sold.reason and a.crypto_holding(U, "cs")[0] > 0
    ok, why = a.bond_cancel(U, a.bond_list(U)["active"][0]["id"])  # 解約すると 125%
    assert not ok and "担保" in why
    assert a.long_close(U, None, price, "numa").ok  # 建玉を閉じれば、売れる・解約できる
    assert a.crypto_sell(U, None, START["cs"], "cs").ok
    assert a.bond_cancel(U, a.bond_list(U)["active"][0]["id"])[0]


def test_selling_spot_is_free_when_there_are_no_margin_positions(eco):
    a = eco.for_guild(G)
    a.crypto_buy(U, 1_000, START["cs"], "cs")
    assert a.crypto_sell(U, None, START["cs"], "cs").ok


# ----------------------------- ロスカット ----------------------------- #
def test_long_is_cut_when_price_falls_and_the_loss_beyond_margin_becomes_debt(eco):
    a = eco.for_guild(G)
    assert a.long_open(U, 150_000, 100.0, 5).ok  # 15,000ペリカ分・担保3,000（全財産）。必要 1,200
    assert a.balance(U) == 0
    assert eco.margin_losscut({"cs": 95.0}) == []  # 損失750 → 有効2,250 ÷ 必要1,140 ≒ 197%
    done = eco.margin_losscut({"cs": 50.0})  # 半値: 損失7,500。担保3,000を超えた4,500が借金に
    assert done == [(G, U, "cs", "long", 7_500)] and a.long_position(U)[0] == 0
    assert a.debt(U) >= 4_500


def test_chain_losscut_stops_as_soon_as_the_ratio_recovers(eco):
    a = eco.for_guild(G)
    a.credit(U, 20_000, "テスト")
    assert a.long_open(U, 100_000, 100.0, 5, "cs").ok  # 10,000ペリカ分・担保2,000
    assert a.long_open(U, 2_000, 1000.0, 5, "teio").ok  # 2,000ペリカ分・担保400
    done = eco.margin_losscut({"cs": 70.0, "teio": 1000.0})  # csだけ30%下落: 損失3,000で、口座全体は債務超過
    assert [(s, side) for _, _, s, side, _ in done] == [("cs", "long")]  # 損の大きい cs を切れば、維持率が戻る
    assert a.long_position(U, "teio")[0] > 0  # teio は切らない


def test_forced_execution_closes_longs_too(eco):
    a = eco.for_guild(G)
    a.credit(U, 20_000, "テスト")
    assert a.long_open(U, 10_000, START["cs"], 2).ok
    with a._lock, a._conn:
        covered = a._seize_assets(U, 100_000, "テスト")
    assert covered > 0 and a.long_positions(U) == [] and a.balance(U) == 0


# ----------------------------- コマンド ----------------------------- #
def make_cog(eco):
    from tests.test_symbols import make_cog as _make

    return _make(eco)


def choice(value):
    return SimpleNamespace(value=value)


def test_order_long_then_holdings_show_the_ratio_and_settle_closes_it(eco):
    from tests.test_market import It

    cog = make_cog(eco)
    eco.for_guild(G).credit(U, 20_000, "テスト")
    it = It()
    asyncio.run(MarketCog.order.callback(cog, it, choice("cs"), 2_000, choice("long"), choice(2)))
    assert "信用買い" in it.sent[-1]["embed"].title and "維持率" in it.sent[-1]["embed"].description
    assert eco.for_guild(G).long_position(U, "cs")[0] > 0
    it = It()
    asyncio.run(MarketCog.holdings.callback(cog, it))
    desc = it.sent[-1]["embed"].description
    assert "信用買い" in desc and "維持率" in desc
    assert ("long:cs", "🪙 CSトークン 信用買い全決済") in cog.positions(U, G)
    it = It()
    asyncio.run(MarketCog.settle.callback(cog, it, "long:cs", None))
    assert "売り戻しました" in it.sent[-1]["embed"].title and eco.for_guild(G).long_positions(U) == []


def test_order_long_is_refused_when_ratio_is_too_low(eco, monkeypatch):
    monkeypatch.setattr(market, "RISK_K", OLD_NUMA_RISK_K)
    from tests.test_market import It

    cog = make_cog(eco)
    eco.for_guild(G).credit(U, 20_000, "テスト")
    it = It()
    asyncio.run(MarketCog.order.callback(cog, it, choice("numa"), 4_000, choice("long"), choice(5)))
    assert it.sent[-1]["ephemeral"] and "維持率" in it.sent[-1]["embed"].description
    assert eco.for_guild(G).long_positions(U) == []
