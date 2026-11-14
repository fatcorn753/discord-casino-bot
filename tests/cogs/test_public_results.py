"""盛り上がる結果は公開（ephemeral でない）、戦略が見える・失敗の結果は本人だけ。"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from cogs.economy.cog import EconomyCog
from cogs.market.cog import MarketCog
from cogs.teiai.panel import StakeModal, TeiaiPanel
from domain import bonds, teiai
from infra.db.economy import Economy
from services.session import Sessions
from tests.domain.test_market import It

pytestmark = pytest.mark.real_scope

G, U, T = 10, 1, 9


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def bot(tmp_path):
    return SimpleNamespace(
        economy=Economy(str(tmp_path / "t.db"), strict=True), sessions=Sessions(cooldown=0), config={}
    )


def public(it) -> bool:
    return not it.sent[-1].get("ephemeral", False)


def test_seed_borrow_is_public_on_success_and_private_on_failure(bot):
    eco = bot.economy.for_guild(G)
    eco.debit_up_to(U, eco.balance(U) - 100, "テスト")  # 手元 100 → 借りられる
    panel = TeiaiPanel(bot, U)
    it = It()
    run(panel.seed.callback(it))
    assert public(it) and "種銭を借りました" in it.sent[-1]["embed"].title and "テスト" in it.sent[-1]["embed"].title
    it = It()
    run(panel.seed.callback(it))  # もう枠まで借りた
    assert not public(it) and "❌" in it.sent[-1]["embed"].title


def test_cds_on_someone_else_is_public_and_on_yourself_is_private(bot):
    eco = bot.economy.for_guild(G)
    eco.credit(U, 5_000, "テスト")
    eco.balance(T)
    panel = TeiaiPanel(bot, U)
    other = StakeModal(panel, T, 15.0)
    other.stake = SimpleNamespace(value="500")
    it = It()
    run(other.on_submit(it))
    assert public(it) and f"<@{T}>" in it.sent[-1]["embed"].title and "破産に賭けた" in it.sent[-1]["embed"].title
    mine = StakeModal(panel, U, 15.0)
    mine.stake = SimpleNamespace(value="500")
    it = It()
    run(mine.on_submit(it))
    assert not public(it) and "CDS を買いました" in it.sent[-1]["embed"].title
    bad = StakeModal(panel, T, 15.0)
    bad.stake = SimpleNamespace(value="10")  # 最低額に満たない
    it = It()
    run(bad.on_submit(it))
    assert not public(it) and "❌" in it.sent[-1]["embed"].title


def test_bond_sale_is_public_on_success_and_private_on_failure(bot):
    cog = MarketCog(bot)
    cog._prices()
    eco = bot.economy.for_guild(G)
    assert eco.bond_buy(U, "normal", 1_000, time.time())[0]
    bond_id = eco.bond_list(U)["active"][0]["id"]
    it = It()
    run(cog._bond_cancel(it, bond_id))
    assert public(it) and "債券を売りました" in it.sent[-1]["embed"].title
    it = It()
    run(cog._bond_cancel(it, 999))
    assert not public(it) and "❌" in it.sent[-1]["embed"].title
    assert bonds.NORMAL


def test_self_bankruptcy_result_is_public(bot, monkeypatch):
    eco = bot.economy.for_guild(G)
    eco.balance(U)
    with eco._lock, eco._conn:
        eco._owe(U, 10_000, "テスト")  # 借金を作る

    async def agree(interaction, title, description, label):
        return True

    monkeypatch.setitem(EconomyCog.bankrupt.callback.__globals__, "confirm_action", agree)
    it = It()
    it.client = SimpleNamespace(get_cog=lambda name: None)
    it.guild.get_member = lambda uid: None
    cog = EconomyCog(bot)
    run(EconomyCog.bankrupt.callback(cog, it))
    assert public(it) and "自己破産しました" in it.sent[-1]["embed"].title and "テスト" in it.sent[-1]["embed"].title
    assert eco.status(U) == teiai.BANKRUPT
