"""ペア・ワラント（Margrabe）と、オプションの裸売り（証拠金・プレミアム即時受取・満期の損失を借金へ）。"""

import asyncio
import time
from types import SimpleNamespace

import pytest
from discord import app_commands

from cogs.market.cog import MarketCog
from cogs.market.embeds import describe_option_done
from domain import market, market_events, teiai
from domain import options as opt
from domain.ports.contracts import OptionRow
from domain.types import INITIAL_BALANCE
from infra.db.repositories import open_repositories
from tests.fakes import as_interaction, invoke
from tests.support import make_market_cog, make_test_bot, set_test_debt

pytestmark = pytest.mark.real_scope

G, U, A = 10, 1, 2
START = {s.key: s.start for s in market.SYMBOLS}
HOURS = 3600


@pytest.fixture
def eco(tmp_path):
    e = open_repositories(str(tmp_path / "t.db"), strict=True).for_guild(G)
    now = time.time()
    for key, price in START.items():
        e.market.market_set(now, price, 0, key)
    return e


def fund_bank(eco, amount=50_000):
    """預金者を作り、銀行に、払い出せる現金を用意する。"""
    eco.wallet.credit(A, amount, "テスト")
    eco.bank.bank_deposit(A, amount, time.time())


def set_price(eco, key, price, ts):
    eco.market.market_set(ts, price, 0, key)


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
    ok, res = eco.options.option_buy(U, opt.PAIR, "cs", 1_000, time.time(), symbol2="teio")
    assert ok and res["spot2"] == START["teio"] and 900 < res["cost"] <= 1_000
    set_price(eco, "cs", START["cs"] * 1.10, res["expiry"] - 10)  # A +10%
    set_price(eco, "teio", START["teio"] * 1.02, res["expiry"] - 10)  # B +2%
    done = eco.options.option_settle(res["expiry"] + 1)
    assert done[0]["status"] == "exercised"
    assert done[0]["paid"] == min(int(res["units"] * (1.10 - 1.02)), res["cost"] * opt.PAYOUT_CAP_MULT)


def test_pair_pays_nothing_when_the_other_symbol_wins_even_if_both_rise(eco):
    fund_bank(eco)
    ok, res = eco.options.option_buy(U, opt.PAIR, "cs", 1_000, time.time(), symbol2="teio")
    set_price(eco, "cs", START["cs"] * 1.05, res["expiry"] - 10)
    set_price(eco, "teio", START["teio"] * 1.20, res["expiry"] - 10)
    assert eco.options.option_settle(res["expiry"] + 1)[0]["status"] == "expired"


def test_pair_pays_even_when_the_whole_market_falls(eco):
    fund_bank(eco)
    ok, res = eco.options.option_buy(U, opt.PAIR, "cs", 1_000, time.time(), symbol2="numa")
    set_price(eco, "cs", START["cs"] * 0.95, res["expiry"] - 10)  # 市場全体が下がっても…
    set_price(eco, "numa", START["numa"] * 0.80, res["expiry"] - 10)  # A が B よりましなら当たり
    assert eco.options.option_settle(res["expiry"] + 1)[0]["paid"] > 0


def test_pair_buy_rules(eco):
    assert not eco.options.option_buy(U, opt.PAIR, "cs", 1_000, time.time())[0]  # 相手がない
    assert not eco.options.option_buy(U, opt.PAIR, "cs", 1_000, time.time(), symbol2="cs")[0]  # 同じ銘柄
    assert not eco.options.option_buy(U, opt.PAIR, "cs", 1_000, time.time(), symbol2="nope")[0]


def test_pair_can_be_sold_before_expiry_and_gains_when_a_pulls_ahead(eco):
    fund_bank(eco)
    ok, res = eco.options.option_buy(U, opt.PAIR, "cs", 1_000, time.time(), symbol2="teio")
    row = eco.options.option_list(U)["active"][0]
    now = time.time()
    base = eco.options.option_value(row, now, {"cs": START["cs"], "teio": START["teio"]})
    ahead = eco.options.option_value(row, now, {"cs": START["cs"] * 1.03, "teio": START["teio"]})
    assert ahead > base
    ok, got = eco.options.option_sell(U, res["id"], time.time())
    assert ok and 0 < got < res["cost"]


