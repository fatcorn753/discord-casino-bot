"""地下作業（クリッカー）: 計算・疲労・受け取り・強化・画面。"""

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


def text(embed) -> str:
    """埋め込みの、見出し・説明・欄をつなげた文字列。"""
    return "\n".join([embed.title or "", embed.description or ""] + [f"{f.name} {f.value}" for f in embed.fields])


@pytest.fixture
def eco(tmp_path):
    e = Economy(str(tmp_path / "t.db"))
    e.set_balance(U, 0, "テスト")
    return e


def click(eco, n, start=T0, step=1.5):
    last = None
    for i in range(n):
        last = eco.labor_click(U, start + i * step, DAY)
    return last


def test_pure_numbers():
    assert lab.pay_per_click(0) == 10 and lab.pay_per_click(5) == 30 and lab.pay_per_click(99) == 30
    assert lab.pay_rate(0) == 1.0 and abs(lab.pay_rate(100) - lab.MIN_PAY_RATE) < 1e-9
    assert lab.fatigue_gain(5) == 1.0
    assert lab.fatigue_now(50, 0, 100) == 30 and lab.fatigue_now(10, 0, 1000) == 0
    assert lab.upgrade_cost(0) == 150 and lab.upgrade_cost(5) is None
    assert lab.click_pay(0, 100) == 4 and lab.click_pay(0, 0) == 10


def test_click_outcome_has_no_accident():
    out = lab.click_outcome(0, 0, 0.0)
    assert out == {"pay": 10, "fatigue": lab.FATIGUE_GAIN}
    assert not hasattr(lab, "accident_chance") and not hasattr(lab, "REST_SECONDS")


def test_works_from_zero_balance_and_pot_flushes_into_balance(eco):
    r = click(eco, 1)
    assert r["ok"] and r["pay"] == 10 and eco.balance(U) == 0  # まだ受け取る前の稼ぎ
    assert eco.labor_state(U, T0 + 2, DAY)["pot"] == 10
    assert eco.labor_collect(U, T0 + 3, DAY) == 10 and eco.balance(U) == 10
    assert eco.labor_state(U, T0 + 4, DAY)["pot"] == 0


def test_earnings_stay_in_the_pot_until_collected_all_at_once(eco):
    total = 0
    for i in range(60):  # 押しても、自動では手元に入らない
        r = eco.labor_click(U, T0 + i * 1.5, DAY)
        assert r["ok"] and "collected" not in r and "accident" not in r and r["pay"] > 0
        total += r["pay"]
    assert eco.balance(U) == 0 and eco.labor_state(U, T0 + 100, DAY)["pot"] == total
    assert eco.labor_collect(U, T0 + 100, DAY) == total and eco.balance(U) == total  # まとめて受け取る
    ledger = eco._conn.execute("SELECT COUNT(*) c FROM ledger WHERE reason='地下作業:稼ぎ'").fetchone()["c"]
    assert ledger == 1  # 台帳の行は、1つだけ


def test_click_interval(eco):
    assert click(eco, 1)["ok"]
    fast = eco.labor_click(U, T0 + 0.2, DAY)
    assert not fast["ok"] and fast["quiet"]
    assert eco.labor_click(U, T0 + 5, DAY)["pay"] > 0  # 事故はなく、毎回稼ぐ
    assert eco.labor_state(U, T0 + 5, DAY)["pot"] > 10


def test_fatigue_reduces_pay_and_recovers_over_time(eco):
    first = click(eco, 1)["pay"]
    click(eco, 20, start=T0 + 10)
    s = eco.labor_state(U, T0 + 10 + 20 * 1.5, DAY)
    assert s["fatigue"] > 30
    later = eco.labor_click(U, T0 + 10 + 20 * 1.5 + 1, DAY)["pay"]
    assert later < first
    assert eco.labor_state(U, T0 + 100_000, DAY)["fatigue"] == 0  # 時間がたてば回復


