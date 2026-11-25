"""信用取引: 信用買い・クロスマージン（担保余裕度・代用有価証券・連鎖ロスカット）。"""

import asyncio
import itertools
import time
from types import SimpleNamespace

import pytest

from cogs.market.cog import MarketCog
from domain import market
from domain.types import INITIAL_BALANCE
from infra.db.repositories import open_repositories
from tests.fakes import invoke

pytestmark = pytest.mark.real_scope

G, U = 10, 1
START = {s.key: s.start for s in market.SYMBOLS}


@pytest.fixture
def eco(tmp_path):
    e = open_repositories(str(tmp_path / "t.db"), strict=True)
    for key, price in START.items():
        e.market.market_set(time.time(), price, 0, key)
    return e


_PRICE_TICKS = itertools.count(1)  # 価格を置くたびに、1秒ずつ後ろにずらす


def set_price(eco, key, price):
    eco.market.market_set(time.time() + next(_PRICE_TICKS), price, 0, key)


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
    res = a.margin.long_open(U, 10_000, 100.0, 2)  # 10枚 = 1,000ペリカ分、2倍 → 担保500
    assert res.ok and res.money == 500 and a.wallet.balance(U) == INITIAL_BALANCE - 500
    close = a.margin.long_close(U, None, 120.0)  # 20%上がった: 利益 200
    assert close.ok and close.profit == 200 and close.money == 700
    assert a.wallet.balance(U) == INITIAL_BALANCE + 200 and a.margin.long_position(U) == (0, 0.0, 0)


def test_long_loses_when_price_falls_and_partial_close_is_proportional(eco):
    a = eco.for_guild(G)
    assert a.margin.long_open(U, 10_000, 100.0, 1).ok  # 担保1000
    half = a.margin.long_close(U, 5_000, 90.0)  # 半分を10%下がったところで: 損失50
    assert half.profit == -50 and a.margin.long_position(U)[0] == 5_000 and a.margin.long_position(U)[2] == 500
    assert a.wallet.balance(U) == INITIAL_BALANCE - 1000 + 450


def test_long_and_short_of_the_same_symbol_cannot_coexist_but_other_symbols_can(eco):
    a = eco.for_guild(G)
    assert a.margin.long_open(U, 2_000, 1000.0, 2, "cs").ok
    refused = a.margin.short_open(U, 2_000, 1000.0, 2, "cs")
    assert not refused.ok and refused.reason.code == "margin_opposite_open"
    assert a.margin.short_open(U, 1_000, 1000.0, 2, "gold").ok
    assert sorted((side, sym) for side, sym, *_ in a.margin.margin_positions(U)) == [("long", "cs"), ("short", "gold")]


def test_margin_equity_counts_longs_in_the_ranking(eco):
    a = eco.for_guild(G)
    set_price(eco, "cs", 100.0)
    a.margin.long_open(U, 10_000, 100.0, 2)
    assert {u: v for _, u, v in a.wallet.top(5)}[U] == INITIAL_BALANCE  # 担保 + 含み損益（0）
    set_price(eco, "cs", 110.0)
    assert {u: v for _, u, v in a.wallet.top(5)}[U] == INITIAL_BALANCE + 100


# ----------------------------- 担保余裕度と建玉の受付 ----------------------------- #
def test_a_position_that_leaves_the_ratio_below_the_open_limit_is_refused(eco, monkeypatch):
    monkeypatch.setattr(market, "RISK_K", OLD_NUMA_RISK_K)  # σ を下げる前の、NUMA の必要証拠金の大きさに合わせる
    a = eco.for_guild(G)
    a.wallet.credit(U, 20_000, "テスト")
    price = START["numa"]  # σ 1.2%: 必要証拠金が大きい
    refused = a.margin.long_open(U, 10_000, price, 5, "numa")  # 担保20% < 必要24%
    assert (
        not refused.ok and refused.reason.code == "margin_ratio_too_low" and a.margin.long_position(U, "numa")[0] == 0
    )
    assert a.margin.long_open(U, 10_000, price, 3, "numa").ok  # 3倍なら 139% で通る
    assert a.margin.margin_status(U)["ratio"] >= market.OPEN_RATIO


