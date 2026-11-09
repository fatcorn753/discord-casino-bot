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


_REAL_ROLL = lab.roll_ores


@pytest.fixture(autouse=True)
def no_ores(monkeypatch):
    """ふつうのテストは、鉱石を掘らない（稼ぎを一定にする）。鉱石のテストは、_REAL_ROLL を戻して使う。"""
    monkeypatch.setattr(lab, "roll_ores", lambda *a, **k: {})


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
    assert lab.contract_fee("apprentice") == 340 and lab.contract_fee("craftsman") == 2_700 and lab.CONTRACT_DAYS == 7
    assert lab.slots(0) == 5 and lab.slots(99) == 10 and lab.dorm_cost(5) is None and lab.dorm_cost(0) == 2_000
    counts = {"apprentice": 2, "craftsman": 1}
    assert lab.idle_rate(counts, "normal") == 2 * 2.0 + 16.5
    assert lab.idle_rate(counts, "low") == pytest.approx(20.5 * lab.WAGE_MULT["low"])
    assert lab.idle_rate({**counts, "foreman": 1}, "normal") == 20.5  # 監督は、基本給を増やさない
    assert lab.foreman_luck(99) == lab.FOREMAN_LUCK_CAP
    assert lab.idle_hours(0, 100 * H) == lab.STOCK_HOURS and lab.idle_hours(100, 50) == 0


def test_hire_costs_the_contract_fee_and_sets_a_7_day_term(eco):
    ok, why = eco.labor_hire(U, "apprentice", T0, DAY)
    assert not ok and "足りません" in why
    eco.credit(U, 5_000, "テスト")
    assert eco.labor_hire(U, "apprentice", T0, DAY) == (True, "")
    assert eco.balance(U) == 5_000 - 340
    assert eco.labor_hire(U, "apprentice", T0, DAY) == (True, "")
    assert eco.balance(U) == 5_000 - 340 - 340  # 同じ種類でも、値段は変わらない
    assert eco.labor_state(U, T0, DAY)["expires"]["apprentice"] == T0 + 7 * 86400
    assert eco.labor_hire(U, "bogus", T0, DAY)[0] is False


def test_workers_earn_while_idle_up_to_the_stock_limit(eco):
    eco.credit(U, 5_000, "テスト")
    eco.labor_hire(U, "apprentice", T0, DAY)
    eco.labor_hire(U, "apprentice", T0, DAY)  # 1時間 4ペリカ（基本給）
    s = eco.labor_state(U, T0 + 2 * H, DAY)
    assert s["rate"] == 4 and s["idle_pending"] == 8 and s["pot_total"] == 8
    assert eco.labor_collect(U, T0 + 2 * H, DAY) == 8
    eco.labor_collect(U, T0 + 2 * H, DAY)
    assert eco.labor_collect(U, T0 + 100 * H, DAY) == int(4 * lab.STOCK_HOURS)  # 放っておいても、12時間分まで
    assert eco.labor_state(U, T0 + 100 * H, DAY)["earned_total"] == 8 + 48


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
    assert eco.labor_state(U, T0, DAY)["rate"] == pytest.approx(4 * lab.WAGE_MULT["low"])
    with eco._lock, eco._conn:
        row = eco._labor_row(U, T0 + 10 * H, DAY, Quit())  # 必ず辞める乱数
        eco._labor_save(U, row)
    assert eco.labor_state(U, T0 + 10 * H, DAY)["workers"] == 0
    assert eco.labor_set_wage(U, "bogus", T0, DAY)[0] is False


def test_wage_change_counts_the_earlier_time_at_the_old_rate(eco):
    eco.credit(U, 5_000, "テスト")
    eco.labor_hire(U, "miner", T0, DAY)
    eco.labor_set_wage(U, "low", T0 + 4 * H, DAY)  # 4時間は、標準で数える
    assert eco.labor_state(U, T0 + 4 * H, DAY)["pot"] == int(4 * 7.5)


def test_foreman_raises_rare_ore_chance_not_the_wage(eco):
    eco.credit(U, 100_000, "テスト")
    eco.labor_hire(U, "craftsman", T0, DAY)
    base = eco.labor_state(U, T0, DAY)["rate"]
    eco.labor_hire(U, "foreman", T0, DAY)
    assert eco.labor_state(U, T0, DAY)["rate"] == base
    assert lab.ore_ev("craftsman", lab.foreman_luck(1)) == pytest.approx(lab.ore_ev("craftsman") * 1.1)


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
    assert "1 / 5人" in d and "見習い" in d and "340" in d and "基本給 2/時" in d and "自動更新" in d
    assert state["pot_total"] == 6 and "6" in text(labor_cog.panel_embed(state))


