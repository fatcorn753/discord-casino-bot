"""テスト共通の設定。

ペリカの記録はサーバーごとに別々（Economy.for_guild）。ほとんどのテストは「1つのサーバー」の話なので、
既定では for_guild が同じ記録を返すようにして、1つの口座として確かめる。
サーバー間で混ざらないことを確かめるテストには、@pytest.mark.real_scope を付ける（本物の for_guild を使う）。
"""

import pytest

from core.economy import Economy


def pytest_configure(config):
    config.addinivalue_line("markers", "real_scope: サーバーごとの記録の分離を、本物の for_guild で確かめるテスト")


@pytest.fixture(autouse=True)
def single_scope(request, monkeypatch):
    if request.node.get_closest_marker("real_scope"):
        return
    monkeypatch.setattr(Economy, "for_guild", lambda self, guild_id: self)