# ----------------------------- 裸売り ----------------------------- #
def test_write_gives_the_premium_now_and_locks_the_margin(eco):
    fund_bank(eco)
    before = eco.wallet.balance(U)
    ok, res = eco.options.option_write(U, opt.CALL, "cs", 1_000, time.time())
    assert ok and res["strike"] == START["cs"] * (1 + opt.default_strike_pct("cs"))
    assert res["margin"] <= 1_000 and res["premium"] > 0
    assert eco.wallet.balance(U) == before - res["margin"] + res["premium"]
    row = eco.options.option_list(U)["active"][0]
    assert row["side"] == "short" and row["margin"] == res["margin"]


def test_write_needs_bank_cash_for_the_premium(eco):
    ok, why = eco.options.option_write(U, opt.CALL, "numa", 3_000, time.time())  # 銀行に現金がない
    assert not ok and why.code == "option_bank_no_cash" and eco.wallet.balance(U) == INITIAL_BALANCE


def test_write_rules(eco):
    fund_bank(eco)
    assert not eco.options.option_write(U, "pair", "cs", 1_000, time.time())[0]  # 裸売りは、コールとプットだけ
    assert not eco.options.option_write(U, opt.CALL, "cs", 50, time.time())[0]  # 最低額
    assert not eco.options.option_write(U, opt.CALL, "cs", INITIAL_BALANCE + 1, time.time())[0]  # 残高不足
    with eco.db.tx():
        set_test_debt(eco, U, 100, guild_id=G)
    assert eco.options.option_write(U, opt.CALL, "cs", 1_000, time.time())[1].code == "debt_blocks"


def test_write_margin_cap(eco):
    fund_bank(eco, 200_000)
    eco.wallet.credit(U, 100_000, "テスト")
    assert eco.options.option_write(U, opt.PUT, "cs", 15_000, time.time())[0]
    ok, why = eco.options.option_write(U, opt.PUT, "cs", 10_000, time.time())
    assert not ok and why.code == "option_max_short_margin" and why.params["max_margin"] == opt.MAX_SHORT_MARGIN


def test_write_expires_worthless_the_premium_is_the_profit(eco):
    fund_bank(eco)
    before = eco.wallet.balance(U)
    ok, res = eco.options.option_write(U, opt.CALL, "cs", 1_000, time.time())
    set_price(eco, "cs", START["cs"], res["expiry"] - 10)  # 動かなかった
    done = eco.options.option_settle(res["expiry"] + 1)
    assert done[0]["status"] == "expired" and done[0]["paid"] == res["margin"]
    assert eco.wallet.balance(U) == before + res["premium"]  # プレミアムが丸ごと利益


def test_write_loss_within_the_margin_is_paid_from_it(eco):
    fund_bank(eco)
    ok, res = eco.options.option_write(U, opt.CALL, "cs", 2_000, time.time())
    price = res["strike"] * 1.05  # 行使価格を 5% 超えた: 損失 = 差 × 口数
    set_price(eco, "cs", price, res["expiry"] - 10)
    loss = opt.intrinsic("call", res["units"], price, res["strike"])
    assert 0 < loss < res["margin"]
    done = eco.options.option_settle(res["expiry"] + 1)
    assert done[0]["status"] == "exercised" and done[0]["paid"] == res["margin"] - loss and eco.debt.debt(U) == 0


def test_write_loss_beyond_the_margin_becomes_debt(eco):
    fund_bank(eco)
    eco.wallet.credit(U, 10_000, "テスト")
    ok, res = eco.options.option_write(U, opt.CALL, "cs", 3_000, time.time())
    eco.wallet.debit_up_to(U, eco.wallet.balance(U), "テスト")  # 手元の現金をなくす（差し押さえるものがない）
    set_price(eco, "cs", res["strike"] * 3, res["expiry"] - 10)  # 大暴騰
    loss = opt.intrinsic("call", res["units"], res["strike"] * 3, res["strike"])
    done = eco.options.option_settle(res["expiry"] + 1)
    assert done[0]["paid"] == 0 and loss > res["margin"]
    assert eco.debt.debt(U) >= loss - res["margin"] and eco.debt.status(U) != teiai.KATAGI  # 借金（地下行き）