def test_fatigue_label_and_no_hints(eco):
    assert labor_cog.fatigue_label(0) == "元気" and labor_cog.fatigue_label(90) == "ぐったり"
    assert not hasattr(labor_cog, "next_goal")  # 画面に、ヒントは出さない
    eco.credit(U, 1_000, "テスト")
    assert "💡" not in text(labor_cog.panel_embed(eco.labor_state(U, T0, DAY)))


# ------------------------------ 栄養ドリンク ------------------------------ #
def buy_drink(eco, qty=1):
    from core import shop

    it = shop.BY_ID[shop.LABOR_DRINK]
    return eco.purchase(U, it.id, shop.label(it), it.price, it.unique, qty)


def test_drink_is_a_shop_item_and_recovers_fatigue(eco):
    from core import shop

    item = shop.BY_ID[shop.LABOR_DRINK]
    assert item.price == 100 and item.kind == shop.CONSUMABLE and shop.LABOR_DRINK in shop.AUTO_USED
    click(eco, 25)
    now = T0 + 25 * 1.5
    ok, why = eco.labor_drink(U, now, DAY)
    assert not ok and "持っていません" in why  # 買っていない
    eco.credit(U, 1_000, "テスト")
    assert buy_drink(eco, 3).ok
    assert eco.labor_state(U, now, DAY)["drinks"] == 3
    before = eco.labor_state(U, now, DAY)["fatigue"]
    assert before > 40 and eco.labor_drink(U, now, DAY) == (True, "")
    assert eco.labor_state(U, now, DAY)["fatigue"] == pytest.approx(max(0.0, before - lab.DRINK_RECOVER))
    assert eco.labor_state(U, now, DAY)["drinks"] == 2


def test_drink_is_not_wasted_when_not_tired(eco):

    eco.credit(U, 1_000, "テスト")
    buy_drink(eco, 1)
    assert eco.labor_drink(U, T0, DAY) == (False, "疲れていません。")
    assert eco.labor_state(U, T0, DAY)["drinks"] == 1  # 減らない


def test_drink_button_in_the_panel(eco):
    view = make_view(eco)
    drink = next(c for c in view.children if getattr(c, "emoji", None) and str(c.emoji) == "🥤")
    assert drink.disabled and "ショップ" in drink.label  # 持っていない
    eco.credit(U, 1_000, "テスト")
    buy_drink(eco, 2)
    now = labor_cog.time.time()
    for i in range(30):  # 画面は、いまの時刻で読むので、いまの時刻で押す
        eco.labor_click(U, now - 60 + i * 1.5, labor_cog.today(view.bot))
    view.refresh()
    drink = next(c for c in view.children if getattr(c, "emoji", None) and str(c.emoji) == "🥤")
    assert drink.label == "ドリンク ×2"
    it = SimpleNamespace(user=SimpleNamespace(id=U), response=Resp(), data={})
    asyncio.run(view._drink(it))
    assert eco.labor_state(U, now, labor_cog.today(view.bot))["drinks"] == 1 and it.response.edits
    assert "回復" in it.response.edits[-1]["embed"].description


# ------------------------------ 契約（7日・自動更新） ------------------------------ #
W = 7 * 86400.0


def worker(i, kind, expires):
    return {"id": i, "kind": kind, "expires_at": expires}


def test_settle_plan_no_renewals_needed():
    plan = lab.settle_plan([worker(1, "apprentice", T0 + W)], "normal", True, T0, T0 + 2 * H, 0, 0)
    assert plan == {"gain": 4, "ores": {}, "pot_spent": 0, "balance_spent": 0, "renewed": [], "removed": [], "quit": []}


def test_settle_plan_renews_from_pot_then_balance():
    now = T0 + 5 * H
    w = [worker(1, "apprentice", T0 + H)]  # 契約は、1時間で切れた
    plan = lab.settle_plan(w, "normal", True, T0, now, 0, 0)  # 払えない（稼ぎも手元も 0）… 更新前の稼ぎは、貯まらない
    assert plan["removed"] == [1] and plan["renewed"] == [] and plan["gain"] == 2  # 切れるまでの1時間分だけ稼いだ
    paid_pot = lab.settle_plan(
        [worker(1, "apprentice", T0 + H), worker(2, "apprentice", T0 + 9 * H)], "normal", True, T0, now, 340, 0
    )
    assert paid_pot["renewed"] == [(1, now + W)] and paid_pot["pot_spent"] == 340 and paid_pot["balance_spent"] == 0
    paid_bal = lab.settle_plan(w, "normal", True, T0, now, 100, 1_000)
    assert paid_bal["pot_spent"] == 100 and paid_bal["balance_spent"] == 240 and paid_bal["removed"] == []


def test_settle_plan_renew_off_removes_expired_workers():
    plan = lab.settle_plan([worker(1, "miner", T0 + H)], "normal", False, T0, T0 + 4 * H, 9_999, 9_999)
    assert plan["removed"] == [1] and plan["pot_spent"] == 0 and plan["balance_spent"] == 0 and plan["gain"] == 7


