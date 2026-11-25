"""会社（#30）: 数字のロジックと、設立・出資・生産・出荷・増築・維持費・倒産・差し押さえ。"""

import pytest

from domain import company as co
from domain import teiai
from domain.errors import Failure
from infra.db.repositories import open_repositories
from tests.support import set_test_debt

pytestmark = pytest.mark.real_scope

G, U, V = 10, 1, 2
DAY = 86400
TZ = 9.0  # 本番の utc_offset_hours
NOON = co.day_start("2026-11-17", TZ) + DAY / 2  # ある日の正午（現地時間）
FOUND_COST = co.FOUND_FEE + co.BUILD_COSTS[0]


@pytest.fixture
def eco(tmp_path):
    return open_repositories(str(tmp_path / "t.db"), strict=True).for_guild(G)


def rich(eco, uid, amount=5_000_000):
    eco.wallet.credit(uid, amount, "テスト")


def found(eco, uid=U, kind="mine", now=NOON):
    rich(eco, uid)
    ok, c = eco.company.company_found(uid, "テスト商事", kind, now)
    assert ok, c
    return c


def house(eco):
    return eco.bank.house_account()["balance"]


# ----------------------------- 数字 ----------------------------- #
def test_check_name_rejects_empty_long_and_markup():
    assert co.check_name("  帝愛商事 ") == (True, "帝愛商事")
    for bad, code in [
        ("", "company_name_empty"),
        ("あ" * (co.NAME_MAX + 1), "company_name_too_long"),
        ("@everyone", "company_name_invalid_chars"),
    ]:
        res = co.check_name(bad)
        assert not res[0] and isinstance(res[1], Failure) and res[1].code == code


def test_invested_and_upgrade_cost_follow_the_build_table():
    assert co.invested(1) == co.BUILD_COSTS[0] and co.invested(co.MAX_LEVEL) == sum(co.BUILD_COSTS)
    assert co.upgrade_cost(1) == co.BUILD_COSTS[1] and co.upgrade_cost(co.MAX_LEVEL) is None
    assert co.daily_upkeep(1) == int(co.BUILD_COSTS[0] * co.UPKEEP_RATE)


def test_day_state_is_the_same_for_everyone_on_a_day_and_within_its_price_range():
    a, b = co.day_state("farm", G, "2026-11-17"), co.day_state("farm", G, "2026-11-17")
    assert a == b
    seen = {co.day_state("farm", G, f"2026-{m:02d}-{d:02d}").condition.key for m in (1, 2, 3, 4) for d in range(1, 29)}
    assert len(seen) >= 4  # 日によって、状態が変わる
    for d in range(1, 29):
        s = co.day_state("farm", G, f"2026-05-{d:02d}")
        assert s.condition.price_lo <= s.price <= s.condition.price_hi


def test_produce_accrues_by_time_and_stops_at_the_storage_cap():
    half = co.produce("mine", 1, G, 0.0, NOON, NOON + DAY / 2, TZ)
    assert half == pytest.approx(co.daily_output("mine", 1) / 2)  # 採掘場は、いつも生産 1.0 倍
    assert co.produce("mine", 1, G, 0.0, NOON, NOON + 30 * DAY, TZ) == pytest.approx(co.storage_cap("mine", 1))
    assert co.produce("mine", 1, G, 5.0, NOON, NOON - 10, TZ) == 5.0  # 時刻が戻っても、減らない


def test_day_boundary_follows_the_configured_offset():
    """日付の切り替わりは、注入された時差（utc_offset_hours）で決まる。UTC 15:00 は JST では翌日、UTC では当日。"""
    utc_15 = co.day_start("2026-11-17", 0.0) + 15 * 3600
    assert co.day_of(utc_15, 9.0) == "2026-11-18"
    assert co.day_of(utc_15, 0.0) == "2026-11-17"
    assert co.day_start("2026-11-18", 9.0) == co.day_start("2026-11-17", 0.0) + 15 * 3600


def test_farm_production_follows_each_days_yield():
    start = co.day_start("2026-11-17", TZ)
    total = co.produce("farm", 5, G, 0.0, start, start + 2 * DAY, TZ)
    expected = sum(
        co.daily_output("farm", 5) * co.day_state("farm", G, d).yield_mult for d in ("2026-11-17", "2026-11-18")
    )
    assert total == pytest.approx(min(expected, co.storage_cap("farm", 5)))