def test_spot_and_bonds_count_as_collateral_with_haircuts(eco, monkeypatch):
    monkeypatch.setattr(market, "RISK_K", OLD_NUMA_RISK_K)
    a = eco.for_guild(G)
    a.wallet.credit(U, 20_000, "テスト")
    price = START["numa"]
    assert not a.margin.long_open(U, 10_000, price, 5, "numa").ok  # 担保だけでは足りない
    assert a.crypto.crypto_buy(U, 1_000, START["cs"], "cs").ok  # 現物 1,000ペリカ分 → 担保評価 800
    assert a.bonds.bond_buy(U, "normal", 1_000, time.time())[0] and a.bonds.bond_buy(U, "junk", 1_000, time.time())[0]
    assert a.margin.margin_status(U)["collateral"] == pytest.approx(800 + 950 + 500, abs=2)
    assert a.margin.long_open(U, 10_000, price, 5, "numa").ok  # 代用有価証券が加わって通る


def test_collateral_cannot_be_sold_if_the_ratio_would_fall_below_the_open_limit(eco, monkeypatch):
    monkeypatch.setattr(market, "RISK_K", OLD_NUMA_RISK_K)
    a = eco.for_guild(G)
    a.wallet.credit(U, 20_000, "テスト")
    price = START["numa"]
    a.crypto.crypto_buy(U, 2_000, START["cs"], "cs")  # 担保評価 1,600
    a.bonds.bond_buy(U, "normal", 1_000, time.time())  # 担保評価 950
    assert a.margin.long_open(U, 40_000, price, 5, "numa").ok  # 16,000ペリカ分・担保3,200・必要3,840 → 担保余裕度 150%
    sold = a.crypto.crypto_sell(U, None, START["cs"], "cs")  # 売ると 108%
    assert not sold.ok and sold.reason.code == "crypto_collateral_locked" and a.crypto.crypto_holding(U, "cs")[0] > 0
    ok, why = a.bonds.bond_cancel(U, a.bonds.bond_list(U)["active"][0]["id"])  # 解約すると 125%
    assert not ok and why.code == "crypto_collateral_locked"
    assert a.margin.long_close(U, None, price, "numa").ok  # 建玉を閉じれば、売れる・解約できる
    assert a.crypto.crypto_sell(U, None, START["cs"], "cs").ok
    assert a.bonds.bond_cancel(U, a.bonds.bond_list(U)["active"][0]["id"])[0]


def test_selling_spot_is_free_when_there_are_no_margin_positions(eco):
    a = eco.for_guild(G)
    a.crypto.crypto_buy(U, 1_000, START["cs"], "cs")
    assert a.crypto.crypto_sell(U, None, START["cs"], "cs").ok


# ----------------------------- ロスカット ----------------------------- #
def test_long_is_cut_when_price_falls_and_the_loss_beyond_margin_becomes_debt(eco):
    a = eco.for_guild(G)
    assert a.margin.long_open(U, 150_000, 100.0, 5).ok  # 15,000ペリカ分・担保3,000（全財産）。必要 1,200
    assert a.wallet.balance(U) == 0
    assert eco.margin.margin_losscut({"cs": 95.0}) == []  # 損失750 → 有効2,250 ÷ 必要1,140 ≒ 197%
    done = eco.margin.margin_losscut({"cs": 50.0})  # 半値: 損失7,500。担保3,000を超えた4,500が借金に
    assert done == [(G, U, "cs", "long", 7_500)] and a.margin.long_position(U)[0] == 0
    assert a.debt.debt(U) >= 4_500


def test_chain_losscut_stops_as_soon_as_the_ratio_recovers(eco):
    a = eco.for_guild(G)
    a.wallet.credit(U, 20_000, "テスト")
    assert a.margin.long_open(U, 100_000, 100.0, 5, "cs").ok  # 10,000ペリカ分・担保2,000
    assert a.margin.long_open(U, 2_000, 1000.0, 5, "teio").ok  # 2,000ペリカ分・担保400
    done = eco.margin.margin_losscut({"cs": 70.0, "teio": 1000.0})  # csだけ30%下落: 損失3,000で、口座全体は債務超過
    assert [(s, side) for _, _, s, side, _ in done] == [("cs", "long")]  # 損の大きい cs を切れば、担保余裕度が戻る
    assert a.margin.long_position(U, "teio")[0] > 0  # teio は切らない


