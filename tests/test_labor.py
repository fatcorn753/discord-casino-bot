"""地下作業（クリッカー）: 計算・疲労・事故・受け取り・強化・画面。"""

import asyncio
import random
from types import SimpleNamespace

import pytest

from cogs import labor_cog
from core import labor as lab
from core.economy import Economy

U = 1
DAY = "2026-11-10"
T0 = 1_700_000_000.0


class NoAccident(random.Random):
    def random(self):
        return 0.99


class AlwaysAccident(random.Random):
    def random(self):
        return 0.0


@pytest.fixture
def eco(tmp_path):
    e = Economy(str(tmp_path / "t.db"))
    e.set_balance(U, 0, "テスト")
    return e


def click(eco, n, rng=None, start=T0, step=1.5):
    last = None
    for i in range(n):
        last = eco.labor_click(U, start + i * step, DAY, rng or NoAccident())
    return last


def test_pure_numbers():
    assert lab.pay_per_click(0) == 10 and lab.pay_per_click(5) == 30 and lab.pay_per_click(99) == 30
    assert lab.pay_rate(0) == 1.0 and abs(lab.pay_rate(100) - lab.MIN_PAY_RATE) < 1e-9
    assert lab.fatigue_gain(5) == 1.0 and lab.accident_chance(5) == pytest.approx(0.005)
    assert lab.fatigue_now(50, 0, 100) == 30 and lab.fatigue_now(10, 0, 1000) == 0
    assert lab.upgrade_cost(0) == 150 and lab.upgrade_cost(5) is None
    assert lab.click_pay(0, 100) == 4 and lab.click_pay(0, 0) == 10


def test_click_outcome_accident_and_normal():
    ok = lab.click_outcome(0, 0, 0, 0.0, NoAccident())
    assert not ok["accident"] and ok["pay"] == 10 and ok["fatigue"] == lab.FATIGUE_GAIN
    bad = lab.click_outcome(0, 0, 0, 0.0, AlwaysAccident())
    assert bad["accident"] and bad["pay"] == 0 and bad["fatigue"] == lab.ACCIDENT_FATIGUE


def test_works_from_zero_balance_and_pot_flushes_into_balance(eco):
    r = click(eco, 1)
    assert r["ok"] and r["pay"] == 10 and eco.balance(U) == 0  # まだ受け取る前の稼ぎ
    assert eco.labor_state(U, T0 + 2, DAY)["pot"] == 10
    assert eco.labor_collect(U, T0 + 3, DAY) == 10 and eco.balance(U) == 10
    assert eco.labor_state(U, T0 + 4, DAY)["pot"] == 0


def test_auto_collect_when_pot_reaches_threshold(eco):
    r = None
    for i in range(30):  # 疲労で稼ぎが減っても、いつかは AUTO_COLLECT に届く
        r = eco.labor_click(U, T0 + i * 1.5, DAY, NoAccident())
        if r["collected"]:
            break
    assert r["collected"] >= lab.AUTO_COLLECT and eco.balance(U) >= lab.AUTO_COLLECT


def test_click_interval_and_accident(eco):
    assert click(eco, 1)["ok"]
    fast = eco.labor_click(U, T0 + 0.2, DAY, NoAccident())
    assert not fast["ok"] and fast["quiet"]
    acc = eco.labor_click(U, T0 + 5, DAY, AlwaysAccident())
    assert acc["ok"] and acc["accident"] and acc["pay"] == 0
    assert eco.labor_state(U, T0 + 5, DAY)["pot"] == 10


def test_fatigue_reduces_pay_and_recovers_over_time(eco):
    first = click(eco, 1)["pay"]
    click(eco, 20, start=T0 + 10)
    s = eco.labor_state(U, T0 + 10 + 20 * 1.5, DAY)
    assert s["fatigue"] > 30
    later = eco.labor_click(U, T0 + 10 + 20 * 1.5 + 1, DAY, NoAccident())["pay"]
    assert later < first
    assert eco.labor_state(U, T0 + 100_000, DAY)["fatigue"] == 0  # 時間がたてば回復


