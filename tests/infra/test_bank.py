"""帝愛銀行（預金・準備率・取り付け）・破産猶予・CDS・種銭の随時貸付・ダブルの信用借入。"""

import time

import pytest

from domain import rates, teiai
from domain.types import INITIAL_BALANCE
from infra.db.repositories import open_repositories
from tests.fakes import must
from tests.support import give_test_kyosai_units, make_test_bot, set_test_balance, set_test_debt

pytestmark = pytest.mark.real_scope

G, A, B, C, T = 10, 1, 2, 3, 9
NOW = time.time()  # CDS の有効期限などが、実際の時刻で判定されるので、いまの時刻を基準にする
HOUR = 3600


@pytest.fixture
def eco(tmp_path):
    return open_repositories(str(tmp_path / "t.db"), strict=True).for_guild(G)


def go_into_debt(eco, uid, amount):
    """uid を、帝愛への借金が amount ほどの状態にする（現金は差し押さえられ、種銭が貸し付けられる）。"""
    eco.wallet.balance(uid)
    with eco.db.tx():
        eco.debt.owe_for(G, uid, amount, "テスト")


# ----------------------------- 預金と準備率 ----------------------------- #
def give_units(eco, uid, n):
    """帝愛の持ち口から uid へ共済の口を n 口移す（口の持ち主が超過損失を負担する場面のため）。"""
    eco.wallet.balance(uid)
    with eco.db.tx():
        give_test_kyosai_units(eco, uid, n, guild_id=G, now=NOW)


def test_deposit_keeps_only_the_reserve_ratio_as_cash(eco):
    eco.wallet.credit(A, 20_000, "テスト")
    ok, have = eco.bank.bank_deposit(A, 10_000, NOW)
    assert ok and have == 10_000 and eco.wallet.balance(A) == INITIAL_BALANCE + 20_000 - 10_000
    s = eco.bank.bank_status(NOW)
    assert s["deposits"] == 10_000 and s["cash"] == pytest.approx(2_000) and s["loaned"] == pytest.approx(8_000)
    assert s["ratio"] == pytest.approx(0.20) and s["level"] == "safe"


def test_only_katagi_can_deposit_and_minimum_applies(eco):
    eco.wallet.credit(A, 5_000, "テスト")
    assert not eco.bank.bank_deposit(A, 50, NOW)[0]  # 最低額
    go_into_debt(eco, B, 10_000)
    assert eco.bank.bank_deposit(B, 100, NOW)[1].code == "bank_katagi_only"
    assert not eco.bank.bank_deposit(A, 10**9, NOW)[0]  # 残高不足


def test_loaned_money_returns_to_cash_over_time(eco):
    eco.wallet.credit(A, 20_000, "テスト")
    eco.bank.bank_deposit(A, 10_000, NOW)
    later = eco.bank.bank_status(NOW + 24 * HOUR)
    assert later["cash"] > 2_000 and later["cash"] + later["loaned"] == pytest.approx(10_000)
    assert later["cash"] == pytest.approx(2_000 + 8_000 * (1 - (1 - teiai.LOAN_RECOVERY_PER_HOUR) ** 24))


def test_withdraw_is_limited_by_available_cash_first_come_first_served(eco):
    eco.wallet.credit(A, 20_000, "テスト")
    eco.wallet.credit(B, 20_000, "テスト")
    eco.bank.bank_deposit(A, 10_000, NOW)
    eco.bank.bank_deposit(B, 10_000, NOW)  # 預金 20,000・現金 4,000
    ok, got = eco.bank.bank_withdraw(A, None, NOW)  # 全額 10,000 は、現金 4,000 を超える
    assert not ok and got.code == "bank_available_limit" and got.params["available"] == 4_000
    assert eco.bank.bank_withdraw(A, 4_000, NOW) == (True, 4_000)  # 早い者勝ち
    ok, why = eco.bank.bank_withdraw(B, 1_000, NOW)  # 現金が尽きた
    assert not ok and why.code == "bank_frozen" and eco.bank.bank_status(NOW)["level"] == "frozen"


def test_grace_locks_reserve_and_triggers_a_run(eco):
    eco.wallet.credit(A, 20_000, "テスト")
    eco.bank.bank_deposit(A, 10_000, NOW)  # 現金 2,000（準備率 20%）
    before = eco.bank.bank_status(NOW)
    go_into_debt(eco, B, 20_000)  # 借金が大きく、猶予に入る → 借金の5%がロックされる
    after = eco.bank.bank_status(NOW)
    assert after["locks"] > before["locks"] and after["available"] < before["available"]
    assert after["ratio"] < before["ratio"]


