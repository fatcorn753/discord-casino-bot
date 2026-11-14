"""地下作業（クリッカー）: 計算・疲労・受け取り・強化・画面。"""

import asyncio
import random
from types import SimpleNamespace

import pytest

from cogs import labor_cog
from core import labor as lab
from core import teiai
from core.economy import Economy

U = 1
DAY = "2026-11-10"
T0 = 1_700_000_000.0


def text(embed) -> str:
    """埋め込みの、見出し・説明・欄をつなげた文字列。"""
    return "\n".join([embed.title or "", embed.description or ""] + [f"{f.name} {f.value}" for f in embed.fields])


_REAL_ROLL = lab.roll_ores
_REAL_RELICS = lab.roll_relics
_REAL_STRATUM = lab.daily_stratum


@pytest.fixture(autouse=True)
def no_ores(monkeypatch):
    """ふつうのテストは、鉱石を掘らない（稼ぎを一定にする）。鉱石のテストは、_REAL_ROLL を戻して使う。"""
    monkeypatch.setattr(lab, "roll_ores", lambda *a, **k: {})
    monkeypatch.setattr(
        lab, "roll_relics", lambda *a, **k: {}
    )  # 発掘品も、掘らない（まれに出て、テストがぶれるのを防ぐ）
    monkeypatch.setattr(lab, "roll_click_bonus", lambda *a, **k: {"crit": False, "ore": None})
    monkeypatch.setattr(lab, "daily_stratum", lambda day: dict(lab.NEUTRAL_STRATUM))
    monkeypatch.setattr(lab, "COMBO_BONUS_RATE", 0.0)
    for kind, rate in (("apprentice", 1.0), ("miner", 3.75), ("craftsman", 8.25)):
        monkeypatch.setitem(
            lab.WORKER_RATE, kind, rate
        )  # 作業員の稼ぎの数字を固定して、計算の仕組みだけを確かめる（本物の数字は、専用のテストで）
    monkeypatch.setattr(teiai, "LABOR_SKIM_RATE", 0.0)  # 帝愛のピンハネは、専用のテストで確かめる


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
    assert lab.pay_per_click(0) == 10 and lab.pay_per_click(5) == 30 and lab.pay_per_click(99) == 50
    assert lab.pay_rate(0) == 1.0 and abs(lab.pay_rate(100) - lab.MIN_PAY_RATE) < 1e-9
    assert lab.fatigue_gain(5) == 1.0
    assert lab.fatigue_now(50, 0, 100) == 40 and lab.fatigue_now(10, 0, 1000) == 0
    assert lab.upgrade_cost(0) == 150 and lab.upgrade_cost(10) is None
    assert lab.click_pay(0, 100) == 2 and lab.click_pay(0, 0) == 10


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


def test_no_balance_limit_and_today_total_resets(eco):
    eco.set_balance(U, 1_000_000, "テスト")  # 金持ちも、働ける
    for i in range(300):  # 残高に上限はない（MAX_CLICKS_PER_DAY・疲労で、稼ぎの速さが頭打ちになるだけ）
        assert eco.labor_click(U, T0 + i * 1.5, DAY)["ok"]
    assert eco.labor_state(U, T0 + 500, DAY)["day_earned"] > 600
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


def test_worker_numbers():
    assert lab.contract_fee("apprentice") == lab.CONTRACT_FEE["apprentice"] and lab.CONTRACT_DAYS == 1  # 契約は1日
    assert lab.slots(0) == 5 and lab.slots(99) == 10 and lab.dorm_cost(5) is None and lab.dorm_cost(0) == 2_000
    counts = {"apprentice": 2, "craftsman": 1}
    assert lab.idle_rate(counts) == 2 * 1.0 + 8.25
    assert lab.idle_rate({**counts, "foreman": 1}) == 10.25  # 監督は、作業員の稼ぎを増やさない
    assert lab.foreman_luck(8) == pytest.approx(2.0) and lab.foreman_luck(0) == 0  # 上限はない
    assert lab.idle_hours(0, 100 * H) == lab.STOCK_HOURS and lab.idle_hours(100, 50) == 0


def test_hire_costs_the_contract_fee_and_sets_a_one_day_term(eco):
    ok, why = eco.labor_hire(U, "apprentice", T0, DAY)
    assert not ok and "足りません" in why
    eco.credit(U, 5_000, "テスト")
    assert eco.labor_hire(U, "apprentice", T0, DAY) == (True, "")
    fee = lab.CONTRACT_FEE["apprentice"]
    assert eco.balance(U) == 5_000 - fee
    assert eco.labor_hire(U, "apprentice", T0, DAY) == (True, "")
    assert eco.balance(U) == 5_000 - fee - fee  # 同じ種類でも、値段は変わらない
    assert eco.labor_state(U, T0, DAY)["expires"]["apprentice"] == T0 + 86400
    assert eco.labor_hire(U, "bogus", T0, DAY)[0] is False


def test_workers_earn_while_idle_up_to_the_stock_limit(eco):
    eco.credit(U, 5_000, "テスト")
    eco.labor_hire(U, "apprentice", T0, DAY)
    eco.labor_hire(U, "apprentice", T0, DAY)  # 1時間 2ペリカ（作業員の稼ぎ）
    s = eco.labor_state(U, T0 + 2 * H, DAY)
    assert s["rate"] == 2 and s["idle_pending"] == 4 and s["pot_total"] == 4
    assert eco.labor_collect(U, T0 + 2 * H, DAY) == 4
    eco.labor_collect(U, T0 + 2 * H, DAY)
    assert eco.labor_collect(U, T0 + 100 * H, DAY) == int(2 * lab.STOCK_HOURS)  # 放っておいても、12時間分まで
    assert eco.labor_state(U, T0 + 100 * H, DAY)["earned_total"] == 4 + 48


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


def test_foreman_raises_rare_ore_chance_not_the_wage(eco):
    eco.credit(U, 100_000, "テスト")
    eco.labor_hire(U, "craftsman", T0, DAY)
    base = eco.labor_state(U, T0, DAY)["rate"]
    eco.labor_hire(U, "foreman", T0, DAY)
    assert eco.labor_state(U, T0, DAY)["rate"] == base
    coal = lab.COAL_P * lab.ORE_VALUE["coal"]  # 石炭は、監督の運に関係ない
    assert lab.ore_ev("craftsman", lab.foreman_luck(1)) - coal == pytest.approx((lab.ore_ev("craftsman") - coal) * 1.25)