def test_sale_takes_pinhane_and_never_pays_more_than_the_budget():
    paid, pinhane, left = co.sale(10_000.0, 1.0, 1_000_000)
    assert paid + pinhane == 10_000 and pinhane == int(10_000 * co.PINHANE_RATE) and left == 0.0
    paid, pinhane, left = co.sale(10_000.0, 1.0, 4_000)
    assert paid <= 4_000 and 0 < left < 10_000  # 払える分だけ出荷し、残りは在庫に残る
    assert co.sale(10_000.0, 1.0, 0) == (0, 0, 10_000.0)
    assert co.sale(0.0, 1.5, 1_000) == (0, 0, 0.0)


# ----------------------------- 設立・資金 ----------------------------- #
def test_found_charges_fee_and_build_cost_and_routes_them_to_teiai(eco):
    rich(eco, U)
    before, bank_before = eco.wallet.balance(U), eco.bank.bank_status(NOON)["cash"]
    ok, c = eco.company.company_found(U, "帝愛商事", "farm", NOON)
    assert ok and c["name"] == "帝愛商事" and c["kind"] == "farm" and c["level"] == 1 and c["funds"] == 0
    assert eco.wallet.balance(U) == before - FOUND_COST
    assert eco.bank.bank_status(NOON)["cash"] == pytest.approx(bank_before)  # 金融部門とは分離（設立費は銀行に入らない）
    assert house(eco) == co.FOUND_FEE + co.BUILD_COSTS[0]  # 設立費も建設費も、帝愛物産の買取プールへ
    assert eco.company.company_found(U, "二社目", "mine", NOON)[1].code == "company_exists"


def test_found_needs_money_a_valid_name_and_no_debt(eco):
    assert eco.company.company_found(U, "貧乏商事", "mine", NOON)[1].code == "company_found_short"
    rich(eco, U)
    assert eco.company.company_found(U, "<@1>", "mine", NOON)[1].code == "company_name_invalid_chars"
    assert eco.company.company_found(U, "謎の会社", "casino", NOON)[1].code == "company_kind_unknown"
    rich(eco, V)
    with eco.db.tx():
        eco.debt.owe_for(G, V, 10_000_000, "テスト")
    assert eco.debt.status(V) != teiai.KATAGI
    assert eco.company.company_found(V, "借金商事", "mine", NOON)[1].code == "company_katagi_only"


def test_invest_and_withdraw_move_money_between_person_and_company(eco):
    found(eco)
    before = eco.wallet.balance(U)
    assert eco.company.company_invest(U, 50_000, NOON) == (True, 50_000)
    assert eco.wallet.balance(U) == before - 50_000
    assert eco.company.company_withdraw(U, 20_000, NOON) == (True, 20_000)
    assert eco.company.company_withdraw(U, 99_999, NOON)[1].code == "company_funds_short"
    assert eco.company.company_withdraw(U, None, NOON) == (True, 30_000)  # 空なら全額
    assert eco.wallet.balance(U) == before
    assert eco.company.company_withdraw(U, None, NOON)[1].code == "company_no_funds"
    assert eco.company.company_invest(U, 0, NOON)[1].code == "company_amount_invalid"
    assert eco.company.company_invest(V, 100, NOON)[1].code == "company_none"


# ----------------------------- 生産・出荷 ----------------------------- #
def test_ship_sells_stock_at_todays_price_from_the_house_account(eco):
    found(eco)
    later = NOON + DAY / 2  # 翌日の 0時ちょうど（維持費の未払いが1日つくが、まだ倒産しない）
    c = eco.company.company_get(U, later)
    assert c["stock"] == pytest.approx(co.daily_output("mine", 1) / 2)
    budget = house(eco)
    ok, r = eco.company.company_ship(U, later)
    assert ok and r["paid"] + r["pinhane"] == int(c["stock"] * c["price"]) and r["left"] == 0
    after = eco.company.company_get(U, later)
    assert after["funds"] == r["paid"] and after["stock"] == 0 and after["sold_total"] == r["paid"]
    assert house(eco) == budget - r["paid"]  # 代金は、買取プールから出る（新しく作らない）
    assert eco.company.company_ship(U, later)[1].code == "company_nothing_to_ship"


def test_ship_is_limited_by_the_house_budget(eco, monkeypatch):
    monkeypatch.setattr(co, "DAILY_GRANT", 0)  # 買取枠なし（買取プールだけ）
    found(eco)
    with eco.db.tx():
        eco.bank.house_pay_for(G, house(eco) - 500)  # 買取予算を 500 だけ残す
    ok, r = eco.company.company_ship(U, NOON + DAY / 2)
    assert ok and r["paid"] <= 500 and r["left"] > 0
    with eco.db.tx():
        eco.bank.house_pay_for(G, house(eco))
    assert eco.company.company_ship(U, NOON + DAY / 2)[1].code == "company_no_budget"


