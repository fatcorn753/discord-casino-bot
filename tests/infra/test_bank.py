"""帝愛銀行（預金・準備率・取り付け）・破産猶予・CDS・種銭の随時貸付・ダブルの信用借入。"""

import time

import pytest

from domain import teiai
from infra.db.economy import INITIAL_BALANCE, Economy

pytestmark = pytest.mark.real_scope

G, A, B, C, T = 10, 1, 2, 3, 9
NOW = time.time()  # CDS の有効期限などが、実際の時刻で判定されるので、いまの時刻を基準にする
HOUR = 3600


@pytest.fixture
def eco(tmp_path):
    return Economy(str(tmp_path / "t.db"), strict=True).for_guild(G)


def go_into_debt(eco, uid, amount):
    """uid を、帝愛への借金が amount ほどの状態にする（現金は差し押さえられ、種銭が貸し付けられる）。"""
    eco.balance(uid)
    with eco._lock, eco._conn:
        eco._owe(uid, amount, "テスト")


# ----------------------------- 預金と準備率 ----------------------------- #
def test_deposit_keeps_only_the_reserve_ratio_as_cash(eco):
    eco.credit(A, 20_000, "テスト")
    ok, have = eco.bank_deposit(A, 10_000, NOW)
    assert ok and have == 10_000 and eco.balance(A) == INITIAL_BALANCE + 20_000 - 10_000
    s = eco.bank_status(NOW)
    assert s["deposits"] == 10_000 and s["cash"] == pytest.approx(2_000) and s["loaned"] == pytest.approx(8_000)
    assert s["ratio"] == pytest.approx(0.20) and s["level"] == "safe"


def test_only_katagi_can_deposit_and_minimum_applies(eco):
    eco.credit(A, 5_000, "テスト")
    assert not eco.bank_deposit(A, 50, NOW)[0]  # 最低額
    go_into_debt(eco, B, 10_000)
    assert "カタギ" in eco.bank_deposit(B, 100, NOW)[1]
    assert not eco.bank_deposit(A, 10**9, NOW)[0]  # 残高不足


def test_loaned_money_returns_to_cash_over_time(eco):
    eco.credit(A, 20_000, "テスト")
    eco.bank_deposit(A, 10_000, NOW)
    later = eco.bank_status(NOW + 24 * HOUR)
    assert later["cash"] > 2_000 and later["cash"] + later["loaned"] == pytest.approx(10_000)
    assert later["cash"] == pytest.approx(2_000 + 8_000 * (1 - (1 - teiai.LOAN_RECOVERY_PER_HOUR) ** 24))


def test_withdraw_is_limited_by_available_cash_first_come_first_served(eco):
    eco.credit(A, 20_000, "テスト")
    eco.credit(B, 20_000, "テスト")
    eco.bank_deposit(A, 10_000, NOW)
    eco.bank_deposit(B, 10_000, NOW)  # 預金 20,000・現金 4,000
    ok, got = eco.bank_withdraw(A, None, NOW)  # 全額 10,000 は、現金 4,000 を超える
    assert not ok and "4,000" in got
    assert eco.bank_withdraw(A, 4_000, NOW) == (True, 4_000)  # 早い者勝ち
    ok, why = eco.bank_withdraw(B, 1_000, NOW)  # 現金が尽きた
    assert not ok and "預金封鎖" in why and eco.bank_status(NOW)["level"] == "frozen"


def test_grace_locks_reserve_and_triggers_a_run(eco):
    eco.credit(A, 20_000, "テスト")
    eco.bank_deposit(A, 10_000, NOW)  # 現金 2,000（準備率 20%）
    before = eco.bank_status(NOW)
    go_into_debt(eco, B, 20_000)  # 借金が大きく、猶予に入る → 借金の5%がロックされる
    after = eco.bank_status(NOW)
    assert after["locks"] > before["locks"] and after["available"] < before["available"]
    assert after["ratio"] < before["ratio"]


def test_yield_is_shared_by_deposit_size_and_comes_from_skim_losses_and_premiums(eco):
    eco.credit(A, 30_000, "テスト")
    eco.credit(B, 30_000, "テスト")
    eco.bank_deposit(A, 20_000, NOW)
    eco.bank_deposit(B, 10_000, NOW)
    with eco._lock, eco._conn:
        eco._bank_income(3_000, 1.0, NOW, needs_depositors=True)
    assert eco.deposit_of(A) == 22_000 and eco.deposit_of(B) == 11_000  # 2:1 で分配


