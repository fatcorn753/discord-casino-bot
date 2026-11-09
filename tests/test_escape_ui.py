"""海外逃亡の画面: 船が進んで、成功と失敗の結果が公開メッセージに出る。"""

import asyncio
from types import SimpleNamespace

import pytest

from cogs import _teiai_escape as esc
from core import teiai
from core.economy import Economy

U = 1


class Msg:
    def __init__(self):
        self.embeds = []

    async def edit(self, embed=None, **kw):
        self.embeds.append(embed)


class It:
    def __init__(self):
        self.user = SimpleNamespace(id=U, display_name="テスト")
        self.sent = []
        self.msg = Msg()
        self.followup = SimpleNamespace(send=self._send)

    async def _send(self, *a, embed=None, wait=False, **kw):
        self.sent.append(embed)
        if wait:
            self.msg.embeds.append(embed)
            return self.msg


@pytest.fixture
def eco(tmp_path):
    e = Economy(str(tmp_path / "t.db"))
    e.credit(U, 10_000, "テスト")
    e._conn.execute("UPDATE users SET balance=1_000, debt=20_000 WHERE user_id=?", (U,))
    e._conn.commit()
    return e


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    async def yes(*a, **k):
        return True

    async def nap(_):
        return None

    monkeypatch.setattr(esc, "confirm_action", yes)
    monkeypatch.setattr(esc.asyncio, "sleep", nap)


def play(eco, win):
    it, hooked = It(), []

    async def after(inter):
        hooked.append(True)

    real = eco.escape_attempt
    eco.escape_attempt = lambda uid: real(uid, rng=SimpleNamespace(random=lambda: 0.0 if win else 0.99))
    asyncio.run(esc.run_escape(it, eco, after))
    return it, hooked


def test_ship_moves_then_success_art_and_role_hook(eco):
    it, hooked = play(eco, True)
    assert len(it.msg.embeds) == len(esc.PROGRESS) + 1  # 最初の1コマ + 進行のコマ + 結果
    assert "成功" in it.msg.embeds[-1].title and "FREE" in it.msg.embeds[-1].description and hooked
    assert eco.debt(U) == 0


def test_failure_shows_caught_art_and_adds_the_fine(eco):
    it, hooked = play(eco, False)
    assert "失敗" in it.msg.embeds[-1].title and "CAUGHT" in it.msg.embeds[-1].description and not hooked
    assert eco.debt(U) == 22_000


def test_not_allowed_shows_an_ephemeral_error(eco):
    eco._conn.execute("UPDATE users SET debt=100 WHERE user_id=?", (U,))
    it, _ = play(eco, True)
    assert "逃亡できません" in it.sent[0].title and teiai.ESCAPE_MIN_DEBT > 100
