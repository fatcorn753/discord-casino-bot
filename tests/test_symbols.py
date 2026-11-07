"""証券の複数銘柄: 相関のある値動き・銘柄ごとの保有・旧データの移行・コマンド構成。"""

import asyncio
import math
import random
import time
from types import SimpleNamespace

import pytest

from cogs.market_cog import MarketCog
from core import market
from core.economy import INITIAL_BALANCE, Economy
from core.session import Sessions

pytestmark = pytest.mark.real_scope

G, U = 10, 1


@pytest.fixture
def eco(tmp_path):
    return Economy(str(tmp_path / "t.db"), strict=True)


def _start():
    return {s.key: s.start for s in market.SYMBOLS}


# ----------------------------- 相関のある値動き ----------------------------- #
def test_cholesky_reproduces_the_covariance_matrix():
    low, n = market.CHOLESKY, len(market.SYMBOLS)
    for i in range(n):
        for j in range(n):
            assert sum(low[i][k] * low[j][k] for k in range(n)) == pytest.approx(market.COVARIANCE[i][j], abs=1e-12)


def test_cholesky_rejects_an_impossible_correlation():
    with pytest.raises(ValueError):
        market.cholesky([[1.0, 2.0], [2.0, 1.0]])  # 相関が1を超える


def test_correlation_is_symmetric_and_defined_for_every_pair():
    for a in market.SYMBOLS:
        assert market.correlation(a.key, a.key) == 1.0
        for b in market.SYMBOLS:
            assert market.correlation(a.key, b.key) == market.correlation(b.key, a.key)


def test_simulated_returns_match_the_target_correlation_without_panic(monkeypatch):
    monkeypatch.setattr(market, "EVENT_PROB", 0.0)  # 有事を切って、通常の相関だけを見る
    rng, n = random.Random(1), 60_000
    prices, series = _start(), {k: [] for k in prices_keys()}
    for _ in range(n):
        new, _ = market.next_prices(prices, rng)
        for k in new:
            series[k].append(math.log(new[k] / prices[k]))
        prices = {k: v if 100 < v < 10_000 else market.BY_KEY[k].start for k, v in new.items()}
    for a, b, want in (("cs", "teio", 0.85), ("cs", "gold", -0.6), ("teio", "gold", -0.5)):
        assert _corr(series[a], series[b]) == pytest.approx(want, abs=0.02)


def prices_keys():
    return [s.key for s in market.SYMBOLS]


def _corr(x, y):
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sxy = sum((p - mx) * (q - my) for p, q in zip(x, y))
    return sxy / math.sqrt(sum((p - mx) ** 2 for p in x) * sum((q - my) ** 2 for q in y))


def test_panic_moves_every_symbol_the_same_way_scaled_by_volatility(monkeypatch):
    monkeypatch.setattr(market, "EVENT_PROB", 1.0)
    for seed in range(40):
        rng = random.Random(seed)
        new, event = market.next_prices(_start(), rng)
        assert event in (-1, 1)
        for s in market.SYMBOLS:
            moved = new[s.key] / s.start - 1
            if abs(moved) > 0.5 * market.EVENT_RANGE[0]:  # 通常の揺れより十分大きいとき、向きは有事の向き
                assert (moved > 0) == (event > 0), (seed, s.key)
        assert new["numa"] / market.NUMA.start != new["cs"] / market.CS.start


# ----------------------------- 相関の表示 ----------------------------- #
def test_correlation_from_history_needs_enough_aligned_points():
    hist = [(float(t), 1000 + t % 7, 0) for t in range(10)]
    assert market.correlation_from_history(hist, hist) is None  # 少なすぎる
    long = [(float(t), 1000 * math.exp(0.01 * math.sin(t)), 0) for t in range(120)]
    assert market.correlation_from_history(long, long) == pytest.approx(1.0)
    inverse = [(t, 1e6 / p, e) for t, p, e in long]
    assert market.correlation_from_history(long, inverse) == pytest.approx(-1.0)
    flat = [(float(t), 500.0, 0) for t in range(120)]
    assert market.correlation_from_history(long, flat) is None  # 動かない銘柄とは求まらない


@pytest.mark.parametrize(
    "value,word", [(0.85, "強く連動"), (0.4, "やや連動"), (0.0, "ほぼ無関係"), (-0.4, "やや逆"), (-0.7, "逆に動く")]
)
def test_describe_correlation_words(value, word):
    assert word in market.describe_correlation(value)
    assert market.describe_correlation(None) == "—"


