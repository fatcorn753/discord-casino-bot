"""市場イベント（ボラティリティ予告・相関崩壊の警報・ロスカット水準の可視化）と、オプションの値段への織り込み。"""

import asyncio
import random
import time
from types import SimpleNamespace

import pytest

from cogs.market_cog import MarketCog, MarketView, OptionAmountModal
from core import market, market_events
from core import options as opt
from core.economy import Economy
from core.session import Sessions

pytestmark = pytest.mark.real_scope

G, U = 10, 1
START = {s.key: s.start for s in market.SYMBOLS}
NOW = 1_000_000.0
MIN = 60


def event(symbol="numa", announce=NOW, factor=3.0):
    start = announce + market_events.LEAD_MIN * MIN
    return {
        "symbol": symbol,
        "announce_ts": announce,
        "start_ts": start,
        "end_ts": start + market_events.DURATION_MIN * MIN,
        "factor": factor,
    }


@pytest.fixture
def root(tmp_path):
    e = Economy(str(tmp_path / "t.db"), strict=True)
    for key, price in START.items():
        e.market_set(time.time(), price, 0, key)
    return e


# ----------------------------- イベントの時間と倍率 ----------------------------- #
def test_phases_and_multiplier_follow_the_timeline():
    ev = event()
    assert market_events.phase(ev, NOW) == "announced" and market_events.multiplier([ev], "numa", NOW) == 1.0
    assert market_events.phase(ev, ev["start_ts"]) == "active"
    assert market_events.multiplier([ev], "numa", ev["start_ts"] + 1) == 3.0
    assert market_events.multiplier([ev], "cs", ev["start_ts"] + 1) == 1.0  # ほかの銘柄は変わらない
    assert (
        market_events.phase(ev, ev["end_ts"]) == "done" and market_events.multiplier([ev], "numa", ev["end_ts"]) == 1.0
    )


def test_priced_weight_is_one_from_the_moment_of_the_announcement():
    ev = market_events.plan_event(1_000.0, __import__("random").Random(1))
    assert market_events.priced_weight(ev, ev["announce_ts"]) == 1.0  # 公知の情報は、予告の瞬間に全部織り込む
    assert market_events.priced_weight(ev, ev["start_ts"]) == 1.0


def test_expected_event_ticks_is_a_small_per_symbol_share_of_the_window():
    ticks = market_events.expected_event_ticks(6 * 3600, 60)
    assert 3.0 < ticks < 6.0  # 6時間のうち、1銘柄が予告に当たる刻みの期待値（約4.5分）
    assert market_events.expected_event_ticks(0, 60) == 0.0


# ----------------------------- 値動きへの反映（期待値は変わらない）----------------------------- #
def test_volatility_multiplier_keeps_the_expected_price_and_widens_the_moves():
    rng = random.Random(5)
    base = {s.key: s.start for s in market.SYMBOLS}
    n, wide, plain, sum_wide = 20_000, [], [], 0.0
    for _ in range(n):
        a, _ = market.next_prices(base, rng, {"numa": 3.0})
        b, _ = market.next_prices(base, rng)
        wide.append(abs(a["numa"] / base["numa"] - 1))
        plain.append(abs(b["numa"] / base["numa"] - 1))
        sum_wide += a["numa"] / base["numa"]
    assert sum_wide / n == pytest.approx(1.0, abs=0.003)  # 期待値は変わらない（マルチンゲール）
    assert sum(wide) / n > 2.5 * sum(plain) / n  # 揺れは、ほぼ3倍


def test_unaffected_symbols_are_identical_with_and_without_the_multiplier():
    base = {s.key: s.start for s in market.SYMBOLS}
    a, _ = market.next_prices(base, random.Random(3), {"numa": 3.0})
    b, _ = market.next_prices(base, random.Random(3))
    assert a["cs"] == b["cs"] and a["gold"] == b["gold"]  # 同じ乱数なら、対象外の銘柄は動きが同じ


# ----------------------------- 予告の抽選 ----------------------------- #
def test_event_step_respects_probability_and_cooldown(root):
    class Rng:
        def __init__(self, v):
            self.v = v

        def random(self):
            return self.v

        def choice(self, seq):
            return seq[0]

    assert root.event_step(NOW, Rng(0.99)) is None  # 外れ
    first = root.event_step(NOW, Rng(0.0))  # 当たり
    assert first and first["symbol"] == "cs" and first["start_ts"] == NOW + market_events.LEAD_MIN * MIN
    assert root.event_step(NOW + 60, Rng(0.0)) is None  # 進行中は、新しい予告を出さない
    after = first["end_ts"] + 10
    assert root.event_step(after, Rng(0.0)) is None  # 終わって間もない（クールダウン）
    later = first["end_ts"] + market_events.COOLDOWN_MIN * MIN + 10
    assert root.event_step(later, Rng(0.0)) is not None  # クールダウンのあとは、また出る