def test_describe_option_done_shows_full_short_loss_beyond_margin(eco):
    """証拠金を超える損失で満期決済された裸売りも、履歴の損益に全損失が反映される。"""
    from cogs.market.embeds import describe_option_done

    fund_bank(eco)
    eco.wallet.credit(U, 10_000, "テスト")
    ok, res = eco.options.option_write(U, opt.CALL, "cs", 3_000, time.time())
    assert ok
    eco.wallet.debit_up_to(U, eco.wallet.balance(U), "テスト")
    set_price(eco, "cs", res["strike"] * 3, res["expiry"] - 10)
    loss = opt.intrinsic(opt.CALL, res["units"], res["strike"] * 3, res["strike"])
    assert loss > res["margin"]
    done = eco.options.option_settle(res["expiry"] + 1)
    expected_net = res["premium"] - loss
    assert expected_net < res["premium"] - res["margin"]
    text = describe_option_done(done[0])
    assert f"（損益 {expected_net:+,}）" in text


def test_settle_does_not_seize_and_double_pay_a_maturing_long_in_the_same_batch(eco):
    """同時満期の裸売りが強制執行を起こしても、同じバッチの買いオプションを二重売却・二重払いしない。"""
    fund_bank(eco, 500_000)
    eco.wallet.credit(U, 20_000, "テスト")
    t0 = time.time()
    ok_s, short = eco.options.option_write(U, opt.CALL, "cs", 2_000, t0)
    ok_l, long = eco.options.option_buy(U, opt.CALL, "cs", 1_000, t0)
    assert ok_s and ok_l
    eco.wallet.debit_up_to(U, eco.wallet.balance(U), "テスト")
    final_price = short["strike"] * 3
    set_price(eco, "cs", final_price, t0 + opt.OPTION_HOURS * HOURS - 10)
    short_loss = opt.intrinsic(opt.CALL, short["units"], final_price, short["strike"])
    long_gross = min(
        opt.intrinsic(opt.CALL, long["units"], final_price, long["strike"]), long["cost"] * opt.PAYOUT_CAP_MULT
    )
    assert short_loss > short["margin"] and long_gross > 0
    done = eco.options.option_settle(t0 + opt.OPTION_HOURS * HOURS + 1)
    by_id = {d["id"]: d for d in done}
    assert set(by_id) == {short["id"], long["id"]}
    assert by_id[long["id"]]["status"] == "exercised"
    assert by_id[long["id"]]["paid"] == long_gross
    assert eco.options.option_list(U)["active"] == []
    # long の配当が1回分だけ手元に入り、short の不足分から差し引かれる（二重払い・二重取りにならない）
    shortage = short_loss - short["margin"]
    expected_debt = max(0, shortage - long_gross)
    expected_bal = max(0, long_gross - shortage)
    assert eco.debt.debt(U) == expected_debt
    assert eco.wallet.balance(U) == expected_bal


def test_settle_does_not_seize_another_maturing_short_in_the_same_batch(eco):
    """同時満期の裸売りを複数持っていても、1件目の強制執行で2件目が途中売却・二重精算されない。"""
    fund_bank(eco, 500_000)
    eco.wallet.credit(U, 20_000, "テスト")
    t0 = time.time()
    ok1, s1 = eco.options.option_write(U, opt.CALL, "cs", 2_000, t0)
    ok2, s2 = eco.options.option_write(U, opt.CALL, "cs", 2_000, t0)
    assert ok1 and ok2
    eco.wallet.debit_up_to(U, eco.wallet.balance(U), "テスト")
    final_price = s1["strike"] * 3
    set_price(eco, "cs", final_price, t0 + opt.OPTION_HOURS * HOURS - 10)
    loss1 = opt.intrinsic(opt.CALL, s1["units"], final_price, s1["strike"])
    loss2 = opt.intrinsic(opt.CALL, s2["units"], final_price, s2["strike"])
    done = eco.options.option_settle(t0 + opt.OPTION_HOURS * HOURS + 1)
    assert [d["status"] for d in done] == ["exercised", "exercised"]
    # 1本目の不足分で自動種銭貸付(SEED)が1回入り、2本目の不足分でその現金が回収されるため +teiai.SEED になる（二重精算されない）
    assert eco.debt.debt(U) == (loss1 - s1["margin"]) + (loss2 - s2["margin"]) + teiai.SEED