def test_forced_execution_closes_longs_too(eco):
    a = eco.for_guild(G)
    a.wallet.credit(U, 20_000, "テスト")
    assert a.margin.long_open(U, 10_000, START["cs"], 2).ok
    with a.db.tx():
        covered = a.debt._seize_assets_for(G, U, 100_000, "テスト")
    assert covered > 0 and a.margin.long_positions(U) == [] and a.wallet.balance(U) == 0


# ----------------------------- コマンド ----------------------------- #
def make_cog(eco):
    from tests.infra.test_symbols import make_cog as _make

    return _make(eco)


def choice(value):
    return SimpleNamespace(value=value)


def test_order_long_then_holdings_show_the_ratio_and_settle_closes_it(eco):
    from tests.domain.test_market import It

    cog = make_cog(eco)
    eco.for_guild(G).wallet.credit(U, 20_000, "テスト")
    it = It()
    asyncio.run(invoke(MarketCog.order_margin, cog, it, choice("cs"), choice("long"), 2_000, choice(2)))
    assert "信用買い" in it.sent[-1]["embed"].title and "担保余裕度" in it.sent[-1]["embed"].description
    assert eco.for_guild(G).margin.long_position(U, "cs")[0] > 0
    it = It()
    asyncio.run(invoke(MarketCog.holdings, cog, it))
    desc = it.sent[-1]["embed"].description
    assert "信用買い" in desc and "担保余裕度" in desc
    assert ("long:cs", "🪙 CSトークン 信用買い") in cog.positions(U, G)
    it = It()
    asyncio.run(invoke(MarketCog.settle, cog, it, "long:cs", None))
    assert "売り戻しました" in it.sent[-1]["embed"].title and eco.for_guild(G).margin.long_positions(U) == []


def test_order_long_is_refused_when_ratio_is_too_low(eco, monkeypatch):
    monkeypatch.setattr(market, "RISK_K", OLD_NUMA_RISK_K)
    from tests.domain.test_market import It

    cog = make_cog(eco)
    eco.for_guild(G).wallet.credit(U, 20_000, "テスト")
    it = It()
    asyncio.run(invoke(MarketCog.order_margin, cog, it, choice("numa"), choice("long"), 800, choice(5)))
    assert it.sent[-1]["ephemeral"] and "担保余裕度" in it.sent[-1]["embed"].description
    assert eco.for_guild(G).margin.long_positions(U) == []


# ----------------------------- 信用金利（借入金利・貸株料） ----------------------------- #
T0 = 1_800_000_000.0
DAY_S = 86_400.0


def test_margin_interest_base_and_rates():
    assert market.margin_interest_base("long", 40_000.0, 8_000) == 32_000  # 借りている額
    assert market.margin_interest_base("long", 5_000.0, 6_000) == 0  # 担保のほうが多ければ、借入なし
    assert market.margin_interest_base("short", 40_000.0, 8_000) == 40_000  # 空売りは、建てた総額
    assert market.BORROW_RATE_PER_DAY["long"] > market.BORROW_RATE_PER_DAY["short"] > 0


def test_long_interest_is_taken_from_the_margin_after_a_day_and_goes_to_the_bank(eco):
    a = eco.for_guild(G)
    a.wallet.credit(U, 100_000, "テスト")
    a.wallet.credit(2, 20_000, "テスト")
    a.bank.bank_deposit(2, 20_000, 1.0)
    assert a.margin.long_open(U, 40_000, 1000, 5, "cs").ok
    margin0 = a.margin.margin_position(U, "cs", "long")[2]
    assert eco.margin.margin_interest(T0) == []  # 数え始め
    deposit0 = a.bank.deposit_of(2)
    cash0 = a.bank.bank_status(T0 + DAY_S)["cash"]
    done = eco.margin.margin_interest(T0 + DAY_S)
    borrowed = 40_000 - margin0
    expected = int(borrowed * market.BORROW_RATE_PER_DAY["long"])
    assert [(d[0], d[1], d[3], d[4]) for d in done] == [(G, U, "long", expected)]
    assert a.margin.margin_position(U, "cs", "long")[2] == margin0 - expected
    assert a.bank.deposit_of(2) == deposit0  # 信託には配らない（#33）
    s = a.bank.bank_status(T0 + DAY_S)
    assert s["cash"] == pytest.approx(cash0 + expected - s["thermostat"]["burned"])  # サーモスタット経由で帝愛銀行の手元へ
    assert any(
        "信用金利" in x["reason"] for x in eco.db.conn.execute("SELECT reason FROM ledger WHERE user_id=?", (U,))
    )