def test_income_goes_to_the_bank_cash_not_to_the_trust(eco):
    eco.wallet.credit(A, 30_000, "テスト")
    eco.bank.bank_deposit(A, 20_000, NOW)
    cash0 = eco.bank.bank_status(NOW)["cash"]
    with eco.db.tx():
        eco.bank.bank_income_for(G, 3_000, NOW)
    assert eco.bank.deposit_of(A) == 20_000  # 信託は、収入の分配ではなく決まった利息を受け取る（#33）
    s = eco.bank.bank_status(NOW)
    assert s["cash"] == pytest.approx(cash0 + 3_000 - s["thermostat"]["burned"])
    assert 0 < s["thermostat"]["burned"] < 3_000


def test_trust_interest_is_paid_hourly_by_size_at_the_trust_rate(eco):
    eco.wallet.credit(A, 300_000, "テスト")
    eco.wallet.credit(B, 300_000, "テスト")
    eco.bank.bank_deposit(A, 200_000, NOW)
    eco.bank.bank_deposit(B, 100_000, NOW)
    assert eco.bank.bank_trust_accrue(NOW) == 0  # 数え始め
    assert eco.bank.bank_trust_accrue(NOW + 1800) == 0  # 1時間たっていない
    cash0 = eco.bank.bank_status(NOW + 86400)["cash"]
    a0, b0 = eco.wallet.balance(A), eco.wallet.balance(B)
    paid = eco.bank.bank_trust_accrue(NOW + 86400)
    rate = eco.bank.bank_status(NOW)["trust_rate"]
    assert rate == pytest.approx(rates.BASE_RISK_FREE_PER_DAY + teiai.TRUST_PREMIUM_PER_DAY)
    assert paid == pytest.approx(300_000 * rates.growth(rate, 1.0), abs=2)
    assert eco.bank.deposit_of(A) == 200_000 and eco.bank.deposit_of(B) == 100_000  # 信託の残高は増やさない（#33）
    gain_a, gain_b = eco.wallet.balance(A) - a0, eco.wallet.balance(B) - b0
    assert gain_a + gain_b == paid and abs(gain_a - 2 * gain_b) <= 2  # 残高に応じて 2:1、手元へ払う
    s = eco.bank.bank_status(NOW + 86400)
    assert (
        s["cash"] == pytest.approx(cash0 - paid + s["thermostat"]["bailed"], abs=2) and s["trust_paid"] == paid
    )  # 手元の現金と熱浴の肩代わりで払う


def test_trust_interest_is_never_taken_from_the_trust_itself(eco):
    eco.wallet.credit(A, 300_000, "テスト")
    eco.bank.bank_deposit(A, 100_000, NOW)  # 手元 2万・地下融資 8万
    eco.bank.bank_trust_accrue(NOW)
    with eco.db.tx():  # 手元の現金を使い切っておく
        cash = eco.bank.bank_sync_for(G, NOW)
        eco.bank.bank_set_for(G, 0.0, cash["loaned"])
        eco.db.conn.execute("UPDATE teiai_bank SET ts=? WHERE guild_id=?", (NOW + 10 * 86400, G))  # 地下融資は戻さない
    give_units(eco, B, 10_000)
    a0 = eco.wallet.balance(A)
    paid = eco.bank.bank_trust_accrue(NOW + 86400)
    assert eco.bank.deposit_of(A) == 100_000  # 信託から利息を払うと、自分で自分に払うことになる
    assert eco.wallet.balance(A) == a0 + paid
    s = eco.bank.bank_status(NOW + 86400)
    assert paid > 0 and s["excess_unpaid"] == 0  # 手元で払えない分は、熱浴と帝愛共済の口の持ち主が負担する（#32・#34）
    charged = eco.db.conn.execute(
        "SELECT COALESCE((SELECT charged FROM kyosai_state WHERE guild_id=?), 0)", (G,)
    ).fetchone()[0]
    assert charged + s["thermostat"]["bailed"] == pytest.approx(paid, abs=1)


def test_income_goes_to_the_bank_even_without_depositors(eco):
    with eco.db.tx():
        eco.bank.bank_income_for(G, 5_000, NOW)
    s = eco.bank.bank_status(NOW)
    assert s["cash"] + s["thermostat"]["burned"] == pytest.approx(5_000) and s["cash"] > 0  # 社債の償還と共済の配当の原資になる（#33 のレビュー）


def test_game_losses_feed_the_bank(eco):
    eco.wallet.credit(A, 20_000, "テスト")
    eco.bank.bank_deposit(A, 10_000, NOW)
    eid = eco.escrow.place_bet(B, 1_000, "テスト")
    cash0 = eco.bank.bank_status(time.time())["cash"]
    eco.escrow.settle(eid, 0, "テスト")  # 1,000 負け → 3% = 30
    assert eco.bank.deposit_of(A) == 10_000
    assert eco.bank.bank_status(time.time())["cash"] == pytest.approx(cash0 + 30, abs=1.0)