def test_the_loss_flows_to_the_bank(eco):
    fund_bank(eco)
    ok, res = eco.options.option_write(U, opt.CALL, "cs", 2_000, time.time())
    before = eco.bank.bank_status(res["expiry"])["cash"]
    set_price(eco, "cs", res["strike"] * 1.05, res["expiry"] - 10)
    loss = opt.intrinsic("call", res["units"], res["strike"] * 1.05, res["strike"])
    eco.options.option_settle(res["expiry"] + 1)
    s = eco.bank.bank_status(res["expiry"] + 1)
    assert s["cash"] == pytest.approx(before + loss - s["thermostat"]["burned"], abs=2)  # 損失が、銀行の収入になる


def test_write_can_be_bought_back_at_the_ask(eco):
    fund_bank(eco)
    before = eco.wallet.balance(U)
    ok, res = eco.options.option_write(U, opt.PUT, "cs", 2_000, time.time())
    ok, back = eco.options.option_sell(U, res["id"], time.time())  # すぐ買い戻す
    assert ok and 0 < back <= res["margin"]
    net = eco.wallet.balance(U) - before
    assert net < 0  # 売値と買値の差（手数料）の分だけ、すぐ戻すと損
    assert eco.options.option_list(U)["done"][0]["status"] == "sold"


def test_write_counts_margin_minus_liability_in_the_ranking_and_is_seized(eco):
    fund_bank(eco)
    ok, res = eco.options.option_write(U, opt.CALL, "cs", 2_000, time.time())
    total = {u: v for _, u, v in eco.wallet.top(5)}[U]
    assert INITIAL_BALANCE - 600 < total < INITIAL_BALANCE + 600  # 証拠金は資産、買い戻しの費用は負債
    eco.wallet.debit_up_to(U, eco.wallet.balance(U), "テスト")
    with eco.db.tx():
        eco.debt._seize_assets_for(G, U, 10_000, "テスト")
    assert eco.options.option_list(U)["active"] == []  # 強制執行で、買い戻して閉じる


def test_long_premium_cap_does_not_count_written_options(eco):
    fund_bank(eco, 200_000)
    eco.wallet.credit(U, 100_000, "テスト")
    eco.options.option_write(U, opt.PUT, "cs", 15_000, time.time())
    assert eco.options.option_buy(U, opt.CALL, "cs", 15_000, time.time())[
        0
    ]  # 買いの上限（掛け金 20,000）は、裸売りの証拠金とは別


# ----------------------------- コマンド ----------------------------- #
def choice(value):
    return SimpleNamespace(value=value)


def new_cog(root):
    """このファイルで読み込んだ MarketCog で作る（他のテストが cogs を読み込み直しても、クラスがずれない）。"""
    cog = make_market_cog(make_test_bot(repos=root))
    cog._market_svc.latest_prices()
    return cog


@pytest.fixture
def cog(tmp_path):
    root = open_repositories(str(tmp_path / "c.db"), strict=True)
    root.for_guild(G).wallet.credit(U, 30_000, "テスト")
    root.for_guild(G).wallet.credit(A, 200_000, "テスト")
    root.for_guild(G).bank.bank_deposit(A, 200_000, time.time())
    return new_cog(root)


def test_order_pair_needs_a_counterpart_and_shows_the_condition(cog):
    from tests.domain.test_market import It

    it = It()
    asyncio.run(invoke(MarketCog.order_pair, cog, it, choice("cs"), choice("cs"), 2_000))
    assert it.sent[-1]["ephemeral"] and "2つの銘柄" in it.sent[-1]["embed"].title
    it = It()
    asyncio.run(invoke(MarketCog.order_pair, cog, it, choice("cs"), choice("teio"), 2_000))
    embed = it.sent[-1]["embed"]
    assert "ペア・ワラント" in embed.title and "CSトークン vs TEIO重工" in embed.title
    assert "上昇率" in embed.description and "想定リターン" in embed.description
    assert cog.bot.repos.for_guild(G).options.option_list(U)["active"][0]["symbol2"] == "teio"


