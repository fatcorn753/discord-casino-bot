"""証券の複数銘柄: 相関のある値動き・銘柄ごとの保有・旧データの移行・コマンド構成。"""

import asyncio
import math
import random
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
def test_old_commands_are_gone_and_securities_group_has_the_expected_commands():
    assert {c.name for c in MarketCog.group.commands} == {
        "価格",
        "チャート",
        "買う",
        "売る",
        "空売り",
        "買い戻し",
        "保有",
        "債券",
    }
    assert {c.name for c in MarketCog.bond_group.commands} == {"買う", "保有", "解約"}
    for cmd in (MarketCog.buy, MarketCog.sell, MarketCog.short, MarketCog.cover):
        assert [c.value for c in cmd._params["symbol"].choices] == prices_keys()


def test_buy_other_symbol_via_command_and_holdings_list_it(eco):
    from tests.test_market import It  # 同じ疑似インタラクションを使う

    bot = SimpleNamespace(economy=eco, sessions=Sessions(cooldown=0), config={})
    cog = MarketCog(bot)
    cog._prices()
    it = It()
    asyncio.run(MarketCog.buy.callback(cog, it, SimpleNamespace(value="gold"), 2_000))
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