def test_hire_tab_buttons_hire_and_dismiss_through_the_panel(eco):
    eco.credit(U, 5_000, "テスト")
    view = make_view(eco)
    it = SimpleNamespace(user=SimpleNamespace(id=U), response=Resp(), data={})
    asyncio.run(view._goto("hire")(it))
    assert view.tab == "hire" and it.response.edits[-1]["embed"].title.startswith("👷")
    labels = [c.label for c in view.children if hasattr(c, "label") and c.label]
    assert any("見習い" in x for x in labels) and any("寮を強化" in x for x in labels)
    assert not any("賃金" in x for x in labels)  # 賃金（待遇）の設定は、なくなった
    assert "賃金" not in text(labor_cog.hire_embed(view.state))
    assert not [c for c in view.children if isinstance(c, labor_cog.discord.ui.Select)]  # まだ、誰もいない
    asyncio.run(view._hirer("apprentice")(it))
    assert eco.labor_state(U, 0, labor_cog.today(view.bot))["apprentice"] == 1
    selects = [c for c in view.children if isinstance(c, labor_cog.discord.ui.Select)]
    assert len(selects) == 1 and selects[0].options[0].value == "apprentice"
    it.data = {"values": ["apprentice"]}
    asyncio.run(view._dismiss(it))
    assert eco.labor_state(U, 0, labor_cog.today(view.bot))["apprentice"] == 0
    asyncio.run(view._goto("work")(it))
    assert view.children[0].label == "働く"


def test_hire_embed_shows_prices_slots_and_pending(eco):
    eco.credit(U, 5_000, "テスト")
    eco.labor_hire(U, "apprentice", T0, DAY)
    state = eco.labor_state(U, T0 + 3 * H, DAY)
    d = text(labor_cog.hire_embed(state))
    assert (
        "1 / 5人" in d
        and "見習い" in d
        and str(lab.CONTRACT_FEE["apprentice"]) in d
        and "稼ぎ 1/時" in d
        and "自動更新" in d
    )
    assert state["pot_total"] == 3 and "3" in text(labor_cog.panel_embed(state))


def test_fatigue_label_and_no_hints(eco):
    assert labor_cog.fatigue_label(0) == "元気" and labor_cog.fatigue_label(90) == "ぐったり"
    assert not hasattr(labor_cog, "next_goal")  # 画面に、ヒントは出さない
    eco.credit(U, 1_000, "テスト")
    assert "💡" not in text(labor_cog.panel_embed(eco.labor_state(U, T0, DAY)))


# ------------------------------ 栄養ドリンコ ------------------------------ #
def buy_drink(eco, qty=1):
    from core import shop

    it = shop.BY_ID[shop.LABOR_DRINK]
    return eco.purchase(U, it.id, shop.label(it), it.price, it.unique, qty)


def test_drink_is_a_shop_item_and_recovers_fatigue(eco):
    from core import shop

    item = shop.BY_ID[shop.LABOR_DRINK]
    assert item.price == 300 and item.kind == shop.CONSUMABLE and shop.LABOR_DRINK in shop.AUTO_USED
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
    assert drink.label == "栄養ドリンコ ×2"
    it = SimpleNamespace(user=SimpleNamespace(id=U), response=Resp(), data={})
    asyncio.run(view._drink(it))
    assert eco.labor_state(U, now, labor_cog.today(view.bot))["drinks"] == 1 and it.response.edits
    assert "回復" in it.response.edits[-1]["embed"].description


# ------------------------------ 契約（7日・自動更新） ------------------------------ #
W = lab.CONTRACT_DAYS * 86400.0
FEE = lab.CONTRACT_FEE  # 契約料（種類ごと）


def worker(i, kind, expires):
    return {"id": i, "kind": kind, "expires_at": expires}


def test_settle_plan_no_renewals_needed():
    plan = lab.settle_plan([worker(1, "apprentice", T0 + W)], True, T0, T0 + 2 * H, 0, 0)
    assert plan == {
        "gain": 2,
        "ores": {},
        "relics": {},
        "finds": [],
        "pot_spent": 0,
        "balance_spent": 0,
        "renewed": [],
        "removed": [],
    }


def test_settle_plan_renews_from_pot_then_balance():
    now = T0 + 5 * H
    w = [worker(1, "apprentice", T0 + H)]  # 契約は、1時間で切れた
    plan = lab.settle_plan(w, True, T0, now, 0, 0)  # 払えない（稼ぎも手元も 0）… 更新前の稼ぎは、貯まらない
    assert plan["removed"] == [1] and plan["renewed"] == [] and plan["gain"] == 1  # 切れるまでの1時間分だけ稼いだ
    paid_pot = lab.settle_plan(
        [worker(1, "apprentice", T0 + H), worker(2, "apprentice", T0 + 9 * H)],
        True,
        T0,
        now,
        FEE["apprentice"],
        0,
    )
    assert paid_pot["renewed"] == [(1, now + W)] and paid_pot["pot_spent"] == FEE["apprentice"]
    assert paid_pot["balance_spent"] == 0
    paid_bal = lab.settle_plan(w, True, T0, now, 100, 1_000)
    assert paid_bal["pot_spent"] == 100 and paid_bal["balance_spent"] == FEE["apprentice"] - 100
    assert paid_bal["removed"] == []


def test_settle_plan_renew_off_removes_expired_workers():
    plan = lab.settle_plan([worker(1, "miner", T0 + H)], False, T0, T0 + 4 * H, 9_999, 9_999)
    assert plan["removed"] == [1] and plan["pot_spent"] == 0 and plan["balance_spent"] == 0 and plan["gain"] == 3


def test_workers_leave_when_contract_ends_and_nothing_pays_for_renewal(eco):
    eco.credit(U, FEE["apprentice"], "テスト")
    eco.labor_hire(
        U, "apprentice", T0, DAY
    )  # 手元は 0。契約が切れるまで、見に来なかった（稼ぎは、24時間分しか数えない）
    now = T0 + W + H
    s = eco.labor_state(U, now, DAY)
    assert s["apprentice"] == 0 and s["renew_fee"] == 0  # 契約料を払えないので、退職
    eco.labor_collect(U, now, DAY)
    assert eco.labor_state(U, now, DAY)["workers"] == 0