def test_order_naked_sell_asks_for_confirmation_and_creates_the_position(cog, monkeypatch):
    from cogs.market import orders
    from tests.domain.test_market import It

    asked = []

    async def agree(interaction, needed, title, text, label):
        asked.append((needed, title, text))
        return interaction.response.send_message

    monkeypatch.setattr(orders, "default_confirmed_reply", agree)  # 確認ダイアログは、内容だけを記録して通す
    it = It()
    asyncio.run(invoke(MarketCog.order_write, cog, it, choice("cs"), choice("call"), 2_000, None))
    assert (
        asked and asked[0][0] is True and "借金になります" in asked[0][2]
    )  # 裸売りは、必ず確認を挟み、借金になりうることを伝える
    assert "コール裸売り" in it.sent[-1]["embed"].title
    row = cog.bot.repos.for_guild(G).options.option_list(U)["active"][0]
    assert row["side"] == "short" and row["kind"] == "call"


def test_order_write_confirmation_states_the_loss_condition_naturally_for_call_and_put(cog):
    """確認ダイアログの損失条件は、コールなら「を超えて上がると」、プットなら「を下回ると」（「を超えて下がる」にはならない）。"""
    from cogs.market import orders
    from tests.domain.test_market import It

    asked = []

    async def agree(interaction, needed, title, text, label):
        asked.append(text)
        return interaction.response.send_message

    asyncio.run(orders.option_write(cog, as_interaction(It()), opt.CALL, "cs", 2_000, confirm_fn=agree))
    assert "を超えて上がると" in asked[-1] and "を超えて下がる" not in asked[-1] and "を下回る" not in asked[-1]
    asyncio.run(orders.option_write(cog, as_interaction(It()), opt.PUT, "cs", 2_000, confirm_fn=agree))
    assert "を下回ると" in asked[-1] and "を超えて下がる" not in asked[-1] and "を超えて上がる" not in asked[-1]


def test_order_write_confirmation_scales_loss_examples_for_far_targets(cog):
    """目標割合20%以上の裸売り確認ダイアログでは、目標割合に応じた変動幅で損失試算を出す（約0ペリカにならない）。"""
    from cogs.market import orders
    from tests.domain.test_market import It

    asked = []

    async def agree(interaction, needed, title, text, label):
        asked.append(text)
        return interaction.response.send_message

    for target, m1, m2 in ((20, 25, 35), (25, 30, 40), (50, 55, 65)):
        asyncio.run(
            orders.option_write(cog, as_interaction(It()), opt.CALL, "cs", 2_000, target=target, confirm_fn=agree)
        )
        assert f"+{m1}%で損失 約" in asked[-1] and f"+{m2}%で約" in asked[-1]
        assert "約0ペリカ" not in asked[-1]
        asyncio.run(
            orders.option_write(cog, as_interaction(It()), opt.PUT, "cs", 2_000, target=target, confirm_fn=agree)
        )
        assert f"-{m1}%で損失 約" in asked[-1] and f"-{m2}%で約" in asked[-1]
        assert "約0ペリカ" not in asked[-1]
    asyncio.run(orders.option_write(cog, as_interaction(It()), opt.CALL, "cs", 2_000, target=10, confirm_fn=agree))
    assert "+10%で損失 約" in asked[-1] and "+20%で約" in asked[-1]


def test_holdings_and_settle_for_a_naked_sell_and_a_pair(cog, monkeypatch):
    from cogs.market import orders
    from tests.domain.test_market import It

    async def agree(interaction, needed, title, text, label):
        return interaction.response.send_message

    monkeypatch.setattr(orders, "default_confirmed_reply", agree)
    asyncio.run(invoke(MarketCog.order_write, cog, It(), choice("cs"), choice("put"), 2_000, None))
    asyncio.run(invoke(MarketCog.order_pair, cog, It(), choice("cs"), choice("numa"), 1_000))
    it = It()
    asyncio.run(invoke(MarketCog.holdings, cog, it))
    text = it.sent[-1]["embed"].description
    assert "プット裸売り" in text and "いま買い戻すと" in text and "ペア・ワラント" in text and "vs" in text
    codes = dict(cog.positions(U, G))
    short_code = next(c for c, label in codes.items() if "買い戻し" in label)
    it = It()
    asyncio.run(invoke(MarketCog.settle, cog, it, short_code, None))
    assert "買い戻しました" in it.sent[-1]["embed"].title
    pair_code = next(c for c, label in dict(cog.positions(U, G)).items() if "ペア" in label)
    it = It()
    asyncio.run(invoke(MarketCog.settle, cog, it, pair_code, None))
    assert "売りました" in it.sent[-1]["embed"].title


