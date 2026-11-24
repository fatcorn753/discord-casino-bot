"""テスト共通の設定。

ペリカの記録はサーバーごとに別々（Economy.for_guild）。ほとんどのテストは「1つのサーバー」の話なので、
既定では for_guild が同じ記録を返すようにして、1つの口座として確かめる。
サーバー間で混ざらないことを確かめるテストには、@pytest.mark.real_scope を付ける（本物の for_guild を使う）。
"""

import pytest

from infra.catalogs import bind_catalogs

bind_catalogs()

from domain import events, missions, skins, teiai  # noqa: E402
from infra.db.base import ScopedRepo  # noqa: E402
from infra.db.repositories import RepositoryBundle  # noqa: E402


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
    config.addinivalue_line(
        "markers", "real_market: 帝愛の出品(#72)を本物の設定で出すテスト（既定では1日0回にして、棚に並べない）"
    )
    config.addinivalue_line(
        "markers", "real_seasons: 季節演出を本物の設定で確かめるテスト（既定では季節演出なしにする）"
    )


@pytest.fixture(autouse=True)
def single_scope(request, monkeypatch):
    if request.node.get_closest_marker("real_scope"):
        return
    monkeypatch.setattr(ScopedRepo, "for_guild", lambda self, guild_id: self)
    monkeypatch.setattr(RepositoryBundle, "for_guild", lambda self, guild_id: self)


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


@pytest.fixture(autouse=True)
def no_teiai_market(request, monkeypatch):
    """帝愛の出品(#72)と発掘品の競りがランダムに並んで、画面のテストが時刻で変わらないよう、既定では出さない。"""
    if request.node.get_closest_marker("real_market"):
        return
    monkeypatch.setattr(teiai, "MARKET_SLOTS_PER_DAY", 0)
    monkeypatch.setattr(teiai, "RELIC_AUCTION_PER_DAY", 0)  # 帝愛の発掘品の競りも、既定では出さない


@pytest.fixture(autouse=True)
def no_seasons(request, monkeypatch):
    """季節演出（クリスマスなど）が、テストを実行した日付によって見た目を変えないよう、既定では季節演出なしにする。"""
    if request.node.get_closest_marker("real_seasons"):
        return
    monkeypatch.setattr(skins, "SEASONS", ())