def test_auto_renew_pays_from_the_pot_when_you_visit_regularly(eco, monkeypatch):
    monkeypatch.setitem(lab.WORKER_RATE, "miner", 35)  # 契約料を稼ぎで払える数字にして、仕組みだけを確かめる
    fee = FEE["miner"]
    eco.credit(U, 5_000, "テスト")
    eco.labor_hire(U, "miner", T0, DAY)  # 1時間 35（1日で 840）
    for k in range(1, 8):  # 3時間ごとに見に来る（クリックでも、決算される）
        eco.labor_click(U, T0 + k * 3 * H, DAY)
    balance_before = eco.balance(U)
    now = T0 + W + H  # 契約が切れた直後
    after = eco.labor_state(U, now, DAY)
    assert after["miner"] == 1 and after["expires"]["miner"] == now + W and after["renew_fee"] == fee
    got = eco.labor_collect(U, now, DAY)
    assert got > 0 and eco.balance(U) == balance_before + got  # 契約料は、稼ぎから引かれた（手元は減らない）
    assert got < 840 + 100  # 1日分の稼ぎ（840 + クリック）から、契約料が引かれて、残りが入る
    assert not any(
        "契約の更新" in r["reason"] for r in eco._conn.execute("SELECT reason FROM ledger WHERE user_id=?", (U,))
    )


def test_renewal_fee_comes_from_balance_when_the_pot_is_empty(eco):
    fee = FEE["miner"]
    eco.credit(U, 5_000 + fee, "テスト")
    eco.labor_hire(U, "miner", T0, DAY)  # 手元に 5,000 残る
    now = T0 + W + H  # 契約が切れるまで、見に来なかった（pot は 0）
    eco.labor_collect(U, now, DAY)
    assert eco.labor_state(U, now, DAY)["miner"] == 1
    assert eco.balance(U) == 5_000 - fee + int(
        3.75 * 24
    )  # 手元から契約料が引かれ、更新した作業員の、24時間分の稼ぎが入る
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
    assert s["apprentice"] == 2 and s["miner"] == 1 and s["expires"]["miner"] == T0 + 7 * 86400  # 移行は、7日の契約
    assert eco._conn.execute("SELECT apprentice+miner c FROM labor WHERE user_id=?", (U,)).fetchone()["c"] == 0


# ------------------------------ 鉱石 ------------------------------ #
class Fixed(random.Random):
    """random() が、決めた値を順に返す（尽きたら 1.0 = 何も出ない）。"""

    def __init__(self, *vals):
        super().__init__()
        self.vals = list(vals)

    def random(self):
        return self.vals.pop(0) if self.vals else 1.0


def test_ore_numbers_are_a_losing_bet_on_average(monkeypatch):
    monkeypatch.undo()  # 本物の作業員の稼ぎ・鉱石の数字で確かめる
    for kind in ("apprentice", "miner", "craftsman"):
        week_hours = lab.STOCK_HOURS * lab.CONTRACT_DAYS  # 毎日1回受け取る人の、契約の期間の働き
        mean = (lab.WORKER_RATE[kind] + lab.ore_ev(kind)) * week_hours
        assert 0.8 * lab.CONTRACT_FEE[kind] < mean < lab.CONTRACT_FEE[kind]  # 平均は赤字（ただし、遠くはない）
        base_only = lab.WORKER_RATE[kind] * week_hours
        assert base_only < 0.7 * lab.CONTRACT_FEE[kind]  # 作業員の稼ぎだけでは、はっきり赤字


def test_roll_ores_follows_the_table_and_foreman_luck():
    assert _REAL_ROLL("miner", 3, 0.0, Fixed()) == {}  # 全部外れ
    # 銅の区間は [0, p)。最初の抽選だけ当たり、あとは外れ
    copper_p = lab.ORES[0][3] * lab.ORE_POWER["miner"]
    assert _REAL_ROLL("miner", 3, 0.0, Fixed(copper_p * 0.5)) == {"copper": 1}
    just_over = copper_p * 1.01  # 銅を超えて、銀の区間に入る
    assert _REAL_ROLL("miner", 1, 0.0, Fixed(just_over)) == {"silver": 1}
    assert _REAL_ROLL("miner", 1, 0.5, Fixed(just_over)) == {"copper": 1}  # 運がいいと、同じ値でも銅に収まる
    assert _REAL_ROLL("foreman", 99, 0.5, Fixed(0.0)) == {}  # 監督は掘らない
    legend = _REAL_ROLL(
        "craftsman", 1, 0.0, Fixed(sum(o[3] for o in lab.ORES[:-2]) * lab.ORE_POWER["craftsman"] + 1e-9)
    )
    assert legend == {"legend": 1}
    assert lab.ore_gain({"gold": 2, "diamond": 1}) == 2 * lab.ORE_VALUE["gold"] + lab.ORE_VALUE["diamond"]


def test_settle_plan_adds_ore_value_to_the_gain(monkeypatch):
    monkeypatch.setattr(lab, "roll_ores", _REAL_ROLL)
    rng = Fixed(0.0, 0.0)  # 2時間とも、銅
    plan = lab.settle_plan([worker(1, "miner", T0 + W)], True, T0, T0 + 2 * H, 0, 0, rng)
    assert plan["ores"] == {"copper": 2} and plan["gain"] == int(3.75 * 2) + 2 * lab.ORE_VALUE["copper"]


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


# ------------------------------ 旧契約の返金 ------------------------------ #
def test_old_contracts_are_refunded_and_cancelled_once_when_the_legacy_column_is_added(tmp_path):
    path = str(tmp_path / "old.db")
    e = Economy(path)
    e.set_balance(U, 10_000, "テスト")
    for kind in ("craftsman", "craftsman", "apprentice", "foreman"):
        assert e.labor_hire(U, kind, T0, DAY)[0]
    balance_after_hiring = e.balance(U)
    assert balance_after_hiring == 10_000 - 2 * FEE["craftsman"] - FEE["apprentice"] - FEE["foreman"]
    e._conn.execute("ALTER TABLE labor_workers DROP COLUMN legacy")  # この列が無かった頃の記録にする
    e._conn.commit()
    e.close() if hasattr(e, "close") else None

    again = Economy(path)  # 起動: 列が足され、旧契約の契約料が戻って、解約される
    assert again.balance(U) == 10_000
    assert again.labor_state(U, T0, DAY)["workers"] == 0
    assert any(
        "契約料の返金" in r["reason"] and r["delta"] == 2 * FEE["craftsman"] + FEE["apprentice"] + FEE["foreman"]
        for r in again._conn.execute("SELECT reason, delta FROM ledger WHERE user_id=?", (U,))
    )
    assert again.labor_hire(U, "miner", T0, DAY)[0]  # 雇い直せる
    third = Economy(path)  # 次の起動では、新しい契約は返金されない
    assert third.labor_state(U, T0, DAY)["miner"] == 1 and third.balance(U) == 10_000 - FEE["miner"]