def test_no_daily_cap_or_balance_limit_and_today_total_resets(eco):
    eco.set_balance(U, 1_000_000, "テスト")  # 金持ちも、働ける
    for i in range(300):  # 1日に、いくらでも稼げる（疲労で、稼ぎの速さが頭打ちになるだけ）
        assert eco.labor_click(U, T0 + i * 1.5, DAY)["ok"]
    assert eco.labor_state(U, T0 + 500, DAY)["day_earned"] > 1_000
    assert eco.labor_state(U, T0 + 500, "2026-11-11")["day_earned"] == 0  # 日が変われば、「今日の稼ぎ」は戻る


def test_income_is_withheld_for_debt(eco):
    eco.set_balance(U, -4_000, "テスト")
    eco.labor_click(U, T0, DAY)
    assert eco.labor_collect(U, T0 + 2, DAY) == 10
    assert eco.debt(U) < 4_000 and eco.balance(U) < 10  # 収入なので、借金の返済へ天引きされた


def test_upgrade_costs_pelica_and_changes_numbers(eco):
    ok, why = eco.labor_upgrade(U, "pick", T0, DAY)
    assert not ok and "足りません" in why
    eco.credit(U, 5_000, "テスト")
    assert eco.labor_upgrade(U, "pick", T0, DAY) == (True, "")
    assert eco.balance(U) == 5_000 - 150 and eco.labor_state(U, T0, DAY)["pick"] == 1
    assert eco.labor_click(U, T0 + 2, DAY)["pay"] == 14
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
    assert labels[0] == "働く" and "休憩" not in labels and any("受け取る" in x for x in labels)
    assert sum("強化" in x for x in labels) == 2
    assert view.children[0].disabled is False and view.children[1].disabled is True  # 稼ぎが無いので、受け取れない
    assert labels[2] == "作業員" and all(c.disabled for c in view.children[3:])  # 元手 0 では、強化できない
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
    assert "疲労" in text(embed) and "今日の稼ぎ" in text(embed)


def test_ranking_total_today_and_my_rank(eco):
    for uid, n in ((1, 3), (2, 9), (3, 5)):
        for i in range(n):
            eco.labor_click(uid, T0 + i * 1.5, DAY)
    top = eco.labor_top(2, None, 3)
    assert [r[1] for r in top["rows"]] == [2, 3] and top["rows"][0][0] == 1
    assert top["me"][0] == 2 and top["me"][3] == 3  # 3 番目の人が、2位
    assert eco.labor_top(10, None, 1)["me"][0] == 3 and eco.labor_top(10, None, 99)["me"] is None
    eco.labor_click(1, T0 + 100, "2026-11-11")  # 別の日に稼いだ人は、今日の順位に出ない
    assert [r[1] for r in eco.labor_top(10, DAY)["rows"]] == [2, 3]  # その日の稼ぎは、日が変わると数えない
    assert [r[1] for r in eco.labor_top(10, "2026-11-11")["rows"]] == [1]
    assert eco.labor_top(10, "2030-01-01")["rows"] == []


def test_ranking_embed_shows_medals_and_my_rank_below_the_list():
    rows = [(i, 100 + i, 1_000 - i, 50) for i in range(1, 11)]
    e = labor_cog.ranking_embed({"rows": rows, "me": (14, 300, 90, 20)}, False)
    assert "🥇" in e.description and "🥉" in e.description and "あなた" in e.description and "通算" in e.title
    assert "まだ、だれも" in labor_cog.ranking_embed({"rows": [], "me": None}, True).description


# ------------------------------ 雇用（作業員） ------------------------------ #
H = 3600.0


class Quit(random.Random):  # 必ず辞める
    def random(self):
        return 0.0