def test_no_depositors_means_skim_and_losses_vanish_as_before(eco):
    with eco._lock, eco._conn:
        eco._bank_income(5_000, 1.0, NOW, needs_depositors=True)
    assert eco.bank_status(NOW)["cash"] == 0  # 預金者がいない銀行に、現金を積み上げない


def test_game_losses_and_skim_feed_deposits(eco):
    eco.credit(A, 20_000, "テスト")
    eco.bank_deposit(A, 10_000, NOW)
    eid = eco.place_bet(B, 1_000, "テスト")
    eco.settle(eid, 0, "テスト")  # 1,000 負け → 3% = 30
    assert eco.deposit_of(A) == 10_030


def test_deposit_counts_in_ranking(eco):
    eco.credit(A, 20_000, "テスト")
    eco.bank_deposit(A, 10_000, NOW)
    assert dict((u, v) for _, u, v in eco.top(5))[A] == INITIAL_BALANCE + 20_000


# ----------------------------- 破産猶予 ----------------------------- #
def test_grace_starts_at_the_threshold_and_ends_when_debt_falls(eco):
    go_into_debt(eco, A, 5_000)
    assert eco.grace_until(A) is None
    go_into_debt(eco, A, 20_000)
    assert eco.grace_until(A) is not None and [u for u, *_ in eco.grace_list()] == [A]
    with eco._lock, eco._conn:
        eco._conn.execute("UPDATE users SET debt=100 WHERE user_id=?", (A,))
        eco._after_debt_change(A)
    assert eco.grace_until(A) is None


def test_expired_grace_forces_bankruptcy_and_not_before(eco):
    go_into_debt(eco, A, 20_000)
    until = eco.grace_until(A)
    assert eco.grace_expire(until - 1) == []
    done = eco.grace_expire(until + 1)
    assert [u for u, _ in done] == [A] and done[0][1]["forced"] is True
    assert eco.status(A) == teiai.BANKRUPT and eco.grace_until(A) is None


def test_no_new_borrowing_during_grace(eco):
    go_into_debt(eco, A, 20_000)
    assert not eco.borrow_seed(A)[0]
    assert not eco.can_borrow_for_bet(A, 100)[0]


# ----------------------------- CDS ----------------------------- #
def test_odds_follow_the_targets_risk_and_stay_in_range():
    assert teiai.cds_odds(teiai.KATAGI, 0) == teiai.CDS_MAX_ODDS
    mid = teiai.cds_odds(teiai.UNDERGROUND, 3_000)
    high = teiai.cds_odds(teiai.UNDERGROUND, 14_000)
    assert teiai.CDS_MAX_ODDS > mid > high >= teiai.CDS_MIN_ODDS
    assert teiai.cds_odds(teiai.KATAGI, 0, 1.2) < teiai.cds_odds(teiai.KATAGI, 0, 2.0)  # 維持率が低いほど危ない


def test_cds_buy_rules(eco):
    eco.credit(A, 5_000, "テスト")
    eco.balance(T)
    assert not eco.cds_buy(A, T, 50)[0]  # 最低額
    assert not eco.cds_buy(A, 12345, 500)[0]  # 遊んだことのない人
    ok, odds = eco.cds_buy(A, T, 500)
    assert ok and odds == teiai.CDS_MAX_ODDS
    go_into_debt(eco, T, 20_000)
    assert "猶予" in eco.cds_buy(A, T, 500)[1]  # 猶予に入ってからは掛けられない


def test_cds_on_someone_else_pays_only_on_forced_bankruptcy_and_capped_by_their_debt(eco):
    eco.credit(A, 20_000, "テスト")  # 預金者
    eco.bank_deposit(A, 10_000, NOW)
    eco.credit(B, 5_000, "テスト")  # CDS の買い手
    eco.balance(T)
    assert eco.cds_buy(B, T, 2_000, NOW)[0]  # 掛け金2,000・15倍 → 配当の見込み 30,000
    balance_before = eco.balance(B)
    go_into_debt(eco, T, 20_000)  # 借金 20,000 前後 → 猶予
    until = eco.grace_until(T)
    debt = eco.debt(T)
    done = eco.grace_expire(until + 1)
    result = done[0][1]
    assert result["cds_paid"] == int(teiai.CDS_PAYOUT_CAP_RATE * debt)  # 見込み 30,000 でも、借金の半分まで
    assert eco.balance(B) == balance_before + result["cds_paid"]
    assert (
        eco.deposit_of(A) < 10_000 + 2_000 * teiai.PREMIUM_YIELD_SHARE
    )  # 払い切れず、預金者の預金が減る（AIG ショック）