def test_deposit_counts_in_ranking(eco):
    eco.wallet.credit(A, 20_000, "テスト")
    eco.bank.bank_deposit(A, 10_000, NOW)
    assert {u: v for _, u, v in eco.wallet.top(5)}[A] == INITIAL_BALANCE + 20_000


# ----------------------------- 破産猶予 ----------------------------- #
def test_grace_starts_at_the_threshold_and_ends_when_debt_falls(eco):
    go_into_debt(eco, A, 5_000)
    assert eco.bank.grace_until(A) is None
    go_into_debt(eco, A, 20_000)
    assert eco.bank.grace_until(A) is not None and [u for u, *_ in eco.bank.grace_list()] == [A]
    with eco.db.tx():
        set_test_debt(eco, A, 100, guild_id=G)
        eco.debt.after_debt_change(G, A)
    assert eco.bank.grace_until(A) is None


def test_expired_grace_forces_bankruptcy_and_not_before(eco):
    go_into_debt(eco, A, 20_000)
    until = eco.bank.grace_until(A)
    assert eco.bank.grace_expire(until - 1) == []
    done = eco.bank.grace_expire(until + 1)
    assert [u for u, _ in done] == [A] and done[0][1]["forced"] is True
    assert eco.debt.status(A) == teiai.BANKRUPT and eco.bank.grace_until(A) is None


def test_no_new_borrowing_during_grace(eco):
    go_into_debt(eco, A, 20_000)
    assert not eco.debt.borrow_seed(A)[0]
    assert not eco.debt.can_borrow_for_bet(A, 100)[0]


def test_grace_penalty_and_a1_work_together_in_strict_guild_scope(eco):
    eco.wallet.credit(A, 10_000, "テスト")
    eco.bank.bank_deposit(A, 4_000, NOW)
    with eco.db.tx():
        set_test_balance(eco, A, 1_000, guild_id=G)
        set_test_debt(eco, A, 14_900, principal=14_900, interest_at=NOW, guild_id=G)
    # 2日後の定期更新: 利息 +298 (15,198) → A-1 で現金 1,000 + 預金 4,000 を回収し 10,198 → 猶予回避
    eco.debt.accrue_all_debt_interest(NOW + 2 * 86400 + 1)
    assert eco.debt.debt_for(G, A) == 10_198
    assert eco.bank.grace_until(A) is None
    # 再び借金して猶予へ入ると初回 24時間、抜けてすぐ再突入すると 12時間に短縮
    with eco.db.tx():
        eco.debt.owe_for(G, A, 6_000, "追加")
    until1 = must(eco.bank.grace_until(A))
    assert until1 > NOW + 23 * HOUR
    with eco.db.tx():
        set_test_debt(eco, A, 1_000, principal=1_000, guild_id=G)
        eco.debt.after_debt_change(G, A)
    assert eco.bank.grace_until(A) is None
    with eco.db.tx():
        eco.debt.owe_for(G, A, 16_000, "再突入")
    until2 = must(eco.bank.grace_until(A))
    assert until2 - time.time() < 13 * HOUR


# ----------------------------- CDS ----------------------------- #
def test_odds_follow_the_targets_risk_and_stay_in_range():
    assert teiai.cds_odds(teiai.KATAGI, 0) == teiai.CDS_MAX_ODDS
    mid = teiai.cds_odds(teiai.UNDERGROUND, 3_000)
    high = teiai.cds_odds(teiai.UNDERGROUND, 14_000)
    assert teiai.CDS_MAX_ODDS > mid > high >= teiai.CDS_MIN_ODDS
    assert teiai.cds_odds(teiai.KATAGI, 0, 1.2) < teiai.cds_odds(teiai.KATAGI, 0, 2.0)  # 維持率が低いほど危ない


def test_cds_buy_rules(eco):
    eco.wallet.credit(A, 5_000, "テスト")
    eco.wallet.balance(T)
    assert not eco.bank.cds_buy(A, T, 50, time.time())[0]  # 最低額
    assert not eco.bank.cds_buy(A, 12345, 500, time.time())[0]  # 遊んだことのない人
    ok, odds = eco.bank.cds_buy(A, T, 500, time.time())
    assert ok and odds == teiai.CDS_MAX_ODDS
    go_into_debt(eco, T, 20_000)
    assert eco.bank.cds_buy(A, T, 500, time.time())[1].code == "cds_target_in_grace"  # 猶予に入ってからは掛けられない