def test_short_pays_a_borrow_fee_on_the_whole_position_and_small_amounts_wait(eco):
    a = eco.for_guild(G)
    a.wallet.credit(U, 100_000, "テスト")
    assert a.margin.short_open(U, 10_000, 1000, 1, "cs").ok  # 建玉 10,000・担保 10,000
    eco.margin.margin_interest(T0)
    assert eco.margin.margin_interest(T0 + 3_600) == []  # 1時間: 0.2 ペリカ。10に貯まるまで、引かない
    m = a.margin.margin_position(U, "cs", "short")[2]
    done = eco.margin.margin_interest(T0 + 40 * DAY_S)  # 40日ぶん: 貸株料 0.05% × 10,000 × 40 = 200
    assert done and done[0][4] == pytest.approx(200, abs=1)
    assert a.margin.margin_position(U, "cs", "short")[2] == m - done[0][4]


def test_interest_never_takes_more_than_the_margin_and_no_double_charge(eco):
    a = eco.for_guild(G)
    a.wallet.credit(U, 100_000, "テスト")
    assert a.margin.short_open(U, 10_000, 1000, 1, "cs").ok
    eco.margin.margin_interest(T0)
    eco.margin.margin_interest(T0 + 10_000 * DAY_S)  # 非常に長い間
    assert a.margin.margin_position(U, "cs", "short")[2] >= 0
    again = eco.margin.margin_interest(T0 + 10_000 * DAY_S)  # 同じ時刻に、もう一度: 追加で引かれない
    assert again == []


def test_old_positions_without_the_interest_columns_get_them_on_startup(tmp_path):
    import sqlite3

    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE crypto_long (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, symbol TEXT NOT NULL, "
        "units INTEGER NOT NULL, entry_value REAL NOT NULL, margin INTEGER NOT NULL, "
        "PRIMARY KEY (guild_id, user_id, symbol))"
    )
    conn.execute("INSERT INTO crypto_long VALUES (?, ?, 'cs', 5000, 5000.0, 2500)", (G, U))
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    again = open_repositories(path, strict=True)
    cols = {r["name"] for r in again.db.conn.execute("PRAGMA table_info(margin_positions)")}
    assert {"interest_at", "accrued"} <= cols
    assert again.for_guild(G).margin.long_position(U, "cs") == (5000, 5000.0, 2500)


# ----------------------------- 最低担保の案内(#69・#75・#78) ----------------------------- #
def test_min_collateral_message_asks_for_more_collateral(eco):
    from cogs.shared.messages import format_failure

    a = eco.for_guild(G)
    set_price(eco, "cs", 1.0)
    res = a.margin.long_open(U, 18 * market.UNIT, 1.0, 2)  # 総額18ペリカ・2倍 → 担保9
    assert not res.ok
    text = format_failure(res.reason)
    assert "担保が 10ペリカ 未満" in text and "証拠金を増やしてください" in text and "枚数" not in text
    assert a.margin.long_open(U, 19 * market.UNIT, 1.0, 2).ok  # 担保 ceil(9.5)=10 なら建てられる


def test_min_collateral_message_says_the_balance_is_not_enough(eco):
    from cogs.shared.messages import format_failure

    a = eco.for_guild(G)
    a.wallet.set_balance(U, 5, "テスト")  # 残高5ペリカ: 最低担保10に届かない
    set_price(eco, "cs", 1.0)
    res = a.margin.long_open(U, 15 * market.UNIT, 1.0, 2)  # 担保 ceil(7.5)=8
    text = format_failure(res.reason)
    assert not res.ok and "残高 5ペリカ では足りません" in text