def test_option_sell_and_bond_orders_respect_active_session(cog):
    """他の取引・ゲーム中（セッション有効時）は、オプション売却・債券購入・債券売却もブロックされる。"""
    from cogs.market import orders
    from tests.domain.test_market import It

    eco = cog.bot.repos.for_guild(G)
    ok_opt, opt_res = eco.options.option_buy(U, opt.CALL, "cs", 1_000, time.time())
    ok_bond, _ = eco.bonds.bond_buy(U, "normal", 1_000, time.time())
    assert ok_opt and ok_bond
    bond_id = eco.bonds.bond_list(U)["active"][0]["id"]

    assert cog._sessions.try_start(U) is None
    try:
        for fn in (
            lambda it: orders.option_sell(cog, as_interaction(it), opt_res["id"]),
            lambda it: orders.bond_buy(cog, as_interaction(it), "normal", 1_000),
            lambda it: orders.bond_cancel(cog, as_interaction(it), bond_id),
        ):
            it = It()
            asyncio.run(fn(it))
            assert it.sent[-1]["ephemeral"] and "いまは取引できません" in it.sent[-1]["embed"].title
        assert len(eco.options.option_list(U)["active"]) == 1
        assert len(eco.bonds.bond_list(U)["active"]) == 1
    finally:
        cog._sessions.end(U)

    it = It()
    asyncio.run(orders.option_sell(cog, as_interaction(it), opt_res["id"]))
    assert "売りました" in it.sent[-1]["embed"].title
    it = It()
    asyncio.run(orders.bond_cancel(cog, as_interaction(it), bond_id))
    assert "売りました" in it.sent[-1]["embed"].title
    assert not cog._sessions.is_active(U)


def test_order_subcommands_have_only_the_fields_they_need():
    """種別ごとのサブコマンドには、その種別に要る欄しかない（無関係な欄による事後エラーが起きない）(#79)。"""
    params = {
        c.name: {p.display_name for p in c.parameters}
        for c in MarketCog.order_group.commands
        if isinstance(c, app_commands.Command)
    }
    assert params == {
        "現物": {"銘柄", "金額"},
        "信用": {"銘柄", "売買", "証拠金", "レバレッジ"},
        "コール": {"銘柄", "掛け金", "目標割合"},
        "プット": {"銘柄", "掛け金", "目標割合"},
        "ペア": {"銘柄", "相手", "掛け金"},
        "裸売り": {"銘柄", "種別", "証拠金", "目標割合"},
        "債券": {"種類", "金額"},
    }
    assert market_events and INITIAL_BALANCE  # 読み込みの確認


def test_settle_passes_the_net_of_the_batch_through_the_thermostat_once(eco, monkeypatch):
    """同時刻の満期は、売り手の損失（入金）と買い手の配当（支払い）を差し引きして、1回だけサーモスタットに通す（#32）。"""
    fund_bank(eco, 500_000)
    eco.wallet.credit(U, 100_000, "テスト")
    eco.wallet.credit(A, 100_000, "テスト")
    ok, short = eco.options.option_write(U, opt.CALL, "cs", 2_000, time.time())
    ok2, long = eco.options.option_buy(A, opt.CALL, "cs", 1_000, time.time())
    assert ok and ok2
    price = short["strike"] * 1.05
    set_price(eco, "cs", price, max(short["expiry"], long["expiry"]) - 10)
    calls = []
    bank = eco.options._bank  # 満期の精算が使う銀行
    real_in, real_out = bank._thermo_inflow_for, bank._thermo_shock_for
    monkeypatch.setattr(bank, "_thermo_inflow_for", lambda g, a, now: (calls.append(a), real_in(g, a, now))[1])
    monkeypatch.setattr(bank, "_thermo_shock_for", lambda g, a, now: (calls.append(-a), real_out(g, a, now))[1])
    bailed = eco.bank.bank_status(time.time())["thermostat"]["bailed"]
    done = eco.options.option_settle(max(short["expiry"], long["expiry"]) + 1)
    gross = {d["side"]: d["gross"] for d in done}
    assert len(done) == 2 and calls == [gross["short"] - gross["long"]]
    after = eco.bank.bank_status(time.time())["thermostat"]
    assert (after["bailed"] > bailed) == (calls[0] < 0)  # 個々の入出金は、サーモスタットを通っていない