def test_cds_on_someone_else_pays_only_on_forced_bankruptcy_and_capped_by_their_debt(eco):
    eco.wallet.credit(A, 20_000, "テスト")  # 預金者
    eco.bank.bank_deposit(A, 10_000, NOW)
    eco.wallet.credit(B, 5_000, "テスト")  # CDS の買い手
    eco.wallet.balance(T)
    assert eco.bank.cds_buy(B, T, 2_000, NOW)[0]  # 掛け金2,000・15倍 → 配当の見込み 30,000
    balance_before = eco.wallet.balance(B)
    go_into_debt(eco, T, 20_000)  # 借金 20,000 前後 → 猶予
    until = eco.bank.grace_until(T)
    debt = eco.debt.debt(T)
    done = eco.bank.grace_expire(until + 1)
    result = done[0][1]
    assert result["cds_paid"] == int(teiai.CDS_PAYOUT_CAP_RATE * debt)  # 見込み 30,000 でも、借金の半分まで
    assert eco.wallet.balance(B) == balance_before + result["cds_paid"]
    assert eco.bank.deposit_of(A) < 10_000  # 払い切れず、帝愛信託が第一損失を負う


def test_cds_pays_nothing_if_the_target_declares_bankruptcy_before_the_grace(eco):
    eco.wallet.credit(B, 5_000, "テスト")
    eco.wallet.balance(T)
    eco.bank.cds_buy(B, T, 1_000, NOW)
    go_into_debt(eco, T, 6_000)  # 借金はあるが、猶予（借金が GRACE_DEBT 以上）には入っていない
    assert eco.bank.grace_until(T) is None and eco.debt.debt(T) > 0
    before = eco.wallet.balance(B)
    ok, result = eco.debt.bankrupt(T)  # 猶予に入る前の自己破産 → 配当なし（結託対策）
    assert ok and result["cds_paid"] == 0 and eco.wallet.balance(B) == before
    assert eco.bank.cds_list(B)["bought"] == []  # 無効になっている


def test_cds_pays_even_if_the_target_declares_bankruptcy_himself_during_the_grace(eco):
    eco.wallet.credit(A, 20_000, "テスト")  # 預金者
    eco.bank.bank_deposit(A, 10_000, NOW)
    eco.wallet.credit(B, 5_000, "テスト")
    eco.wallet.balance(T)
    assert eco.bank.cds_buy(B, T, 1_000, NOW)[0]
    go_into_debt(eco, T, 20_000)
    assert eco.bank.grace_until(T) is not None
    debt = eco.debt.debt(T)
    before = eco.wallet.balance(B)
    ok, result = eco.debt.bankrupt(T)  # 猶予に入ってから、時間切れの直前に自分で破産しても、配当は出る
    assert ok and result["forced"] is False
    assert result["cds_paid"] == min(int(1_000 * 15.0), int(teiai.CDS_PAYOUT_CAP_RATE * debt))
    assert eco.wallet.balance(B) == before + result["cds_paid"]


def test_cds_expires_if_nothing_happens(eco):
    eco.wallet.credit(B, 5_000, "テスト")
    eco.wallet.balance(T)
    eco.bank.cds_buy(B, T, 1_000, NOW)
    assert len(eco.bank.cds_list(B)["bought"]) == 1
    eco.bank.grace_expire(NOW + (teiai.CDS_TERM_DAYS + 1) * 86400)
    assert eco.bank.cds_list(B)["bought"] == []


def test_self_cds_covers_only_what_assets_cannot_and_gives_no_cash(eco):
    eco.wallet.credit(A, 20_000, "テスト")
    eco.bank.bank_deposit(A, 10_000, NOW)
    eco.wallet.credit(B, 5_000, "テスト")
    assert eco.bank.cds_buy(B, B, 1_000, NOW)[0]  # 自分に掛ける（15倍・見込み 15,000）
    with eco.db.tx():
        cash = eco.wallet.balance(B)
        eco.debt.owe_for(G, B, cash + 3_000, "テスト")  # 手元の現金 + 3,000 の負け
    assert eco.debt.debt(B) == 0 and eco.debt.status(B) == teiai.KATAGI  # 借金にも地下落ちにもならない
    assert eco.wallet.balance(B) == 0  # 先に、自分の資産（現金）が差し押さえられている（CDS は、その先の 3,000 だけ）
    assert eco.bank.cds_list(B)["bought"] == []  # 使い切り


def test_self_cds_does_not_protect_assets_first_loss_is_borne_by_the_player(eco):
    eco.wallet.credit(B, 5_000, "テスト")
    eco.bank.cds_buy(B, B, 1_000, NOW)
    total_before = eco.wallet.balance(B) + eco.bank.deposit_of(B)
    with eco.db.tx():
        eco.debt.owe_for(G, B, 12_000, "テスト")
    # 掛け金を払い、さらに自分の資産で払える分は失う。CDS は、残りだけ
    assert eco.wallet.balance(B) + eco.bank.deposit_of(B) < total_before - 1_000


