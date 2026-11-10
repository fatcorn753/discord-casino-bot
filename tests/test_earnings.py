"""定期決算イベント（毎週火曜13:30）と、デイリーの裏情報メモ。"""

import asyncio
import datetime
import random
import time
from types import SimpleNamespace

import pytest

from cogs.market_cog import MarketCog
from core import earnings as ea
from core import market
from core.economy import Economy
from core.session import Sessions

pytestmark = pytest.mark.real_scope

TZ = 9.0
JST = datetime.timezone(datetime.timedelta(hours=TZ))


def ts(y, m, d, hh, mm=0):
    return datetime.datetime(y, m, d, hh, mm, tzinfo=JST).timestamp()


# 2026-11-10 は火曜日
TUE_BEFORE = ts(2026, 11, 10, 12, 0)
TUE_AFTER = ts(2026, 11, 10, 14, 0)


# ----------------------------- 純粋なロジック ----------------------------- #
def test_next_release_is_the_coming_tuesday_1330_and_announce_is_the_monday_9am_before():
    assert ea.next_release(TUE_BEFORE, TZ) == ts(2026, 11, 10, 13, 30)
    assert ea.next_release(TUE_AFTER, TZ) == ts(2026, 11, 17, 13, 30)  # 発表のあとは、来週
    assert ea.next_release(ts(2026, 11, 13, 9), TZ) == ts(2026, 11, 17, 13, 30)  # 金曜からでも、次の火曜
    assert ea.announce_time(ts(2026, 11, 10, 13, 30), TZ) == ts(2026, 11, 9, 9, 0)


def test_plan_picks_a_target_and_size_and_sometimes_a_surprise():
    rng = random.Random(1)
    plans = [ea.plan(TUE_BEFORE, TZ, rng) for _ in range(400)]
    assert {p["symbol"] for p in plans} == set(ea.SYMBOLS) and {p["direction"] for p in plans} == {1, -1}
    sizes = [p["size"] for p in plans]
    assert min(sizes) >= ea.SIZE_RANGE[0] and max(sizes) <= ea.SURPRISE_RANGE[1]
    surprise = sum(s >= ea.SURPRISE_RANGE[0] for s in sizes) / len(sizes)
    assert 0.08 < surprise < 0.25  # 大サプライズは、ときどき
    assert all(
        s >= ea.SURPRISE_RANGE[0] > market.DAILY_LIMIT for s in sizes if s > ea.SIZE_RANGE[1]
    )  # 値幅制限を突き抜ける


def test_factor_is_symmetric_in_log_terms():
    assert ea.factor(1, 0.2) == pytest.approx(1.2) and ea.factor(-1, 0.2) == pytest.approx(1 / 1.2)


def test_memo_direction_is_true_about_seventy_percent_of_the_time():
    rng = random.Random(3)
    hits = sum(ea.memo_direction(1, rng) == 1 for _ in range(4000)) / 4000
    assert 0.66 < hits < 0.74


# ----------------------------- 記録と発表 ----------------------------- #
@pytest.fixture
def eco(tmp_path):
    e = Economy(str(tmp_path / "t.db"), strict=True)
    for s in market.SYMBOLS:
        e.market_set(time.time(), s.start, 0, s.key)
    return e


def test_ensure_creates_one_pending_earnings_and_it_is_public_only_after_the_monday_announce(eco):
    rng = random.Random(5)
    ev = eco.earnings_ensure(ts(2026, 11, 8, 12), TZ, rng)  # 日曜
    assert ev is not None and eco.earnings_ensure(ts(2026, 11, 8, 13), TZ, rng) is None  # 発表前は、1つだけ
    assert eco.earnings_upcoming(ts(2026, 11, 8, 12)) is None  # 月曜9:00までは、公開しない
    pub = eco.earnings_upcoming(ts(2026, 11, 9, 9, 1))
    assert pub["symbol"] == ev["symbol"] and "direction" not in pub and "size" not in pub  # 方向と大きさは、漏れない
    assert [r["id"] for r in eco.earnings_to_announce(ts(2026, 11, 9, 9, 1))] == [ev["id"]]
    assert eco.earnings_to_announce(ts(2026, 11, 9, 10)) == []  # 告知は、1回だけ


def test_release_marks_it_done_once_and_the_next_one_is_planned_for_next_week(eco):
    eco.earnings_ensure(ts(2026, 11, 8, 12), TZ, random.Random(5))
    assert eco.earnings_release_due(ts(2026, 11, 10, 13, 29)) == []
    due = eco.earnings_release_due(ts(2026, 11, 10, 13, 30))
    assert len(due) == 1 and eco.earnings_release_due(ts(2026, 11, 10, 14)) == []
    nxt = eco.earnings_ensure(ts(2026, 11, 10, 14), TZ, random.Random(6))
    assert nxt["release_at"] == ts(2026, 11, 17, 13, 30)


# ----------------------------- 価格への反映 ----------------------------- #
def make_cog(eco):
    bot = SimpleNamespace(economy=eco, sessions=Sessions(cooldown=0), config={}, guilds=[])
    return MarketCog(bot)