def test_vol_multipliers_only_while_the_event_is_active(root):
    ev = root.event_step(NOW, force=event())
    assert root.vol_multipliers(NOW) == {}  # 予告中
    assert root.vol_multipliers(ev["start_ts"] + 1) == {"numa": 3.0}
    assert root.vol_multipliers(ev["end_ts"] + 1) == {}


# ----------------------------- オプションの値段への織り込み ----------------------------- #
def test_announced_event_is_fully_priced_at_once_and_does_not_rise_as_it_approaches():
    ev = event()
    args = ("call", "numa", 400.0, 420.0, 6 * 3600)
    none = opt.ask_per_unit(*args, now=NOW, events=())
    at_announce = opt.ask_per_unit(*args, now=NOW, events=[ev])
    near_start = opt.ask_per_unit(*args, now=ev["start_ts"] - 1, events=[ev])
    assert at_announce > none  # 予告が出た瞬間に、全部織り込む
    assert near_start == pytest.approx(
        at_announce, rel=0.02
    )  # 開始に近づいても、値段は（時間の経過の分を除いて）上がらない


def test_peacetime_prices_already_include_the_expected_future_announcements():
    """予告のない平時も、「これから予告が出る」期待の分だけ、揺れが大きく見積もられている（無裁定）。"""
    base = opt.total_variance("numa", 6 * 3600, 0.0, [event("cs")])  # numa に予告がない
    own = opt.total_variance("numa", 6 * 3600, 0.0, [event("numa")])
    ticks = 6 * 3600 / opt.TICK_SECONDS
    plain = ticks * (market.COVARIANCE[2][2] + opt.jump_variance_per_tick("numa"))
    assert base > plain  # 平時の期待の分が、足されている
    assert own != base  # 自分の銘柄の予告があれば、その予告を明示的に数える


def test_an_event_on_another_symbol_does_not_change_the_price():
    args = ("call", "cs", 1000.0, 1050.0, 6 * 3600)
    assert opt.ask_per_unit(*args, now=NOW, events=[event("numa")]) == opt.ask_per_unit(*args, now=NOW, events=())


def test_no_front_running_buying_right_after_the_announcement_costs_the_same_as_just_before_the_start(root):
    eco = root.for_guild(G)
    eco.credit(U, 20_000, "テスト")
    ev = root.event_step(time.time(), force=event(announce=time.time()))
    ok, early = eco.option_buy(U, opt.CALL, "numa", 1_000, ev["announce_ts"])
    ok, late = eco.option_buy(U, opt.CALL, "numa", 1_000, ev["start_ts"] - 1)
    assert early["units"] == pytest.approx(late["units"], rel=0.03)  # 予告の直後に仕込んでも、安く買えない


# ----------------------------- 相関崩壊の警報 ----------------------------- #
def line(start, end, n=61):
    return [start * (end / start) ** (i / (n - 1)) for i in range(n)]


def test_alert_when_correlated_symbols_diverge():
    hist = {"cs": line(1000, 1100), "teio": line(1200, 1190), "numa": line(400, 400), "gold": line(3000, 3000)}
    alerts = market_events.spread_alerts(hist)
    assert alerts and (alerts[0]["a"], alerts[0]["b"]) == ("cs", "teio") and alerts[0]["z"] > market_events.Z_ALERT
    assert "CSトークン" in market_events.describe_alert(alerts[0])


def test_no_alert_when_they_move_together_or_slightly():
    together = {"cs": line(1000, 1100), "teio": line(1200, 1320), "numa": line(400, 400), "gold": line(3000, 3000)}
    assert [a for a in market_events.spread_alerts(together) if (a["a"], a["b"]) == ("cs", "teio")] == []
    slight = {"cs": line(1000, 1005), "teio": line(1200, 1200), "numa": line(400, 400), "gold": line(3000, 3000)}
    assert market_events.spread_alerts(slight) == []


