"""テスト共通の設定。

ペリカの記録はサーバーごとに別々（Economy.for_guild）。ほとんどのテストは「1つのサーバー」の話なので、
既定では for_guild が同じ記録を返すようにして、1つの口座として確かめる。
サーバー間で混ざらないことを確かめるテストには、@pytest.mark.real_scope を付ける（本物の for_guild を使う）。
"""

import pytest

from domain import events, missions
from infra.db.economy import Economy


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_missions: 日付で決まる本物のミッションを使うテスト（既定ではミッションを空にして、日付で結果が変わらないようにする）",
    )
    config.addinivalue_line(
        "markers", "real_events: 日付と時刻で決まる本物の突発イベントを使うテスト（既定ではイベントなしにする）"
    )
    config.addinivalue_line(
        "markers", "new_players: クイズの報酬の条件（ゲームを3回以上）を、本物の回数で確かめるテスト"
    )
    config.addinivalue_line("markers", "real_scope: サーバーごとの記録の分離を、本物の for_guild で確かめるテスト")


@pytest.fixture(autouse=True)
def single_scope(request, monkeypatch):
    if request.node.get_closest_marker("real_scope"):
        return
    monkeypatch.setattr(Economy, "for_guild", lambda self, guild_id: self)


@pytest.fixture(autouse=True)
def no_random_missions(request, monkeypatch):
    """ミッションの報酬が日付によって入る・入らないでテストの結果が変わらないよう、既定では今日のミッションを空にする。"""
    if request.node.get_closest_marker("real_missions"):
        return
    monkeypatch.setattr(missions, "today_missions", lambda day: [])


@pytest.fixture(autouse=True)
def no_events(request, monkeypatch):
    """突発イベントが、テストを実行した時刻によって結果を変えないよう、既定ではイベントなしにする。"""
    if request.node.get_closest_marker("real_events"):
        return
    monkeypatch.setattr(events, "active", lambda guild_id, now: None)