def test_workers_leave_when_contract_ends_and_nothing_pays_for_renewal(eco):
    eco.credit(U, 340, "テスト")
    eco.labor_hire(U, "apprentice", T0, DAY)  # 手元は 0。1週間、見に来なかった（稼ぎは、12時間分しか数えない）
    now = T0 + W + H
    s = eco.labor_state(U, now, DAY)
    assert s["apprentice"] == 0 and s["renew_fee"] == 0  # 契約料を払えないので、退職
    eco.labor_collect(U, now, DAY)
    assert eco.labor_state(U, now, DAY)["workers"] == 0


def test_auto_renew_pays_from_the_pot_when_you_visit_regularly(eco, monkeypatch):
    monkeypatch.setitem(lab.WORKER_RATE, "miner", 35)  # 契約料を稼ぎで払える数字にして、仕組みだけを確かめる
    eco.credit(U, 5_000, "テスト")
    eco.labor_hire(U, "miner", T0, DAY)  # 1時間 35。契約料 1,200
    for k in range(1, 17):  # 10時間ごとに見に来る（クリックでも、決算される）
        eco.labor_click(U, T0 + k * 10 * H, DAY)
    balance_before = eco.balance(U)
    now = T0 + W + H  # 契約が切れた直後
    after = eco.labor_state(U, now, DAY)
    assert after["miner"] == 1 and after["expires"]["miner"] == now + W and after["renew_fee"] == 1_200
    got = eco.labor_collect(U, now, DAY)
    assert got > 1_200 and eco.balance(U) == balance_before + got  # 契約料は、稼ぎから引かれた（手元は減らない）
    assert not any(
        "契約の更新" in r["reason"] for r in eco._conn.execute("SELECT reason FROM ledger WHERE user_id=?", (U,))
    )


def test_renewal_fee_comes_from_balance_when_the_pot_is_empty(eco):
    eco.credit(U, 6_200, "テスト")
    eco.labor_hire(U, "miner", T0, DAY)  # 契約料 1,200。手元に 5,000 残る
    now = T0 + W + H  # 1週間、見に来なかった（pot は 0）
    eco.labor_collect(U, now, DAY)
    assert eco.labor_state(U, now, DAY)["miner"] == 1
    assert eco.balance(U) == 5_000 - 1_200 + int(
        7.5 * 12
    )  # 手元から契約料が引かれ、更新した作業員の、12時間分の稼ぎが入る
    assert any(
        "契約の更新" in r["reason"] for r in eco._conn.execute("SELECT reason FROM ledger WHERE user_id=?", (U,))
    )


def test_renew_toggle_off_lets_contract_lapse(eco):
    eco.credit(U, 2_000, "テスト")
    eco.labor_hire(U, "apprentice", T0, DAY)
    assert eco.labor_state(U, T0, DAY)["renew"] == 1
    assert eco.labor_set_renew(U, False, T0 + H, DAY) == (True, "")
    assert eco.labor_state(U, T0 + H, DAY)["renew"] == 0
    assert eco.labor_state(U, T0 + W + 2 * H, DAY)["apprentice"] == 0  # 切れて、更新されずに退職


def test_dismiss_removes_the_earliest_expiring_worker(eco):
    eco.credit(U, 5_000, "テスト")
    eco.labor_hire(U, "apprentice", T0, DAY)
    eco.labor_hire(U, "apprentice", T0 + 3 * 86400, DAY)
    assert eco.labor_dismiss(U, "apprentice", T0 + 3 * 86400, DAY) == (True, "")
    assert eco.labor_state(U, T0 + 3 * 86400, DAY)["expires"]["apprentice"] == T0 + 3 * 86400 + W  # 古い契約から消える


def test_old_count_columns_migrate_to_contracts(eco):
    from core import schema

    eco._conn.execute("INSERT OR IGNORE INTO labor (guild_id, user_id) VALUES (?, ?)", (eco._g(), U))
    eco._conn.execute("UPDATE labor SET apprentice=2, miner=1 WHERE guild_id=? AND user_id=?", (eco._g(), U))
    schema._migrate_labor_counts(eco._conn, T0)
    schema._migrate_labor_counts(eco._conn, T0)  # 二度動かしても、増えない
    s = eco.labor_state(U, T0, DAY)
    assert s["apprentice"] == 2 and s["miner"] == 1 and s["expires"]["miner"] == T0 + W
    assert eco._conn.execute("SELECT apprentice+miner c FROM labor WHERE user_id=?", (U,)).fetchone()["c"] == 0


# ------------------------------ 鉱石 ------------------------------ #
class Fixed(random.Random):
    """random() が、決めた値を順に返す（尽きたら 1.0 = 何も出ない）。"""

    def __init__(self, *vals):
        super().__init__()
        self.vals = list(vals)

    def random(self):
        return self.vals.pop(0) if self.vals else 1.0