def test_worker_numbers():
    assert lab.worker_cost("apprentice", 0) == 500 and lab.worker_cost("apprentice", 1) == 650
    assert lab.slots(0) == 5 and lab.slots(99) == 10 and lab.dorm_cost(5) is None and lab.dorm_cost(0) == 2_000
    counts = {"apprentice": 2, "craftsman": 1}
    assert lab.idle_rate(counts, "normal") == 2 * 10 + 80
    assert lab.idle_rate(counts, "low") == pytest.approx(100 * 1.25)
    assert lab.idle_rate({**counts, "foreman": 1}, "normal") == pytest.approx(100 * 1.08)
    assert lab.foreman_bonus(99) == lab.FOREMAN_BONUS_CAP
    assert lab.idle_hours(0, 100 * H) == lab.STOCK_HOURS and lab.idle_hours(100, 50) == 0


def test_hire_costs_pelica_and_grows_in_price(eco):
    ok, why = eco.labor_hire(U, "apprentice", T0, DAY)
    assert not ok and "足りません" in why
    eco.credit(U, 5_000, "テスト")
    assert eco.labor_hire(U, "apprentice", T0, DAY) == (True, "")
    assert eco.balance(U) == 5_000 - 500
    assert eco.labor_hire(U, "apprentice", T0, DAY) == (True, "")
    assert eco.balance(U) == 5_000 - 500 - 650  # 同じ種類は、値段が上がる
    assert eco.labor_hire(U, "bogus", T0, DAY)[0] is False


def test_workers_earn_while_idle_up_to_the_stock_limit(eco):
    eco.credit(U, 5_000, "テスト")
    eco.labor_hire(U, "apprentice", T0, DAY)
    eco.labor_hire(U, "apprentice", T0, DAY)  # 1時間 20ペリカ
    s = eco.labor_state(U, T0 + 2 * H, DAY)
    assert s["rate"] == 20 and s["idle_pending"] == 40 and s["pot_total"] == 40
    assert eco.labor_collect(U, T0 + 2 * H, DAY) == 40
    eco.labor_collect(U, T0 + 2 * H, DAY)
    assert eco.labor_collect(U, T0 + 100 * H, DAY) == int(20 * lab.STOCK_HOURS)  # 放っておいても、12時間分まで
    assert eco.labor_state(U, T0 + 100 * H, DAY)["earned_total"] == 40 + 240


def test_idle_income_is_taxed_for_debt_and_counts_for_ranking(eco):
    eco.set_balance(U, 1_000, "テスト")
    eco.labor_hire(U, "apprentice", T0, DAY)
    eco.set_balance(U, -2_000, "テスト")
    eco.labor_collect(U, T0 + 10 * H, DAY)
    assert eco.debt(U) < 2_000 and eco.labor_top(10, None)["rows"][0][1] == U


def test_slots_and_dorm(eco):
    eco.credit(U, 1_000_000, "テスト")
    for _ in range(5):
        assert eco.labor_hire(U, "apprentice", T0, DAY)[0]
    ok, why = eco.labor_hire(U, "apprentice", T0, DAY)
    assert not ok and "いっぱい" in why
    assert eco.labor_dorm(U, T0, DAY) == (True, "") and eco.labor_hire(U, "apprentice", T0, DAY)[0]
    for _ in range(4):
        eco.labor_dorm(U, T0, DAY)
    assert eco.labor_dorm(U, T0, DAY) == (False, "寮は、もう最大です。")


def test_dismiss_frees_a_slot_without_refund(eco):
    eco.credit(U, 100_000, "テスト")
    for _ in range(5):
        eco.labor_hire(U, "apprentice", T0, DAY)
    before = eco.balance(U)
    assert eco.labor_dismiss(U, "apprentice", T0, DAY) == (True, "")
    assert eco.balance(U) == before and eco.labor_hire(U, "miner", T0, DAY)[0]
    assert eco.labor_dismiss(U, "foreman", T0, DAY)[0] is False


