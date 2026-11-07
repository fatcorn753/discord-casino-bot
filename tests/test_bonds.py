"""債券: 購入、満期の入金、倒産、解約、上限、ランキング、再起動。"""

import asyncio
import random
from types import SimpleNamespace

import pytest

from cogs.bond_cog import BondCog, describe_done
from cogs.market_cog import MarketCog
from core import bonds
from core.economy import INITIAL_BALANCE, Economy

NOW = 1_000_000.0
DAY = 86400


class Always:
    """乱数を固定する: random() が常に value を返す。"""

    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


@pytest.fixture
def eco(tmp_path):
    return Economy(str(tmp_path / "t.db"))


def test_normal_bond_pays_principal_plus_interest_at_maturity(eco):
    assert eco.bond_buy(1, "normal", 2000, NOW) == (True, "")
    assert eco.balance(1) == INITIAL_BALANCE - 2000
    assert eco.bond_settle(NOW + 3 * DAY - 1) == []  # まだ満期ではない
    done = eco.bond_settle(NOW + 3 * DAY)
    assert [(d["status"], d["paid"]) for d in done] == [("paid", 2080)]  # +4%
    assert eco.balance(1) == INITIAL_BALANCE + 80
    assert eco.bond_settle(NOW + 9 * DAY) == []  # 二重に払わない


def test_junk_bond_pays_high_interest_or_defaults(eco):
    eco.bond_buy(1, "junk", 1000, NOW)
    eco.bond_buy(2, "junk", 1000, NOW)
    # 倒産率20%: random()*100 < 20 なら倒産
    assert eco.bond_settle(NOW + DAY, Always(0.19))[0]["status"] == "defaulted"
    assert eco.balance(1) == INITIAL_BALANCE - 1000 and eco.balance(2) == INITIAL_BALANCE - 1000
    eco.bond_buy(3, "junk", 1000, NOW)
    done = eco.bond_settle(NOW + DAY, Always(0.20))
    assert [(d["user_id"], d["status"], d["paid"]) for d in done if d["user_id"] == 3] == [(3, "paid", 1200)]


def test_normal_bond_never_defaults(eco):
    eco.bond_buy(1, "normal", 1000, NOW)
    assert eco.bond_settle(NOW + 3 * DAY, Always(0.0))[0]["status"] == "paid"


def test_default_rate_is_roughly_twenty_percent():
    rng = random.Random(3)
    eco_n = 4000
    hits = sum(rng.random() * 100 < bonds.JUNK.default_pct for _ in range(eco_n))
    assert 0.18 < hits / eco_n < 0.22
    assert 0.95 < (1 - bonds.JUNK.default_pct / 100) * (1 + bonds.JUNK.rate / 100) < 0.97  # 期待値は約96%


def test_cancel_only_normal_with_penalty(eco):
    eco.bond_buy(1, "normal", 2000, NOW)
    eco.bond_buy(1, "junk", 1000, NOW)
    by_kind = {r["kind"]: r["id"] for r in eco.bond_list(1)["active"]}
    ids = [by_kind["normal"], by_kind["junk"]]
    assert eco.bond_cancel(1, ids[1]) == (False, "ジャンク債は解約できません。")
    assert eco.bond_cancel(1, ids[0]) == (True, 1800)  # 10%引き
    assert eco.balance(1) == INITIAL_BALANCE - 3000 + 1800
    assert not eco.bond_cancel(1, ids[0])[0] and not eco.bond_cancel(2, ids[1])[0]  # 二重・他人の債券