def test_self_cds_total_payout_is_capped(eco):
    eco.wallet.credit(B, 100_000, "テスト")
    assert eco.bank.cds_buy(B, B, 3_000, NOW)[0]  # 15倍・見込み 45,000
    ok, why = eco.bank.cds_buy(B, B, 1_000, NOW)  # 合計 60,000 > 50,000
    assert not ok and why.code == "cds_self_cap" and why.params["cap"] == teiai.CDS_SELF_CAP
    assert eco.bank.cds_buy(B, B, 300, NOW)[0]  # 合計 49,500 までなら買える


# ----------------------------- 種銭・信用ダブル・預金の差し押さえ ----------------------------- #
def test_borrow_seed_any_time_up_to_the_frame(eco):
    eco.wallet.debit_up_to(A, INITIAL_BALANCE - 500, "テスト")
    ok, got = eco.debt.borrow_seed(A)
    assert ok and got == teiai.SEED - 500 and eco.wallet.balance(A) == teiai.SEED
    assert eco.debt.debt(A) == got and eco.debt.status(A) == teiai.UNDERGROUND
    assert not eco.debt.borrow_seed(A)[0]  # 枠まで借りたので、もう借りられない


def test_add_to_escrow_with_credit_borrows_the_shortfall_from_teiai(eco):
    eid = eco.escrow.place_bet(A, 2_000, "ブラックジャック")
    eco.wallet.debit_up_to(A, eco.wallet.balance(A) - 500, "テスト")  # 手元 500 しかない
    assert not eco.escrow.add_to_escrow(eid, 2_000)  # 信用なしでは追加できない
    assert eco.escrow.add_to_escrow(eid, 2_000, credit=True)
    assert eco.escrow.escrow_amount(eid) == 4_000 and eco.debt.debt(A) == 1_500 and eco.wallet.balance(A) == 0


def test_credit_is_limited_to_the_seed_frame(eco):
    eid = eco.escrow.place_bet(A, 3_000, "ブラックジャック")
    eco.wallet.debit_up_to(A, eco.wallet.balance(A), "テスト")
    assert eco.escrow.add_to_escrow(eid, 3_000, credit=True)  # 足りない 3,000 は枠いっぱいで借りられる
    eid2 = eco.escrow.place_bet(B, 3_000, "ブラックジャック")
    eco.wallet.debit_up_to(B, eco.wallet.balance(B), "テスト")
    eco.wallet.credit(B, 1, "テスト")
    ok, why = eco.debt.can_borrow_for_bet(B, 3_500)
    assert (
        not ok
        and why.code == "credit_limit_exceeded"
        and why.params["seed"] == teiai.SEED
        and not eco.escrow.add_to_escrow(eid2, 3_500, credit=True)
    )


def test_deposits_are_seized_when_cash_runs_out(eco):
    eco.wallet.credit(A, 20_000, "テスト")
    eco.bank.bank_deposit(A, 10_000, NOW)
    eco.wallet.debit_up_to(A, eco.wallet.balance(A), "テスト")  # 現金 0・預金 10,000
    with eco.db.tx():
        eco.debt.owe_for(G, A, 4_000, "テスト")
    assert eco.bank.deposit_of(A) == 6_000 and eco.debt.debt(A) == 0  # 預金から払って、借金にならない


def test_bankruptcy_liquidates_deposits_too(eco):
    eco.wallet.credit(A, 20_000, "テスト")
    eco.bank.bank_deposit(A, 10_000, NOW)
    go_into_debt(eco, A, 80_000)  # 現金と預金をすべて差し押さえられた上で、借金になる
    ok, _ = eco.debt.bankrupt(A)
    assert ok and eco.bank.deposit_of(A) == 0


# ----------------------------- パネル ----------------------------- #
def test_panel_shows_meter_grace_list_and_my_cds(eco):
    from cogs.teiai.panel import panel_embed

    eco.wallet.credit(A, 20_000, "テスト")
    eco.bank.bank_deposit(A, 10_000, NOW)
    eco.wallet.credit(B, 5_000, "テスト")
    eco.wallet.balance(T)
    eco.bank.cds_buy(B, T, 1_000, NOW)
    eco.bank.cds_buy(B, B, 500, NOW)
    text = panel_embed(make_test_bot(repos=eco).services.teiai, G, A).description
    assert (
        "帝愛銀行" in must(panel_embed(make_test_bot(repos=eco).services.teiai, G, A).title)
        and "準備率" in must(text)
        and "余裕あり" in must(text)
    )
    go_into_debt(eco, T, 20_000)
    e = panel_embed(make_test_bot(repos=eco).services.teiai, G, B)
    names = [f.name for f in e.fields]
    assert any("破産猶予" in must(n) for n in names) and any("CDS" in must(n) for n in names)
    mine = next(f for f in e.fields if f.name == "あなた").value
    assert "自分" in must(mine) and f"<@{T}>" in must(mine)  # 持っている CDS（自分と他人）が並ぶ


