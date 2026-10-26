import datetime

import pytest

from core.economy import INITIAL_BALANCE, Economy
from core.session import Sessions


@pytest.fixture
def eco(tmp_path):
    return Economy(str(tmp_path / "t.db"))


def test_new_user_gets_initial_balance_once(eco):
    assert eco.balance(1) == INITIAL_BALANCE
    assert eco.balance(1) == INITIAL_BALANCE          # 2回目に増えない
    assert len(eco.history(1)) == 1


def test_bet_is_escrowed_and_settled(eco):
    eid = eco.place_bet(1, 1000, "テスト")
    assert eco.balance(1) == INITIAL_BALANCE - 1000    # 賭け金は先に預かる
    assert eco.settle(eid, 2500, "テスト") == 1500     # 損益 = 払い戻し − 賭け金
    assert eco.balance(1) == INITIAL_BALANCE + 1500


def test_settle_twice_pays_only_once(eco):
    eid = eco.place_bet(1, 500, "テスト")
    assert eco.settle(eid, 1000, "テスト") == 500
    assert eco.settle(eid, 1000, "テスト") is None     # 二重精算は無視
    assert eco.balance(1) == INITIAL_BALANCE + 500


def test_bet_rejected_when_insufficient_and_balance_untouched(eco):
    assert eco.place_bet(1, INITIAL_BALANCE + 1, "テスト") is None
    assert eco.place_bet(1, 0, "テスト") is None
    assert eco.balance(1) == INITIAL_BALANCE


def test_loss_pays_zero_and_balance_never_negative(eco):
    eid = eco.place_bet(1, INITIAL_BALANCE, "テスト")
    assert eco.settle(eid, 0, "テスト") == -INITIAL_BALANCE
    assert eco.balance(1) == 0
    assert eco.debit_up_to(1, 500, "倍払い") == 0       # 0 から先は引けない
    assert eco.balance(1) == 0


def test_debit_up_to_caps_at_balance(eco):
    assert eco.debit_up_to(1, 10_000, "倍払い") == INITIAL_BALANCE
    assert eco.balance(1) == 0


def test_refund_all_escrows_returns_unfinished_bets(eco):
    eco.place_bet(1, 1000, "A")
    eco.place_bet(2, 700, "B")
    assert eco.balance(1) == INITIAL_BALANCE - 1000
    assert eco.refund_all_escrows() == 2
    assert eco.balance(1) == INITIAL_BALANCE and eco.balance(2) == INITIAL_BALANCE
    assert eco.refund_all_escrows() == 0


def test_add_to_escrow_for_double_down(eco):
    eid = eco.place_bet(1, 1000, "ブラックジャック")
    assert eco.add_to_escrow(eid, 1000)
    assert eco.escrow_amount(eid) == 2000 and eco.balance(1) == INITIAL_BALANCE - 2000
    assert not eco.add_to_escrow(eid, INITIAL_BALANCE)    # 足りないときは追加できない
    assert eco.escrow_amount(eid) == 2000


def test_daily_streak_and_once_per_day(eco):
    d1 = datetime.date(2026, 10, 20)
    r1 = eco.claim_daily(1, d1)
    assert r1.ok and r1.amount == Economy.DAILY_BASE and r1.streak == 1
    assert not eco.claim_daily(1, d1).ok                  # 同じ日は2回受け取れない
    r2 = eco.claim_daily(1, d1 + datetime.timedelta(days=1))
    assert r2.streak == 2 and r2.amount == Economy.DAILY_BASE + Economy.DAILY_STREAK_BONUS
    r3 = eco.claim_daily(1, d1 + datetime.timedelta(days=5))   # 間が空くと連続は1に戻る
    assert r3.streak == 1 and r3.amount == Economy.DAILY_BASE


def test_daily_streak_bonus_is_capped(eco):
    day = datetime.date(2026, 1, 1)
    last = None
    for i in range(30):
        last = eco.claim_daily(1, day + datetime.timedelta(days=i))
    cap = Economy.DAILY_BASE + Economy.DAILY_STREAK_BONUS * Economy.DAILY_STREAK_CAP
    assert last.amount == cap


def test_ranking_global_and_per_guild(eco):
    for uid, extra in ((1, 5000), (2, 100), (3, 9000)):
        eco.credit(uid, extra, "テスト")
    assert [uid for _, uid, _ in eco.top(10)] == [3, 1, 2]
    eco.touch_guild_user(10, 1)
    eco.touch_guild_user(10, 2)
    ids = eco.guild_user_ids(10)
    assert [uid for _, uid, _ in eco.top(10, ids)] == [1, 2]           # サーバー内だけで並べる
    assert eco.rank_of(2, ids) == (2, INITIAL_BALANCE + 100, 2)
    assert eco.rank_of(99) is None


def test_transfer_rules(eco):
    day = 0.0
    assert not eco.transfer(1, 1, 100, 0.1, 20_000, day).ok                    # 自分には送れない
    r = eco.transfer(1, 2, 1000, 0.1, 20_000, day)
    assert r.ok and r.received == 1000 and r.fee == 100
    assert eco.balance(1) == INITIAL_BALANCE - 1100 and eco.balance(2) == INITIAL_BALANCE + 1000   # 手数料は消える
    assert not eco.transfer(1, 2, INITIAL_BALANCE, 0.1, 20_000, day).ok        # 手数料込みで足りない
    assert not eco.transfer(1, 2, 0, 0.1, 20_000, day).ok


def test_transfer_daily_limit(eco):
    eco.credit(1, 100_000, "テスト")
    assert eco.transfer(1, 2, 1000, 0.0, 1500, 0.0).ok
    blocked = eco.transfer(1, 2, 1000, 0.0, 1500, 0.0)
    assert not blocked.ok and "1日の送金" in blocked.reason
    assert eco.transfer(1, 2, 1000, 0.0, 1500, 1e12).ok                         # 日付が変われば集計が戻る


def test_stats_tracking(eco):
    eid = eco.place_bet(1, 100, "テスト")
    eco.settle(eid, 1000, "テスト")
    s = eco.stats(1)
    assert s["games_played"] == 1 and s["total_wagered"] == 100 and s["biggest_win"] == 900


def test_sessions_block_double_play_and_cooldown():
    s = Sessions(cooldown=2.0)
    assert s.try_start(1, now=100.0) is None
    assert "遊んでいる" in s.try_start(1, now=100.5)    # 同時に2つは始められない
    s.end(1, now=101.0)
    assert "少し待って" in s.try_start(1, now=102.0)    # 終了直後は待つ
    assert s.try_start(1, now=103.5) is None
    assert s.try_start(2, now=100.0) is None            # 他の人には影響しない