# ----------------------------- 銘柄ごとの保有・価格・空売り ----------------------------- #
def test_prices_history_and_holdings_are_kept_per_symbol(eco):
    eco.market_set(1.0, 1100, 0, "cs")
    eco.market_set(1.0, 1300, 0, "teio")
    assert eco.market_latest("cs")[1] == 1100 and eco.market_latest("teio")[1] == 1300
    assert eco.market_latest("gold") is None and eco.latest_prices() == {"cs": 1100, "teio": 1300}
    a = eco.for_guild(G)
    a.crypto_buy(U, 1_000, 1100, "cs")
    a.crypto_buy(U, 1_500, 1300, "teio")
    assert [s for s, _, _ in a.holdings(U)] == ["cs", "teio"]
    assert a.crypto_holding(U, "gold") == (0, 0)
    assert not a.crypto_sell(U, 1, 1000, "gold").ok  # 持っていない銘柄は売れない
    assert a.crypto_sell(U, None, 1300, "teio").ok and [s for s, _, _ in a.holdings(U)] == ["cs"]
    assert [u for u, _, _ in a.crypto_holders(symbol="teio")] == []


def test_shorts_are_kept_per_symbol_and_losscut_uses_each_price(eco):
    a = eco.for_guild(G)
    assert a.short_open(U, 10_000, 100.0, 5, "cs").ok and a.short_open(U, 10_000, 100.0, 5, "gold").ok
    assert [s for s, *_ in a.short_positions(U)] == ["cs", "gold"]
    done = eco.short_losscut({"cs": 100.0, "gold": 111.0})  # goldだけ +11%
    assert done == [(G, U, 110)] and [s for s, *_ in a.short_positions(U)] == ["cs"]
    eco.market_set(1.0, 100.0, 0, "cs")
    assert a.short_equity()[U] == 200  # 残った CS の建玉の担保だけ（gold は決済済み）


def test_total_assets_count_every_symbol(eco):
    a = eco.for_guild(G)
    for k, p in (("cs", 1000.0), ("teio", 1200.0)):
        eco.market_set(1.0, p, 0, k)
    r1, r2 = a.crypto_buy(U, 1_000, 1000.0, "cs"), a.crypto_buy(U, 1_500, 1200.0, "teio")
    eco.market_set(2.0, 1100.0, 0, "cs")
    eco.market_set(2.0, 1200.0, 0, "teio")
    worth = a.crypto_values()[U]
    assert worth == market.sell_quote(r1.units, 1100.0)[0] + market.sell_quote(r2.units, 1200.0)[0]


# ----------------------------- 旧データの移行 ----------------------------- #
def test_old_single_symbol_tables_become_cs_without_losing_rows(tmp_path):
    path = str(tmp_path / "old.db")
    first = Economy(path, strict=True)
    c = first._conn
    for t in ("market_prices", "crypto", "crypto_short"):
        c.execute(f"DROP TABLE {t}")
    c.execute("CREATE TABLE market_prices (ts REAL PRIMARY KEY, price REAL NOT NULL, event INTEGER NOT NULL DEFAULT 0)")
    c.execute(
        "CREATE TABLE crypto (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, units INTEGER NOT NULL DEFAULT 0,"
        " cost INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (guild_id, user_id))"
    )
    c.execute(
        "CREATE TABLE crypto_short (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, units INTEGER NOT NULL DEFAULT 0,"
        " entry_value REAL NOT NULL DEFAULT 0, margin INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (guild_id, user_id))"
    )
    c.execute("INSERT INTO market_prices VALUES (5.0, 1234.0, 1)")
    c.execute("INSERT INTO crypto VALUES (?, ?, 7000, 7000)", (G, U))
    c.execute("INSERT INTO crypto_short VALUES (?, ?, 2000, 2000.0, 1000)", (G, U))
    c.execute("DROP TABLE chart_posts")
    c.execute(
        "CREATE TABLE chart_posts (channel_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL, guild_id INTEGER NOT NULL DEFAULT 0, period TEXT NOT NULL DEFAULT '24h', ts REAL NOT NULL DEFAULT 0)"
    )
    c.execute("INSERT INTO chart_posts (channel_id, message_id) VALUES (5, 99)")
    c.commit()
    c.close()
    again = Economy(path, strict=True)
    a = again.for_guild(G)
    assert again.market_latest("cs") == (5.0, 1234.0, 1) or tuple(again.market_latest("cs")) == (5.0, 1234.0, 1)
    assert a.crypto_holding(U) == (7000, 7000) and a.short_position(U) == (2000, 2000.0, 1000)
    assert a.holdings(U) == [("cs", 7000, 7000)]
    assert again.live_chart_posts()[0]["symbol"] == "cs"
    again._conn.close()
    third = Economy(path, strict=True)  # 2回目の起動でも壊れない（移行は1回だけ）
    assert third.for_guild(G).crypto_holding(U) == (7000, 7000)
    assert third._conn.execute("SELECT COUNT(*) FROM market_prices").fetchone()[0] == 1