def test_negative_correlation_pairs_alert_when_they_move_the_same_way():
    hist = {"cs": line(1000, 1100), "teio": line(1200, 1320), "numa": line(400, 400), "gold": line(3000, 3300)}
    pairs = {(a["a"], a["b"]) for a in market_events.spread_alerts(hist)}
    assert ("cs", "gold") in pairs  # ふつう逆に動く2銘柄が、そろって上がった


def test_alerts_need_enough_history():
    assert market_events.spread_alerts({"cs": [1000, 1100], "teio": [1200, 1100]}) == []


# ----------------------------- ロスカット水準の地図 ----------------------------- #
def test_cut_map_finds_the_price_where_the_ratio_breaks(root):
    eco = root.for_guild(G)
    assert eco.long_open(U, 10_000, 1000.0, 5, "cs").ok  # 10枚=10,000ペリカ分、5倍 → 担保2,000（残高3,000の範囲）
    rows = eco.margin_cut_map()
    assert len(rows) == 1
    symbol, side, price, notional = rows[0]
    assert symbol == "cs" and side == "long" and price < 1000.0 and notional > 0
    below = eco._margin_status_locked(U, {**eco.latest_prices(), "cs": price * 0.999})["ratio"]
    above = eco._margin_status_locked(U, {**eco.latest_prices(), "cs": price * 1.001})["ratio"]
    assert below < market.LOSSCUT_RATIO <= above  # その価格を境に、維持率が 100% を割る


def test_cut_map_is_empty_without_positions(root):
    assert root.for_guild(G).margin_cut_map() == []


# ----------------------------- 市況の画面 ----------------------------- #
@pytest.fixture
def cog(root):
    root.for_guild(G).credit(U, 30_000, "テスト")
    cog = MarketCog(SimpleNamespace(economy=root, sessions=Sessions(cooldown=0), config={}))
    cog._prices()  # このファイルで読み込んだ MarketCog で作る（他のテストが cogs を読み込み直しても、クラスがずれない）
    return cog


def test_summary_shows_event_alert_and_cut_zones(cog):
    now = time.time()
    cog.eco.event_step(now, force=event(announce=now))
    for s in market.SYMBOLS:  # 直近の履歴: CS だけ急騰（相関崩壊）
        for i in range(61):
            price = START[s.key] * ((1.10 if s.key == "cs" else 1.0) ** (i / 60))
            cog.eco.market_set(now - (60 - i) * 60, price, 0, s.key)
    cog.eco.for_guild(G).long_open(U, 20_000, cog._price("cs"), 5, "cs")
    embed = cog.summary_embed(G)
    names = [f.name for f in embed.fields]
    assert any("市場イベント" in n for n in names) and any("相関崩壊" in n for n in names)
    event_text = next(f.value for f in embed.fields if "市場イベント" in f.name)
    assert "NUMAバイオ" in event_text and "予告" in event_text


def test_summary_without_events_has_no_extra_fields(cog):
    assert cog.summary_embed(G).fields == []


def test_view_offers_quick_buy_buttons_only_while_an_event_exists(cog):
    plain = MarketView(cog, U, G)
    assert len(plain.children) == 2
    now = time.time()
    cog.eco.event_step(now, force=event(announce=now))
    view = MarketView(cog, U, G)
    labels = [c.label for c in view.children if getattr(c, "label", None)]
    assert any("NUMAバイオの爆騰コール" in x for x in labels) and any("NUMAバイオの暴落プット" in x for x in labels)


def test_quick_buy_modal_buys_the_option(cog):
    from tests.test_market import It

    modal = OptionAmountModal(cog, opt.CALL, "numa")
    modal.amount = SimpleNamespace(value="2,000")
    it = It()
    asyncio.run(modal.on_submit(it))
    assert "爆騰コール" in it.sent[-1]["embed"].title
    assert cog.eco.for_guild(G).option_list(U)["active"][0]["symbol"] == "numa"
    bad = OptionAmountModal(cog, opt.PUT, "numa")
    bad.amount = SimpleNamespace(value="abc")
    it = It()
    asyncio.run(bad.on_submit(it))
    assert it.sent[-1]["ephemeral"] and "❌" in it.sent[-1]["embed"].title


def test_tick_announces_and_applies_events(cog):
    """価格の更新（step_prices）が、予告の抽選と倍率の適用を呼ぶ。"""
    now = time.time()
    cog.eco.event_step(now - 20 * 60, force=event(announce=now - 20 * 60))  # 5分前に始まった（発生中）
    new, _ = cog.step_prices()
    assert set(new) == {s.key for s in market.SYMBOLS}
    assert cog.eco.vol_multipliers(time.time()) == {"numa": 3.0}