def test_release_moves_the_fair_price_and_a_surprise_pins_the_displayed_price(eco, monkeypatch):
    cog = make_cog(eco)
    now = time.time()
    for s in market.SYMBOLS:
        eco.market_set(now - 86400 + 1, s.start, 0, s.key)
        eco.market_set(now - 10, s.start, 0, s.key)
    with eco._lock, eco._conn:
        eco._conn.execute(
            "INSERT INTO earnings (symbol, announce_at, release_at, direction, size) VALUES ('teio', ?, ?, 1, 0.5)",
            (now - 3 * 86400, now - 1),
        )
    monkeypatch.setattr(market, "next_prices", lambda prices, rng, vol=None: (dict(prices), 0))  # 地合いの揺れは無し
    shown, _ = cog.step_prices()
    start = market.BY_KEY["teio"].start
    assert eco.market_fair_get("teio") == pytest.approx(round(start * 1.5, 2), abs=0.02)  # 理論価格は +50%
    assert shown["teio"] == pytest.approx(round(start * 1.3, 2), abs=0.02)  # 画面は +30% で止まる
    assert eco.market_pin("teio") == 1  # ストップ高に張り付く
    assert cog._earnings_results and cog._earnings_results[0]["symbol"] == "teio"
    assert eco.market_pin("cs") == 0


def test_ordinary_earnings_stay_inside_the_band_and_do_not_pin(eco, monkeypatch):
    cog = make_cog(eco)
    now = time.time()
    for s in market.SYMBOLS:
        eco.market_set(now - 86400 + 1, s.start, 0, s.key)
        eco.market_set(now - 10, s.start, 0, s.key)
    with eco._lock, eco._conn:
        eco._conn.execute(
            "INSERT INTO earnings (symbol, announce_at, release_at, direction, size) VALUES ('numa', ?, ?, -1, 0.15)",
            (now - 3 * 86400, now - 1),
        )
    monkeypatch.setattr(market, "next_prices", lambda prices, rng, vol=None: (dict(prices), 0))
    shown, _ = cog.step_prices()
    start = market.BY_KEY["numa"].start
    assert shown["numa"] == pytest.approx(round(start / 1.15, 2), abs=0.02) and eco.market_pin("numa") == 0


# ----------------------------- 告知・表示・メモ ----------------------------- #
def test_board_shows_the_upcoming_earnings_only_after_the_announce(eco):
    cog = make_cog(eco)
    with eco._lock, eco._conn:
        now = time.time()
        eco._conn.execute(
            "INSERT INTO earnings (symbol, announce_at, release_at, direction, size) VALUES ('teio', ?, ?, 1, 0.1)",
            (now + 3600, now + 86400),
        )
    assert cog.earnings_lines() == ""  # まだ予告前
    with eco._lock, eco._conn:
        eco._conn.execute("UPDATE earnings SET announce_at=?", (now - 60,))
    text = cog.earnings_lines()
    assert market.BY_KEY["teio"].name in text and "決算発表" in text


def test_memo_arrives_by_chance_once_per_earnings_and_is_sometimes_wrong(eco):
    a = eco.for_guild(10)
    assert a.earnings_memo(1, ts(2026, 11, 9, 10), random.Random(1)) is None  # 決算が登録されていない
    ev = eco.earnings_ensure(ts(2026, 11, 8, 12), TZ, random.Random(5))
    always = SimpleNamespace(random=lambda: 0.0)
    text = a.earnings_memo(1, ts(2026, 11, 9, 10), always)
    assert text and "極秘メモ" in text and "ガセ" in text and market.BY_KEY[ev["symbol"]].name in text
    assert a.earnings_memo(1, ts(2026, 11, 9, 11), always) is None  # 同じ決算では、1人1回
    assert a.earnings_memo(2, ts(2026, 11, 9, 11), always)  # 別の人には届く
    never = SimpleNamespace(random=lambda: 0.99)
    assert eco.for_guild(10).earnings_memo(3, ts(2026, 11, 9, 11), never) is None  # 抽選に外れる
    assert a.earnings_memo(4, ts(2026, 11, 10, 14), always) is None  # 発表後（次の決算は未登録）は届かない


def test_post_announces_and_reports_results_to_each_allowed_guild(eco, monkeypatch):
    from cogs import market_cog

    sent = []

    class Chan:
        async def send(self, **kw):
            sent.append(kw["embed"])

    guild = SimpleNamespace(id=7)
    cog = make_cog(eco)
    cog.bot.guilds = [guild]
    monkeypatch.setattr(market_cog.approval, "is_allowed", lambda *a: True)
    monkeypatch.setattr("cogs.weekly_cog.announce_channel", lambda g, e: Chan())
    now = time.time()
    with eco._lock, eco._conn:
        eco._conn.execute(
            "INSERT INTO earnings (symbol, announce_at, release_at, direction, size) VALUES ('teio', ?, ?, 1, 0.1)",
            (now - 60, now + 86400),
        )
    cog._earnings_results = [{"symbol": "numa", "direction": -1, "size": 0.4}]
    asyncio.run(cog._earnings_posts())
    titles = [e.title for e in sent]
    assert any("今週の決算発表" in t for t in titles) and any("悪決算" in t for t in titles)
    asyncio.run(cog._earnings_posts())
    assert len(sent) == 2  # 告知は1回だけ・結果は出し切った