def test_rest_recovers_and_blocks_clicks(eco):
    click(eco, 15)
    now = T0 + 15 * 1.5
    before = eco.labor_state(U, now, DAY)["fatigue"]
    ok, _ = eco.labor_rest(U, now, DAY)
    assert ok and eco.labor_state(U, now, DAY)["fatigue"] == pytest.approx(before - lab.REST_RECOVER)
    blocked = eco.labor_click(U, now + 5, DAY, NoAccident())
    assert not blocked["ok"] and "休憩中" in blocked["reason"]
    assert eco.labor_click(U, now + lab.REST_SECONDS + 1, DAY, NoAccident())["ok"]
    assert eco.labor_rest(U, T0 + 10**6, DAY) == (False, "疲れていません。")


def test_no_daily_cap_or_balance_limit_and_today_total_resets(eco):
    eco.set_balance(U, 1_000_000, "テスト")  # 金持ちも、働ける
    for i in range(300):  # 1日に、いくらでも稼げる（疲労で、稼ぎの速さが頭打ちになるだけ）
        assert eco.labor_click(U, T0 + i * 1.5, DAY, NoAccident())["ok"]
    assert eco.labor_state(U, T0 + 500, DAY)["day_earned"] > 1_000
    assert eco.labor_state(U, T0 + 500, "2026-11-11")["day_earned"] == 0  # 日が変われば、「今日の稼ぎ」は戻る


def test_income_is_withheld_for_debt(eco):
    eco.set_balance(U, -4_000, "テスト")
    eco.labor_click(U, T0, DAY, NoAccident())
    assert eco.labor_collect(U, T0 + 2, DAY) == 10
    assert eco.debt(U) < 4_000 and eco.balance(U) < 10  # 収入なので、借金の返済へ天引きされた


def test_upgrade_costs_pelica_and_changes_numbers(eco):
    ok, why = eco.labor_upgrade(U, "pick", T0, DAY)
    assert not ok and "足りません" in why
    eco.credit(U, 5_000, "テスト")
    assert eco.labor_upgrade(U, "pick", T0, DAY) == (True, "")
    assert eco.balance(U) == 5_000 - 150 and eco.labor_state(U, T0, DAY)["pick"] == 1
    assert eco.labor_click(U, T0 + 2, DAY, NoAccident())["pay"] == 14
    assert eco.labor_upgrade(U, "bogus", T0, DAY)[0] is False
    for _ in range(10):
        eco.labor_upgrade(U, "glove", T0, DAY)
    assert eco.labor_state(U, T0, DAY)["glove"] <= lab.MAX_LEVEL


def test_state_does_not_create_rows(eco):
    s = eco.labor_state(999, T0, DAY)
    assert s["balance"] == 0 and s["day_earned"] == 0
    assert eco._conn.execute("SELECT COUNT(*) c FROM labor WHERE user_id=999").fetchone()["c"] == 0


# ------------------------------ 画面 ------------------------------ #
class Resp:
    def __init__(self):
        self.edits, self.sent = [], []

    async def edit_message(self, **kw):
        self.edits.append(kw)

    async def send_message(self, *a, **kw):
        self.sent.append((a, kw))


def make_view(eco):
    bot = SimpleNamespace(economy=SimpleNamespace(for_guild=lambda g: eco), config={})
    return labor_cog.LaborView(bot, U, 0)


def test_panel_view_buttons_and_owner_check(eco):
    view = make_view(eco)
    labels = [c.label for c in view.children]
    assert labels[0] == "働く" and "休憩" in labels and any("受け取る" in x for x in labels)
    assert sum("強化" in x for x in labels) == 3
    assert view.children[0].disabled is False and view.children[2].disabled is True  # 稼ぎが無いので、受け取れない
    assert all(c.disabled for c in view.children[3:])  # 元手 0 では、強化できない
    stranger = SimpleNamespace(user=SimpleNamespace(id=U + 1), response=Resp())
    assert asyncio.run(view.interaction_check(stranger)) is False and stranger.response.sent


def test_panel_work_and_collect_update_message(eco):
    view = make_view(eco)
    it = SimpleNamespace(user=SimpleNamespace(id=U), response=Resp())
    asyncio.run(view._work(it))
    assert it.response.edits and it.response.edits[-1]["embed"].title.startswith("⛏")
    day = labor_cog.today(view.bot)
    assert eco.labor_state(U, 0, day)["pot"] == 10 and eco.balance(U) == 0
    asyncio.run(view._collect(it))
    assert eco.balance(U) == 10 and eco.labor_state(U, 0, day)["pot"] == 0
    embed = labor_cog.panel_embed(view.state)
    assert "疲労" in embed.description and "今日の稼ぎ" in embed.description