def test_panel_shows_frozen_state(eco):
    from cogs.teiai.panel import panel_embed

    eco.wallet.credit(A, 20_000, "テスト")
    eco.bank.bank_deposit(A, 10_000, NOW)
    go_into_debt(eco, B, 200_000)  # 巨額の借金で、ロックが現金を超える
    assert "支払停止" in must(panel_embed(make_test_bot(repos=eco).services.teiai, G, A).description)


# ----------------------------- 最前線サーモスタット（#32） ----------------------------- #
def _thermo(eco):
    return eco.bank.bank_status(NOW)["thermostat"]


def test_thermostat_enforces_by_default_and_observes_when_disabled(eco, monkeypatch):
    from domain import thermostat

    assert thermostat.ENFORCE is True
    monkeypatch.setattr(thermostat, "ENFORCE", False)
    with eco.db.tx():
        eco.bank.bank_income_for(G, 5_000_000, NOW)
    s = eco.bank.bank_status(NOW)
    assert s["cash"] == pytest.approx(5_000_000)  # 観測モードでは現金に全額入る
    t = s["thermostat"]
    assert (
        t["enforce"] is False and t["burned"] > 4_000_000
    )  # 器（下限 E_MIN）を超えた分は、消えるはずだった額として記録
    assert t["load"] == pytest.approx(5_000_000 - t["burned"]) and t["room"] == pytest.approx(0.0, abs=1.0)
    with eco.db.tx():
        assert eco.bank.bank_pay_for(G, 1_000_000, NOW) == 1_000_000
    s = eco.bank.bank_status(NOW)
    assert s["cash"] == pytest.approx(4_000_000)  # 観測モードでは肩代わりは現金に注入されない
    assert 0 < s["thermostat"]["bailed"] < 1_000_000 and s["thermostat"]["load"] < 0  # 黒字帯を使い切ってから肩代わり


def test_thermostat_enforced_burns_the_excess_inflow_and_shares_only_what_arrived(eco, monkeypatch):
    from domain import thermostat

    monkeypatch.setattr("domain.thermostat.ENFORCE", True)
    eco.wallet.credit(A, 20_000, "テスト")
    eco.bank.bank_deposit(A, 10_000, NOW)
    cash_before = eco.bank.bank_status(NOW)["cash"]
    with eco.db.tx():
        eco.bank.bank_income_for(G, 12_000_000, NOW)
    s = eco.bank.bank_status(NOW)
    net = s["cash"] - cash_before
    assert net == pytest.approx(thermostat.E_MIN, rel=0.001)  # 器の下限で頭打ち。残りは熱浴へ消える
    assert s["thermostat"]["burned"] == pytest.approx(12_000_000 - net)
    assert eco.bank.deposit_of(A) == 10_000  # 信託には配らない


def test_thermostat_enforced_bails_out_only_the_excess_after_the_trust_takes_the_first_loss(eco, monkeypatch):
    from domain import thermostat

    monkeypatch.setattr("domain.thermostat.ENFORCE", True)
    eco.wallet.credit(A, 2_000_000, "テスト")
    eco.bank.bank_deposit(A, 1_000_000, NOW)  # 手元 20万・地下融資 80万
    with eco.db.tx():
        paid = eco.bank.bank_pay_for(G, 5_000_000, NOW)
    assert paid == 5_000_000
    assert eco.bank.deposit_of(A) == 0  # 手元 20万の次に、信託 100万が第一損失で全損（マイナスにはならない）
    t = _thermo(eco)
    # サーモスタットに通るのは、現金で払った 20万 + 超過損失 380万 だけ（信託の取り崩し分は通さない）
    assert t["bailed"] == pytest.approx(4_000_000 - thermostat.E_MIN, rel=0.001)
    assert t["load"] == pytest.approx(-thermostat.E_MIN, rel=0.001)
    # 肩代わりは超過損失 380万を埋め、余りは手元へ
    assert eco.bank.bank_status(NOW)["cash"] == pytest.approx(t["bailed"] - 3_800_000, rel=0.01)


