"""海外逃亡の画面: 船が進んで、成功と失敗の結果が公開メッセージに出る。"""

import asyncio
from types import SimpleNamespace

import pytest

from cogs.teiai import escape as esc
from domain import teiai
from infra.db.repositories import open_repositories
from tests.fakes import as_interaction
from tests.support import make_test_bot, set_test_balance, set_test_debt

U = 1


class Msg:
    def __init__(self):
        self.embeds = []

    async def edit(self, embed=None, **kw):
        self.embeds.append(embed)


class It:
    def __init__(self):
        self.user = SimpleNamespace(id=U, display_name="テスト")
        self.guild_id = 0
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
    e = open_repositories(str(tmp_path / "t.db"))
    e.wallet.credit(U, 10_000, "テスト")
    with e.db.tx():
        set_test_balance(e, U, 1_000)
        set_test_debt(e, U, 20_000)
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

    real = eco.debt.escape_attempt
    eco.debt.escape_attempt = lambda uid, now: real(uid, now, rng=SimpleNamespace(random=lambda: 0.0 if win else 0.99))
    asyncio.run(esc.run_escape(as_interaction(it), make_test_bot(repos=eco).services.teiai, after))
    return it, hooked


def test_ship_moves_then_success_art_and_role_hook(eco):
    it, hooked = play(eco, True)
    assert len(it.msg.embeds) == len(esc.PROGRESS) + 1  # 最初の1コマ + 進行のコマ + 結果
    assert "成功" in it.msg.embeds[-1].title and "FREE" in it.msg.embeds[-1].description and hooked
    assert eco.debt.debt(U) == 0


def test_failure_shows_caught_art_and_adds_the_fine(eco):
    it, hooked = play(eco, False)
    assert "失敗" in it.msg.embeds[-1].title and "CAUGHT" in it.msg.embeds[-1].description and not hooked
    assert eco.debt.debt(U) == 22_000


def test_not_allowed_shows_an_ephemeral_error(eco):
    with eco.db.tx():
        set_test_debt(eco, U, 100)
    it, _ = play(eco, True)
    assert "逃亡できません" in it.sent[0].title and teiai.ESCAPE_MIN_DEBT > 100
