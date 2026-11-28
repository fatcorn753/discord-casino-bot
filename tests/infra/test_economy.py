import datetime

import pytest

from domain import teiai
from domain.types import DAILY_BASE, DAILY_STREAK_BONUS, DAILY_STREAK_CAP, INITIAL_BALANCE
from infra.db.repositories import open_repositories
from services.session import Sessions
from tests.fakes import must
from tests.support import set_test_debt


@pytest.fixture
def eco(tmp_path):
    return open_repositories(str(tmp_path / "t.db"))


def test_new_user_gets_initial_balance_once(eco):
    assert eco.wallet.balance(1) == INITIAL_BALANCE
    assert eco.wallet.balance(1) == INITIAL_BALANCE  # 2回目に増えない
    assert len(eco.wallet.history(1)) == 1


def test_bet_is_escrowed_and_settled(eco):
    eid = eco.escrow.place_bet(1, 1000, "テスト")
    assert eco.wallet.balance(1) == INITIAL_BALANCE - 1000  # 賭け金は先に預かる
    assert eco.escrow.settle(eid, 2500, "テスト") == 1500  # 損益 = 払い戻し − 賭け金
    assert eco.wallet.balance(1) == INITIAL_BALANCE + 1500


def test_settle_twice_pays_only_once(eco):
    eid = eco.escrow.place_bet(1, 500, "テスト")
    assert eco.escrow.settle(eid, 1000, "テスト") == 500
    assert eco.escrow.settle(eid, 1000, "テスト") is None  # 二重精算は無視
    assert eco.wallet.balance(1) == INITIAL_BALANCE + 500


def test_bet_rejected_when_insufficient_and_balance_untouched(eco):
    assert eco.escrow.place_bet(1, INITIAL_BALANCE + 1, "テスト") is None
    assert eco.escrow.place_bet(1, 0, "テスト") is None
    assert eco.wallet.balance(1) == INITIAL_BALANCE


def test_loss_pays_zero_and_balance_never_negative(eco):
    eid = eco.escrow.place_bet(1, INITIAL_BALANCE, "テスト")
    assert eco.escrow.settle(eid, 0, "テスト") == -INITIAL_BALANCE
    assert eco.wallet.balance(1) == 0
    assert eco.wallet.debit_up_to(1, 500, "倍払い") == 0  # 0 から先は引けない
    assert eco.wallet.balance(1) == 0


def test_debit_up_to_caps_at_balance(eco):
    assert eco.wallet.debit_up_to(1, 10_000, "倍払い") == INITIAL_BALANCE
    assert eco.wallet.balance(1) == 0


def test_refund_all_escrows_returns_unfinished_bets(eco):
    eco.escrow.place_bet(1, 1000, "A")
    eco.escrow.place_bet(2, 700, "B")
    assert eco.wallet.balance(1) == INITIAL_BALANCE - 1000
    assert eco.escrow.refund_all_escrows() == 2
    assert eco.wallet.balance(1) == INITIAL_BALANCE and eco.wallet.balance(2) == INITIAL_BALANCE
    assert eco.escrow.refund_all_escrows() == 0


def test_add_to_escrow_for_double_down(eco):
    eid = eco.escrow.place_bet(1, 1000, "ブラックジャック")
    assert eco.escrow.add_to_escrow(eid, 1000)
    assert eco.escrow.escrow_amount(eid) == 2000 and eco.wallet.balance(1) == INITIAL_BALANCE - 2000
    assert not eco.escrow.add_to_escrow(eid, INITIAL_BALANCE)  # 足りないときは追加できない
    assert eco.escrow.escrow_amount(eid) == 2000


def test_daily_streak_and_once_per_day(eco):
    d1 = datetime.date(2026, 10, 20)
    r1 = eco.wallet.claim_daily(1, d1)
    assert r1.ok and r1.amount == DAILY_BASE and r1.streak == 1
    assert not eco.wallet.claim_daily(1, d1).ok  # 同じ日は2回受け取れない
    r2 = eco.wallet.claim_daily(1, d1 + datetime.timedelta(days=1))
    assert r2.streak == 2 and r2.amount == DAILY_BASE + DAILY_STREAK_BONUS
    r3 = eco.wallet.claim_daily(1, d1 + datetime.timedelta(days=5))  # 間が空くと連続は1に戻る
    assert r3.streak == 1 and r3.amount == DAILY_BASE


def test_daily_streak_bonus_is_capped(eco):
    day = datetime.date(2026, 1, 1)
    last = None
    for i in range(30):
        last = eco.wallet.claim_daily(1, day + datetime.timedelta(days=i))
    cap = DAILY_BASE + DAILY_STREAK_BONUS * DAILY_STREAK_CAP
    assert last is not None and last.amount == cap


def test_ranking_global_and_per_guild(eco):
    for uid, extra in ((1, 5000), (2, 100), (3, 9000)):
        eco.wallet.credit(uid, extra, "テスト")
    assert [uid for _, uid, _ in eco.wallet.top(10)] == [3, 1, 2]
    ids = {1, 2}
    assert [uid for _, uid, _ in eco.wallet.top(10, ids)] == [1, 2]  # サーバー内だけで並べる
    assert eco.wallet.rank_of(2, ids) == (2, INITIAL_BALANCE + 100, 2)
    assert eco.wallet.rank_of(99) is None


