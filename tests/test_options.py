"""オプション（爆騰コール・暴落プット）: 値付け・購入・満期の決済・売却・上限・画面。"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from cogs.market_cog import MarketCog
from core import market, teiai
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


def set_price(eco, key, price, ts=None):
    eco.market_set(time.time() + 1 if ts is None else ts, price, 0, key)


# ----------------------------- 値付け ----------------------------- #
def test_put_call_parity_holds_with_zero_rate():
    s, k, v = 1000.0, 1050.0, 0.01
    assert opt.fair_price("call", s, k, v) - opt.fair_price("put", s, k, v) == pytest.approx(s - k)


def test_price_is_intrinsic_at_expiry_and_never_below_it():
    assert opt.fair_price("call", 1100, 1050, 0) == 50 and opt.fair_price("put", 1100, 1050, 0) == 0
    for v in (0.0005, 0.005, 0.05):
        assert opt.fair_price("call", 1100, 1050, v) >= 50 - 1e-9
        assert opt.fair_price("put", 900, 950, v) >= 50 - 1e-9


def test_price_grows_with_volatility_and_time():
    assert (
        opt.total_variance("numa", 6 * HOURS)
        > opt.total_variance("cs", 6 * HOURS)
        > opt.total_variance("gold", 6 * HOURS)
    )
    cheap = opt.fair_price("call", 1000, 1050, opt.total_variance("cs", 1 * HOURS))
    dear = opt.fair_price("call", 1000, 1050, opt.total_variance("cs", 6 * HOURS))
    assert 0 <= cheap < dear


def test_variance_includes_the_jump_events():
    plain = market.COVARIANCE[0][0] * 360
    assert opt.total_variance("cs", 360 * opt.TICK_SECONDS) > plain * 1.2  # 有事の分で、CS は約3割増え


def test_ask_is_above_bid_by_the_spread():
    ask = opt.ask_per_unit("call", "cs", 1000, 1050, 6 * HOURS)
    bid = opt.bid_per_unit("call", "cs", 1000, 1050, 6 * HOURS)
    assert ask > bid > 0 and ask / bid == pytest.approx((1 + opt.SPREAD) / (1 - opt.SPREAD))


def test_strikes_are_five_percent_out_of_the_money():
    assert opt.strike_for("call", 1000.0) == 1050.0 and opt.strike_for("put", 1000.0) == 950.0


# ----------------------------- 購入 ----------------------------- #
def test_buy_charges_the_premium_and_records_strike_and_expiry(eco):
    ok, res = eco.option_buy(U, opt.CALL, "cs", 1_000, 5_000.0)
    assert ok and res["strike"] == 1050.0 and res["expiry"] == 5_000.0 + 6 * HOURS
    assert 900 < res["cost"] <= 1_000 and eco.balance(U) == INITIAL_BALANCE - res["cost"]
    row = eco.option_list(U)["active"][0]
    assert row["units"] == res["units"] and row["premium"] == res["cost"] and row["kind"] == "call"


def test_premium_feeds_the_bank(eco):
    eco.credit(A, 5_000, "テスト")
    eco.bank_deposit(A, 5_000, 1.0)
    before = eco.bank_status(1.0)["cash"]
    ok, res = eco.option_buy(U, opt.PUT, "cs", 1_000, 1.0)
    assert ok and eco.bank_status(1.0)["cash"] == pytest.approx(before + res["cost"])


def test_buy_rules(eco):
    assert not eco.option_buy(U, "call", "cs", 50)[0]  # 最低額
    assert not eco.option_buy(U, "straddle", "cs", 1_000)[0]  # 種類
    assert not eco.option_buy(U, "call", "cs", INITIAL_BALANCE + 1)[0]  # 残高不足
    with eco._lock, eco._conn:
        eco._conn.execute("UPDATE users SET debt=100 WHERE user_id=?", (U,))
    assert "借金" in eco.option_buy(U, "call", "cs", 1_000)[1]


def test_open_premium_cap(eco):
    eco.credit(U, 100_000, "テスト")
    assert eco.option_buy(U, "call", "cs", 15_000)[0]
    ok, why = eco.option_buy(U, "put", "cs", 10_000)
    assert not ok and f"{opt.MAX_OPEN_PREMIUM:,}" in why


# ----------------------------- 満期の決済 ----------------------------- #
def test_call_pays_the_difference_above_the_strike_and_loss_is_the_premium_only(eco):
    eco.credit(A, 500_000, "テスト")
    eco.bank_deposit(A, 500_000, 1.0)  # 配当を払える銀行
    ok, res = eco.option_buy(U, opt.CALL, "cs", 1_000, 1.0)
    set_price(
        eco, "cs", 1060.0, ts=res["expiry"] - 10
    )  # 満期の直前の価格: 行使価格 1,050 を 10 超える（配当は、掛け金の50倍が上限）
    done = eco.option_settle(res["expiry"] + 1)
    assert [d["status"] for d in done] == ["exercised"]
    assert done[0]["paid"] == min(int(res["units"] * 10 / market.UNIT), opt.PAYOUT_CAP_MULT * 1_000)
    assert eco.option_list(U)["active"] == []


def test_put_pays_when_price_falls_and_out_of_the_money_pays_nothing(eco):
    eco.credit(A, 500_000, "テスト")
    eco.bank_deposit(A, 500_000, 1.0)
    ok, res = eco.option_buy(U, opt.PUT, "cs", 1_000, 1.0)  # 行使価格 950
    set_price(eco, "cs", 940.0, ts=res["expiry"] - 10)  # 行使価格 950 を 10 下回る
    assert eco.option_settle(res["expiry"] + 1)[0]["paid"] == min(
        int(res["units"] * 10 / market.UNIT), opt.PAYOUT_CAP_MULT * 1_000
    )
    ok, res2 = eco.option_buy(U, opt.PUT, "cs", 500, 2.0)  # 買い直し（行使価格 900 の少し下）
    set_price(eco, "cs", 2000.0, ts=res2["expiry"] - 10)  # 上がった: 外れ
    done = eco.option_settle(res2["expiry"] + 1)
    assert done[-1]["status"] == "expired" and done[-1]["paid"] == 0


def test_settlement_uses_the_price_at_expiry_not_the_latest(eco):
    eco.credit(A, 50_000, "テスト")
    eco.bank_deposit(A, 50_000, 1.0)
    ok, res = eco.option_buy(U, opt.CALL, "cs", 1_000, 1.0)
    set_price(eco, "cs", 1100.0, ts=res["expiry"] - 10)  # 満期の時点では 1,100
    set_price(eco, "cs", 900.0, ts=res["expiry"] + 500)  # あとで下がっても、関係ない
    done = eco.option_settle(res["expiry"] + 1000)
    assert done[0]["final"] == 1100.0 and done[0]["paid"] > 0


def test_payout_is_capped_at_fifty_times_the_premium(eco):
    eco.credit(A, 50_000, "テスト")
    eco.bank_deposit(A, 50_000, 1.0)
    ok, res = eco.option_buy(U, opt.CALL, "gold", 200, 1.0)  # 値動きの小さい銘柄の、安いコール
    set_price(eco, "gold", START["gold"] * 3, ts=res["expiry"] - 10)  # 3倍に急騰
    done = eco.option_settle(res["expiry"] + 1)
    assert done[0]["paid"] == res["cost"] * opt.PAYOUT_CAP_MULT


def test_the_bank_pays_only_what_it_has_and_never_creates_money(eco):
    ok, res = eco.option_buy(U, opt.CALL, "cs", 1_000, 1.0)  # 預金者なし: 銀行の手元は、掛け金だけ
    cash = eco.bank_status(1.0)["cash"]
    set_price(eco, "cs", 3000.0, ts=res["expiry"] - 10)  # 大当たり
    done = eco.option_settle(res["expiry"] + 1)
    assert done[0]["paid"] <= cash and eco.bank_status(res["expiry"] + 1)["cash"] >= 0


def test_winnings_above_the_premium_count_as_income_for_the_debt_withholding(eco):
    eco.credit(A, 50_000, "テスト")
    eco.bank_deposit(A, 50_000, 1.0)
    ok, res = eco.option_buy(U, opt.CALL, "cs", 1_000, 1.0)
    with eco._lock, eco._conn:  # 買ったあとに、借金ができた
        eco._conn.execute("UPDATE users SET debt=20000 WHERE user_id=?", (U,))
    set_price(eco, "cs", 1300.0, ts=res["expiry"] - 10)
    done = eco.option_settle(res["expiry"] + 1)
    profit = done[0]["paid"] - res["cost"]
    assert profit > 0 and eco.debt(U) == 20_000 - int(
        profit * teiai.REPAY_RATE
    )  # 純利益の3割が、借金の返済に天引きされる


# ----------------------------- 売却・差し押さえ・ランキング ----------------------------- #
def test_selling_before_expiry_pays_the_bid_price_less_than_the_premium_at_first(eco):
    eco.credit(A, 5_000, "テスト")
    eco.bank_deposit(A, 5_000, 1.0)
    ok, res = eco.option_buy(U, opt.CALL, "cs", 1_000, 1.0)
    ok, got = eco.option_sell(U, res["id"], 1.0)
    assert ok and 0 < got < res["cost"]  # 売買の手数料（スプレッド）の分だけ、買った直後は損
    assert eco.option_list(U)["active"] == [] and eco.option_list(U)["done"][0]["status"] == "sold"
    assert not eco.option_sell(U, res["id"], 1.0)[0]  # 二重には売れない


def test_option_gains_value_when_the_price_moves_in_favour(eco):
    ok, res = eco.option_buy(U, opt.CALL, "cs", 1_000, 1.0)
    row = eco.option_list(U)["active"][0]
    now = 1.0
    base = eco.option_value(row, now, {"cs": 1000.0})
    assert eco.option_value(row, now, {"cs": 1040.0}) > base > eco.option_value(row, now, {"cs": 960.0})


def test_options_count_in_the_ranking_and_are_seized_for_debt(eco):
    ok, res = eco.option_buy(U, opt.CALL, "cs", 1_000)
    assert dict((u, v) for _, u, v in eco.top(5))[U] > INITIAL_BALANCE - res["cost"]  # 掛け金は、評価額として残る
    eco.debit_up_to(U, eco.balance(U), "テスト")
    with eco._lock, eco._conn:
        covered = eco._seize_assets(U, 10_000, "テスト")
    assert covered > 0 and eco.option_list(U)["active"] == []


# ----------------------------- コマンド ----------------------------- #
def make_cog(eco_root):
    """このファイルで読み込んだ MarketCog で作る（他のテストが cogs を読み込み直しても、クラスがずれない）。"""
    cog = MarketCog(SimpleNamespace(economy=eco_root, sessions=Sessions(cooldown=0), config={}))
    cog._prices()
    return cog


def choice(value):
    return SimpleNamespace(value=value)


@pytest.fixture
def cog(tmp_path):
    root = Economy(str(tmp_path / "c.db"), strict=True)
    root.for_guild(G).credit(U, 20_000, "テスト")
    return make_cog(root)


def test_order_call_shows_condition_returns_and_holdings_and_settle_sells_it(cog):
    from tests.test_market import It

    it = It()
    asyncio.run(MarketCog.order.callback(cog, it, choice("cs"), 2_000, choice("call"), None))
    embed = it.sent[-1]["embed"]
    assert "爆騰コール" in embed.title and "最大損失はこの掛け金のみ" in embed.description
    assert "勝利条件" in embed.description and "想定リターン" in embed.description
    eco = cog.eco.for_guild(G)
    oid = eco.option_list(U)["active"][0]["id"]
    it = It()
    asyncio.run(MarketCog.holdings.callback(cog, it))
    text = it.sent[-1]["embed"].description
    assert "オプション" in text and f"#{oid}" in text and "いま売ると" in text
    assert (f"opt:{oid}", "🚀 爆騰コール #" + str(oid) + " 売却") in cog.positions(U, G)
    it = it_sell = It()
    asyncio.run(MarketCog.settle.callback(cog, it_sell, f"opt:{oid}", None))
    assert "売りました" in it.sent[-1]["embed"].title and eco.option_list(U)["active"] == []


def test_order_put_and_refusals(cog):
    from tests.test_market import It

    it = It()
    asyncio.run(MarketCog.order.callback(cog, it, choice("gold"), 1_000, choice("put"), None))
    assert "暴落プット" in it.sent[-1]["embed"].title
    for args in (
        (choice("cs"), 1_000, choice("call"), choice(3)),  # レバレッジなし
        (choice("bond_normal"), 1_000, choice("call"), None),  # 債券には買えない
    ):
        it = It()
        asyncio.run(MarketCog.order.callback(cog, it, *args))
        assert it.sent[-1]["ephemeral"] and "❌" in it.sent[-1]["embed"].title