def test_daily_grant_covers_the_shortfall_up_to_the_cap_and_resets_next_day(eco, monkeypatch):
    monkeypatch.setattr(co, "DAILY_GRANT", 300)
    found(eco)
    with eco.db.tx():
        eco.bank.house_pay_for(G, house(eco) - 200)  # 買取プールは 200 だけ
    t = NOON + DAY / 2
    assert eco.company.company_get(U, t)["budget"] == 200 + 300
    ok, r = eco.company.company_ship(U, t)
    assert ok and r["paid"] == 500 and r["left"] > 0  # プールの 200 と、買取枠の 300 まで
    assert house(eco) == 0 and eco.company.company_get(U, t)["budget"] == 0
    assert eco.company.company_ship(U, t)[1].code == "company_no_budget"  # 同じ日は、もう出ない
    with eco.db.tx():  # 維持費が買取プールに入るので、次の日は空にしてから確かめる
        eco.company.company_get(U, t + DAY)
        eco.bank.house_pay_for(G, house(eco))
    ok, r = eco.company.company_ship(U, t + DAY)
    assert ok and r["paid"] == 300  # 日が変わると、買取枠が戻る


def test_company_operations_never_create_pelica_while_the_house_can_pay(eco):
    """個人の残高・会社資金・買取プール・銀行の現金の合計は、買取プールで払える間は、会社の操作では増えない。"""

    def total():
        c = eco.company.company_get(U, t) or {"funds": 0}
        return eco.wallet.balance(U) + c["funds"] + house(eco) + eco.bank.bank_status(t)["cash"]

    t = NOON
    rich(eco, U, 20_000_000)
    start = total()
    eco.company.company_found(U, "循環商事", "farm", t)
    eco.company.company_invest(U, 5_000_000, t)
    for day in range(1, 40):
        t = NOON + day * DAY
        eco.company.company_ship(U, t)
        if day in (5, 12):
            eco.company.company_upgrade(U, t)
        if day == 20:
            eco.company.company_change_kind(U, "mine", t)
    eco.company.company_withdraw(U, None, t)
    assert eco.bank.bank_status(t)["cash"] == pytest.approx(0.0)  # 帝愛銀行とは完全に隔離
    assert total() == pytest.approx(start)  # 行き先が変わるだけ。1ペリカも増えも減りもしない


# ----------------------------- 増築・種類の変更 ----------------------------- #
def test_upgrade_spends_company_funds_and_raises_output(eco):
    found(eco)
    assert eco.company.company_upgrade(U, NOON)[1].code == "company_funds_short"
    cost = co.BUILD_COSTS[1]
    eco.company.company_invest(U, cost, NOON)
    budget = house(eco)
    assert eco.company.company_upgrade(U, NOON) == (True, 2)
    c = eco.company.company_get(U, NOON)
    assert c["funds"] == 0 and c["invested"] == co.invested(2) and house(eco) == budget + cost
    assert c["daily_output"] == pytest.approx(co.invested(2) * co.KINDS["mine"].yield_rate)


def test_upgrade_stops_at_max_level(eco):
    found(eco)
    rich(eco, U, 10_000_000)
    assert eco.company.company_invest(U, sum(co.BUILD_COSTS[1:]), NOON)[0]  # Lv2〜5 の建設費の合計
    for level in range(2, co.MAX_LEVEL + 1):
        assert eco.company.company_upgrade(U, NOON) == (True, level)
    assert eco.company.company_upgrade(U, NOON)[1].code == "company_max_level"


def test_change_kind_resets_level_and_stock(eco):
    found(eco)
    eco.company.company_invest(U, 1_000_000, NOON)
    eco.company.company_upgrade(U, NOON)
    later = NOON + DAY / 4
    assert eco.company.company_change_kind(U, "mine", later)[1].code == "company_same_kind"
    assert eco.company.company_change_kind(U, "farm", later) == (True, "")
    c = eco.company.company_get(U, later)
    assert c["kind"] == "farm" and c["level"] == 1 and c["stock"] == 0
    assert c["funds"] == 1_000_000 - co.BUILD_COSTS[1] - co.BUILD_COSTS[0]


