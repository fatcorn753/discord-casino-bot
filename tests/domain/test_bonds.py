"""債券: 購入、満期の入金、ポアソン倒産、DCF売却、上限、ランキング、再起動。"""

import asyncio
import math
import random
from types import SimpleNamespace

import pytest

from cogs.market.bond_cog import BondCog, describe_done
from cogs.market.cog import MarketCog
from domain import bonds
from infra.db.economy import INITIAL_BALANCE, Economy

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
    e = Economy(str(tmp_path / "t.db"))
    e._bank_sync(NOW)  # 行を作る
    e._bank_set(1_000_000, 0)  # 利息の原資。手元現金を十分に積んでおく（原資が足りない場合は別のテストで確かめる）
    return e


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


def test_only_normal_bonds_can_be_sold_at_the_dcf_price(eco):
    eco.bond_buy(1, "normal", 2000, NOW)
    eco.bond_buy(1, "junk", 1000, NOW)
    by_kind = {r["kind"]: r["id"] for r in eco.bond_list(1)["active"]}
    ids = [by_kind["normal"], by_kind["junk"]]
    ok_sell, why_sell = eco.bond_cancel(1, ids[1], NOW)
    assert not ok_sell and why_sell.code == "bond_not_sellable" and why_sell.params["name"] == "ジャンク債"
    ok, got = eco.bond_cancel(1, ids[0], NOW)  # 買った直後: 満期の2,080を、72時間ぶん割り引く
    assert ok and got == int(2080 * math.exp(-bonds.SALE_DISCOUNT_PER_HOUR * 72)) and 1900 < got < 2000
    assert eco.balance(1) == INITIAL_BALANCE - 3000 + got
    assert not eco.bond_cancel(1, ids[0], NOW)[0] and not eco.bond_cancel(2, ids[1], NOW)[0]  # 二重・他人の債券


def test_dcf_price_rises_toward_the_maturity_payout_and_never_exceeds_it():
    k, due = bonds.NORMAL, NOW + 72 * 3600
    prices = [bonds.sale_value(k, 1000, NOW + h * 3600, due) for h in range(0, 73, 6)]
    assert prices == sorted(prices) and prices[-1] == 1040  # 満期の瞬間は、満期の受取額そのもの
    assert all(p <= 1040 for p in prices) and prices[0] < 1000 < prices[-1]  # 直後は元本割れ、待てば元本より増える


def test_selling_late_beats_selling_early(eco):
    eco.bond_buy(1, "normal", 2_000, NOW)
    bond_id = eco.bond_list(1)["active"][0]["id"]
    early = bonds.sale_value(bonds.NORMAL, 2_000, NOW + 3600, NOW + 72 * 3600)
    ok, late = eco.bond_cancel(1, bond_id, NOW + 60 * 3600)
    assert ok and late > early


# ----------------------------- ジャンク債の倒産（ポアソン過程）----------------------------- #
def test_hazard_gives_twenty_percent_over_the_term_and_is_additive():
    k = bonds.JUNK
    assert bonds.default_probability(k, k.hours) == pytest.approx(0.20)
    one, two = bonds.default_probability(k, 1), bonds.default_probability(k, 2)
    assert 1 - (1 - one) ** 2 == pytest.approx(two)  # 分けて引いても、まとめて引いても、生き残る確率は同じ
    assert bonds.hazard_per_hour(bonds.NORMAL) == 0 and bonds.default_probability(bonds.NORMAL, 72) == 0


def test_junk_can_default_before_maturity_and_is_not_paid_later(eco):
    eco.bond_buy(1, "junk", 1000, NOW)
    done = eco.bond_settle(NOW + 3600, Always(0.0))  # 1時間後（満期の23時間前）に倒産
    assert [(d["status"], d["paid"]) for d in done] == [("defaulted", 0)]
    assert eco.bond_list(1)["active"] == [] and eco.balance(1) == INITIAL_BALANCE - 1000
    assert eco.bond_settle(NOW + DAY, Always(0.0)) == []  # 満期が来ても、もう何も起こらない


def test_survivors_are_paid_at_maturity_with_no_extra_lottery(eco):
    eco.bond_buy(1, "junk", 1000, NOW)
    assert eco.bond_settle(NOW + 3600, Always(0.99)) == []  # 生き残った
    row = eco.bond_list(1)["active"][0]
    assert row["checked"] == NOW + 3600  # 抽選済みの時刻が進む
    done = eco.bond_settle(NOW + DAY + 500, Always(0.99))  # 満期のあとに呼ばれても、満期までの分しか引かない
    assert [(d["status"], d["paid"]) for d in done] == [("paid", 1200)]


def test_per_minute_rolls_and_one_big_roll_default_equally_often():
    """1分ごとに引いても、止まっていたあとに1回で引いても、満期までに倒産する割合は約20%（同じ）。"""
    rng_a, rng_b, n = random.Random(1), random.Random(2), 3000
    minute = 1 - bonds.default_probability(bonds.JUNK, 1 / 60)
    survived_a = sum(all(rng_a.random() >= 1 - minute for _ in range(24 * 60)) for _ in range(n // 6))
    one_roll = bonds.default_probability(bonds.JUNK, 24)
    survived_b = sum(rng_b.random() >= one_roll for _ in range(n))
    assert abs(survived_a / (n // 6) - 0.80) < 0.04 and abs(survived_b / n - 0.80) < 0.03


def test_buy_guards(eco):
    assert not eco.bond_buy(1, "normal", 999, NOW)[0]  # 最低額
    assert not eco.bond_buy(1, "normal", INITIAL_BALANCE + 1, NOW)[0]  # 残高不足
    eco.stats(2)
    eco._conn.execute("UPDATE users SET debt=500 WHERE user_id=2")
    assert not eco.bond_buy(2, "normal", 1000, NOW)[0]  # 借金中
    eco.credit(3, 20_000, "テスト")
    assert eco.bond_buy(3, "junk", 6000, NOW)[0] is True
    ok, reason = eco.bond_buy(3, "junk", 6000, NOW)  # ジャンク債は合計10,000まで
    assert not ok and reason.code == "bond_max_total" and reason.params["room"] == 4000
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
    monkeypatch.setattr("cogs.market.bond_cog.approval.is_allowed", lambda *a: True)
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
    from cogs.shared import ranking

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


def test_interest_is_capped_by_the_house_cash_but_principal_is_always_paid_in_full(eco):
    """利息は帝愛の手元現金から払う（新しく作らない）。足りなければ利息だけ減り、元本は必ず全額返る。"""
    eco._bank_set(30, 0)  # 利息の原資がほとんどない
    eco.bond_buy(1, "normal", 2000, NOW)  # +4%で、本来の利息は80
    done = eco.bond_settle(NOW + 3 * DAY)
    assert [(d["status"], d["paid"]) for d in done] == [("paid", 2000 + 30)]  # 利息は、残っていた30だけ
    assert eco.balance(1) == INITIAL_BALANCE - 2000 + 2030


def test_no_interest_at_all_when_the_house_has_no_cash(eco):
    eco._bank_set(0, 0)
    eco.bond_buy(1, "normal", 2000, NOW)
    done = eco.bond_settle(NOW + 3 * DAY)
    assert done[0]["paid"] == 2000  # 元本だけ。預金者からは一切引かない
