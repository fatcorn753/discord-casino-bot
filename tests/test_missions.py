"""連勝・デイリーミッション・勝ちの演出。"""

import datetime

import pytest

from core import missions
from core.economy import INITIAL_BALANCE, Economy

pytestmark = pytest.mark.real_scope

DAY = datetime.date(2026, 10, 30)
G, U = 10, 1


@pytest.fixture
def eco(tmp_path):
    return Economy(str(tmp_path / "t.db"), strict=True).for_guild(G)


def test_missions_are_same_for_everyone_on_a_day_and_change_by_day():
    a = missions.today_missions(DAY)
    assert len(a) == missions.PER_DAY and len({m.id for m in a}) == missions.PER_DAY
    assert a == missions.today_missions(DAY)
    assert any(missions.today_missions(DAY + datetime.timedelta(days=i)) != a for i in range(1, 6))


def test_streak_counts_wins_resets_on_loss_and_ignores_draws(eco):
    for expect in (1, 2, 3):
        assert eco.record_play(U, "コイン", 100, 100, DAY)["streak"] == expect
    assert eco.record_play(U, "コイン", 100, 0, DAY)["streak"] == 3          # 引き分けは変えない
    ev = eco.record_play(U, "コイン", 100, -100, DAY)
    assert ev["streak"] == 0 and eco.win_streak(U) == (0, 3)                  # 自己ベストは残る
    assert eco.record_play(U, "コイン", 100, 100, DAY)["new_best"] is False


def test_completing_missions_pays_once_and_all_three_give_a_bonus(eco, monkeypatch):
    fixed = [missions.BY_ID["play5"], missions.BY_ID["win3"], missions.BY_ID["wager5k"]]
    monkeypatch.setattr(missions, "today_missions", lambda day: fixed)
    eco.balance(U)
    paid, bonus = 0, 0
    for _ in range(8):
        ev = eco.record_play(U, "スロット", 1_000, 500, DAY)
        paid += sum(r for _, r in ev["completed"])
        bonus += ev["bonus"]
    assert paid == 300 + 400 + 400 and bonus == missions.COMPLETE_BONUS          # 報酬は1回ずつ、ボーナスも1回
    assert eco.balance(U) == INITIAL_BALANCE + paid + bonus
    assert all(done for _, _, done in eco.mission_status(U, DAY))
    ev = eco.record_play(U, "スロット", 1_000, 500, DAY)
    assert ev["completed"] == [] and ev["bonus"] == 0                            # 達成後は増えない


def test_progress_is_per_server(eco):
    eco.record_play(U, "コイン", 100, 100, DAY)
    other = eco.for_guild(20)
    assert other.win_streak(U) == (0, 0) and all(p == 0 for _, p, _ in other.mission_status(U, DAY))


def test_status_does_not_create_an_account(eco):
    eco.mission_status(99, DAY)
    assert eco.peek_stats(99) is None


@pytest.mark.parametrize("bet,net,has", [(100, 50, ""), (100, 300, "大勝ち"), (100, 1_000, "特大勝ち"), (100, 5_000, "ジャックポット"), (100, -100, "")])
def test_big_win_line_by_ratio(bet, net, has):
    line = missions.big_win_line(bet, net)
    assert (has in line) if has else line == ""


def test_streak_line_starts_at_two_and_marks_new_best():
    assert missions.streak_line(1, True) == ""
    assert "2連勝" in missions.streak_line(2, True) and "自己ベスト" not in missions.streak_line(2, True)
    assert "自己ベスト" in missions.streak_line(3, True) and "自己ベスト" not in missions.streak_line(3, False)


def test_jackpot_banner_only_from_50x_profit_and_is_a_code_block():
    assert not missions.is_jackpot(100, 4_999) and missions.is_jackpot(100, 5_000) and not missions.is_jackpot(0, 5_000)
    assert missions.JACKPOT_ART.startswith("```") and missions.JACKPOT_ART.endswith("```")
    assert max(len(line) for line in missions.JACKPOT_ART.split("\n")) <= 30       # スマホでも折り返さない幅


def test_small_bets_do_not_count_for_missions(eco, monkeypatch):
    monkeypatch.setattr(missions, "today_missions", lambda day: [missions.BY_ID["play5"], missions.BY_ID["win3"], missions.BY_ID["wager5k"]])
    for _ in range(20):
        ev = eco.record_play(U, "コイン", missions.MIN_BET - 1, 5, DAY)
        assert ev["completed"] == []
    assert all(p == 0 for _, p, _ in eco.mission_status(U, DAY))
    assert eco.win_streak(U)[0] == 20                                   # 連勝の表示は数える


def test_mission_streak_does_not_carry_over_from_yesterday(eco, monkeypatch):
    monkeypatch.setattr(missions, "today_missions", lambda day: [missions.BY_ID["streak3"], missions.BY_ID["play5"], missions.BY_ID["win3"]])
    yesterday = DAY - datetime.timedelta(days=1)
    for _ in range(3):
        eco.record_play(U, "コイン", 100, 100, yesterday)
    ev = eco.record_play(U, "コイン", 100, 0, DAY)                        # 引き分けで、前日の連勝から報酬が出ない
    assert not any(m.id == "streak3" for m, _ in ev["completed"])
    for _ in range(2):
        eco.record_play(U, "コイン", 100, 100, DAY)
    assert not any(m.id == "streak3" and done for m, _, done in eco.mission_status(U, DAY))
    ev = eco.record_play(U, "コイン", 100, 100, DAY)                      # 今日3連勝して初めて達成
    assert any(m.id == "streak3" for m, _ in ev["completed"])


def test_play_state_table_from_the_previous_version_is_upgraded(tmp_path):
    path = str(tmp_path / "old.db")
    e = Economy(path, strict=True)
    e._conn.execute("DROP TABLE play_state")
    e._conn.execute("CREATE TABLE play_state (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, win_streak INTEGER NOT NULL DEFAULT 0, "
                    "best_streak INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (guild_id, user_id))")
    e._conn.execute("INSERT INTO play_state VALUES (10, 1, 4, 6)")
    e._conn.commit()
    e._conn.close()
    again = Economy(path, strict=True).for_guild(G)
    assert again.win_streak(U) == (4, 6)
    assert again.record_play(U, "コイン", 100, 100, DAY)["streak"] == 5