def test_transfer_rules(eco):
    day = 0.0
    assert not eco.wallet.transfer(1, 1, 100, 0.1, 20_000, day).ok  # 自分には送れない
    r = eco.wallet.transfer(1, 2, 1000, 0.1, 20_000, day)
    assert r.ok and r.received == 1000 and r.fee == 100
    assert (
        eco.wallet.balance(1) == INITIAL_BALANCE - 1100 and eco.wallet.balance(2) == INITIAL_BALANCE + 1000
    )  # 手数料は消える
    assert not eco.wallet.transfer(1, 2, INITIAL_BALANCE, 0.1, 20_000, day).ok  # 手数料込みで足りない
    assert not eco.wallet.transfer(1, 2, 0, 0.1, 20_000, day).ok


def test_transfer_daily_limit(eco):
    eco.wallet.credit(1, 100_000, "テスト")
    assert eco.wallet.transfer(1, 2, 1000, 0.0, 1500, 0.0).ok
    blocked = eco.wallet.transfer(1, 2, 1000, 0.0, 1500, 0.0)
    assert not blocked.ok and blocked.reason.code == "transfer_daily_limit"
    assert eco.wallet.transfer(1, 2, 1000, 0.0, 1500, 1e12).ok  # 日付が変われば集計が戻る


def test_stats_tracking(eco):
    eid = eco.escrow.place_bet(1, 100, "テスト")
    eco.escrow.settle(eid, 1000, "テスト")
    s = eco.wallet.stats(1)
    assert s["games_played"] == 1 and s["total_wagered"] == 100 and s["biggest_win"] == 900


def test_sessions_block_double_play_and_cooldown():
    s = Sessions(cooldown=2.0)
    assert s.try_start(1, now=100.0) is None
    assert "遊んでいる" in must(s.try_start(1, now=100.5))  # 同時に2つは始められない
    s.end(1, now=101.0)
    assert "少し待って" in must(s.try_start(1, now=102.0))  # 終了直後は待つ
    assert s.try_start(1, now=103.5) is None
    assert s.try_start(2, now=100.0) is None  # 他の人には影響しない


def test_debt_is_withheld_from_income_only_and_blocks_spending(eco):
    eco.wallet.balance(1)
    eid = eco.escrow.place_bet(1, INITIAL_BALANCE, "チンチロ")
    assert eco.escrow.settle_with_penalty(eid, 0, "チンチロ", 5000)[:2] == (
        -INITIAL_BALANCE - 5000,
        5000,
    )  # 残高0、5000が借金
    assert (eco.wallet.balance(1), eco.debt.debt(1)) == (
        teiai.SEED,
        5000 + teiai.SEED,
    )  # 手元が空なので、帝愛が種銭を貸す（借金に加わる）
    assert eco.shop.purchase(1, "x", "x", 100, False).ok is False and eco.crypto.crypto_buy(1, 100, 10.0).ok is False
    assert eco.wallet.transfer(1, 2, 10, 0.0, 10_000, 0).ok is False  # 借金中は、お金を逃がせない
    result = eco.wallet.claim_daily(1, datetime.date(2026, 11, 1))  # デイリー: 先にピンハネ、残りから天引き
    assert result.skimmed == 200 and result.repaid == 240 and result.seed == 0  # 1000 の 20% と、残り 800 の 30%
    assert (eco.wallet.balance(1), eco.debt.debt(1)) == (teiai.SEED + 800 - 240, 5000 + teiai.SEED - 240)
    eco.wallet.credit(1, 6000, "テスト")  # 管理者の付与などは、収入ではないので、天引きしない
    assert eco.debt.debt(1) == 5000 + teiai.SEED - 240
    ok, paid = eco.debt.repay_debt(1)  # 自分から返す（返せる限り全額）
    assert ok and eco.debt.debt(1) == 0 and eco.wallet.balance(1) == teiai.SEED + 800 - 240 + 6000 - paid
    assert eco.wallet.top(5)[0][1] in (1, 2)


def test_refund_is_not_taken_for_debt_and_ranking_subtracts_debt(eco):
    eco.wallet.balance(1)
    eco.wallet.balance(2)
    eid = eco.escrow.place_bet(1, 1000, "コイン")
    set_test_debt(eco, 1, 500)
    eco.escrow.refund(eid)
    assert eco.wallet.balance(1) == INITIAL_BALANCE and eco.debt.debt(1) == 500  # 返金は徴収しない
    assert {u: a for _, u, a in eco.wallet.top(5)} == {1: INITIAL_BALANCE - 500, 2: INITIAL_BALANCE}


def test_transfer_daily_limit_counts_only_the_amount_not_the_fee(eco):
    eco.wallet.credit(1, 100_000, "テスト")
    ok = eco.wallet.transfer(1, 2, 20_000, 0.1, 20_000, 0.0)  # 上限ちょうどの金額を、手数料つきで送れる
    assert ok.ok and ok.fee == 2_000 and eco.wallet.balance(2) == INITIAL_BALANCE + 20_000
    assert eco.wallet.balance(1) == INITIAL_BALANCE + 100_000 - 22_000
    blocked = eco.wallet.transfer(1, 2, 1, 0.1, 20_000, 0.0)
    assert not blocked.ok and blocked.reason.code == "transfer_daily_limit"  # 上限に達したあとは、1ペリカも送れない
    assert eco.wallet.transfer(1, 2, 100, 0.1, 20_000, 1e12).ok


def test_history_shows_transfer_and_fee_as_one_line(eco):
    from cogs.shared.messages import format_history

    eco.wallet.credit(1, 5_000, "テスト")
    eco.wallet.transfer(1, 2, 1_000, 0.1, 20_000, 0.0)
    lines = format_history(eco.wallet.history(1, 10), 3)
    assert "送金 → <@2>" in lines[0] and "-1,100" in lines[0] and "手数料" not in lines[0]