# ------------------------------ 帝愛のピンハネ ------------------------------ #
def test_teiai_skims_the_labor_earnings_when_collected(eco, monkeypatch):
    monkeypatch.setattr(teiai, "LABOR_SKIM_RATE", 0.10)
    eco.set_balance(U, 0, "テスト")
    click(eco, 10)
    pot = eco.labor_state(U, T0 + 100, DAY)["pot_total"]
    assert pot > 0
    got = eco.labor_collect(U, T0 + 100, DAY, detail=True)
    skim = int(pot * 0.10)
    assert got["skimmed"] == skim and got["amount"] == pot - skim
    assert eco.balance(U) == pot - skim  # 手元に入るのは、ピンハネの残り
    assert any(
        "慈善団体への寄付（地下作業" in r["reason"]
        for r in eco._conn.execute("SELECT reason FROM ledger WHERE user_id=?", (U,))
    )


def test_labor_skim_goes_to_the_teiai_house_account(eco, monkeypatch):
    monkeypatch.setattr(teiai, "LABOR_SKIM_RATE", 0.10)
    assert eco.house_account() == {"balance": 0, "skimmed": 0}
    click(eco, 20)
    got = eco.labor_collect(U, T0 + 100, DAY, detail=True)
    assert got["skimmed"] > 0
    assert eco.house_account() == {"balance": got["skimmed"], "skimmed": got["skimmed"]}  # 全額が、胴元の口座に入る
    click(eco, 20, start=T0 + 200)
    again = eco.labor_collect(U, T0 + 400, DAY, detail=True)
    assert eco.house_account()["balance"] == got["skimmed"] + again["skimmed"]


def test_the_skim_applies_to_everyone_and_the_debt_withholding_takes_its_share_of_the_rest(eco, monkeypatch):
    monkeypatch.setattr(teiai, "LABOR_SKIM_RATE", 0.10)
    eco._conn.execute("UPDATE users SET balance=0, debt=1000 WHERE user_id=?", (U,))
    click(eco, 20)
    pot = eco.labor_state(U, T0 + 100, DAY)["pot_total"]
    debt_before = eco.debt(U)
    got = eco.labor_collect(U, T0 + 100, DAY, detail=True)
    net = pot - int(pot * 0.10)
    assert got["amount"] == net
    assert eco.debt(U) == debt_before - int(net * teiai.REPAY_RATE)  # 借金の天引きは、ピンハネの残りにかかる


