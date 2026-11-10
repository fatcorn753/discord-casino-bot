"""ストップ高・ストップ安: 裏の理論価格との分離・2連ストップ・張り付き中の売買ロック。"""

import time

import pytest

from core import market
from core.economy import Economy

pytestmark = pytest.mark.real_scope

G, U = 10, 1
START = {s.key: s.start for s in market.SYMBOLS}


@pytest.fixture
def eco(tmp_path):
    e = Economy(str(tmp_path / "t.db"), strict=True)
    for key, price in START.items():
        e.market_set(time.time(), price, 0, key)
    return e


def pin(eco, symbol, state):
    eco.market_fair_set(symbol, 1.0, state, time.time())


# ----------------------------- 理論価格との分離 ----------------------------- #
def test_the_displayed_price_stops_at_the_limit_while_the_fair_price_goes_beyond():
    ref = {"cs": 1000.0}
    fair = {"cs": 500.0}  # 裏では半値まで暴落
    shown = market.limit_to_day(fair, ref)
    assert shown["cs"] == pytest.approx(700.0)  # 画面は -30% で止まる
    assert market.pin_states(fair, ref) == {"cs": -1}
    assert market.pin_states({"cs": 1400.0}, ref) == {"cs": 1}
    assert market.pin_states({"cs": 900.0}, ref) == {"cs": 0}  # 値幅の内側なら、張り付かない
    assert market.pin_states({"cs": 500.0}, {}) == {"cs": 0}  # 基準がなければ張り付かない


def test_two_limit_down_days_in_a_row_when_the_fair_price_stays_below():
    fair = {"cs": 400.0}
    day1 = market.limit_to_day(fair, {"cs": 1000.0})  # 700
    day2 = market.limit_to_day(fair, day1)  # 翌日の基準は 700 → 490
    assert day1["cs"] == pytest.approx(700.0) and day2["cs"] == pytest.approx(490.0)
    assert market.pin_states(fair, day1) == {"cs": -1}  # 2日目も張り付き（2連ストップ安）
    day3 = market.limit_to_day(fair, day2)  # 343 → 理論価格 400 は、もう値幅の内側
    assert day3["cs"] == pytest.approx(400.0) and market.pin_states(fair, day2) == {"cs": 0}


def test_fair_price_survives_a_restart_and_pin_is_read_back(eco):
    assert eco.market_pin("cs") == 0 and eco.market_fair_get("cs") is None
    eco.market_fair_set("cs", 321.5, -1, time.time())
    assert eco.market_fair_get("cs") == 321.5 and eco.market_pin("cs") == -1


# ----------------------------- 売買ロック ----------------------------- #
def test_limit_down_locks_selling_spot_closing_long_and_selling_calls_but_not_buying(eco):
    a = eco.for_guild(G)
    a.credit(U, 500_000, "テスト")
    assert a.crypto_buy(U, 20_000, 100.0).ok
    assert a.long_open(U, 10_000, 100.0, 2).ok
    assert a.short_open(U, 10_000, 100.0, 2, "teio").ok
    ok, opt = a.option_buy(U, "call", "cs", 5_000)
    assert ok
    pin(eco, "cs", -1)
    sold = a.crypto_sell(U, None, 70.0)
    assert not sold.ok and "ストップ安" in sold.reason and "約定しません" in sold.reason
    closed = a.long_close(U, None, 70.0)
    assert not closed.ok and "ストップ安" in closed.reason
    ok, why = a.option_sell(U, a.option_list(U)["active"][0]["id"])
    assert not ok and "ストップ安" in why
    assert a.crypto_buy(U, 1_000, 70.0).ok  # 逆張りの買いは通る
    pin(eco, "teio", -1)
    assert a.short_close(U, None, 70.0, "teio").ok  # 空売りの買い戻しは通る（利益確定）


def test_limit_up_locks_buying_spot_opening_long_and_covering_shorts_but_not_selling(eco):
    a = eco.for_guild(G)
    a.credit(U, 500_000, "テスト")
    assert a.crypto_buy(U, 20_000, 100.0).ok
    assert a.short_open(U, 10_000, 100.0, 2, "teio").ok
    pin(eco, "cs", 1)
    pin(eco, "teio", 1)
    bought = a.crypto_buy(U, 1_000, 130.0)
    assert not bought.ok and "ストップ高" in bought.reason
    opened = a.long_open(U, 1_000, 130.0, 2)
    assert not opened.ok and "ストップ高" in opened.reason
    cover = a.short_close(U, None, 130.0, "teio")
    assert not cover.ok and "ストップ高" in cover.reason and "売り手がいなくて" in cover.reason
    a.credit(2, 500_000, "テスト")
    pin(eco, "cs", 0)
    assert a.crypto_buy(2, 20_000, 100.0).ok
    pin(eco, "cs", 1)
    assert a.crypto_sell(2, None, 130.0).ok  # 利益確定の現物売りは通る
    assert a.short_open(U, 1_000, 130.0, 2, "teio").ok  # 空売りの新規は通る（逆張り）


def test_locks_apply_per_symbol_and_clear_when_the_pin_is_released(eco):
    a = eco.for_guild(G)
    a.credit(U, 500_000, "テスト")
    assert a.crypto_buy(U, 20_000, 100.0, "teio").ok
    pin(eco, "cs", -1)
    assert a.crypto_sell(U, None, 100.0, "teio").ok  # ほかの銘柄は、売れる
    assert a.crypto_buy(U, 20_000, 100.0).ok
    pin(eco, "cs", 0)
    assert a.crypto_sell(U, None, 100.0).ok  # 張り付きが外れれば、売れる


def test_forced_liquidation_is_not_blocked_by_the_lock(eco):
    """ロスカットや破産の整理は、張り付き価格で執行する（ロックしない）。"""
    a = eco.for_guild(G)
    a.credit(U, 500_000, "テスト")
    assert a.long_open(U, 10_000, 100.0, 5).ok
    pin(eco, "cs", -1)
    with a._lock, a._conn:
        res = a._margin_close_locked(U, "long", None, 70.0, "ロスカット", "cs", owe=True)
    assert res.ok


# ----------------------------- 価格の更新と表示 ----------------------------- #
def test_step_prices_keeps_the_displayed_price_in_the_band_and_records_the_pin(eco, monkeypatch):
    from types import SimpleNamespace

    from cogs.market_cog import MarketCog
    from core.session import Sessions

    bot = SimpleNamespace(economy=eco, sessions=Sessions(cooldown=0), config={})
    cog = MarketCog(bot)
    now = time.time()
    for key, price in START.items():  # 24時間前の価格を入れる
        eco.market_set(now - 86400 + 1, price, 0, key)
        eco.market_set(now - 10, price, 0, key)
    crash = {k: p * 0.4 for k, p in START.items()}  # 裏の理論価格は、4割まで暴落
    monkeypatch.setattr(market, "next_prices", lambda prices, rng, vol=None: (dict(crash), 0))
    shown, _ = cog.step_prices()
    for key, price in START.items():
        assert shown[key] == pytest.approx(round(price * 0.7, 2), abs=0.02)  # 画面は -30% で止まる
        assert eco.market_pin(key) == -1 and eco.market_fair_get(key) == pytest.approx(price * 0.4)
    assert "ストップ安" in cog.pin_lines("cs") and "現物の売却" in cog.pin_lines("cs")
    eco.market_fair_set("cs", START["cs"], 0, now)
    assert cog.pin_lines("cs") == ""
