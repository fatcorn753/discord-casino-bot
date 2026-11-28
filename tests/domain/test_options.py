"""オプション（爆騰コール・暴落プット）: 値付け・購入・満期の決済・売却・上限・画面。"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from cogs.market.cog import MarketCog
from cogs.market.embeds import option_target_text
from domain import market, teiai
from domain import options as opt
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


def set_price(eco, key, price, ts=None):
    eco.market.market_set(time.time() + 1 if ts is None else ts, price, 0, key)


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
    assert opt.strike_for("call", 1000.0) == 1020.0 and opt.strike_for("put", 1000.0) == 980.0


# ----------------------------- 購入 ----------------------------- #
def test_buy_charges_the_premium_and_records_strike_and_expiry(eco):
    ok, res = eco.options.option_buy(U, opt.CALL, "cs", 1_000, 5_000.0)
    assert ok and res["strike"] == 1030.0 and res["expiry"] == 5_000.0 + 6 * HOURS
    assert 900 < res["cost"] <= 1_000 and eco.wallet.balance(U) == INITIAL_BALANCE - res["cost"]
    row = eco.options.option_list(U)["active"][0]
    assert row["units"] == res["units"] and row["premium"] == res["cost"] and row["kind"] == "call"


def test_premium_feeds_the_bank(eco):
    eco.wallet.credit(A, 5_000, "テスト")
    eco.bank.bank_deposit(A, 5_000, 1.0)
    before = eco.bank.bank_status(1.0)["cash"]
    ok, res = eco.options.option_buy(U, opt.PUT, "cs", 1_000, 1.0)
    s = eco.bank.bank_status(1.0)
    assert ok and s["cash"] == pytest.approx(before + res["cost"] - s["thermostat"]["burned"])


def test_buy_rules(eco):
    assert not eco.options.option_buy(U, "call", "cs", 50, time.time())[0]  # 最低額
    assert not eco.options.option_buy(U, "straddle", "cs", 1_000, time.time())[0]  # 種類
    assert not eco.options.option_buy(U, "call", "cs", INITIAL_BALANCE + 1, time.time())[0]  # 残高不足
    with eco.db.tx():
        set_test_debt(eco, U, 100, guild_id=G)
    assert eco.options.option_buy(U, "call", "cs", 1_000, time.time())[1].code == "debt_blocks"


def test_open_premium_cap(eco):
    eco.wallet.credit(U, 100_000, "テスト")
    assert eco.options.option_buy(U, "call", "cs", 15_000, time.time())[0]
    ok, why = eco.options.option_buy(U, "put", "cs", 10_000, time.time())
    assert not ok and why.code == "option_max_open_premium" and why.params["max_open"] == opt.MAX_OPEN_PREMIUM


# ----------------------------- 満期の決済 ----------------------------- #
def test_call_pays_the_difference_above_the_strike_and_loss_is_the_premium_only(eco):
    eco.wallet.credit(A, 500_000, "テスト")
    eco.bank.bank_deposit(A, 500_000, 1.0)  # 配当を払える銀行
    ok, res = eco.options.option_buy(U, opt.CALL, "cs", 1_000, 1.0)
    set_price(
        eco, "cs", 1070.0, ts=res["expiry"] - 10
    )  # 満期の直前の価格: 行使価格 1,030 を 40 超える（配当は、掛け金の50倍が上限）
    done = eco.options.option_settle(res["expiry"] + 1)
    assert [d["status"] for d in done] == ["exercised"]
    assert done[0]["paid"] == min(int(res["units"] * 40 / market.UNIT), opt.PAYOUT_CAP_MULT * 1_000)
    assert eco.options.option_list(U)["active"] == []


def test_put_pays_when_price_falls_and_out_of_the_money_pays_nothing(eco):
    eco.wallet.credit(A, 500_000, "テスト")
    eco.bank.bank_deposit(A, 500_000, 1.0)
    ok, res = eco.options.option_buy(U, opt.PUT, "cs", 1_000, 1.0)  # 行使価格 970
    set_price(eco, "cs", 930.0, ts=res["expiry"] - 10)  # 行使価格 970 を 40 下回る
    assert eco.options.option_settle(res["expiry"] + 1)[0]["paid"] == min(
        int(res["units"] * 40 / market.UNIT), opt.PAYOUT_CAP_MULT * 1_000
    )
    ok, res2 = eco.options.option_buy(U, opt.PUT, "cs", 500, 2.0)  # 買い直し（行使価格 900 の少し下）
    set_price(eco, "cs", 2000.0, ts=res2["expiry"] - 10)  # 上がった: 外れ
    done = eco.options.option_settle(res2["expiry"] + 1)
    assert done[-1]["status"] == "expired" and done[-1]["paid"] == 0


def test_settlement_uses_the_price_at_expiry_not_the_latest(eco):
    eco.wallet.credit(A, 50_000, "テスト")
    eco.bank.bank_deposit(A, 50_000, 1.0)
    ok, res = eco.options.option_buy(U, opt.CALL, "cs", 1_000, 1.0)
    set_price(eco, "cs", 1100.0, ts=res["expiry"] - 10)  # 満期の時点では 1,100
    set_price(eco, "cs", 900.0, ts=res["expiry"] + 500)  # あとで下がっても、関係ない
    done = eco.options.option_settle(res["expiry"] + 1000)
    assert done[0]["final"] == 1100.0 and done[0]["paid"] > 0


def test_payout_is_capped_at_fifty_times_the_premium(eco):
    eco.wallet.credit(A, 50_000, "テスト")
    eco.bank.bank_deposit(A, 50_000, 1.0)
    ok, res = eco.options.option_buy(U, opt.CALL, "gold", 200, 1.0)  # 値動きの小さい銘柄の、安いコール
    set_price(eco, "gold", START["gold"] * 3, ts=res["expiry"] - 10)  # 3倍に急騰
    done = eco.options.option_settle(res["expiry"] + 1)
    assert done[0]["paid"] == res["cost"] * opt.PAYOUT_CAP_MULT


def test_the_bank_pays_only_what_it_has_and_never_creates_money(eco):
    ok, res = eco.options.option_buy(U, opt.CALL, "cs", 1_000, 1.0)  # 預金者なし: 銀行の手元は、掛け金だけ
    cash = eco.bank.bank_status(1.0)["cash"]
    set_price(eco, "cs", 3000.0, ts=res["expiry"] - 10)  # 大当たり
    done = eco.options.option_settle(res["expiry"] + 1)
    # 手元で足りない分は、熱浴の肩代わりと帝愛共済の口の持ち主が負担する（#32・#34）。それを超えては払わない
    s = eco.bank.bank_status(res["expiry"] + 1)
    charged = eco.db.conn.execute(
        "SELECT COALESCE((SELECT charged FROM kyosai_state WHERE guild_id=?), 0)", (eco.bank.guild_id,)
    ).fetchone()[0]
    assert done[0]["paid"] <= cash + s["thermostat"]["bailed"] + charged + 1
    assert done[0]["paid"] < done[0]["gross"] and s["excess_unpaid"] > 0 and s["cash"] >= 0


def test_winnings_above_the_premium_count_as_income_for_the_debt_withholding(eco):
    eco.wallet.credit(A, 50_000, "テスト")
    eco.bank.bank_deposit(A, 50_000, 1.0)
    ok, res = eco.options.option_buy(U, opt.CALL, "cs", 1_000, 1.0)
    with eco.db.tx():  # 買ったあとに、借金ができた
        set_test_debt(eco, U, 20_000, guild_id=G)
    set_price(eco, "cs", 1300.0, ts=res["expiry"] - 10)
    done = eco.options.option_settle(res["expiry"] + 1)
    profit = done[0]["paid"] - res["cost"]
    assert profit > 0 and eco.debt.debt(U) == 20_000 - int(
        profit * teiai.REPAY_RATE
    )  # 純利益の3割が、借金の返済に天引きされる


# ----------------------------- 売却・差し押さえ・ランキング ----------------------------- #
def test_selling_before_expiry_pays_the_bid_price_less_than_the_premium_at_first(eco):
    eco.wallet.credit(A, 5_000, "テスト")
    eco.bank.bank_deposit(A, 5_000, 1.0)
    ok, res = eco.options.option_buy(U, opt.CALL, "cs", 1_000, 1.0)
    ok, got = eco.options.option_sell(U, res["id"], 1.0)
    assert ok and 0 < got < res["cost"]  # 売買の手数料（スプレッド）の分だけ、買った直後は損
    assert eco.options.option_list(U)["active"] == [] and eco.options.option_list(U)["done"][0]["status"] == "sold"
    assert not eco.options.option_sell(U, res["id"], 1.0)[0]  # 二重には売れない


def test_option_gains_value_when_the_price_moves_in_favour(eco):
    ok, res = eco.options.option_buy(U, opt.CALL, "cs", 1_000, 1.0)
    row = eco.options.option_list(U)["active"][0]
    now = 1.0
    base = eco.options.option_value(row, now, {"cs": 1000.0})
    assert eco.options.option_value(row, now, {"cs": 1040.0}) > base > eco.options.option_value(row, now, {"cs": 960.0})


def test_options_count_in_the_ranking_and_are_seized_for_debt(eco):
    ok, res = eco.options.option_buy(U, opt.CALL, "cs", 1_000, time.time())
    assert {u: v for _, u, v in eco.wallet.top(5)}[U] > INITIAL_BALANCE - res["cost"]  # 掛け金は、評価額として残る
    eco.wallet.debit_up_to(U, eco.wallet.balance(U), "テスト")
    with eco.db.tx():
        covered = eco.debt._seize_assets_for(eco.debt.guild_id, U, 10_000, "テスト")
    assert covered > 0 and eco.options.option_list(U)["active"] == []


# ----------------------------- コマンド ----------------------------- #
def make_cog(bot):
    """このファイルで読み込んだ MarketCog で作る（他のテストが cogs を読み込み直しても、クラスがずれない）。"""
    cog = make_market_cog(bot)
    cog._market_svc.latest_prices()
    return cog


def choice(value):
    return SimpleNamespace(value=value)


@pytest.fixture
def cog(tmp_path):
    bot = make_test_bot(str(tmp_path / "c.db"), strict=True)
    bot.repos.for_guild(G).wallet.credit(U, 20_000, "テスト")
    return make_cog(bot)


def test_order_call_shows_condition_returns_and_holdings_and_settle_sells_it(cog):
    from tests.domain.test_market import It

    it = It()
    asyncio.run(invoke(MarketCog.order_call, cog, it, choice("cs"), 2_000, None))
    embed = it.sent[-1]["embed"]
    assert "爆騰コール" in embed.title and "最大損失はこの掛け金のみ" in embed.description
    assert "勝利条件" in embed.description and "想定リターン" in embed.description
    eco = cog.bot.repos.for_guild(G)
    row = eco.options.option_list(U)["active"][0]
    assert f"{market.fmt(row['strike'])}**（現在 {market.fmt(row['spot'])} の" in embed.description
    oid = row["id"]
    it = It()
    asyncio.run(invoke(MarketCog.holdings, cog, it))
    text = it.sent[-1]["embed"].description
    assert "オプション" in text and f"#{oid}" in text and "いま売ると" in text
    assert (f"opt:{oid}", "🚀 爆騰コール #" + str(oid) + " 売却") in cog.positions(U, G)
    it = it_sell = It()
    asyncio.run(invoke(MarketCog.settle, cog, it_sell, f"opt:{oid}", None))
    assert "売りました" in it.sent[-1]["embed"].title and eco.options.option_list(U)["active"] == []


def test_order_put_and_refusals(cog):
    from tests.domain.test_market import It

    it = It()
    asyncio.run(invoke(MarketCog.order_put, cog, it, choice("gold"), 1_000, None))
    assert "暴落プット" in it.sent[-1]["embed"].title
    it = It()
    asyncio.run(invoke(MarketCog.order_put, cog, it, choice("gold"), 10, None))  # 掛け金が少なすぎる
    assert it.sent[-1]["ephemeral"] and "❌" in it.sent[-1]["embed"].title


# ----------------------------- 目標割合（当たりの基準を選ぶ） ----------------------------- #
def test_default_target_follows_each_symbols_volatility():
    assert set(opt.STRIKE_BY_SYMBOL) == {s.key for s in market.SYMBOLS}
    # 値動きの大きい銘柄ほど、既定の基準が遠い（GOLD < TEIO < CS < NUMA）
    order = sorted(opt.STRIKE_BY_SYMBOL, key=lambda key: opt.STRIKE_BY_SYMBOL[key])
    assert order == ["gold", "teio", "cs", "numa"]
    assert opt.default_strike_pct("unknown") == opt.STRIKE_OTM
    assert opt.strike_for("call", 1000.0, 0.10) == 1100.0 and opt.strike_for("put", 1000.0, 0.10) == 900.0


def test_a_chosen_target_sets_the_strike_and_a_farther_one_is_cheaper_per_unit(eco):
    eco.wallet.credit(U, 100_000, "テスト")
    near = eco.options.option_buy(U, opt.CALL, "cs", 1_000, 1.0, strike_pct=0.01)[1]
    far = eco.options.option_buy(U, opt.CALL, "cs", 1_000, 1.0, strike_pct=0.10)[1]
    assert near["strike"] == 1010.0 and far["strike"] == 1100.0
    assert far["units"] > near["units"]  # 遠い基準は、1口が安いので、同じ掛け金で多く買える
    default = eco.options.option_buy(U, opt.PUT, "teio", 1_000, 1.0)[1]
    assert default["strike"] == round(START["teio"] * (1 - opt.default_strike_pct("teio")), 2)


def test_target_validation_and_writing_with_a_target(eco):
    eco.wallet.credit(U, 100_000, "テスト")
    for bad in (0.0, -0.01, 0.51, 1.0):
        ok, why = eco.options.option_buy(U, opt.CALL, "cs", 1_000, 1.0, strike_pct=bad)
        assert not ok and why.code == "option_target_pct_range"
    eco.wallet.credit(A, 50_000, "テスト")
    eco.bank.bank_deposit(A, 50_000, 1.0)
    ok, res = eco.options.option_write(U, opt.CALL, "cs", 5_000, 1.0, strike_pct=0.08)
    assert ok and res["strike"] == round(START["cs"] * 1.08, 2)
    assert not eco.options.option_write(U, opt.CALL, "cs", 5_000, 1.0, strike_pct=0.9)[0]


def test_buy_amount_suggests_shares_of_the_balance_only_before_typing(eco):
    cog = make_market_cog(make_test_bot(repos=eco))
    eco.wallet.set_balance(U, 8_000, "テスト")
    it = SimpleNamespace(guild_id=G, user=SimpleNamespace(id=U))

    async def names(current):
        return [c.name for c in await cog._buy_amount_autocomplete(as_interaction(it), current)]

    got = asyncio.run(names(""))
    assert any("残高 8,000" in n for n in got)
    assert [n for n in got if "%" in n] == [
        "残高の25%（2,000ペリカ）",
        "残高の50%（4,000ペリカ）",
        "残高の75%（6,000ペリカ）",
        "残高の100%（8,000ペリカ）",
    ]
    typed = asyncio.run(names("500"))  # 数字を打ち始めたら、割合の候補は出さない
    assert len(typed) == 1 and "500" in typed[0]


# ----------------------------- 想定リターンの試算表示 ----------------------------- #
def _target_text(kind, strike_pct, price=1000.0, cost=1000):
    """option_target_text の表示。1枚（market.UNIT口）を cost ペリカで買ったとして試算する。"""
    strike = opt.strike_for(kind, price, strike_pct)
    return option_target_text(kind, market.BY_KEY["cs"], None, strike_pct, price, strike, market.UNIT, cost)


def test_target_text_shows_positive_returns_with_strike_sized_moves_when_target_is_at_least_20pct():
    for kind, s in ((opt.CALL, 1), (opt.PUT, -1)):
        for strike_pct in (0.20, 0.25, opt.MAX_TARGET_PCT / 100):
            lines = [ln for ln in _target_text(kind, strike_pct).splitlines() if "想定リターン" in ln]
            moves = (round(strike_pct + 0.05, 2), round(strike_pct + 0.15, 2))
            assert len(lines) == 2
            for line, move in zip(lines, moves, strict=True):
                assert f"{s * move * 100:+.0f}%" in line  # 目標割合を上回る変動幅が表示される
                assert "約 0ペリカ" not in line  # 行使価格を超えるので、想定リターンは 0 より大きい


def test_target_text_keeps_the_traditional_10_and_20_pct_moves_below_20pct():
    call_text = _target_text(opt.CALL, 0.10)
    assert "+10%" in call_text and "+20%" in call_text and "+30%" not in call_text
    put_text = _target_text(opt.PUT, 0.10)
    assert "-10%" in put_text and "-20%" in put_text and "+10%" not in put_text


def test_option_confirm_quote_includes_upcoming_earnings_jump(tmp_path):
    from cogs.market import orders
    from cogs.market.embeds import option_target_text
    from tests.domain.test_market import It

    bot = make_test_bot(str(tmp_path / "c.db"), strict=True)
    eco = bot.repos.for_guild(G)
    eco.wallet.credit(U, 50_000, "テスト")
    eco.wallet.credit(A, 100_000, "テスト")
    eco.bank.bank_deposit(A, 100_000, 1.0)
    cog = make_market_cog(bot)
    cog._market_svc.latest_prices()
    now = cog._market_svc.clock.now_ts()
    with eco.db.tx():
        eco.db.conn.execute(
            "INSERT INTO earnings (symbol, announce_at, release_at, direction, size, released) VALUES (?, ?, ?, 1, 0.25, 0)",
            ("cs", now - 60, now + 3600),
        )

    prompts: list[str] = []

    async def capture_confirm(interaction, needed, title, text, label):
        prompts.append(text)
        return await orders.default_confirmed_reply(interaction, False, title, text, label)

    it_buy = as_interaction(It())
    asyncio.run(orders.option_buy(cog, it_buy, opt.CALL, "cs", 10_000, confirm_fn=capture_confirm))
    row = eco.options.option_list(U)["active"][0]
    expected = option_target_text(
        opt.CALL,
        market.BY_KEY["cs"],
        None,
        opt.default_strike_pct("cs"),
        row["spot"],
        row["strike"],
        row["units"],
        row["premium"],
    )
    assert expected in prompts[-1]

    it_write = It()
    asyncio.run(orders.option_write(cog, as_interaction(it_write), opt.CALL, "cs", 5_000, confirm_fn=capture_confirm))
    write_embed = it_write.sent[-1]["embed"].description
    paid_token = write_embed.splitlines()[0].split("**")[1]
    assert f"プレミアム {paid_token}" in prompts[-1]
