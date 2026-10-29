"""CSトークン: 価格の動き・売買の計算・チャート・コマンド。"""

import asyncio
import random
from types import SimpleNamespace

import discord
import pytest

from cogs.market_cog import MarketCog
from core import market
from core.economy import INITIAL_BALANCE, Economy
from core.session import Sessions

pytestmark = pytest.mark.real_scope

G, U = 10, 1


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def eco(tmp_path):
    return Economy(str(tmp_path / "t.db"), strict=True)


# ----------------------------- 価格の動き ----------------------------- #
def test_price_stays_in_range_and_moves():
    rng, p, seen = random.Random(3), market.START_PRICE, set()
    for _ in range(20_000):
        p, ev = market.next_price(p, rng)
        assert market.MIN_PRICE <= p <= market.MAX_PRICE and ev in (-1, 0, 1)
        seen.add(p)
    assert len(seen) > 100


@pytest.mark.parametrize("start", [25.0, 100.0, 1000.0, 5000.0])
def test_one_step_expectation_is_unbiased_at_every_price_level(start):
    """どの価格でも、1分後の価格の平均が変わらない（偏りがあると、持つだけ・安く買うだけで儲かってしまう）。"""
    rng, n = random.Random(11), 400_000
    mean = sum(market.next_price(start, rng)[0] for _ in range(n)) / n
    assert abs(mean / start - 1) < 4e-5 * (1 + 25 / start)           # 小さい価格は丸め（小数2桁）のぶん、許容を少し広げる


def test_event_part_is_exactly_symmetric():
    """急騰と急落は ×(1+x) / ×(1-x) を同じ確率で。期待値はちょうど1。"""
    lo, hi = market.EVENT_RANGE
    up = sum(1 + (lo + (hi - lo) * (i + 0.5) / 1000) for i in range(1000)) / 1000
    down = sum(1 - (lo + (hi - lo) * (i + 0.5) / 1000) for i in range(1000)) / 1000
    assert abs((up + down) / 2 - 1) < 1e-12


def test_low_prices_keep_moving():
    """低い価格で、丸めのせいで動きが止まらない。"""
    rng, p, same = random.Random(2), 25.0, 0
    for _ in range(20_000):
        new, _ = market.next_price(p, rng)
        same += new == p
        p = 25.0 if new < 25 else new
    assert same / 20_000 < 0.2


def test_events_happen_sometimes():
    rng, p, events = random.Random(5), market.START_PRICE, 0
    for _ in range(6_000):
        p, ev = market.next_price(p, rng)
        events += bool(ev)
    assert 3 <= events <= 30