def test_shortfall_within_the_trust_is_shared_by_size_and_never_goes_negative(eco):
    eco.wallet.credit(A, 300_000, "テスト")
    eco.wallet.credit(B, 300_000, "テスト")
    eco.bank.bank_deposit(A, 200_000, NOW)  # 手元 4万
    eco.bank.bank_deposit(B, 100_000, NOW)  # 手元 計6万
    give_units(eco, A, 5_000)
    give_units(eco, B, 5_000)
    with eco.db.tx():
        paid = eco.bank.bank_pay_for(G, 90_000, NOW)  # 手元 6万 + 信託から 3万
    assert paid == 90_000
    assert eco.bank.deposit_of(A) == 180_000 and eco.bank.deposit_of(B) == 90_000  # 1割ずつ按分
    with eco.db.tx():
        paid = eco.bank.bank_pay_for(G, 1_000_000, NOW)  # 信託の総額を超える
    assert paid == 1_000_000 and eco.bank.deposit_of(A) == 0 and eco.bank.deposit_of(B) == 0
    # 信託の総額を超えた 73万は、熱浴の肩代わりと帝愛共済（#34）の口の持ち主が負担する
    s = eco.bank.bank_status(NOW)
    assert s["excess_unpaid"] == 0
    charged = eco.db.conn.execute(
        "SELECT COALESCE((SELECT charged FROM kyosai_state WHERE guild_id=?), 0)", (G,)
    ).fetchone()[0]
    assert s["thermostat"]["bailed"] + charged - s["cash"] == pytest.approx(730_000, abs=1)


def test_thermostat_bank_bears_the_whole_loss_while_the_load_is_positive(eco, monkeypatch):
    monkeypatch.setattr("domain.thermostat.ENFORCE", True)
    eco.wallet.credit(A, 200_000, "テスト")
    eco.bank.bank_deposit(A, 100_000, NOW)
    with eco.db.tx():
        eco.bank.bank_income_for(G, 15_000, NOW)  # 熱量をプラスにしておく
    load = _thermo(eco)["load"]
    with eco.db.tx():
        eco.bank.bank_pay_for(G, int(load) - 1, NOW)  # 黒字帯の中の支払い
    t = _thermo(eco)
    assert t["bailed"] == 0.0 and t["load"] == pytest.approx(1.0, abs=1.0)


def test_thermostat_batch_burns_only_the_net_inflow_after_the_cash_arrived(eco, monkeypatch):
    from domain import thermostat

    monkeypatch.setattr("domain.thermostat.ENFORCE", True)
    with eco.db.tx():
        eco.bank.bank_income_for(G, 3_000_000, NOW, thermo=False)
        eco.bank.bank_pay_for(G, 1_000_000, NOW, thermo=False)
        eco.bank.bank_thermo_batch_for(G, 2_000_000, NOW)
    s = eco.bank.bank_status(NOW)
    _, net, burn = thermostat.absorb_inflow(thermostat.State(), 2_000_000)
    assert s["thermostat"]["burned"] == pytest.approx(burn) and s["thermostat"]["bailed"] == 0.0
    assert s["cash"] == pytest.approx(2_000_000 - burn)  # 純額のうち、器に収まった分だけが残る


def test_thermostat_batch_injects_the_bailout_for_a_net_outflow(eco, monkeypatch):
    from domain import thermostat

    monkeypatch.setattr("domain.thermostat.ENFORCE", True)
    with eco.db.tx():
        eco.bank.bank_thermo_batch_for(G, -500_000, NOW)  # 支払いの前に、肩代わりを手元へ入れる
    s = eco.bank.bank_status(NOW)
    _, _, bailout = thermostat.absorb_shock(thermostat.State(), 500_000)
    assert s["thermostat"]["bailed"] == pytest.approx(bailout) and s["cash"] == pytest.approx(bailout)


def test_thermostat_enforced_bond_maturity_with_trust_haircut_never_burns_nor_goes_negative(eco, monkeypatch):
    """ENFORCE でも、手元現金が足りず信託ヘアカットが発生する社債償還では、信託取り崩し分が流入へ反転せず、現金はマイナスにならない。"""
    from domain import thermostat

    monkeypatch.setattr(thermostat, "ENFORCE", True)
    eco.wallet.credit(A, 1_000_000, "テスト")
    eco.bank.bank_deposit(A, 500_000, NOW)  # 信託 50万・手元 10万
    assert eco.bonds.bond_buy(A, "normal", 30_000, NOW)[0]
    rate = eco.bonds.bond_list(A)["active"][0]["rate"]
    interest = int(30_000 * rate / 100)
    with eco.db.tx():  # 手元現金を 0 にして、償還は信託のヘアカットで払う状態にする
        cash = eco.bank.bank_sync_for(G, NOW)
        eco.bank.bank_set_for(G, 0.0, cash["loaned"])
        eco.db.conn.execute("UPDATE teiai_bank SET ts=? WHERE guild_id=?", (NOW + 10 * 86400, G))  # 地下融資は戻さない
    done = eco.bonds.bond_settle(NOW + 3 * 86400)
    assert [(d["status"], d["paid"]) for d in done] == [("paid", 30_000 + interest)]  # 元本+利息を満額払う
    s = eco.bank.bank_status(NOW + 3 * 86400)
    # サーモスタットには純額（元本相殺後の利息だけの流出）しか通らず、burn も bailout も発生しない
    assert s["thermostat"]["burned"] == 0 and s["thermostat"]["bailed"] == 0
    assert s["cash"] >= 0  # 信託取り崩し分が burn に乗って現金がマイナスにならない
    assert eco.bank.deposit_of(A) == 500_000 - 30_000 - interest  # 信託が第一損失