# ----------------------------- 維持費・倒産 ----------------------------- #
def test_upkeep_is_charged_each_day_from_company_funds(eco):
    found(eco)
    eco.company.company_invest(U, 10_000, NOON)
    budget = house(eco)
    c = eco.company.company_get(U, NOON + 2 * DAY)
    fee = co.daily_upkeep(1)
    assert c["funds"] == 10_000 - 2 * fee and c["unpaid_days"] == 0 and house(eco) == budget + 2 * fee


def test_three_unpaid_days_bankrupt_the_company_but_not_the_person(eco):
    found(eco)
    eco.company.company_invest(U, 100, NOON)  # 維持費1日分にも足りない
    personal = eco.wallet.balance(U)
    assert eco.company.company_get(U, NOON + 2 * DAY)["unpaid_days"] == 2
    gone = eco.company.company_settle_all(NOON + 3 * DAY)
    assert gone == [{"user_id": U, "name": "テスト商事"}]
    assert eco.company.company_get(U, NOON + 3 * DAY) is None
    assert eco.wallet.balance(U) == personal  # 個人の資産は巻き込まない
    assert any(n.code == "company_bankrupt" for n in eco.notices.take_notices(U))
    assert eco.company.company_found(U, "再起商事", "mine", NOON + 3 * DAY)[0]  # もう一度作れる


def test_paying_again_clears_the_unpaid_streak(eco):
    found(eco)
    assert eco.company.company_get(U, NOON + 2 * DAY)["unpaid_days"] == 2
    eco.company.company_invest(U, 50_000, NOON + 2 * DAY)
    assert eco.company.company_get(U, NOON + 3 * DAY)["unpaid_days"] == 0


# ----------------------------- 純資産・差し押さえ ----------------------------- #
def test_company_counts_toward_net_worth_at_funds_plus_build_cost(eco):
    rich(eco, U)
    before = eco.wallet.rank_of(U)[1]
    eco.company.company_found(U, "資産商事", "mine", NOON)
    eco.company.company_invest(U, 200_000, NOON)
    assert eco.wallet.rank_of(U)[1] == before - co.FOUND_FEE  # 減るのは設立費だけ。建設費と会社資金は資産に数える


def test_forced_execution_takes_company_funds_before_the_rest_becomes_debt(eco):
    found(eco)
    eco.company.company_invest(U, 300_000, NOON)
    cash = eco.wallet.balance(U)
    with eco.db.tx():
        eco.debt.owe_for(G, U, cash + 100_000, "テスト")
    c = eco.company.company_get(U, NOON)
    assert c is not None and c["funds"] == 200_000 and c["level"] == 1  # 会社資金から 10万。事業所は残る


def test_total_asset_ranking_counts_the_company_without_crashing(eco):
    """#73: 会社を持つ人がいると、総資産のランキングが KeyError: 'company' で落ちていた。"""
    from domain.types import FIXED_KEYS, asset_groups

    found(eco)
    rows = eco.assets.net_assets(None, NOON)
    me = next(p for uid, _total, p in rows if uid == U)
    assert "company" in FIXED_KEYS and me["company"] == co.invested(1)
    assert asset_groups(me)["fixed"] >= co.invested(1)


# ----------------------------- ほかの人の会社への出資（#71） ----------------------------- #
W = 3


def test_dividend_split_and_stake_values_are_pure_math():
    assert co.dividend_split(10_000, 0.2, {V: 3000, W: 1000}) == {V: 1500, W: 500}
    assert co.dividend_split(10_000, 0.9, {V: 1}) == {V: int(10_000 * co.DIVIDEND_MAX)}  # 上限で止まる
    assert co.dividend_split(10_000, 0.0, {V: 1}) == {} and co.dividend_split(10_000, 0.3, {}) == {}
    assert co.stake_values(500_000, {}) == (500_000, {})
    assert co.stake_values(500_000, {V: 100_000}) == (400_000, {V: 100_000})  # 二重に数えない
    owner, inv = co.stake_values(50_000, {V: 75_000, W: 25_000})  # 会社の価値が出資を下回ると目減り
    assert owner == 0 and inv == {V: 37_500, W: 12_500}


def test_invest_in_another_company_moves_money_into_its_funds(eco):
    found(eco)
    rich(eco, V)
    before = eco.wallet.balance(V)
    ok, r = eco.company.stake_invest(V, U, 50_000, NOON)
    assert ok and r == {"amount": 50_000, "total": 50_000, "name": "テスト商事"}
    assert eco.wallet.balance(V) == before - 50_000
    c = eco.company.company_get(U, NOON)
    assert c["funds"] == 50_000 and c["stakes_total"] == 50_000 and c["investors"] == 1 and c["withdrawable"] == 0
    ok, r = eco.company.stake_invest(V, U, 10_000, NOON)
    assert ok and r["total"] == 60_000  # 同じ会社への出資は、積み増し
    [h] = eco.company.stake_holdings(V, NOON)
    assert h["owner_id"] == U and h["amount"] == 60_000 and h["share"] == 1.0


