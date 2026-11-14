"""証券（4銘柄）: 価格の動き・売買の計算・チャート・コマンド。"""

import asyncio
import itertools
import math
import random
import time
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
def start_prices(**override):
    prices = {s.key: s.start for s in market.SYMBOLS}
    prices.update(override)
    return prices


def test_prices_stay_in_range_and_move():
    rng, prices, seen = random.Random(3), start_prices(), {k: set() for k in market.BY_KEY}
    for _ in range(20_000):
        prices, ev = market.next_prices(prices, rng)
        assert ev in (-1, 0, 1)
        for k, p in prices.items():
            assert market.MIN_PRICE <= p <= market.MAX_PRICE
            seen[k].add(p)
    assert all(len(v) > 100 for v in seen.values())


@pytest.mark.parametrize("key", [s.key for s in market.SYMBOLS])
def test_one_step_expectation_is_unbiased_for_every_symbol(key):
    """どの銘柄でも、1分後の価格の平均が変わらない（偏りがあると、持つだけ・安く買うだけで儲かってしまう）。"""
    rng, n, sym = random.Random(11), 300_000, market.BY_KEY[key]
    start = start_prices()
    total = sum(market.next_prices(start, rng)[0][key] for _ in range(n))
    assert abs(total / n / sym.start - 1) < 5 * (sym.sigma + 0.002) / n**0.5


@pytest.mark.parametrize("start", [25.0, 100.0, 1000.0, 5000.0])
def test_one_step_expectation_is_unbiased_at_every_price_level(start):
    rng, n = random.Random(11), 300_000
    base = start_prices(cs=start)
    mean = sum(market.next_prices(base, rng)[0]["cs"] for _ in range(n)) / n
    assert abs(mean / start - 1) < 5e-5 * (1 + 25 / start)  # 小さい価格は丸め（小数2桁）のぶん、許容を少し広げる


def test_event_part_is_exactly_symmetric():
    """急騰と急落は ×(1+x) / ×(1-x) を同じ確率で。期待値はちょうど1。"""
    lo, hi = market.EVENT_RANGE
    up = sum(1 + (lo + (hi - lo) * (i + 0.5) / 1000) for i in range(1000)) / 1000
    down = sum(1 - (lo + (hi - lo) * (i + 0.5) / 1000) for i in range(1000)) / 1000
    assert abs((up + down) / 2 - 1) < 1e-12


def test_low_prices_keep_moving():
    """低い価格で、丸めのせいで動きが止まらない。"""
    rng, same = random.Random(2), 0
    for _ in range(20_000):
        new, _ = market.next_prices(start_prices(cs=25.0), rng)
        same += new["cs"] == 25.0
    assert same / 20_000 < 0.2


def test_events_happen_sometimes():
    rng, p, events = random.Random(5), start_prices(), 0
    for _ in range(6_000):
        p, ev = market.next_prices(p, rng)
        events += bool(ev)
    assert 3 <= events <= 30