def test_the_default_skim_is_one_percent_and_the_screens_call_it_a_charity_donation(eco, monkeypatch):
    monkeypatch.setattr(teiai, "LABOR_SKIM_RATE", 0.01)
    assert teiai.labor_skim(10_000) == 100
    monkeypatch.setattr(lab, "roll_ores", lambda kind, h, luck, rng: {"diamond": 1} if h > 0 else {})
    eco.credit(U, 5_000, "テスト")
    eco.labor_hire(U, "miner", T0, DAY)
    view = make_view(eco)
    assert "総寄付金額" in labor_cog.panel_embed(eco.labor_state(U, T0, DAY)).footer.text
    sent = []

    async def send(content, **kw):
        sent.append(content)

    it = SimpleNamespace(
        user=SimpleNamespace(id=U, mention="<@1>"), response=Resp(), data={}, followup=SimpleNamespace(send=send)
    )
    monkeypatch.setattr(labor_cog.time, "time", lambda: T0 + H)
    asyncio.run(view._collect(it))
    desc = it.response.edits[-1]["embed"].description
    assert "慈善団体に寄付されます" in desc and str(lab.ORE_VALUE["diamond"] // 100) in desc  # ダイヤの1%


def test_cheap_coal_shows_up_often_for_every_worker_and_ignores_power_and_luck():
    assert lab.COAL_P >= 0.3 and lab.ORE_VALUE["coal"] <= 5
    for kind in ("apprentice", "miner", "craftsman"):
        any_per_hour = lab.COAL_P + sum(p * lab.ORE_POWER[kind] for o, _, _, p, v in lab.ORES if o not in lab.FLAT)
        assert any_per_hour >= 0.4  # 1時間に、4割以上の確率で、何か掘れる
    rng = random.Random(3)
    for kind in ("apprentice", "craftsman"):
        n = 4000
        coal = sum(_REAL_ROLL(kind, 1, 0.0, rng).get("coal", 0) for _ in range(n)) / n
        assert abs(coal - lab.COAL_P) < 0.03  # 作業員の種類で変わらない
    boosted = sum(_REAL_ROLL("apprentice", 1, 2.0, rng).get("coal", 0) for _ in range(4000)) / 4000
    assert abs(boosted - lab.COAL_P) < 0.03  # 監督の運でも変わらない
    assert _REAL_ROLL("foreman", 50, 0.0, rng) == {}  # 監督は、掘らない
    assert [k for k in lab.ORE_VALUE][:1] == ["copper"] and lab.ORE_VALUE["coal"] < lab.ORE_VALUE["copper"]


def test_ore_text_lists_the_most_valuable_first():
    text = labor_cog.ore_text({"coal": 5, "diamond": 1, "copper": 2})
    assert text.index("ダイヤ") < text.index("銅") < text.index("石炭")


def test_uncollected_shows_how_much_of_it_the_workers_earned(eco, monkeypatch):
    from cogs.labor_cog import hire_embed, panel_embed

    monkeypatch.setattr(lab, "roll_ores", lambda kind, h, luck, rng: {"copper": 2} if kind == "miner" and h > 0 else {})
    eco.credit(U, 10_000, "テスト")
    eco.labor_hire(U, "miner", T0, DAY)
    eco.labor_click(U, T0 + 2 * H, DAY)  # 触れると決算される（クリックの稼ぎも、未受け取りに入る）
    state = eco.labor_state(U, T0 + 2 * H, DAY)
    assert 0 < state["pot_workers_total"] < state["pot_total"]  # 作業員の分は、全体の一部
    worker_part = state["pot_workers_total"]
    for embed in (panel_embed(state), hire_embed(state)):
        field = next(f for f in embed.fields if f.name == "🎒 未受け取り")
        assert f"うち作業員 {worker_part:,}ペリカ" in field.value
    eco.labor_collect(U, T0 + 2 * H, DAY)
    after = eco.labor_state(U, T0 + 2 * H, DAY)
    assert after["pot_workers_total"] == 0  # 受け取ると、0 に戻る


def test_uncollected_hides_the_worker_part_when_there_are_no_workers(eco):
    from cogs.labor_cog import panel_embed

    state = eco.labor_state(U, T0, DAY)
    field = next(f for f in panel_embed(state).fields if f.name == "🎒 未受け取り")
    assert "作業員" not in field.value


def test_finds_are_logged_with_time_and_worker_and_shown_without_coal(eco, monkeypatch):
    from cogs.labor_cog import panel_embed

    monkeypatch.setattr(
        lab, "roll_ores", lambda kind, h, luck, rng: {"copper": 1, "coal": 3, "diamond": 1} if h > 0 else {}
    )
    eco.credit(U, 10_000, "テスト")
    eco.labor_hire(U, "miner", T0, DAY)
    eco.labor_click(U, T0 + 3 * H, DAY)  # 触れると決算される
    finds = eco.labor_state(U, T0 + 3 * H, DAY)["finds"]
    assert sorted(f["ore"] for f in finds) == ["copper", "diamond"]  # 石炭は出さない
    assert all(T0 < f["at"] <= T0 + 3 * H and f["kind"] == "miner" for f in finds)  # 時刻は、働いた時間のあいだ
    assert eco.labor_state(U, T0 + 3 * H, DAY)["finds"] == finds  # 読むだけでは増えない
    field = next(f for f in panel_embed(eco.labor_state(U, T0 + 3 * H, DAY)).fields if f.name.startswith("📜"))
    assert "坑夫" in field.value and "ダイヤ" in field.value and "石炭" not in field.value
    eco.labor_collect(U, T0 + 3 * H, DAY)
    assert len(eco.labor_state(U, T0 + 3 * H, DAY)["finds"]) == 2  # 受け取っても、履歴は残る


def test_finds_log_keeps_only_the_latest_entries(eco, monkeypatch):
    monkeypatch.setattr(lab, "roll_ores", lambda kind, h, luck, rng: {"copper": 1} if h > 0 else {})
    monkeypatch.setattr(type(eco), "FINDS_KEEP", 5)
    eco.credit(U, 50_000, "テスト")
    eco.labor_hire(U, "miner", T0, DAY)
    for i in range(1, 9):
        eco.labor_click(U, T0 + i * H, DAY)
        eco.labor_collect(U, T0 + i * H, DAY)
    count = eco._conn.execute("SELECT COUNT(*) FROM labor_finds WHERE user_id=?", (U,)).fetchone()[0]
    assert count == 5


# ----------------------------- 押した記録（自動操作の確認用） ----------------------------- #
def test_each_click_is_logged_per_hour_with_gap_statistics(eco):
    eco.credit(U, 1_000, "テスト")
    t = T0
    for gap in (3.0, 3.0, 3.0, 3.0):  # 機械のように、きっちり3秒おき
        eco.labor_click(U, t, DAY)
        t += gap
    rows = eco.labor_click_hours(U, T0 - 10)
    assert len(rows) == 1 and rows[0]["clicks"] == 4 and rows[0]["gaps"] == 3  # 最初の1回は、前の押下がない
    report = lab.click_report(rows)
    assert report["clicks"] == 4 and report["active_hours"] == 1
    assert report["rows"][0]["mean_gap"] == pytest.approx(3.0) and report["rows"][0]["cv"] == pytest.approx(
        0.0, abs=1e-6
    )
    assert report["rows"][0]["min_gap"] == pytest.approx(3.0)


def test_human_like_gaps_have_a_large_variation_and_rejected_clicks_are_not_logged(eco):
    eco.credit(U, 1_000, "テスト")
    t = T0
    for gap in (1.2, 4.0, 2.0, 9.0, 1.5, 6.0):
        eco.labor_click(U, t, DAY)
        t += gap
    assert not eco.labor_click(U, t - 6.0 + 0.2, DAY)["ok"]  # 直前の押下から0.2秒後の連打は、記録しない
    report = lab.click_report(eco.labor_click_hours(U, T0 - 10))
    assert report["clicks"] == 6 and report["rows"][0]["cv"] > 0.3


def test_click_report_finds_the_longest_unbroken_run_and_ignores_long_gaps():
    def hour(h, clicks=100):
        return {
            "hour": h,
            "clicks": clicks,
            "pay": 10,
            "gaps": clicks - 1,
            "sum_gap": 200.0,
            "sum_gap2": 400.0,
            "min_gap": 2.0,
        }

    report = lab.click_report([hour(1), hour(2), hour(3), hour(7), hour(8)])
    assert report["active_hours"] == 5 and report["longest_run"] == 3
    assert lab.click_report([])["longest_run"] == 0 and lab.click_report([])["rows"] == []


def test_click_log_keeps_working_across_hours(eco):
    eco.credit(U, 1_000, "テスト")
    eco.labor_click(U, T0, DAY)
    eco.labor_click(U, T0 + 3700, DAY)  # 次の時間
    rows = eco.labor_click_hours(U, T0 - 10)
    assert [r["clicks"] for r in rows] == [1, 1] and rows[1]["hour"] == rows[0]["hour"] + 1
    assert eco.labor_click_hours(U, T0 + 3700) == [rows[1]]  # since より前は出ない


# ----------------------------- 発掘員と発掘品（#24） ----------------------------- #
def test_contract_is_one_day_and_the_ore_numbers_scale_to_one_day(monkeypatch):
    monkeypatch.undo()  # 本物の作業員の稼ぎ・鉱石の数字で確かめる
    assert lab.CONTRACT_DAYS == 1 and lab.STOCK_HOURS == 24.0
    for kind in ("apprentice", "miner", "craftsman"):  # 1日の平均は、契約料の8割台（運の勝負）
        mean = (lab.WORKER_RATE[kind] + lab.ore_ev(kind)) * 24
        assert 0.8 * lab.CONTRACT_FEE[kind] < mean < lab.CONTRACT_FEE[kind], kind


def test_ordinary_workers_find_relics_by_chance_and_the_foreman_does_not_dig():
    rng = random.Random(4)
    assert "archaeologist" not in lab.WORKERS  # 専用の作業員は、いない
    assert _REAL_RELICS("foreman", 5000, 0.0, rng) == {}  # 監督は、探さない
    for kind in ("apprentice", "miner", "craftsman"):
        found = _REAL_RELICS(kind, 20000, 0.0, rng)
        assert found and all(i in {r[0] for r in lab.RELICS} for i in found), kind
    total = {k: sum(_REAL_RELICS(k, 60000, 0.0, rng).values()) for k in ("apprentice", "miner", "craftsman")}
    assert total["apprentice"] < total["miner"] < total["craftsman"]  # 上位の作業員ほど、見つけやすい
    per_hour = sum(r[3] for r in lab.RELICS) * lab.RELIC_POWER["craftsman"]
    assert 0.004 < per_hour < 0.01  # 職人1人で、1週間に1つほど


def test_relic_table_is_ordered_by_rarity_and_values_fit_the_pair_cap():
    ps = [r[3] for r in lab.RELICS]
    vs = [r[4] for r in lab.RELICS]
    assert ps == sorted(ps, reverse=True) and vs == sorted(vs)  # 珍しいほど価値が高い
    from core import p2p

    assert max(vs) <= p2p.PAIR_DAILY_LIMIT  # 一番高い発掘品も、1回の取引の上限に収まる


def test_foreman_luck_raises_relic_chances_too():
    rng = random.Random(8)
    plain = sum(sum(_REAL_RELICS("craftsman", 1, 0.0, rng).values()) for _ in range(200000))
    lucky = sum(sum(_REAL_RELICS("craftsman", 1, 1.0, rng).values()) for _ in range(200000))
    assert lucky > plain * 1.5


def test_relics_pile_up_and_are_collected_into_the_inventory_even_with_no_pelica(eco, monkeypatch):
    monkeypatch.setattr(
        lab,
        "roll_relics",
        lambda kind, h, luck, rng: {"fossil": 2, "crown": 1} if kind == "miner" and h > 0 else {},
    )
    eco.credit(U, 5_000, "テスト")
    assert eco.labor_hire(U, "miner", T0, DAY)[0]
    seen = eco.labor_state(U, T0 + 3 * H, DAY)
    assert seen["relics"] == {}  # 見込みでは、見つけない
    eco.labor_click(U, T0 + 3 * H, DAY)  # 触れると決算される
    held = eco.labor_state(U, T0 + 3 * H, DAY)["relics"]
    assert held == {"relic_fossil": 2, "relic_crown": 1}
    got = eco.labor_collect(U, T0 + 3 * H, DAY, detail=True)
    assert got["relics"] == held and eco.inventory(U)["relic_fossil"] == 2 and eco.inventory(U)["relic_crown"] == 1
    assert eco.labor_state(U, T0 + 3 * H, DAY)["relics"] == {}  # 受け取ったら、空
    finds = eco.labor_state(U, T0 + 3 * H, DAY)["finds"]
    assert {f["ore"] for f in finds} == {"relic_fossil", "relic_crown"}  # 掘り出しの履歴にも出る


def test_relics_show_on_the_screens_and_a_big_relic_is_announced_publicly(eco, monkeypatch):
    monkeypatch.setattr(
        lab,
        "roll_relics",
        lambda kind, h, luck, rng: {"tear": 1, "fossil": 1} if kind == "craftsman" and h > 0 else {},
    )
    eco.credit(U, 5_000, "テスト")
    eco.labor_hire(U, "craftsman", T0, DAY)
    eco.labor_click(U, T0 + H, DAY)
    state = eco.labor_state(U, T0 + H, DAY)
    assert "発掘品" in text(labor_cog.panel_embed(state)) and "竜の涙" in text(labor_cog.panel_embed(state))
    assert "発掘品" in text(labor_cog.hire_embed(state))  # 雇用の画面に、「まれに発掘品」と出る
    assert "目安" in labor_cog.finds_text(state["finds"])
    view = make_view(eco)
    sent = []

    async def send(content, **kw):
        sent.append(content)

    it = SimpleNamespace(
        user=SimpleNamespace(id=U, mention="<@1>"), response=Resp(), data={}, followup=SimpleNamespace(send=send)
    )
    monkeypatch.setattr(labor_cog.time, "time", lambda: T0 + H)
    asyncio.run(view._collect(it))
    assert sent and "竜の涙" in sent[0] and "三葉虫" not in sent[0]  # 大当たりだけ、公開で知らせる
    assert "発掘品が持ち物に入りました" in it.response.edits[-1]["embed"].description


def test_collect_button_works_when_only_relics_are_waiting(eco, monkeypatch):
    monkeypatch.setattr(
        lab, "roll_relics", lambda kind, h, luck, rng: {"fossil": 1} if kind == "craftsman" and h > 0 else {}
    )
    eco.credit(U, 5_000, "テスト")
    eco.labor_hire(U, "craftsman", T0, DAY)
    eco.labor_click(U, T0 + H, DAY)
    eco.labor_collect(U, T0 + H, DAY)  # クリックの稼ぎと、最初の発掘品を、先に受け取る
    monkeypatch.setattr(labor_cog.time, "time", lambda: T0 + 3 * H)
    eco.labor_click(U, T0 + 3 * H, DAY)  # 触れると、次の分が決算される（発掘品が入る）
    eco._conn.execute("UPDATE labor SET pot=0, pot_workers=0 WHERE guild_id=? AND user_id=?", (eco._g(), U))
    view = make_view(eco)
    assert view.state["pot_total"] == 0 and view.state["relics"]  # お金はなく、発掘品だけがある
    assert view.children[1].label.startswith("受け取る") and view.children[1].disabled is False


def test_old_archaeologist_contracts_are_refunded_and_removed_once(eco):
    from core import schema

    eco.credit(U, 1_000, "テスト")
    before = eco.balance(U)
    with eco._lock, eco._conn:
        eco._conn.execute(
            "INSERT INTO labor_workers (guild_id, user_id, kind, expires_at) VALUES (?, ?, 'archaeologist', ?)",
            (eco._g(), U, T0 + 86400),
        )
        schema._refund_archaeologists(eco._conn, T0)
        schema._refund_archaeologists(eco._conn, T0)  # 二度動かしても、増えない
    assert eco.balance(U) == before + schema.ARCHAEOLOGIST_FEE
    assert eco.labor_state(U, T0, DAY)["workers"] == 0  # 状態を読んでも、落ちない
    assert any(
        "発掘員の廃止" in r["reason"] for r in eco._conn.execute("SELECT reason FROM ledger WHERE user_id=?", (U,))
    )


# ----------------------------- 寄付総額 ----------------------------- #
def test_donation_total_accumulates_across_collections_and_shows_on_the_screens(eco, monkeypatch):
    monkeypatch.setattr(teiai, "LABOR_SKIM_RATE", 0.10)  # 1回ごとに、はっきり出る率にする
    eco.credit(U, 5_000, "テスト")
    assert eco.labor_state(U, T0, DAY)["donated"] == 0
    first = eco.labor_collect(U, T0, DAY, detail=True)
    assert first["donated"] == 0  # まだ稼いでいない
    for k in range(40):
        eco.labor_click(U, T0 + 5 + k * 2, DAY)
    one = eco.labor_collect(U, T0 + 100, DAY, detail=True)
    assert one["skimmed"] > 0 and one["donated"] == one["skimmed"]
    for k in range(30):
        eco.labor_click(U, T0 + 200 + k * 2, DAY)
    two = eco.labor_collect(U, T0 + 300, DAY, detail=True)
    assert two["donated"] == one["skimmed"] + two["skimmed"]  # 合計が増えていく
    state = eco.labor_state(U, T0 + 300, DAY)
    assert state["donated"] == two["donated"]
    assert state["donated_all"] >= two["donated"]  # サーバー全員の合計は、記録として残る（画面には出さない）


def test_collect_message_shows_the_donation_total_instead_of_the_rate(eco, monkeypatch):
    monkeypatch.setattr(teiai, "LABOR_SKIM_RATE", 0.10)
    eco.credit(U, 5_000, "テスト")
    for k in range(50):
        eco.labor_click(U, T0 + k * 2, DAY)
    view = make_view(eco)
    it = SimpleNamespace(user=SimpleNamespace(id=U, mention="<@1>"), response=Resp(), data={}, followup=None)
    monkeypatch.setattr(labor_cog.time, "time", lambda: T0 + 200)
    asyncio.run(view._collect(it))
    desc = it.response.edits[-1]["embed"].description
    assert "慈善団体に寄付されます" in desc and "寄付総額" not in desc and "稼ぎの" not in desc


def test_the_wage_setting_is_gone_and_workers_do_not_quit(eco):
    """待遇（標準 / 低賃金）の設定と、辞める仕組みは、なくなった。作業員は、契約が切れて更新できないときだけ、いなくなる。"""
    assert not hasattr(lab, "WAGES") and not hasattr(lab, "WAGE_MULT") and not hasattr(lab, "QUIT_PER_DAY")
    assert not hasattr(eco, "labor_set_wage")
    eco.credit(U, 5_000, "テスト")
    for kind in ("apprentice", "miner", "craftsman"):
        eco.labor_hire(U, kind, T0, DAY)
    plan = lab.settle_plan([worker(1, "miner", T0 + W)], True, T0, T0 + 10 * H, 0, 0, random.Random(1))
    assert "quit" not in plan
    assert eco.labor_state(U, T0 + 10 * H, DAY)["workers"] == 3  # 契約の間は、必ず残る


def test_opening_the_workplace_again_deletes_the_previous_panel(eco):
    deleted, n = [], iter(range(100, 200))

    class Partial:
        def __init__(self, cid):
            self.cid = cid

        def get_partial_message(self, mid):
            return SimpleNamespace(delete=lambda: self._del(mid))

        async def _del(self, mid):
            deleted.append((self.cid, mid))

    class It:
        guild_id, user = 0, SimpleNamespace(id=U)

        def __init__(self):
            self.response = SimpleNamespace(send_message=self._send)
            self.mid = next(n)

        async def _send(self, **kw):
            pass

        async def original_response(self):
            return SimpleNamespace(channel=SimpleNamespace(id=7), id=self.mid)

    bot = SimpleNamespace(economy=SimpleNamespace(for_guild=lambda g: eco), config={}, get_partial_messageable=Partial)
    cog = labor_cog.LaborCog(bot)
    first, second = It(), It()
    asyncio.run(cog.work.callback(cog, first))
    assert deleted == []
    asyncio.run(cog.work.callback(cog, second))
    assert deleted == [(7, first.mid)]  # 前に開いた作業場だけが消える


def test_merge_select_appears_only_with_ten_and_merges(eco):
    def give_relic(n):
        eco._conn.execute(
            "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, 'relic_fossil', ?) "
            "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty = ?",
            (eco._g(), U, n, n),
        )
        eco._conn.commit()

    import discord

    give_relic(9)
    assert not any(isinstance(c, discord.ui.Select) for c in make_view(eco).children)
    give_relic(10)
    view = make_view(eco)
    select = next(c for c in view.children if isinstance(c, discord.ui.Select))
    assert "★" in select.options[0].label
    it = SimpleNamespace(user=SimpleNamespace(id=U), response=Resp(), data={"values": ["relic_fossil"]})
    asyncio.run(view._merge(it))
    assert eco.relic_stock(U) == {"relic_fossil_s1": 1}
    assert "合体した" in it.response.edits[-1]["embed"].description


def test_tools_go_up_to_level_ten_with_doubling_costs():
    assert lab.MAX_LEVEL == 10 and len(lab.UPGRADE_COST) == 10
    assert lab.pay_per_click(10) == 50 and lab.pay_per_click(5) == 30  # 伸びは今までと同じ
    assert lab.fatigue_gain(5) == 1.0 and lab.fatigue_gain(10) == 0.5  # 手袋は Lv6 から緩やかに（0 以下にならない）
    assert all(lab.fatigue_gain(a) > lab.fatigue_gain(a + 1) > 0 for a in range(10) if a + 1 <= 10)
    assert lab.upgrade_cost(5) == 7_000 and lab.upgrade_cost(9) == 112_000 and lab.upgrade_cost(10) is None


def test_donation_total_is_the_sum_of_everyone_in_the_server(eco, monkeypatch):
    monkeypatch.setattr(teiai, "LABOR_SKIM_RATE", 0.10)
    eco.credit(2, 5_000, "テスト")
    for uid in (U, 2):
        for k in range(50):
            eco.labor_click(uid, T0 + k * 2, DAY)
    a = eco.labor_collect(U, T0 + 200, DAY, detail=True)
    b = eco.labor_collect(2, T0 + 201, DAY, detail=True)
    assert a["donated"] > 0 and b["donated"] > 0
    assert b["donated_all"] == a["donated"] + b["donated"] == eco.labor_donated_total()
    assert eco.labor_state(U, T0 + 202, DAY)["donated_all"] == a["donated"] + b["donated"]


def test_click_count_is_capped_per_day_and_workers_are_not(eco, monkeypatch):
    monkeypatch.setattr(lab, "MAX_CLICKS_PER_DAY", 4)
    monkeypatch.setattr(lab, "click_pay", lambda pick, fatigue: 30)
    monkeypatch.setattr(lab, "pay_per_click", lambda pick: 30)
    pays = [eco.labor_click(U, T0 + i * 2, DAY)["pay"] for i in range(8)]
    assert pays[:4] == [30] * 4  # 上限（回数）までは、ふつうに稼げる
    assert pays[4:] == [lab.OVER_CAP_PAY] * 4  # 上限を超えたら、1ペリカずつ
    assert eco.labor_state(U, T0 + 20, DAY)["day_click"] == 8
    assert eco.labor_click(U, T0 + 100_000, "2026-11-20")["pay"] > lab.OVER_CAP_PAY  # 日が変われば、また稼げる


# ----------------------------- 自動操作の疑いがあるときの、早めの打ち止め ----------------------------- #
def test_is_mechanical_hour_needs_enough_clicks_and_low_variation():
    base = {"gaps": 200, "sum_gap": 200.0, "sum_gap2": 200.0, "clicks": 200}  # 間隔はすべて1.0秒（ばらつき0）
    assert lab.is_mechanical_hour(base)
    assert not lab.is_mechanical_hour({**base, "clicks": lab.MECH_MIN_CLICKS - 1})  # 回数が足りない
    assert not lab.is_mechanical_hour({**base, "gaps": 300, "sum_gap": 300.0, "sum_gap2": 3_000.0})  # ばらつきが大きい
    assert not lab.is_mechanical_hour(
        {**base, "gaps": 1, "sum_gap": 1.0, "sum_gap2": 1.0}
    )  # サンプルが1回では判定しない


def test_auto_click_suspected_needs_consecutive_mechanical_hours():
    mech = {"clicks": 200, "gaps": 200, "sum_gap": 200.0, "sum_gap2": 200.0}
    rows = [{"hour": h, **mech} for h in (10, 11, 12)]
    assert lab.auto_click_suspected(rows, 13)  # 直近3時間、ずっと機械的
    assert not lab.auto_click_suspected(rows[1:], 13)  # 2時間しかない
    assert not lab.auto_click_suspected(rows, 14)  # 「いまの時間」が13になり、直近3時間に抜けが出る
    human = {"clicks": 50, "gaps": 50, "sum_gap": 100.0, "sum_gap2": 400.0}  # ばらつきも回数も、人間らしい
    assert not lab.auto_click_suspected([{"hour": h, **human} for h in (10, 11, 12)], 13)


def test_labor_click_stops_early_after_three_mechanical_hours(eco):
    hour0 = int(T0 // 3600)
    mech = (200, 200, 200.0, 200.0, 1.0)  # clicks, gaps, sum_gap, sum_gap2, min_gap
    for h in (hour0 - 3, hour0 - 2, hour0 - 1):
        eco._conn.execute(
            "INSERT INTO labor_click_hours (guild_id, user_id, hour, clicks, pay, gaps, sum_gap, sum_gap2, min_gap) "
            "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (eco._g(), U, h, *mech[:1], *mech[1:]),
        )
    eco._conn.commit()
    r = eco.labor_click(U, T0, DAY)
    assert r["ok"] and r["pay"] == lab.OVER_CAP_PAY  # 上限にはまだ届いていないのに、早めに打ち止め
    assert eco.labor_state(U, T0 + 1, DAY)["day_click"] < lab.MAX_CLICKS_PER_DAY


# ----------------------------- 鉱脈ヒット・コンボ・今日の地層 ----------------------------- #
def test_crit_chance_scales_with_pick_workers_and_stratum():
    assert lab.crit_chance(10, 0) > lab.crit_chance(5, 0) > lab.crit_chance(0, 0)
    assert lab.crit_chance(0, 4) > lab.crit_chance(0, 0)
    rich = next(s for s in lab.STRATA if s["id"] == "rich_vein")
    assert lab.crit_chance(5, 0, rich) > lab.crit_chance(5, 0)


def test_daily_stratum_is_deterministic_and_varies_across_days(monkeypatch):
    monkeypatch.setattr(lab, "daily_stratum", _REAL_STRATUM)
    assert lab.daily_stratum("")["id"] == "normal"
    days = [f"2026-11-{d:02d}" for d in range(10, 25)]
    assert lab.daily_stratum(days[0]) == lab.daily_stratum(days[0])
    assert len({lab.daily_stratum(d)["id"] for d in days}) >= 3


def test_labor_click_applies_crit_combo_and_hand_ore(eco, monkeypatch):
    monkeypatch.setattr(lab, "COMBO_BONUS_RATE", 0.5)
    monkeypatch.setattr(lab, "roll_click_bonus", lambda *a, **k: {"crit": True, "ore": "copper"})
    # 1〜9回目は会心+手掘り鉱石、10回目はさらにコンボ加算
    for i in range(9):
        r = eco.labor_click(U, T0 + i * 2000, DAY)
        assert r["crit"] is True and r["ore"] == "copper" and r["combo"] is False
        assert r["pay"] == round(10 * lab.CRIT_MULT) and r["ore_pay"] == lab.ORE_VALUE["copper"]
    tenth = eco.labor_click(U, T0 + 9 * 2000, DAY)
    assert tenth["combo"] is True and tenth["pay"] == round(10 * lab.CRIT_MULT) + round(10 * 0.5)
    state = eco.labor_state(U, T0 + 9 * 2000 + 1, DAY)
    assert state["ores"].get("copper") == 10


def test_work_panel_shows_stratum_and_crit_note(eco, monkeypatch):
    rich = next(s for s in lab.STRATA if s["id"] == "rich_vein")
    monkeypatch.setattr(lab, "daily_stratum", lambda day: dict(rich))
    monkeypatch.setattr(lab, "roll_click_bonus", lambda *a, **k: {"crit": True, "ore": "gold"})
    view = make_view(eco)
    assert "今日の地層" in text(labor_cog.panel_embed(view.state))
    it = SimpleNamespace(user=SimpleNamespace(id=U), response=Resp())
    asyncio.run(view._work(it))
    desc = it.response.edits[-1]["embed"].description
    assert "鉱脈ヒット" in desc and "金" in desc