def test_thermostat_enforced_early_bond_sale_counts_only_the_discount_as_inflow(eco, monkeypatch):
    """cash=0・信託ありで通常債を中途売却しても、サーモスタットへの流入は元本との差（値引き）の範囲で、現金はマイナスにならない。"""
    from domain import thermostat

    monkeypatch.setattr(thermostat, "ENFORCE", True)
    eco.wallet.credit(A, 1_000_000, "テスト")
    eco.bank.bank_deposit(A, 200_000, NOW)  # 信託 20万
    assert eco.bonds.bond_buy(A, "normal", 20_000, NOW)[0]
    with eco.db.tx():
        cash = eco.bank.bank_sync_for(G, NOW)
        eco.bank.bank_set_for(G, 0.0, cash["loaned"])
        eco.db.conn.execute("UPDATE teiai_bank SET ts=? WHERE guild_id=?", (NOW + 10 * 86400, G))  # 地下融資は戻さない
    bond_id = eco.bonds.bond_list(A)["active"][0]["id"]
    ok, payout = eco.bonds.bond_cancel(A, bond_id, NOW)
    assert ok and payout < 20_000  # 買った直後の売却は元本割れ
    net = 20_000 - payout  # サーモスタットに流入として通る純額（信託取り崩し分は乗らない）
    _, _, burn = thermostat.absorb_inflow(thermostat.State(), net)
    s = eco.bank.bank_status(NOW)
    assert s["thermostat"]["burned"] == pytest.approx(burn, rel=0.01) and s["thermostat"]["bailed"] == 0.0
    assert s["cash"] >= 0  # burn が手元から引かれてもマイナスにならない
    assert eco.bank.deposit_of(A) == 200_000 - payout  # 売却代は信託のヘアカットで払った


def test_thermostat_state_cools_down_over_time(eco):
    from domain import thermostat

    with eco.db.tx():
        eco.bank.bank_income_for(G, 100_000, NOW)
    hot = _thermo(eco)["load"]
    later = eco.bank.bank_status(NOW + 7 * 86400)["thermostat"]
    assert (
        later["load"] == pytest.approx(hot / 10) and later["capacity"] > thermostat.E_MIN
    )  # 7日で 1/10。器は遅れて育つ


def test_thermostat_columns_are_added_to_an_existing_database(tmp_path):
    import sqlite3

    from domain import thermostat

    path = str(tmp_path / "old.db")
    cash_before = 1_000.0
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE teiai_bank (guild_id INTEGER PRIMARY KEY, cash REAL NOT NULL DEFAULT 0, "
        "loaned REAL NOT NULL DEFAULT 0, ts REAL NOT NULL DEFAULT 0)"
    )
    conn.execute("INSERT INTO teiai_bank (guild_id, cash, loaned, ts) VALUES (?, ?, 0.0, ?)", (G, cash_before, NOW))
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    reopened = open_repositories(path, strict=True).for_guild(G)
    s = reopened.bank.bank_status(NOW)
    assert s["cash"] == pytest.approx(cash_before)  # 既存の現金はそのまま
    assert s["thermostat"]["load"] == 0.0 and s["thermostat"]["capacity"] == pytest.approx(thermostat.E_MIN)


def test_panel_shows_the_thermostat_and_teiai_bussan_pool(eco, monkeypatch):
    from cogs.teiai.panel import panel_embed
    from domain import thermostat

    with eco.db.tx():
        eco.bank.bank_income_for(G, 50_000, NOW)
    embed = panel_embed(make_test_bot(repos=eco).services.teiai, G, A)
    thermo_field = next(f for f in embed.fields if "サーモスタット" in must(f.name))
    assert "熱量" in must(thermo_field.value) and "観測だけ" not in must(thermo_field.value)
    pool_field = next(f for f in embed.fields if "帝愛物産" in must(f.name))
    assert "/会社" in must(pool_field.value)
    full_text = "\n".join(f"{f.name} {f.value}" for f in embed.fields)
    assert "ピンハネ" not in full_text and "胴元の口座" not in full_text

    monkeypatch.setattr(thermostat, "ENFORCE", False)
    obs_embed = panel_embed(make_test_bot(repos=eco).services.teiai, G, A)
    obs_field = next(f for f in obs_embed.fields if "サーモスタット" in must(f.name))
    assert "観測だけ" in must(obs_field.value)