# ----------------------------- 売買の計算 ----------------------------- #
@pytest.mark.parametrize("amount,price", [(100, 1000), (5_000, 1000), (10, 20), (99_999, 777), (3_000, 50_000)])
def test_buy_quote_never_exceeds_the_amount_and_is_maximal(amount, price):
    units, total, fee = market.buy_quote(amount, price)
    assert total <= amount and fee >= 0
    if units:
        gross = -(-(units + 1) * price // market.UNIT)
        assert gross + -(-int(gross * market.FEE_RATE * 1000) // 1000) > amount  # もう1口は買えない


def test_buy_then_sell_immediately_never_gains():
    for amount in (100, 1_234, 50_000):
        units, total, _ = market.buy_quote(amount, 1000)
        proceeds, _ = market.sell_quote(units, 1000)
        assert proceeds <= total  # 手数料なしでも、端数の切り捨てで得はしない


def test_too_small_amount_buys_nothing():
    assert market.buy_quote(10, 50_000) == (0, 0, 0)


# ----------------------------- 売買と記録 ----------------------------- #
def ledger_sum(eco, guild, user):
    return eco._conn.execute(
        "SELECT COALESCE(SUM(delta), 0) FROM ledger WHERE guild_id=? AND user_id=?", (guild, user)
    ).fetchone()[0]


def test_buy_and_sell_move_balance_and_ledger_consistently(eco):
    a = eco.for_guild(G)
    a.balance(U)
    r = a.crypto_buy(U, 3_000, 1000)
    assert r.ok and a.balance(U) == INITIAL_BALANCE - r.money and a.crypto_holding(U) == (r.units, r.money)
    s = a.crypto_sell(U, None, 1100)
    assert s.ok and a.crypto_holding(U) == (0, 0) and s.profit == s.money - r.money
    assert a.balance(U) == INITIAL_BALANCE - r.money + s.money
    assert ledger_sum(eco, G, U) == a.balance(U)  # 履歴の合計と残高が一致する


def test_partial_sell_keeps_cost_basis_proportional(eco):
    a = eco.for_guild(G)
    r = a.crypto_buy(U, 2_000, 1000)
    half = r.units // 2
    s = a.crypto_sell(U, half, 1000)
    units, cost = a.crypto_holding(U)
    assert s.ok and units == r.units - half and cost == r.money - r.money * half // r.units


def test_cannot_oversell_or_buy_beyond_balance(eco):
    a = eco.for_guild(G)
    assert not a.crypto_sell(U, 1, 1000).ok  # 持っていない
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
_MESSAGE_IDS = itertools.count(1001)  # メッセージIDは、どの発言でも重ならない


class FakeChannel:
    """メッセージの削除を記録するだけのチャンネル。"""

    shared = {}

    def __init__(self, cid):
        self.id = cid
        self.deleted = FakeChannel.shared.setdefault(cid, [])
        self.fail = False

    def get_partial_message(self, mid):
        channel = self

        class P:
            async def delete(self):
                if channel.fail:
                    raise discord.NotFound(SimpleNamespace(status=404, reason="x"), "gone")
                channel.deleted.append(mid)

        return P()


class It:
    def __init__(self, uid=U, gid=G):
        self.user = SimpleNamespace(id=uid, display_name="テスト")
        self.guild = SimpleNamespace(id=gid)
        self.guild_id = gid
        self.sent = []
        self.channel = FakeChannel(5)
        self.response = SimpleNamespace(send_message=self._send, defer=self._defer)
        self.followup = SimpleNamespace(send=self._send)

    async def _send(self, *a, **kw):
        self.sent.append(kw)
        self._next_id = next(_MESSAGE_IDS)

    async def original_response(self):
        return SimpleNamespace(id=self._next_id, channel=self.channel)

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
    run(cog._buy(it, "cs", 1_000))
    assert "買いました" in it.sent[-1]["embed"].title
    it = It()
    run(MarketCog.holdings.callback(cog, it))
    assert "保有" in it.sent[-1]["embed"].description
    it = It()
    run(cog._sell(it, "cs", None, None))
    assert "売りました" in it.sent[-1]["embed"].title
    assert cog.bot.sessions.try_start(U) is None  # 取引のあと、プレイ中のままにならない


def test_trading_is_refused_during_a_game(cog):
    assert cog.bot.sessions.try_start(U) is None
    it = It()
    run(cog._buy(it, "cs", 1_000))
    assert "取引できません" in it.sent[-1]["embed"].title
    assert cog.bot.economy.for_guild(G).crypto_holding(U) == (0, 0)


def test_price_chart_and_step(cog):
    it = It()
    cog.step_prices()
    run(MarketCog.board.callback(cog, it, None, None))
    assert "1枚" in it.sent[-1]["embed"].description and "TEIO重工" in it.sent[-1]["embed"].description
    it = It()
    run(MarketCog.board.callback(cog, it, SimpleNamespace(value="cs"), None))
    assert "```" in it.sent[-1]["embed"].description and "24時間" in it.sent[-1]["embed"].title


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
    cog.bot.economy.for_guild(G).crypto_buy(U, 1_000, cog._price())
    cog.bot.economy.market_set(time_now() + 1, int(cog._price() * 1.1), 0)
    e = cog.chart_embed("24h", guild_id=G)
    field = next(f for f in e.fields if "保有者" in f.name)
    assert "保有者" in field.name and f"<@{U}>" in field.value and "+" in field.value and "取得" in field.value
    assert not any(
        "保有者" in f.name for f in cog.chart_embed("24h", guild_id=20).fields
    )  # 別サーバーの保有者は載らない
    assert not any("保有者" in f.name for f in cog.chart_embed("24h").fields)


def time_now():
    import time

    return time.time()


def test_sell_shows_position_change_and_remaining(cog):
    run(cog._buy(It(), "cs", 2_000))
    have, _ = cog.bot.economy.for_guild(G).crypto_holding(U)
    cog.bot.economy.market_set(time_now() + 1, int(cog._price() * 1.2), 0)
    it = It()
    run(cog._sell(it, "cs", have / 2000, None))  # 半分売る
    text = it.sent[-1]["embed"].description
    assert "+" in text and "%" in text and "取得" in text and "残りの持ち分" in text and "評価損益" in text
    it = It()
    run(cog._sell(it, "cs", None, None))
    assert "全部売りました" in it.sent[-1]["embed"].description


# ----------------------------- 順序・端数 ----------------------------- #
def test_latest_price_is_the_last_recorded_even_if_the_clock_went_back(eco):
    eco.market_set(5_000.0, 500, 0)
    eco.market_set(100.0, 900, 0)  # 時計が戻って、あとの行のほうが ts が小さい
    assert eco.market_latest()[1] == 900


def test_selling_one_unit_at_a_time_keeps_cost_basis_sane(eco):
    a = eco.for_guild(G)
    r = a.crypto_buy(U, 1_000, 1001.0)
    for _ in range(r.units - 1):
        a.crypto_sell(U, 1, 1001.0)
    units, cost = a.crypto_holding(U)
    assert units == 1 and cost <= 2  # 取得額が不自然に残らない
    assert abs(market.pnl_percent(1001.0, 1, cost)) < 100 or cost == 1


def test_dust_can_be_sold_out(eco):
    a = eco.for_guild(G)
    a.credit(U, 1, "テスト")
    eco._conn.execute("INSERT INTO crypto (guild_id, user_id, units, cost) VALUES (?, ?, 9, 10)", (G, U))
    eco._conn.commit()
    assert not a.crypto_sell(U, 5, 100.0).ok  # 一部だけでは、受け取りが1ペリカ未満で断られる
    assert a.crypto_sell(U, None, 100.0).ok and a.crypto_holding(U) == (0, 0)  # 全部なら、端数を片付けられる


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


# ----------------------------- チャートは1チャンネルに1つ ----------------------------- #
def test_new_chart_replaces_the_previous_one_in_the_same_channel_only(cog):
    FakeChannel.shared.clear()
    a1, a2, b1 = It(), It(), It()
    b1.channel = FakeChannel(6)
    run(MarketCog.board.callback(cog, a1, SimpleNamespace(value="cs"), None))
    assert FakeChannel.shared[5] == []  # 最初のチャートは、何も消さない
    run(MarketCog.board.callback(cog, b1, SimpleNamespace(value="cs"), None))  # 別のチャンネルのチャートは、触らない
    assert FakeChannel.shared[5] == [] and FakeChannel.shared[6] == []
    run(MarketCog.board.callback(cog, a2, SimpleNamespace(value="cs"), None))  # 同じチャンネルで、もう一度
    assert FakeChannel.shared[5] == [a1._next_id]  # 前のチャートだけが消える（別チャンネルのは残る）


def test_chart_command_still_works_when_the_old_chart_is_already_gone(cog):
    FakeChannel.shared.clear()
    it1, it2 = It(), It()
    run(MarketCog.board.callback(cog, it1, SimpleNamespace(value="cs"), None))
    it2.channel.fail = True  # 前のチャートはすでに消えている
    run(MarketCog.board.callback(cog, it2, SimpleNamespace(value="cs"), None))
    assert "```" in it2.sent[-1]["embed"].description  # 失敗しても、チャート自体は出る


# ----------------------------- チャートの自動更新（出してから60分だけ） ----------------------------- #
class EditChan:
    def __init__(self, cid=5):
        self.id, self.edits, self.deleted, self.error = cid, [], [], None

    def get_partial_message(self, mid):
        chan = self

        class P:
            async def edit(self, **kw):
                if chan.error:
                    raise chan.error
                chan.edits.append((mid, kw["embed"]))

            async def delete(self):
                chan.deleted.append(mid)

        return P()


def live_env(cog, channel, guilds=(G,)):
    cog.bot.get_channel = lambda cid: channel if cid == channel.id else None
    cog.bot.get_guild = lambda gid: SimpleNamespace(id=gid) if gid in guilds else None


def test_chart_command_registers_a_live_chart_that_updates_then_ends(cog):
    from cogs import market_cog

    ch = EditChan(5)
    live_env(cog, ch)
    it = It()
    run(MarketCog.board.callback(cog, it, SimpleNamespace(value="cs"), None))
    first = it.sent[-1]["embed"]
    assert "自動で更新（あと" in first.footer.text
    post = cog.bot.economy.live_chart_posts()[0]
    assert post["channel_id"] == 5 and post["guild_id"] == G and post["period"] == "24h"
    now = post["ts"] + 120
    run(cog._refresh_post(post, now, {}))  # 2分後: 更新される（まだ続く）
    assert (
        len(ch.edits) == 1 and "自動で更新（あと" in ch.edits[0][1].footer.text and cog.bot.economy.live_chart_posts()
    )
    end = post["ts"] + market_cog.LIVE_MINUTES * 60
    run(cog._refresh_post(post, end, {}))  # 60分後: 最後に1回書き換えて、終わる
    assert "更新は終了しました" in ch.edits[-1][1].footer.text and cog.bot.economy.live_chart_posts() == []
    assert len(ch.edits) == 2  # 終わったあとは、もう更新しない


def test_tick_updates_only_live_posts_and_shares_one_embed_per_view(cog, monkeypatch):
    chans = {5: EditChan(5), 6: EditChan(6), 7: EditChan(7)}
    cog.bot.get_channel = lambda cid: chans.get(cid)
    cog.bot.get_guild = lambda gid: SimpleNamespace(id=gid)
    now = time.time()
    for cid in (5, 6):
        cog.bot.economy.swap_chart_post(cid, 100 + cid, G, "24h", now - 60)  # 更新中の2つ（同じサーバー・同じ期間）
    cog.bot.economy.swap_chart_post(7, 107, G, "24h", now - 60)
    cog.bot.economy.end_chart_post(7)  # 終わったもの
    built = []
    orig = cog.chart_embed
    monkeypatch.setattr(cog, "chart_embed", lambda *a, **kw: built.append(1) or orig(*a, **kw))
    cache = {}
    for post in cog.bot.economy.live_chart_posts():
        run(cog._refresh_post(post, now, cache))
    assert len(chans[5].edits) == 1 and len(chans[6].edits) == 1 and chans[7].edits == []
    assert len(built) == 1  # 同じ表示は、1回だけ作って使い回す


def test_deleted_chart_stops_being_updated_and_errors_do_not_stop_others(cog):
    ch = EditChan(5)
    live_env(cog, ch)
    cog.bot.economy.swap_chart_post(5, 105, G, "24h", time.time())
    post = cog.bot.economy.live_chart_posts()[0]
    ch.error = discord.NotFound(SimpleNamespace(status=404, reason="x"), "gone")  # 誰かがチャートを消した
    run(cog._refresh_post(post, time.time(), {}))
    assert cog.bot.economy.live_chart_posts() == []
    cog.bot.economy.swap_chart_post(5, 106, G, "24h", time.time())
    ch.error = discord.Forbidden(
        SimpleNamespace(status=403, reason="x"), "no"
    )  # 権限が無い: 例外にせず、次の周期で再試行
    run(cog._refresh_post(cog.bot.economy.live_chart_posts()[0], time.time(), {}))
    assert cog.bot.economy.live_chart_posts()


def test_legacy_or_unknown_rows_end_without_crashing(cog):
    ch = EditChan(5)
    live_env(cog, ch)
    cog.bot.economy.swap_chart_post(5, 105, 0, "24h", 0)  # 古い版で作られた行（サーバーが分からない）
    run(cog._refresh_post(cog.bot.economy.live_chart_posts()[0], time.time(), {}))
    assert ch.edits == [] and cog.bot.economy.live_chart_posts() == []


def test_new_chart_in_the_same_channel_replaces_the_live_one(cog):
    FakeChannel.shared.clear()
    live_env(cog, EditChan(5))
    a, b = It(), It()
    run(MarketCog.board.callback(cog, a, SimpleNamespace(value="cs"), None))
    run(MarketCog.board.callback(cog, b, SimpleNamespace(value="cs"), None))
    live = cog.bot.economy.live_chart_posts()
    assert len(live) == 1 and live[0]["message_id"] == b._next_id  # 更新の対象は、新しいチャートだけ
    assert FakeChannel.shared[5] == [a._next_id]  # 前のチャートは消える


def test_old_chart_board_is_removed_once(cog):
    ch = EditChan(5)
    live_env(cog, ch)
    cog.bot.economy._conn.execute(
        "INSERT INTO chart_boards (guild_id, channel_id, message_id) VALUES (?, 5, 777)", (G,)
    )
    cog.bot.economy._conn.commit()
    run(cog._retire_old_boards())
    assert ch.deleted == [777] and cog.bot.economy.list_chart_boards() == []  # 掲示のメッセージを消し、設定も捨てる
    run(cog._retire_old_boards())
    assert ch.deleted == [777]  # 2回目は何もしない


def test_chart_posts_table_from_the_previous_version_is_upgraded(tmp_path):
    path = str(tmp_path / "old.db")
    e = Economy(path, strict=True)
    e._conn.execute("DROP TABLE chart_posts")
    e._conn.execute("CREATE TABLE chart_posts (channel_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL)")
    e._conn.execute("INSERT INTO chart_posts VALUES (5, 99)")
    e._conn.commit()
    e._conn.close()
    again = Economy(path, strict=True)
    row = again.live_chart_posts()[0]
    assert row["channel_id"] == 5 and row["guild_id"] == 0 and row["period"] == "24h"


# ----------------------------- 空売り ----------------------------- #
def set_price(eco, price):
    eco.market_set(time.time() + set_price.n, price, 0)
    set_price.n += 1


set_price.n = 1


def test_short_profits_when_price_falls_and_returns_margin(eco):
    a = eco.for_guild(G)
    set_price(eco, 100.0)
    res = a.short_open(U, 10_000, 100.0, 2)  # 10枚 = 1,000ペリカ分、2倍 → 担保500
    assert res.ok and res.money == 500 and a.balance(U) == INITIAL_BALANCE - 500
    close = a.short_close(U, None, 80.0)  # 20%下がった: 利益 200
    assert close.ok and close.profit == 200 and close.money == 700
    assert a.balance(U) == INITIAL_BALANCE + 200 and a.short_position(U) == (0, 0.0, 0)


def test_short_partial_close_is_proportional_and_loss_beyond_margin_is_collected(eco):
    a = eco.for_guild(G)
    assert a.short_open(U, 10_000, 100.0, 1).ok  # 担保1000
    half = a.short_close(U, 5_000, 120.0)  # 半分を、20%上がったところで決済: 損失100
    assert half.profit == -100 and a.short_position(U)[0] == 5_000 and a.short_position(U)[2] == 500
    last = a.short_close(
        U, None, 500.0
    )  # 急騰: 担保500に対して損失2,000。戻りは0で、超えた1,500は手元の現金から取り立てる
    assert last.money == 0 and last.profit == -2000
    assert a.balance(U) == INITIAL_BALANCE - 1000 + 400 - 1500 and a.debt(U) == 0


def test_short_validation(eco):
    a = eco.for_guild(G)
    assert not a.short_open(U, 10_000, 100.0, 4).ok  # 選べない倍率
    assert not a.short_open(U, 1, 100.0, 1).ok  # 担保が少なすぎる
    assert not a.short_open(U, 100_000, 100.0, 1).ok  # 担保が残高より多い（10,000ペリカ必要）
    assert not a.short_close(U, None, 100.0).ok  # 建てていない
    a.balance(U)
    a._conn.execute("UPDATE users SET debt=100 WHERE user_id=?", (U,))
    assert "借金" in a.short_open(U, 10_000, 100.0, 1).reason  # 借金中は建てられない


def test_losscut_closes_accounts_whose_maintenance_ratio_falls_below_100_percent(eco, monkeypatch):
    monkeypatch.setattr(
        market, "RISK_K", 20.0 * 0.004 / market.CS.sigma
    )  # CS の σ が 0.004 だった頃と、同じ大きさの必要証拠金
    a = eco.for_guild(G)
    a.short_open(U, 10_000, 100.0, 5)  # 担保200・必要証拠金 80。+12% を超えて有効証拠金が 80 を割るとロスカット
    a.short_open(2, 10_000, 100.0, 1)  # 担保1000。ずっと余裕がある
    assert eco.margin_losscut({"cs": 111.0}) == []  # 損失110 → 有効90 ÷ 必要89 ≒ 101%
    done = eco.margin_losscut({"cs": 113.0})
    assert done == [(G, U, "cs", "short", 130)] and a.short_position(U)[0] == 0 and a.short_position(2)[0] == 10_000
    assert a.balance(U) == INITIAL_BALANCE - 200 + 70  # 担保の残り70が戻る


def test_short_counts_in_ranking_and_payout_repays_debt(eco):
    a = eco.for_guild(G)
    set_price(eco, 100.0)
    a.short_open(U, 10_000, 100.0, 2)
    assert dict((u, v) for _, u, v in a.top(5))[U] == INITIAL_BALANCE  # 担保 + 含み損益（0）で、総資産は変わらない
    set_price(eco, 90.0)
    assert dict((u, v) for _, u, v in a.top(5))[U] == INITIAL_BALANCE + 100
    a._conn.execute("UPDATE users SET debt=300 WHERE user_id=?", (U,))  # 建てたあとに借金ができても
    a.short_close(U, None, 90.0)  # 決済で戻るのは、もともと自分のお金なので、天引きされない
    assert a.debt(U) == 300
    assert a.repay_debt(U)[0] and a.debt(U) == 0


def test_short_commands(cog):
    it = It()
    run(cog._short(it, "cs", 1.0, None, 2))
    assert "空売りしました" in it.sent[-1]["embed"].title
    assert cog.bot.economy.for_guild(G).short_position(U)[0] == 1_000
    it = It()
    run(MarketCog.holdings.callback(cog, it))
    assert "空売り" in it.sent[-1]["embed"].description and "維持率" in it.sent[-1]["embed"].description
    it = It()
    run(cog._cover(it, "cs", None, None))
    assert "買い戻しました" in it.sent[-1]["embed"].title and cog.bot.economy.for_guild(G).short_position(U)[0] == 0
    it = It()
    run(cog._cover(it, "cs", None, None))
    assert "買い戻せません" in it.sent[-1]["embed"].title  # 建てていない
    it = It()
    run(cog._cover(it, "cs", 1.0, discord.app_commands.Choice(name="半分", value="half")))
    assert "どちらか1つ" in it.sent[-1]["embed"].title


def test_chart_lists_short_sellers_with_profit_when_price_falls(cog):
    price = cog._price()
    cog.bot.economy.for_guild(G).short_open(U, 2_000, price, 2)
    cog.bot.economy.market_set(time_now() + 1, round(price * 0.9, 2), 0)  # 10%下がった: 空売りは利益
    e = cog.chart_embed("24h", guild_id=G)
    field = next(f for f in e.fields if "空売り" in f.name)
    assert f"<@{U}>" in field.value and "2.000枚" in field.value and "+10.0%" in field.value and "建値" in field.value
    assert not any("空売り" in f.name for f in cog.chart_embed("24h", guild_id=20).fields)  # 別サーバーは載らない


def test_short_can_be_sized_by_amount_or_lots_but_not_both(cog):
    price = cog._price()
    it = It()
    run(cog._short(it, "cs", None, 1_000, 2))  # 金額1,000ペリカ分
    units = cog.bot.economy.for_guild(G).short_position(U)[0]
    assert "空売りしました" in it.sent[-1]["embed"].title and units == int(1_000 * 1_000 // price)
    it = It()
    run(cog._short(it, "cs", 1.0, 1_000, None))
    assert "どちらか1つ" in it.sent[-1]["embed"].title
    it = It()
    run(cog._short(it, "cs", None, None, None))
    assert "どちらか1つ" in it.sent[-1]["embed"].title


# ----------------------------- 値動きの大きさと値幅制限 ----------------------------- #
def test_daily_volatility_is_realistic_and_a_halving_in_a_day_does_not_happen():
    rng = random.Random(5)
    days = {k: [] for k in ("cs", "teio", "numa", "gold")}
    for _ in range(150):  # 1日（1,440分）を、150回
        prices = {s.key: s.start for s in market.SYMBOLS}
        start = dict(prices)
        for _ in range(1440):
            prices, _ = market.next_prices(prices, rng)
        for k in days:
            days[k].append(math.log(prices[k] / start[k]))
    for k, want in (("gold", 0.015), ("teio", 0.027), ("cs", 0.046), ("numa", 0.083)):
        sd = (sum(x * x for x in days[k]) / len(days[k])) ** 0.5
        assert want * 0.6 < sd < want * 1.7, (k, sd)  # 有事ぶんの上振れを見込んだ幅
    assert min(min(v) for v in days.values()) > math.log(0.5)  # 1日で半値になった試行はない


def test_limit_to_day_clamps_each_symbol_to_plus_minus_the_limit_of_the_reference():
    ref = {"cs": 1000.0, "numa": 400.0}
    out = market.limit_to_day({"cs": 600.0, "numa": 600.0, "gold": 5.0}, ref, 0.30)
    assert out["cs"] == pytest.approx(700.0) and out["numa"] == pytest.approx(520.0)  # 下は −30%、上は +30%
    assert out["gold"] == 5.0  # 基準のない銘柄は、そのまま
    inside = market.limit_to_day({"cs": 1100.0}, ref, 0.30)
    assert inside["cs"] == 1100.0


def test_step_prices_never_drops_below_the_limit_from_24_hours_ago(eco):
    cog = MarketCog(SimpleNamespace(economy=eco, sessions=Sessions(), config={}))
    now = time.time()
    for sym in market.SYMBOLS:
        eco.market_set(now - 86_000, sym.start, 0, sym.key)  # 約24時間前
        eco.market_set(now - 5, sym.start * 0.71, 0, sym.key)  # すでに −29% まで下がっている
    cog.step_prices()
    for sym in market.SYMBOLS:
        assert eco.market_latest(sym.key)[1] >= sym.start * 0.70 - 0.01


# ----------------------------- 期待成長率（drift） ----------------------------- #
def test_drift_is_small_positive_and_below_the_normal_bond_yield():
    bond_per_day = 0.04 / 3  # 通常債: 72時間で +4%
    for sym in market.SYMBOLS:
        assert 0 < sym.drift < bond_per_day
    assert market.drift_per_minute(market.CS, market.CS.start) == pytest.approx(market.CS.drift / 1440)
    assert market.drift_per_minute(market.CS, market.CS.start / 2) == pytest.approx(
        market.CS.drift / 1440
    )  # 安いときは、弱めない


def test_drift_fades_out_as_the_price_rises_toward_the_band_and_stops_there():
    cs = market.CS
    full = market.drift_per_minute(cs, cs.start)
    mid = market.drift_per_minute(cs, cs.start * 2)
    top = market.drift_per_minute(cs, cs.start * market.DRIFT_BAND)
    assert full > mid > 0 and top == 0.0  # 開始価格の DRIFT_BAND 倍で、成長しなくなる
    assert market.drift_per_minute(cs, cs.start * market.DRIFT_BAND * 3) == 0.0


def test_a_step_without_any_shock_moves_the_price_by_exactly_the_drift(monkeypatch):
    class Calm:
        @staticmethod
        def gauss(mu, sigma):
            return 0.0

        @staticmethod
        def random():
            return 1.0  # 有事なし

    prices = {s.key: s.start for s in market.SYMBOLS}
    with_drift, _ = market.next_prices(prices, Calm)
    monkeypatch.setattr(market, "drift_per_minute", lambda sym, price: 0.0)
    without, _ = market.next_prices(prices, Calm)
    for sym in market.SYMBOLS:
        assert with_drift[sym.key] / without[sym.key] == pytest.approx(math.exp(sym.drift / 1440), rel=1e-4)


# ----------------------------- 利益への税（キャピタルゲイン税） ----------------------------- #
def test_capital_gains_tax_is_progressive_and_zero_for_small_profits():
    tax = market.capital_gains_tax
    assert tax(0) == tax(-500) == tax(10_000) == 0  # 損失と、1万までは非課税
    assert tax(20_000) == 300  # 1万超の 10,000 に 3%
    assert tax(100_000) == int(90_000 * 0.03)  # 2,700
    assert tax(200_000) == 2_700 + int(100_000 * 0.07)  # 7,000のぶん
    assert tax(1_000_000) == 2_700 + int(900_000 * 0.07)
    assert tax(3_000_000) == 2_700 + 63_000 + int(2_000_000 * 0.12)  # 最高税率12%
    assert tax(50_000) < tax(60_000) < tax(500_000)  # 利益が増えれば、税も増える


def test_a_big_spot_gain_is_taxed_and_the_tax_goes_to_depositors(eco):
    a = eco.for_guild(G)
    a.credit(U, 500_000, "テスト")
    a.credit(2, 20_000, "テスト")
    a.bank_deposit(2, 20_000, 1.0)  # 預金者
    r = a.crypto_buy(U, 200_000, 1000)
    before_deposit = a.deposit_of(2)
    s = a.crypto_sell(U, None, 1300)  # 約 +30%
    assert s.ok and s.profit > 50_000
    assert s.tax == market.capital_gains_tax(s.profit) > 0
    assert a.balance(U) == INITIAL_BALANCE + 500_000 - r.money + s.money  # 受け取りは、税を引いた額
    assert a.deposit_of(2) == before_deposit + s.tax  # 預金者がひとりなので、税が全額、預金に入る
    assert any(
        "キャピタルゲイン税" in x["reason"]
        for x in eco._conn.execute("SELECT reason FROM ledger WHERE user_id=?", (U,))
    )


def test_small_gains_and_losses_are_not_taxed_on_spot_and_margin(eco):
    a = eco.for_guild(G)
    a.credit(U, 100_000, "テスト")
    a.crypto_buy(U, 10_000, 1000)
    win = a.crypto_sell(U, None, 1100)  # 利益 約1,000
    assert win.ok and 0 < win.profit <= 10_000 and win.tax == 0  # 1万ペリカまでの利益は、非課税
    a.crypto_buy(U, 10_000, 1000)
    lose = a.crypto_sell(U, None, 900)
    assert lose.profit < 0 and lose.tax == 0


def test_a_big_margin_gain_is_taxed_when_closed_by_the_player(eco):
    a = eco.for_guild(G)
    a.credit(U, 600_000, "テスト")
    assert a.long_open(U, 400_000, 1000, 5, "cs").ok  # 40万ペリカぶんの信用買い（担保8万）
    margin = a.margin_position(U, "cs", "long")[2]
    before = a.balance(U)
    res = a.long_close(U, None, 1300, "cs")  # 価格 +30%: 利益 約12万
    assert res.ok and res.profit > 100_000
    assert res.tax == market.capital_gains_tax(res.profit) > 0
    assert a.balance(U) == before + margin + res.profit - res.tax  # 担保 + 利益 - 税


def test_forced_sales_do_not_pay_the_gains_tax(eco):
    a = eco.for_guild(G)
    a.credit(U, 500_000, "テスト")
    a.crypto_buy(U, 200_000, 1000)
    with eco._lock, eco._conn:  # 強制執行などが使う売却（taxable を付けない）
        forced = a._crypto_sell_locked(U, None, 1300, "cs")
    assert forced.ok and forced.profit > 50_000 and forced.tax == 0


def test_holdings_without_buttons_does_not_pass_view_none(cog):
    """保有が無くてボタンも無いとき、view=None を渡さない（本物の discord.py は None で落ちる）。"""
    it = It()
    run(MarketCog.holdings.callback(cog, it))
    assert it.sent[-1].get("view", 0) is not None