def test_buy_guards(eco):
    assert not eco.bond_buy(1, "normal", 999, NOW)[0]  # 最低額
    assert not eco.bond_buy(1, "normal", INITIAL_BALANCE + 1, NOW)[0]  # 残高不足
    eco.stats(2)
    eco._conn.execute("UPDATE users SET debt=500 WHERE user_id=2")
    assert not eco.bond_buy(2, "normal", 1000, NOW)[0]  # 借金中
    eco.credit(3, 20_000, "テスト")
    assert eco.bond_buy(3, "junk", 6000, NOW)[0] is True
    ok, reason = eco.bond_buy(3, "junk", 6000, NOW)  # ジャンク債は合計10,000まで
    assert not ok and "あと 4,000" in reason
    assert eco.bond_buy(3, "normal", 1000, NOW)[0]  # 種類ごとに別の上限


def test_principal_counts_toward_ranking_and_not_stats(eco):
    eco.balance(1)
    before = eco.rank_of(1)[1]
    eco.bond_buy(1, "normal", 2000, NOW)
    assert eco.rank_of(1)[1] == before
    s = eco.stats(1)
    assert s["games_played"] == 0 and s["total_wagered"] == 0
    eco.bond_settle(NOW + 3 * DAY)
    assert eco.rank_of(1)[1] == before + 80  # 払い済みは残高に入っている


def test_bonds_survive_a_restart_and_list_shows_recent_results(tmp_path):
    path = str(tmp_path / "t.db")
    first = Economy(path)
    first.bond_buy(1, "junk", 1000, NOW)
    first.bond_buy(1, "normal", 1000, NOW)
    second = Economy(path)
    assert len(second.bond_list(1)["active"]) == 2
    second.bond_settle(NOW + DAY, Always(0.0))  # ジャンク債だけ満期
    data = second.bond_list(1)
    assert len(data["active"]) == 1 and data["done"][0]["status"] == "defaulted"
    assert "倒産" in describe_done(data["done"][0])


def test_cog_run_once_settles_every_guild(eco, monkeypatch):
    monkeypatch.setattr("cogs.bond_cog.approval.is_allowed", lambda *a: True)
    bot = SimpleNamespace(guilds=[SimpleNamespace(id=1)], config={}, economy=SimpleNamespace(for_guild=lambda gid: eco))
    eco.bond_buy(1, "normal", 1000, 1.0)  # とっくに満期
    BondCog(bot).run_once()
    assert eco.bond_list(1)["active"] == [] and eco.balance(1) == INITIAL_BALANCE + 40


def test_buy_command_settles_first_and_replies(eco):
    bot = SimpleNamespace(guilds=[], config={}, economy=SimpleNamespace(for_guild=lambda gid: eco))
    cog = MarketCog(bot)
    sent = []

    class Resp:
        async def send_message(self, **kw):
            sent.append(kw["embed"].title)

    inter = SimpleNamespace(user=SimpleNamespace(id=1), guild=SimpleNamespace(id=1), guild_id=1, response=Resp())
    asyncio.run(cog._bond_buy(inter, "junk", 2000))
    asyncio.run(cog._bond_buy(inter, "junk", 9000))  # 上限超え
    assert sent == ["✅ 🗑️ ジャンク債を買いました", "❌ 買えません"]


def test_ranking_total_and_display_include_the_bond_principal(eco):
    from core import ranking

    eco.balance(1)
    eco.balance(2)
    eco.bond_buy(1, "normal", 2000, NOW)
    eco.bond_buy(1, "junk", 1000, NOW)
    assert eco.bond_values() == {1: 3000}
    assert [(r, u, t) for r, u, t in eco.top(5)] == [
        (1, 1, INITIAL_BALANCE),
        (2, 2, INITIAL_BALANCE),
    ]  # 債券を買っても総資産は変わらない
    e = ranking.build_embed(eco, None, "ランキング")
    assert (
        f"**{INITIAL_BALANCE:,}ペリカ**（うち債券 3,000）" in e.description
    )  # 現金は 0 でも、預けた分が総資産に入り、内訳が出る
    eco.bond_settle(NOW + 3 * DAY, Always(0.99))  # 満期で残高に戻れば、内訳は消える
    assert "債券" not in ranking.build_embed(eco, None, "ランキング").description
    assert eco.bond_values() == {}
