"""テスト共通の設定。

ペリカの記録はサーバーごとに別々（Economy.for_guild）。ほとんどのテストは「1つのサーバー」の話なので、
既定では for_guild が同じ記録を返すようにして、1つの口座として確かめる。
サーバー間で混ざらないことを確かめるテストには、@pytest.mark.real_scope を付ける（本物の for_guild を使う）。
"""

import pytest

from core import missions
from core.economy import Economy


def pytest_configure(config):
    config.addinivalue_line("markers", "real_missions: 日付で決まる本物のミッションを使うテスト（既定ではミッションを空にして、日付で結果が変わらないようにする）")
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