# ----------------------------- コマンド ----------------------------- #
def test_securities_group_has_four_commands_and_order_offers_every_symbol_and_bond():
    assert {c.name for c in MarketCog.group.commands} == {"注文", "保有", "市況", "決済"}
    values = [c.value for c in MarketCog.order._params["symbol"].choices]
    assert values == prices_keys() + ["bond_normal", "bond_junk"]
    assert [c.value for c in MarketCog.board._params["symbol"].choices] == prices_keys()


def test_buy_other_symbol_via_command_and_holdings_list_it(eco):
    from tests.test_market import It  # 同じ疑似インタラクションを使う

    bot = SimpleNamespace(economy=eco, sessions=Sessions(cooldown=0), config={})
    cog = MarketCog(bot)
    cog._prices()
    it = It()
    asyncio.run(cog._buy(it, "gold", 2_000))
    assert "GOLD地金" in it.sent[-1]["embed"].title
    assert eco.for_guild(G).crypto_holding(U, "gold")[0] > 0 and eco.for_guild(G).crypto_holding(U, "cs") == (0, 0)
    it = It()
    asyncio.run(MarketCog.holdings.callback(cog, it))
    assert "GOLD地金" in it.sent[-1]["embed"].description
    assert eco.for_guild(G).balance(U) < INITIAL_BALANCE


def test_step_prices_records_every_symbol_at_the_same_time(eco):
    bot = SimpleNamespace(economy=eco, sessions=None, config={})
    cog = MarketCog(bot)
    new, _ = cog.step_prices()
    assert set(new) == set(prices_keys())
    stamps = {eco.market_latest(k)[0] for k in new}
    assert len(stamps) == 1 and eco.latest_prices() == new


# ----------------------------- 注文・保有・市況・決済 ----------------------------- #
def make_cog(eco):
    bot = SimpleNamespace(economy=eco, sessions=Sessions(cooldown=0), config={})
    cog = MarketCog(bot)
    cog._prices()
    return cog


def choice(value):
    return SimpleNamespace(value=value)


def test_order_command_dispatches_spot_short_and_bond(eco):
    from tests.test_market import It

    cog = make_cog(eco)
    eco.for_guild(G).credit(U, 20_000, "テスト")
    it = It()
    asyncio.run(MarketCog.order.callback(cog, it, choice("teio"), 2_000, None, None))  # 現物買い
    assert eco.for_guild(G).crypto_holding(U, "teio")[0] > 0
    it = It()
    asyncio.run(MarketCog.order.callback(cog, it, choice("gold"), 1_000, choice("short"), choice(2)))
    assert eco.for_guild(G).short_position(U, "gold")[0] > 0 and "空売り" in it.sent[-1]["embed"].title
    it = It()
    asyncio.run(MarketCog.order.callback(cog, it, choice("bond_normal"), 1_000, None, None))
    assert len(eco.for_guild(G).bond_list(U)["active"]) == 1 and "通常債" in it.sent[-1]["embed"].title


def test_order_command_refuses_nonsense_combinations(eco):
    from tests.test_market import It

    cog = make_cog(eco)
    for args in (
        (choice("bond_junk"), 1_000, choice("short"), None),  # 債券は空売りできない
        (choice("bond_junk"), 1_000, None, choice(3)),  # 債券にレバレッジなし
        (choice("cs"), 1_000, None, choice(2)),  # 現物買いにレバレッジなし
    ):
        it = It()
        asyncio.run(MarketCog.order.callback(cog, it, *args))
        assert it.sent[-1]["ephemeral"] and "❌" in it.sent[-1]["embed"].title
    assert eco.for_guild(G).holdings(U) == [] and eco.for_guild(G).bond_list(U)["active"] == []
    assert eco.for_guild(G).balance(U) == INITIAL_BALANCE  # 何も買われていない


def test_bond_below_minimum_is_refused(eco):
    from tests.test_market import It

    cog = make_cog(eco)
    it = It()
    asyncio.run(MarketCog.order.callback(cog, it, choice("bond_normal"), 500, None, None))
    assert "買えません" in it.sent[-1]["embed"].title and eco.for_guild(G).balance(U) == INITIAL_BALANCE