def test_low_wage_pays_more_but_workers_quit(eco):
    eco.credit(U, 100_000, "テスト")
    eco.labor_hire(U, "apprentice", T0, DAY)
    eco.labor_hire(U, "apprentice", T0, DAY)
    assert eco.labor_set_wage(U, "low", T0, DAY) == (True, "")
    assert eco.labor_state(U, T0, DAY)["rate"] == pytest.approx(25)
    with eco._lock, eco._conn:
        row = eco._labor_row(U, T0 + 10 * H, DAY, Quit())  # 必ず辞める乱数
        eco._labor_save(U, row)
    assert eco.labor_state(U, T0 + 10 * H, DAY)["workers"] == 0
    assert eco.labor_set_wage(U, "bogus", T0, DAY)[0] is False


def test_wage_change_counts_the_earlier_time_at_the_old_rate(eco):
    eco.credit(U, 5_000, "テスト")
    eco.labor_hire(U, "miner", T0, DAY)
    eco.labor_set_wage(U, "low", T0 + 4 * H, DAY)  # 4時間は、標準で数える
    assert eco.labor_state(U, T0 + 4 * H, DAY)["pot"] == 4 * 35


def test_foreman_raises_everyones_income(eco):
    eco.credit(U, 100_000, "テスト")
    eco.labor_hire(U, "craftsman", T0, DAY)
    base = eco.labor_state(U, T0, DAY)["rate"]
    eco.labor_hire(U, "foreman", T0, DAY)
    assert eco.labor_state(U, T0, DAY)["rate"] == pytest.approx(base * 1.08)


def test_hire_tab_buttons_hire_and_dismiss_through_the_panel(eco):
    eco.credit(U, 5_000, "テスト")
    view = make_view(eco)
    it = SimpleNamespace(user=SimpleNamespace(id=U), response=Resp(), data={})
    asyncio.run(view._goto("hire")(it))
    assert view.tab == "hire" and it.response.edits[-1]["embed"].title.startswith("👷")
    labels = [c.label for c in view.children if hasattr(c, "label") and c.label]
    assert (
        any("見習い" in x for x in labels) and any("寮を強化" in x for x in labels) and any("賃金" in x for x in labels)
    )
    assert not [c for c in view.children if isinstance(c, labor_cog.discord.ui.Select)]  # まだ、誰もいない
    asyncio.run(view._hirer("apprentice")(it))
    assert eco.labor_state(U, 0, labor_cog.today(view.bot))["apprentice"] == 1
    selects = [c for c in view.children if isinstance(c, labor_cog.discord.ui.Select)]
    assert len(selects) == 1 and selects[0].options[0].value == "apprentice"
    it.data = {"values": ["apprentice"]}
    asyncio.run(view._dismiss(it))
    assert eco.labor_state(U, 0, labor_cog.today(view.bot))["apprentice"] == 0
    asyncio.run(view._wage(it))
    assert eco.labor_state(U, 0, labor_cog.today(view.bot))["wage"] == "low"
    assert "低賃金" in text(labor_cog.hire_embed(view.state))
    asyncio.run(view._goto("work")(it))
    assert view.children[0].label == "働く"


def test_hire_embed_shows_prices_slots_and_pending(eco):
    eco.credit(U, 5_000, "テスト")
    eco.labor_hire(U, "apprentice", T0, DAY)
    state = eco.labor_state(U, T0 + 3 * H, DAY)
    d = text(labor_cog.hire_embed(state))
    assert "1 / 5人" in d and "見習い" in d and "650" in d and "10/時" in d  # 次の1人は 650
    assert state["pot_total"] == 30 and "30" in text(labor_cog.panel_embed(state))


def test_next_goal_guides_a_new_player(eco):
    s = eco.labor_state(U, T0, DAY)
    assert "あと **500ペリカ**" in labor_cog.next_goal(s)  # 手元が足りないときは、あといくらかを出す
    eco.credit(U, 1_000, "テスト")
    assert "見習いを雇えます" in labor_cog.next_goal(eco.labor_state(U, T0, DAY))
    eco.labor_hire(U, "apprentice", T0, DAY)
    assert "ツルハシ" in labor_cog.next_goal(eco.labor_state(U, T0, DAY))
    assert labor_cog.fatigue_label(0) == "元気" and labor_cog.fatigue_label(90) == "ぐったり"