def test_ore_numbers_are_a_losing_bet_on_average():
    for kind in ("apprentice", "miner", "craftsman"):
        week_hours = lab.STOCK_HOURS * 7  # 毎日1回受け取る人の、1週間の働き
        mean = (lab.WORKER_RATE[kind] + lab.ore_ev(kind)) * week_hours
        mean_low = (lab.WORKER_RATE[kind] * lab.WAGE_MULT["low"] + lab.ore_ev(kind)) * week_hours
        assert 0.8 * lab.CONTRACT_FEE[kind] < mean < lab.CONTRACT_FEE[kind]  # 平均は赤字（ただし、遠くはない）
        assert mean_low < lab.CONTRACT_FEE[kind]  # 低賃金でも、平均は赤字
        base_only = lab.WORKER_RATE[kind] * lab.WAGE_MULT["low"] * week_hours
        assert base_only < 0.7 * lab.CONTRACT_FEE[kind]  # 基本給だけでは、はっきり赤字


def test_roll_ores_follows_the_table_and_foreman_luck():
    assert _REAL_ROLL("miner", 3, 0.0, Fixed()) == {}  # 全部外れ
    # 銅の区間は [0, p)。最初の抽選だけ当たり、あとは外れ
    copper_p = lab.ORES[0][3] * lab.ORE_POWER["miner"]
    assert _REAL_ROLL("miner", 3, 0.0, Fixed(copper_p * 0.5)) == {"copper": 1}
    just_over = copper_p * 1.01  # 銅を超えて、銀の区間に入る
    assert _REAL_ROLL("miner", 1, 0.0, Fixed(just_over)) == {"silver": 1}
    assert _REAL_ROLL("miner", 1, 0.5, Fixed(just_over)) == {"copper": 1}  # 運がいいと、同じ値でも銅に収まる
    assert _REAL_ROLL("foreman", 99, 0.5, Fixed(0.0)) == {}  # 監督は掘らない
    legend = _REAL_ROLL("craftsman", 1, 0.0, Fixed(sum(o[3] for o in lab.ORES[:-1]) * 1.9 + 1e-9))
    assert legend == {"legend": 1}
    assert lab.ore_gain({"gold": 2, "diamond": 1}) == 2 * 400 + 3_000


def test_settle_plan_adds_ore_value_to_the_gain(monkeypatch):
    monkeypatch.setattr(lab, "roll_ores", _REAL_ROLL)
    rng = Fixed(0.0, 0.0)  # 2時間とも、銅
    plan = lab.settle_plan([worker(1, "miner", T0 + W)], "normal", True, T0, T0 + 2 * H, 0, 0, rng)
    assert plan["ores"] == {"copper": 2} and plan["gain"] == int(7.5 * 2) + 2 * 20


def test_ores_pile_up_until_collected_and_are_reported(eco, monkeypatch):
    monkeypatch.setattr(lab, "roll_ores", lambda kind, h, luck, rng: {"copper": 2, "diamond": 1} if h > 0 else {})
    eco.credit(U, 5_000, "テスト")
    eco.labor_hire(U, "miner", T0, DAY)
    s1 = eco.labor_state(U, T0 + H, DAY)
    assert s1["ores"] == {}  # 見込みでは、掘らない
    eco.labor_click(U, T0 + H, DAY)  # 触れると決算される
    s2 = eco.labor_state(U, T0 + H, DAY)
    assert s2["ores"] == {"copper": 2, "diamond": 1}
    assert s2["pot_total"] >= 3_040 + int(7.5)
    got = eco.labor_collect(U, T0 + H, DAY, detail=True)
    assert got["ores"] == {"copper": 2, "diamond": 1} and got["amount"] >= 3_040
    assert eco.labor_state(U, T0 + H, DAY)["ores"] == {}


def test_collect_announces_big_ores_publicly(eco, monkeypatch):
    monkeypatch.setattr(lab, "roll_ores", lambda kind, h, luck, rng: {"copper": 1, "diamond": 1} if h > 0 else {})
    eco.credit(U, 5_000, "テスト")
    eco.labor_hire(U, "miner", T0, DAY)
    view = make_view(eco)
    sent = []

    async def send(content, **kw):
        sent.append(content)

    it = SimpleNamespace(
        user=SimpleNamespace(id=U, mention="<@1>"), response=Resp(), data={}, followup=SimpleNamespace(send=send)
    )
    monkeypatch.setattr(labor_cog.time, "time", lambda: T0 + H)
    asyncio.run(view._collect(it))
    assert sent and "ダイヤ" in sent[0] and "銅" not in sent[0]
    assert "掘り出し" in it.response.edits[-1]["embed"].description