def test_cds_pays_nothing_if_the_target_declares_bankruptcy_before_the_grace(eco):
    eco.credit(B, 5_000, "テスト")
    eco.balance(T)
    eco.cds_buy(B, T, 1_000, NOW)
    go_into_debt(eco, T, 6_000)  # 借金はあるが、猶予（借金が GRACE_DEBT 以上）には入っていない
    assert eco.grace_until(T) is None and eco.debt(T) > 0
    before = eco.balance(B)
    ok, result = eco.bankrupt(T)  # 猶予に入る前の自己破産 → 配当なし（結託対策）
    assert ok and result["cds_paid"] == 0 and eco.balance(B) == before
    assert eco.cds_list(B)["bought"] == []  # 無効になっている


def test_cds_pays_even_if_the_target_declares_bankruptcy_himself_during_the_grace(eco):
    eco.credit(A, 20_000, "テスト")  # 預金者
    eco.bank_deposit(A, 10_000, NOW)
    eco.credit(B, 5_000, "テスト")
    eco.balance(T)
    assert eco.cds_buy(B, T, 1_000, NOW)[0]
    go_into_debt(eco, T, 20_000)
    assert eco.grace_until(T) is not None
    debt = eco.debt(T)
    before = eco.balance(B)
    ok, result = eco.bankrupt(T)  # 猶予に入ってから、時間切れの直前に自分で破産しても、配当は出る
    assert ok and result["forced"] is False
    assert result["cds_paid"] == min(int(1_000 * 15.0), int(teiai.CDS_PAYOUT_CAP_RATE * debt))
    assert eco.balance(B) == before + result["cds_paid"]


def test_cds_expires_if_nothing_happens(eco):
    eco.credit(B, 5_000, "テスト")
    eco.balance(T)
    eco.cds_buy(B, T, 1_000, NOW)
    assert len(eco.cds_list(B)["bought"]) == 1
    eco.grace_expire(NOW + (teiai.CDS_TERM_DAYS + 1) * 86400)
    assert eco.cds_list(B)["bought"] == []


def test_self_cds_covers_only_what_assets_cannot_and_gives_no_cash(eco):
    eco.credit(A, 20_000, "テスト")
    eco.bank_deposit(A, 10_000, NOW)
    eco.credit(B, 5_000, "テスト")
    assert eco.cds_buy(B, B, 1_000, NOW)[0]  # 自分に掛ける（15倍・見込み 15,000）
    with eco._lock, eco._conn:
        cash = eco._balance(B)
        eco._owe(B, cash + 3_000, "テスト")  # 手元の現金 + 3,000 の負け
    assert eco.debt(B) == 0 and eco.status(B) == teiai.KATAGI  # 借金にも地下落ちにもならない
    assert eco.balance(B) == 0  # 先に、自分の資産（現金）が差し押さえられている（CDS は、その先の 3,000 だけ）
    assert eco.cds_list(B)["bought"] == []  # 使い切り


def test_self_cds_does_not_protect_assets_first_loss_is_borne_by_the_player(eco):
    eco.credit(B, 5_000, "テスト")
    eco.cds_buy(B, B, 1_000, NOW)
    total_before = eco.balance(B) + eco.deposit_of(B)
    with eco._lock, eco._conn:
        eco._owe(B, 12_000, "テスト")
    # 掛け金を払い、さらに自分の資産で払える分は失う。CDS は、残りだけ
    assert eco.balance(B) + eco.deposit_of(B) < total_before - 1_000


def test_self_cds_total_payout_is_capped(eco):
    eco.credit(B, 100_000, "テスト")
    assert eco.cds_buy(B, B, 3_000, NOW)[0]  # 15倍・見込み 45,000
    ok, why = eco.cds_buy(B, B, 1_000, NOW)  # 合計 60,000 > 50,000
    assert not ok and "5" in why and "まで" in why
    assert eco.cds_buy(B, B, 300, NOW)[0]  # 合計 49,500 までなら買える