# ----------------------------- 売買の計算 ----------------------------- #
@pytest.mark.parametrize("amount,price", [(100, 1000), (5_000, 1000), (10, 20), (99_999, 777), (3_000, 50_000)])
def test_buy_quote_never_exceeds_the_amount_and_is_maximal(amount, price):
    units, total, fee = market.buy_quote(amount, price)
    assert total <= amount and fee >= 0
    if units:
        gross = -(-(units + 1) * price // market.UNIT)
        assert gross + -(-int(gross * market.FEE_RATE * 1000) // 1000) > amount            # もう1口は買えない


def test_buy_then_sell_immediately_never_gains():
    for amount in (100, 1_234, 50_000):
        units, total, _ = market.buy_quote(amount, 1000)
        proceeds, _ = market.sell_quote(units, 1000)
        assert proceeds <= total                                  # 手数料なしでも、端数の切り捨てで得はしない


def test_too_small_amount_buys_nothing():
    assert market.buy_quote(10, 50_000) == (0, 0, 0)


# ----------------------------- 売買と記録 ----------------------------- #
def ledger_sum(eco, guild, user):
    return eco._conn.execute("SELECT COALESCE(SUM(delta), 0) FROM ledger WHERE guild_id=? AND user_id=?", (guild, user)).fetchone()[0]


def test_buy_and_sell_move_balance_and_ledger_consistently(eco):
    a = eco.for_guild(G)
    a.balance(U)
    r = a.crypto_buy(U, 3_000, 1000)
    assert r.ok and a.balance(U) == INITIAL_BALANCE - r.money and a.crypto_holding(U) == (r.units, r.money)
    s = a.crypto_sell(U, None, 1100)
    assert s.ok and a.crypto_holding(U) == (0, 0) and s.profit == s.money - r.money
    assert a.balance(U) == INITIAL_BALANCE - r.money + s.money
    assert ledger_sum(eco, G, U) == a.balance(U)               # 履歴の合計と残高が一致する


def test_partial_sell_keeps_cost_basis_proportional(eco):
    a = eco.for_guild(G)
    r = a.crypto_buy(U, 2_000, 1000)
    half = r.units // 2
    s = a.crypto_sell(U, half, 1000)
    units, cost = a.crypto_holding(U)
    assert s.ok and units == r.units - half and cost == r.money - r.money * half // r.units


def test_cannot_oversell_or_buy_beyond_balance(eco):
    a = eco.for_guild(G)
    assert not a.crypto_sell(U, 1, 1000).ok                              # 持っていない
    assert not a.crypto_buy(U, INITIAL_BALANCE + 1, 1000).ok
    r = a.crypto_buy(U, 1_000, 1000)
    assert not a.crypto_sell(U, r.units + 1, 1000).ok
    assert a.crypto_holding(U)[0] == r.units and a.balance(U) == INITIAL_BALANCE - r.money


def test_holdings_are_per_server_but_price_is_shared(eco):
    a, b = eco.for_guild(G), eco.for_guild(20)
    a.crypto_buy(U, 1_000, 1000)
    assert b.crypto_holding(U) == (0, 0) and b.balance(U) == INITIAL_BALANCE
    eco.market_set(100.0, 1234, 0)
    assert a.market_latest()[1] == b.market_latest()[1] == 1234


def test_market_history_and_prune(eco):
    for i in range(5):
        eco.market_set(float(i), 1000 + i, 0)
    assert [p for _, p, _ in eco.market_history(2.0)] == [1002, 1003, 1004]
    eco.market_prune(3.0)
    assert [p for _, p, _ in eco.market_history(0.0)] == [1003, 1004]


# ----------------------------- チャート ----------------------------- #
def test_chart_shape_labels_and_flat_market():
    text = market.render_chart([1000, 1100, 1200, 1300, 1400], width=32, height=6)
    lines = text.split("\n")
    assert len(lines) == 7 and lines[0].lstrip().startswith("1,400") and "1,000" in lines[5]
    assert "1,000" in market.render_chart([1000] * 10, width=32, height=4)
    assert market.render_chart([]) == "（まだデータがありません）"


def test_chart_downsamples_to_width():
    text = market.render_chart(list(range(1, 500)), width=32, height=4)
    assert max(len(line) for line in text.split("\n")) <= 6 + 2 + 32


# ----------------------------- コマンド ----------------------------- #
class It:
    def __init__(self, uid=U, gid=G):
        self.user = SimpleNamespace(id=uid, display_name="テスト")
        self.guild = SimpleNamespace(id=gid)
        self.guild_id = gid
        self.sent = []
        self.response = SimpleNamespace(send_message=self._send, defer=self._defer)
        self.followup = SimpleNamespace(send=self._send)

    async def _send(self, *a, **kw):
        self.sent.append(kw)

    async def _defer(self, **kw):
        return None


@pytest.fixture
def cog(eco):
    bot = SimpleNamespace(economy=eco, sessions=Sessions(cooldown=0), config={})
    c = MarketCog(bot)
    c._price()
    return c


def test_buy_sell_holdings_commands(cog):
    it = It()
    run(MarketCog.buy.callback(cog, it, 1_000))
    assert "買いました" in it.sent[-1]["embed"].title
    it = It()
    run(MarketCog.holdings.callback(cog, it))
    assert "保有" in it.sent[-1]["embed"].description
    it = It()
    run(MarketCog.sell.callback(cog, it, None))
    assert "売りました" in it.sent[-1]["embed"].title
    assert cog.bot.sessions.try_start(U) is None                       # 取引のあと、プレイ中のままにならない


def test_trading_is_refused_during_a_game(cog):
    assert cog.bot.sessions.try_start(U) is None
    it = It()
    run(MarketCog.buy.callback(cog, it, 1_000))
    assert "取引できません" in it.sent[-1]["embed"].title
    assert cog.eco.for_guild(G).crypto_holding(U) == (0, 0)


def test_price_chart_and_step(cog):
    it = It()
    cog.step_price()
    run(MarketCog.price.callback(cog, it))
    assert "現在" in it.sent[-1]["embed"].description
    it = It()
    run(MarketCog.chart.callback(cog, it, None))
    assert "```" in it.sent[-1]["embed"].description and "24時間" in it.sent[-1]["embed"].title


def test_board_refresh_edits_without_reading_history(cog):
    class Ch:
        id = 5

        def __init__(self):
            self.edited = []

        def get_partial_message(self, mid):
            ch = self

            class P:
                async def edit(self, **kw):
                    ch.edited.append(kw["embed"])
            return P()

        async def fetch_message(self, mid):
            raise discord.Forbidden(SimpleNamespace(status=403, reason="x"), "no history")
    ch = Ch()
    guild = SimpleNamespace(id=G, get_channel=lambda cid: ch)
    cog.bot.get_guild = lambda gid: guild
    cog.eco.set_chart_board(G, 5, 99)
    run(cog._refresh_board(cog.eco.list_chart_boards()[0]))
    assert len(ch.edited) == 1 and market.NAME in ch.edited[0].title


# ----------------------------- 保有者の表示 ----------------------------- #
def test_avg_price_and_percent():
    units, total, _ = market.buy_quote(2_000, 1000)
    assert abs(market.avg_price(units, total) - 1000) <= 1
    assert market.pnl_percent(1100, units, total) == pytest.approx(10.0, abs=0.2)
    assert market.pnl_percent(900, units, total) == pytest.approx(-10.0, abs=0.2)
    assert market.avg_price(0, 0) == 0 and market.pnl_percent(1000, 0, 0) == 0.0


def test_holders_are_per_server_sorted_and_limited(eco):
    a, b = eco.for_guild(G), eco.for_guild(20)
    a.crypto_buy(1, 500, 1000)
    a.crypto_buy(2, 2_000, 1000)
    a.crypto_buy(3, 1_000, 1000)
    b.crypto_buy(9, 1_000, 1000)
    assert [u for u, _, _ in a.crypto_holders()] == [2, 3, 1] and [u for u, _, _ in a.crypto_holders(2)] == [2, 3]
    assert [u for u, _, _ in b.crypto_holders()] == [9]


def test_chart_lists_holders_with_cost_price_and_percent(cog):
    cog.eco.for_guild(G).crypto_buy(U, 1_000, cog._price())
    cog.eco.market_set(time_now() + 1, int(cog._price() * 1.1), 0)
    e = cog.chart_embed("24h", guild_id=G)
    field = e.fields[0]
    assert "保有者" in field.name and f"<@{U}>" in field.value and "+" in field.value and "取得" in field.value
    assert not cog.chart_embed("24h", guild_id=20).fields                      # 別サーバーの保有者は載らない
    assert not cog.chart_embed("24h").fields


def time_now():
    import time
    return time.time()


def test_sell_shows_position_change_and_remaining(cog):
    run(MarketCog.buy.callback(cog, It(), 2_000))
    have, _ = cog.eco.for_guild(G).crypto_holding(U)
    cog.eco.market_set(time_now() + 1, int(cog._price() * 1.2), 0)
    it = It()
    run(MarketCog.sell.callback(cog, it, have / 2000))                 # 半分売る
    text = it.sent[-1]["embed"].description
    assert "+" in text and "%" in text and "取得" in text and "残りの持ち分" in text and "評価損益" in text
    it = It()
    run(MarketCog.sell.callback(cog, it, None))
    assert "全部売りました" in it.sent[-1]["embed"].description


# ----------------------------- 順序・端数 ----------------------------- #
def test_latest_price_is_the_last_recorded_even_if_the_clock_went_back(eco):
    eco.market_set(5_000.0, 500, 0)
    eco.market_set(100.0, 900, 0)                    # 時計が戻って、あとの行のほうが ts が小さい
    assert eco.market_latest()[1] == 900


def test_selling_one_unit_at_a_time_keeps_cost_basis_sane(eco):
    a = eco.for_guild(G)
    r = a.crypto_buy(U, 1_000, 1001.0)
    for _ in range(r.units - 1):
        a.crypto_sell(U, 1, 1001.0)
    units, cost = a.crypto_holding(U)
    assert units == 1 and cost <= 2                      # 取得額が不自然に残らない
    assert abs(market.pnl_percent(1001.0, 1, cost)) < 100 or cost == 1


def test_dust_can_be_sold_out(eco):
    a = eco.for_guild(G)
    a.credit(U, 1, "テスト")
    eco._conn.execute("INSERT INTO crypto (guild_id, user_id, units, cost) VALUES (?, ?, 9, 10)", (G, U))
    eco._conn.commit()
    assert not a.crypto_sell(U, 5, 100.0).ok            # 一部だけでは、受け取りが1ペリカ未満で断られる
    assert a.crypto_sell(U, None, 100.0).ok and a.crypto_holding(U) == (0, 0)      # 全部なら、端数を片付けられる


def test_fractional_prices_round_trip_never_gains(eco):
    rng = random.Random(8)
    for _ in range(2_000):
        price = round(rng.uniform(20, 3_000), 2)
        amount = rng.randint(10, 100_000)
        units, total, _ = market.buy_quote(amount, price)
        if units:
            assert total <= amount and market.sell_quote(units, price)[0] <= total


def test_price_format_switches_to_decimals_below_100():
    assert market.fmt(1234.5) == "1,234" or market.fmt(1234.5) == "1,235"
    assert market.fmt(45.678) == "45.68" and market.pel(45.678) == "45.68ペリカ"