# ----------------------------- 決済済み履歴の表示 ----------------------------- #
def done_row(
    side: str = "long",
    kind: str = opt.CALL,
    status: str = "expired",
    premium: int = 500,
    paid: int = 0,
    margin: int = 0,
    final: float | None = 1_000.0,
    symbol: str = "cs",
    symbol2: str | None = None,
    spot2: float | None = None,
) -> OptionRow:
    """決済済みオプションの行（テスト用の既定値つき）。describe_option_done の表示を確かめる。"""
    return OptionRow(
        id=1,
        guild_id=G,
        user_id=U,
        kind=kind,
        symbol=symbol,
        strike=1_020.0,
        units=1_000,
        premium=premium,
        expiry=time.time() + 6 * HOURS,
        spot=1_000.0,
        ts=time.time(),
        status=status,
        paid=paid,
        final=final,
        symbol2=symbol2,
        spot2=spot2,
        side=side,
        margin=margin,
    )


def test_describe_option_done_uses_the_slot_icon_for_a_naked_short():
    """裸売りの決済済み履歴は 🎰（保有一覧の option_line と同じ絵文字）で、損益も載る。"""
    row = done_row(side="short", status="expired", premium=500, paid=2_000, margin=2_000)
    text = describe_option_done(row)
    net = row["premium"] + row["paid"] - row["margin"]
    assert text.startswith("🎰 コール裸売り") and "満期・無事" in text and f"（損益 {net:+,}）" in text
    row = done_row(side="short", kind=opt.PUT, status="exercised", premium=500, paid=500, margin=2_000)
    text = describe_option_done(row)
    # 行使された裸売りの損益は、証拠金の精算ではなく満期価格の本質的価値から出す（#150）
    assert row["final"] is not None
    net = row["premium"] - opt.intrinsic(row["kind"], row["units"], row["final"], row["strike"])
    assert text.startswith("🎰 プット裸売り") and "満期・損失" in text and f"（損益 {net:+,}）" in text
    row = done_row(side="short", status="sold", premium=500, paid=300, margin=2_000)
    text = describe_option_done(row)
    net = row["premium"] + row["paid"] - row["margin"]
    assert text.startswith("🎰 コール裸売り") and "買い戻し" in text and f"（損益 {net:+,}）" in text


def test_describe_option_done_keeps_the_kind_icon_for_the_long_side():
    """買い側の決済済み履歴は、従来どおり種類の絵文字（🚀 / 📉 / ⚖️）で表示される。"""
    row = done_row(side="long", kind=opt.CALL, status="exercised", premium=500, paid=3_000)
    text = describe_option_done(row)
    assert text.startswith("🚀 爆騰コール") and "当たり" in text and "（+2,500）" in text
    row = done_row(side="long", kind=opt.PUT, status="sold", premium=500, paid=200)
    text = describe_option_done(row)
    assert text.startswith("📉 暴落プット") and "売却" in text and "（-300）" in text
    row = done_row(side="long", kind=opt.PUT, status="expired", premium=500, paid=0, final=900.0)
    text = describe_option_done(row)
    assert text.startswith("📉 暴落プット") and "外れ" in text
    row = done_row(side="long", kind=opt.PAIR, status="expired", premium=500, paid=0, symbol2="teio", spot2=500.0)
    text = describe_option_done(row)
    assert text.startswith("⚖️ ペア・ワラント") and "CSトークン vs TEIO重工" in text