def test_positions_and_settle_command_close_each_kind(eco):
    from tests.test_market import It

    cog = make_cog(eco)
    a = eco.for_guild(G)
    a.credit(U, 20_000, "テスト")
    a.crypto_buy(U, 1_000, cog._price("cs"), "cs")
    a.short_open(U, 2_000, cog._price("teio"), 1, "teio")  # 確認ボタンが出ない小さい額
    a.bond_buy(U, "normal", 1_000, time.time())
    a.bond_buy(U, "junk", 1_000, time.time())  # ジャンク債は解約できない: 候補に出ない
    codes = [c for c, _ in cog.positions(U, G)]
    bond_id = next(c for c in codes if c.startswith("bond:"))
    assert codes[:2] == ["spot:cs", "short:teio"] and len(codes) == 3
    for code in ("spot:cs", "short:teio", bond_id):
        it = It()
        asyncio.run(MarketCog.settle.callback(cog, it, code, None))
        assert "❌" not in it.sent[-1]["embed"].title, code
    assert a.holdings(U) == [] and a.short_positions(U) == []
    assert [r["kind"] for r in a.bond_list(U)["active"]] == ["junk"]
    it = It()
    asyncio.run(MarketCog.settle.callback(cog, it, "bond:99999", None))  # 無い番号
    assert "解約できません" in it.sent[-1]["embed"].title
    it = It()
    asyncio.run(MarketCog.settle.callback(cog, it, "なにか", None))
    assert "決済できません" in it.sent[-1]["embed"].title


def test_settle_half_of_a_spot_position(eco):
    from tests.test_market import It

    cog = make_cog(eco)
    a = eco.for_guild(G)
    a.crypto_buy(U, 2_000, cog._price("cs"), "cs")
    have = a.crypto_holding(U, "cs")[0]
    asyncio.run(MarketCog.settle.callback(cog, It(), "spot:cs", choice("half")))
    assert a.crypto_holding(U, "cs")[0] == have - have // 2


def test_holdings_screen_lists_everything_with_a_button_per_position(eco):
    from tests.test_market import It

    cog = make_cog(eco)
    a = eco.for_guild(G)
    a.crypto_buy(U, 1_000, cog._price("gold"), "gold")
    a.bond_buy(U, "normal", 1_000, time.time())
    it = It()
    asyncio.run(MarketCog.holdings.callback(cog, it))
    text = it.sent[-1]["embed"].description
    assert "GOLD地金" in text and "債券" in text and "通常債" in text
    view = it.sent[-1]["view"]
    assert [b.label for b in view.children] == [
        "🥇 GOLD地金 全売却",
        f"🏦 通常債 #{a.bond_list(U)['active'][0]['id']} 解約",
    ]
    it = It()
    asyncio.run(view.children[0].callback(it))  # ボタンで、全部売る
    assert a.holdings(U) == [] and "売りました" in it.sent[-1]["embed"].title


def test_holdings_view_only_works_for_the_owner(eco):
    from cogs.market_cog import HoldingsView, MarketView

    cog = make_cog(eco)
    sent = []

    class Resp:
        async def send_message(self, *a, **kw):
            sent.append((a, kw))

    stranger = SimpleNamespace(user=SimpleNamespace(id=999), response=Resp())
    for view in (HoldingsView(cog, U, G), MarketView(cog, U, G)):
        assert asyncio.run(view.interaction_check(stranger)) is False
        assert asyncio.run(view.interaction_check(SimpleNamespace(user=SimpleNamespace(id=U), response=Resp()))) is True
    assert len(sent) == 2


def test_empty_holdings_has_no_buttons(eco):
    from tests.test_market import It

    cog = make_cog(eco)
    it = It()
    asyncio.run(MarketCog.holdings.callback(cog, it))
    assert "まだ持っていません" in it.sent[-1]["embed"].description and it.sent[-1]["view"] is None


def test_board_without_symbol_shows_summary_with_switcher_and_with_symbol_posts_a_live_chart(eco):
    from tests.test_market import It

    cog = make_cog(eco)
    it = It()
    asyncio.run(MarketCog.board.callback(cog, it, None, None))
    assert "全銘柄" in it.sent[-1]["embed"].title and it.sent[-1]["ephemeral"] and it.sent[-1]["view"]
    view = it.sent[-1]["view"]
    assert [len(s.options) for s in view.children] == [5, len(market.PERIODS)]
    swap = []

    class Resp:
        async def edit_message(self, **kw):
            swap.append(kw["embed"].title)

    view.symbol_select._values = ["teio"]
    asyncio.run(view._on_symbol(SimpleNamespace(response=Resp())))
    view.period_select._values = ["1h"] if "1h" in market.PERIODS else [next(iter(market.PERIODS))]
    asyncio.run(view._on_period(SimpleNamespace(response=Resp())))
    view.symbol_select._values = ["all"]
    asyncio.run(view._on_symbol(SimpleNamespace(response=Resp())))
    assert "TEIO重工" in swap[0] and "TEIO重工" in swap[1] and "全銘柄" in swap[2]
    it = It()
    asyncio.run(MarketCog.board.callback(cog, it, choice("numa"), None))
    assert "NUMAバイオ" in it.sent[-1]["embed"].title and "view" not in it.sent[-1]