# ----------------------------- 種銭・信用ダブル・預金の差し押さえ ----------------------------- #
def test_borrow_seed_any_time_up_to_the_frame(eco):
    eco.debit_up_to(A, INITIAL_BALANCE - 500, "テスト")
    ok, got = eco.borrow_seed(A)
    assert ok and got == teiai.SEED - 500 and eco.balance(A) == teiai.SEED
    assert eco.debt(A) == got and eco.status(A) == teiai.UNDERGROUND
    assert not eco.borrow_seed(A)[0]  # 枠まで借りたので、もう借りられない


def test_add_to_escrow_with_credit_borrows_the_shortfall_from_teiai(eco):
    eid = eco.place_bet(A, 2_000, "ブラックジャック")
    eco.debit_up_to(A, eco.balance(A) - 500, "テスト")  # 手元 500 しかない
    assert not eco.add_to_escrow(eid, 2_000)  # 信用なしでは追加できない
    assert eco.add_to_escrow(eid, 2_000, credit=True)
    assert eco.escrow_amount(eid) == 4_000 and eco.debt(A) == 1_500 and eco.balance(A) == 0


def test_credit_is_limited_to_the_seed_frame(eco):
    eid = eco.place_bet(A, 3_000, "ブラックジャック")
    eco.debit_up_to(A, eco.balance(A), "テスト")
    assert eco.add_to_escrow(eid, 3_000, credit=True)  # 足りない 3,000 は枠いっぱいで借りられる
    eid2 = eco.place_bet(B, 3_000, "ブラックジャック")
    eco.debit_up_to(B, eco.balance(B), "テスト")
    eco.credit(B, 1, "テスト")
    ok, why = eco.can_borrow_for_bet(B, 3_500)
    assert not ok and "3,000" in why and not eco.add_to_escrow(eid2, 3_500, credit=True)


def test_deposits_are_seized_when_cash_runs_out(eco):
    eco.credit(A, 20_000, "テスト")
    eco.bank_deposit(A, 10_000, NOW)
    eco.debit_up_to(A, eco.balance(A), "テスト")  # 現金 0・預金 10,000
    with eco._lock, eco._conn:
        eco._owe(A, 4_000, "テスト")
    assert eco.deposit_of(A) == 6_000 and eco.debt(A) == 0  # 預金から払って、借金にならない


def test_bankruptcy_liquidates_deposits_too(eco):
    eco.credit(A, 20_000, "テスト")
    eco.bank_deposit(A, 10_000, NOW)
    go_into_debt(eco, A, 80_000)  # 現金と預金をすべて差し押さえられた上で、借金になる
    ok, _ = eco.bankrupt(A)
    assert ok and eco.deposit_of(A) == 0


# ----------------------------- パネル ----------------------------- #
def test_panel_shows_meter_grace_list_and_my_cds(eco):
    from cogs.teiai.panel import panel_embed

    eco.credit(A, 20_000, "テスト")
    eco.bank_deposit(A, 10_000, NOW)
    eco.credit(B, 5_000, "テスト")
    eco.balance(T)
    eco.cds_buy(B, T, 1_000, NOW)
    eco.cds_buy(B, B, 500, NOW)
    text = panel_embed(eco, A).description
    assert "帝愛銀行" in panel_embed(eco, A).title and "準備率" in text and "余裕あり" in text
    go_into_debt(eco, T, 20_000)
    e = panel_embed(eco, B)
    names = [f.name for f in e.fields]
    assert any("破産猶予" in n for n in names) and any("CDS" in n for n in names)
    mine = next(f for f in e.fields if f.name == "あなた").value
    assert "自分" in mine and f"<@{T}>" in mine  # 持っている CDS（自分と他人）が並ぶ


def test_panel_shows_frozen_state(eco):
    from cogs.teiai.panel import panel_embed

    eco.credit(A, 20_000, "テスト")
    eco.bank_deposit(A, 10_000, NOW)
    go_into_debt(eco, B, 200_000)  # 巨額の借金で、ロックが現金を超える
    assert "支払停止" in panel_embed(eco, A).description