def test_invest_rules_self_min_cap_katagi_and_no_company(eco):
    found(eco)
    rich(eco, V)
    assert eco.company.stake_invest(U, U, 50_000, NOON)[1].code == "stake_self"
    assert eco.company.stake_invest(V, U, co.STAKE_MIN - 1, NOON)[1].code == "stake_min"
    assert eco.company.stake_invest(V, W, 50_000, NOON)[1].code == "stake_no_company"
    cap = co.stake_cap(1)
    assert cap == co.BUILD_COSTS[0] * 2
    assert eco.company.stake_invest(V, U, cap + 1, NOON)[1].code == "stake_cap"
    assert eco.company.stake_invest(V, U, cap, NOON)[0]
    assert eco.company.stake_invest(V, U, co.STAKE_MIN, NOON)[1].code == "stake_cap"  # もう枠がない
    rich(eco, W)
    with eco.db.tx():
        set_test_debt(eco, W, 5000, guild_id=G)
    assert eco.company.stake_invest(W, U, co.STAKE_MIN, NOON)[1].code == "company_katagi_only"


def test_owner_cannot_withdraw_investors_money_but_can_upgrade_with_it(eco):
    found(eco)
    rich(eco, V)
    assert eco.company.stake_invest(V, U, 200_000, NOON)[0]
    assert eco.company.company_withdraw(U, None, NOON)[1].code == "company_funds_locked"
    assert eco.company.company_invest(U, 30_000, NOON)[0]  # 社長自身のお金は引き出せる
    assert eco.company.company_withdraw(U, 30_001, NOON)[1].code == "company_withdraw_over"
    assert eco.company.company_withdraw(U, None, NOON) == (True, 30_000)
    ok, level = eco.company.company_upgrade(U, NOON)  # 増築には、出資者のお金も使える
    assert ok and level == 2


def test_shipping_pays_dividends_to_investors_by_their_stake(eco):
    found(eco)
    rich(eco, V)
    rich(eco, W)
    assert eco.company.stake_invest(V, U, 30_000, NOON)[0]
    assert eco.company.stake_invest(W, U, 10_000, NOON)[0]
    assert eco.company.company_set_dividend(U, 0.4, NOON) == (True, 0.4)
    assert eco.company.company_set_dividend(U, co.DIVIDEND_MAX + 0.01, NOON)[1].code == "dividend_rate_range"
    v0, w0 = eco.wallet.balance(V), eco.wallet.balance(W)
    later = NOON + DAY / 2
    funds0 = eco.company.company_get(U, later)["funds"]
    ok, r = eco.company.company_ship(U, later)
    assert ok and r["paid"] > 0
    want = co.dividend_split(r["paid"], 0.4, {V: 30_000, W: 10_000})
    assert r["dividends"] == sum(want.values()) > 0
    assert eco.wallet.balance(V) == v0 + want[V] and eco.wallet.balance(W) == w0 + want[W]
    assert eco.company.company_get(U, later)["funds"] == funds0 + r["paid"] - r["dividends"]
    assert {h["owner_id"]: h["dividends"] for h in eco.company.stake_holdings(V, later)} == {U: want[V]}


def test_net_worth_splits_the_company_between_owner_and_investors(eco):
    found(eco)
    rich(eco, V)
    values: dict[int, int] = {}
    with eco.db.tx():
        eco.company.collect_values(G, values)
    assert values == {U: co.invested(1)}
    assert eco.company.stake_invest(V, U, 40_000, NOON)[0]
    values = {}
    with eco.db.tx():
        eco.company.collect_values(G, values)
    assert values == {U: co.invested(1), V: 40_000}  # 会社の価値は 出資4万+建設費、社長は建設費の分だけ


def test_bankruptcy_of_the_company_wipes_the_stakes(eco):
    found(eco)
    rich(eco, V)
    assert eco.company.stake_invest(V, U, 20_000, NOON)[0]
    # 出資者のお金は引き出せないので、維持費で会社資金を使い切って倒産するまで待つ
    far = NOON + DAY * 400
    eco.company.company_settle_all(far)
    assert eco.company.company_get(U, far) is None
    assert eco.company.stake_holdings(V, far) == []
    assert eco.db.conn.execute("SELECT COUNT(*) FROM company_stakes").fetchone()[0] == 0
