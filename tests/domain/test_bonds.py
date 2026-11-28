"""債券: 購入、満期の入金、ポアソン倒産、DCF売却、上限、ランキング、再起動。"""

import asyncio
import math
import random
from types import SimpleNamespace

import pytest

from cogs.market.bond_cog import describe_done
from domain import bonds
from domain.types import INITIAL_BALANCE
from infra.db.repositories import open_repositories
from tests.fakes import as_interaction, must
from tests.support import make_bond_cog, make_market_cog, make_ranking_repos, make_test_bot, set_test_debt

NOW = 1_000_000.0
DAY = 86400


class Always(random.Random):
    """乱数を固定する: random() が常に value を返す。"""

    def __init__(self, value):
        super().__init__(0)
        self.value = value

    def random(self):
        return self.value


@pytest.fixture
def eco(tmp_path):
    e = open_repositories(str(tmp_path / "t.db"))
    e.bank.bank_sync_for(0, NOW)  # 行を作る
    e.bank.bank_set_for(
        0, 1_000_000, 0
    )  # 利息の原資。手元現金を十分に積んでおく（原資が足りない場合は別のテストで確かめる）
    return e


def test_normal_bond_pays_principal_plus_interest_at_maturity(eco):
    assert eco.bonds.bond_buy(1, "normal", 2000, NOW) == (True, "")
    assert eco.wallet.balance(1) == INITIAL_BALANCE - 2000
    assert eco.bonds.bond_settle(NOW + 3 * DAY - 1) == []  # まだ満期ではない
    done = eco.bonds.bond_settle(NOW + 3 * DAY)
    assert [(d["status"], d["paid"]) for d in done] == [("paid", 2080)]  # +4%
    assert eco.wallet.balance(1) == INITIAL_BALANCE + 80
    assert eco.bonds.bond_settle(NOW + 9 * DAY) == []  # 二重に払わない


def test_junk_bond_pays_high_interest_or_defaults(eco):
    eco.bonds.bond_buy(1, "junk", 1000, NOW)
    eco.bonds.bond_buy(2, "junk", 1000, NOW)
    # 倒産率20%: random()*100 < 20 なら倒産
    assert eco.bonds.bond_settle(NOW + DAY, Always(0.19))[0]["status"] == "defaulted"
    assert eco.wallet.balance(1) == INITIAL_BALANCE - 1000 and eco.wallet.balance(2) == INITIAL_BALANCE - 1000
    eco.bonds.bond_buy(3, "junk", 1000, NOW)
    done = eco.bonds.bond_settle(NOW + DAY, Always(0.20))
    assert [(d["user_id"], d["status"], d["paid"]) for d in done if d["user_id"] == 3] == [(3, "paid", 1200)]


def test_normal_bond_never_defaults(eco):
    eco.bonds.bond_buy(1, "normal", 1000, NOW)
    assert eco.bonds.bond_settle(NOW + 3 * DAY, Always(0.0))[0]["status"] == "paid"


def test_default_rate_is_roughly_twenty_percent():
    rng = random.Random(3)
    eco_n = 4000
    hits = sum(rng.random() * 100 < bonds.JUNK.default_pct for _ in range(eco_n))
    assert 0.18 < hits / eco_n < 0.22
    assert 0.95 < (1 - bonds.JUNK.default_pct / 100) * (1 + bonds.JUNK.rate / 100) < 0.97  # 期待値は約96%


def test_only_normal_bonds_can_be_sold_at_the_dcf_price(eco):
    eco.bonds.bond_buy(1, "normal", 2000, NOW)
    eco.bonds.bond_buy(1, "junk", 1000, NOW)
    by_kind = {r["kind"]: r["id"] for r in eco.bonds.bond_list(1)["active"]}
    ids = [by_kind["normal"], by_kind["junk"]]
    ok_sell, why_sell = eco.bonds.bond_cancel(1, ids[1], NOW)
    assert not ok_sell and why_sell.code == "bond_not_sellable" and why_sell.params["name"] == "ジャンク債"
    ok, got = eco.bonds.bond_cancel(1, ids[0], NOW)  # 買った直後: 満期の2,080を、72時間ぶん割り引く
    assert ok and got == int(2080 * math.exp(-bonds.SALE_DISCOUNT_PER_HOUR * 72)) and 1900 < got < 2000
    assert eco.wallet.balance(1) == INITIAL_BALANCE - 3000 + got
    assert (
        not eco.bonds.bond_cancel(1, ids[0], NOW)[0] and not eco.bonds.bond_cancel(2, ids[1], NOW)[0]
    )  # 二重・他人の債券


def test_dcf_price_rises_toward_the_maturity_payout_and_never_exceeds_it():
    k, due = bonds.NORMAL, NOW + 72 * 3600
    prices = [bonds.sale_value(k, 1000, NOW + h * 3600, due) for h in range(0, 73, 6)]
    assert prices == sorted(prices) and prices[-1] == 1040  # 満期の瞬間は、満期の受取額そのもの
    assert all(p <= 1040 for p in prices) and prices[0] < 1000 < prices[-1]  # 直後は元本割れ、待てば元本より増える


def test_selling_late_beats_selling_early(eco):
    eco.bonds.bond_buy(1, "normal", 2_000, NOW)
    bond_id = eco.bonds.bond_list(1)["active"][0]["id"]
    early = bonds.sale_value(bonds.NORMAL, 2_000, NOW + 3600, NOW + 72 * 3600)
    ok, late = eco.bonds.bond_cancel(1, bond_id, NOW + 60 * 3600)
    assert ok and late > early


# ----------------------------- ジャンク債の倒産（ポアソン過程）----------------------------- #
def test_hazard_gives_twenty_percent_over_the_term_and_is_additive():
    k = bonds.JUNK
    assert bonds.default_probability(k, k.hours) == pytest.approx(0.20)
    one, two = bonds.default_probability(k, 1), bonds.default_probability(k, 2)
    assert 1 - (1 - one) ** 2 == pytest.approx(two)  # 分けて引いても、まとめて引いても、生き残る確率は同じ
    assert bonds.hazard_per_hour(bonds.NORMAL) == 0 and bonds.default_probability(bonds.NORMAL, 72) == 0


def test_junk_can_default_before_maturity_and_is_not_paid_later(eco):
    eco.bonds.bond_buy(1, "junk", 1000, NOW)
    done = eco.bonds.bond_settle(NOW + 3600, Always(0.0))  # 1時間後（満期の23時間前）に倒産
    assert [(d["status"], d["paid"]) for d in done] == [("defaulted", 0)]
    assert eco.bonds.bond_list(1)["active"] == [] and eco.wallet.balance(1) == INITIAL_BALANCE - 1000
    assert eco.bonds.bond_settle(NOW + DAY, Always(0.0)) == []  # 満期が来ても、もう何も起こらない


def test_survivors_are_paid_at_maturity_with_no_extra_lottery(eco):
    eco.bonds.bond_buy(1, "junk", 1000, NOW)
    assert eco.bonds.bond_settle(NOW + 3600, Always(0.99)) == []  # 生き残った
    row = eco.bonds.bond_list(1)["active"][0]
    assert row["checked"] == NOW + 3600  # 抽選済みの時刻が進む
    done = eco.bonds.bond_settle(NOW + DAY + 500, Always(0.99))  # 満期のあとに呼ばれても、満期までの分しか引かない
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
    assert not eco.bonds.bond_buy(1, "normal", 999, NOW)[0]  # 最低額
    assert not eco.bonds.bond_buy(1, "normal", INITIAL_BALANCE + 1, NOW)[0]  # 残高不足
    eco.wallet.stats(2)
    with eco.db.tx():
        set_test_debt(eco, 2, 500)
    assert not eco.bonds.bond_buy(2, "normal", 1000, NOW)[0]  # 借金中
    eco.wallet.credit(3, 20_000, "テスト")
    assert eco.bonds.bond_buy(3, "junk", 6000, NOW)[0] is True
    ok, reason = eco.bonds.bond_buy(3, "junk", 6000, NOW)  # ジャンク債は合計10,000まで
    assert not ok and reason.code == "bond_max_total" and reason.params["room"] == 4000
    assert eco.bonds.bond_buy(3, "normal", 1000, NOW)[0]  # 種類ごとに別の上限


def test_principal_counts_toward_ranking_and_not_stats(eco):
    eco.wallet.balance(1)
    before = eco.wallet.rank_of(1)[1]
    eco.bonds.bond_buy(1, "normal", 2000, NOW)
    assert eco.wallet.rank_of(1)[1] == before
    s = eco.wallet.stats(1)
    assert s["games_played"] == 0 and s["total_wagered"] == 0
    eco.bonds.bond_settle(NOW + 3 * DAY)
    assert eco.wallet.rank_of(1)[1] == before + 80  # 払い済みは残高に入っている


def test_bonds_survive_a_restart_and_list_shows_recent_results(tmp_path):
    path = str(tmp_path / "t.db")
    first = open_repositories(path)
    first.bonds.bond_buy(1, "junk", 1000, NOW)
    first.bonds.bond_buy(1, "normal", 1000, NOW)
    second = open_repositories(path)
    assert len(second.bonds.bond_list(1)["active"]) == 2
    second.bonds.bond_settle(NOW + DAY, Always(0.0))  # ジャンク債だけ満期
    data = second.bonds.bond_list(1)
    assert len(data["active"]) == 1 and data["done"][0]["status"] == "defaulted"
    assert "倒産" in describe_done(data["done"][0])


def test_cog_run_once_settles_every_guild(eco, monkeypatch):
    monkeypatch.setattr("domain.approval.is_allowed", lambda *a: True)
    bot = make_test_bot(repos=eco)
    bot.guilds = [SimpleNamespace(id=1)]
    eco.bonds.bond_buy(1, "normal", 1000, 1.0)  # とっくに満期
    make_bond_cog(bot).run_once()
    assert eco.bonds.bond_list(1)["active"] == [] and eco.wallet.balance(1) == INITIAL_BALANCE + 40


def test_buy_command_settles_first_and_replies(eco):
    from cogs.market import orders

    bot = make_test_bot(repos=eco)
    cog = make_market_cog(bot)
    sent = []

    class Resp:
        async def send_message(self, **kw):
            sent.append(kw["embed"].title)

    inter = SimpleNamespace(user=SimpleNamespace(id=1), guild=SimpleNamespace(id=1), guild_id=1, response=Resp())
    asyncio.run(orders.bond_buy(cog, as_interaction(inter), "junk", 2000))
    asyncio.run(orders.bond_buy(cog, as_interaction(inter), "junk", 9000))  # 上限超え
    assert sent == ["✅ 🗑️ ジャンク債を買いました", "❌ 買えません"]


def test_ranking_total_and_display_include_the_bond_principal(eco):
    from cogs.shared import ranking

    eco.wallet.balance(1)
    eco.wallet.balance(2)
    eco.bonds.bond_buy(1, "normal", 2000, NOW)
    eco.bonds.bond_buy(1, "junk", 1000, NOW)
    assert eco.bonds.bond_values() == {1: 3000}
    assert [(r, u, t) for r, u, t in eco.wallet.top(5)] == [
        (1, 1, INITIAL_BALANCE),
        (2, 2, INITIAL_BALANCE),
    ]  # 債券を買っても総資産は変わらない
    e = ranking.build_embed(make_ranking_repos(eco), None, "ランキング")
    assert f"**{INITIAL_BALANCE:,}ペリカ**（流動 {INITIAL_BALANCE:,}）" in must(
        e.description
    )  # 現金は減っても、預けた債券の元本が流動資産に入り、純資産は変わらない
    eco.bonds.bond_settle(NOW + 3 * DAY, Always(0.99))  # 満期で残高に戻っても、純資産は同じ
    assert f"**{INITIAL_BALANCE:,}ペリカ**" in must(
        ranking.build_embed(make_ranking_repos(eco), None, "ランキング").description
    )
    assert eco.bonds.bond_values() == {}


def test_interest_is_paid_in_full_even_when_the_house_cash_runs_short(eco):
    """社債はシニア: 利息も手元の現金で打ち切らず、足りない分は信託・共済が負担する（#35）。"""
    eco.bank.bank_set_for(0, 30, 0)  # 利息の原資がほとんどない
    eco.bonds.bond_buy(1, "normal", 2000, NOW)
    done = eco.bonds.bond_settle(NOW + 3 * DAY)
    paid = done[0]["paid"]
    assert done[0]["status"] == "paid" and paid > 2000 + 30  # 手元の 30 で打ち切らない
    assert eco.wallet.balance(1) == INITIAL_BALANCE - 2000 + paid
    s = eco.bank.bank_status(NOW + 3 * DAY)
    # 共済の口の持ち主がいなければ、熱浴の肩代わりを除いた分が未払いとして残る
    assert s["cash"] == 0 and s["excess_unpaid"] + s["thermostat"]["bailed"] == pytest.approx(paid - 2000 - 30, abs=1)


def test_interest_is_still_paid_when_the_house_has_no_cash(eco):
    eco.bank.bank_set_for(0, 0, 0)
    eco.bonds.bond_buy(1, "normal", 2000, NOW)
    done = eco.bonds.bond_settle(NOW + 3 * DAY)
    assert done[0]["paid"] > 2000  # 元本も利息も満額（満期まで持つほうが、途中で売るより得）


# ------------------------------------------------------------------ #
# 帝愛社債（#35）: 発行体は帝愛銀行。元本は銀行の現金に入り、銀行から返す
# ------------------------------------------------------------------ #
def _cash(eco, now=NOW):
    return eco.bank.bank_sync_for(0, now)["cash"]


def test_bond_principal_goes_into_the_bank_and_counts_as_a_liability(eco):
    eco.bonds.bond_buy(1, "normal", 2000, NOW)
    eco.bonds.bond_buy(2, "junk", 1000, NOW)
    s = eco.bank.bank_status(NOW)
    assert s["cash"] == pytest.approx(1_003_000) and s["bonds"] == 3000
    assert s["ratio"] == pytest.approx(1_003_000 / 3000)  # 信託がなくても、社債の元本に対する準備率が出る


def test_maturity_returns_the_principal_from_the_bank_and_only_the_interest_is_the_banks_cost(eco):
    eco.bonds.bond_buy(1, "normal", 2000, NOW)
    eco.bonds.bond_settle(NOW + 3 * DAY)
    assert _cash(eco, NOW + 3 * DAY) == pytest.approx(1_000_000 - 80)
    assert eco.bank.bank_status(NOW + 3 * DAY)["bonds"] == 0


def test_a_sale_pays_the_refund_from_the_bank_and_the_discount_stays_with_the_bank(eco):
    eco.bonds.bond_buy(1, "normal", 2000, NOW)
    bond_id = eco.bonds.bond_list(1)["active"][0]["id"]
    ok, got = eco.bonds.bond_cancel(1, bond_id, NOW)
    assert ok and got < 2000
    assert _cash(eco) == pytest.approx(1_000_000 + 2000 - got)  # 銀行の儲けは、元本と手取りの差だけ
    assert eco.wallet.balance(1) == INITIAL_BALANCE - 2000 + got


def test_a_late_sale_above_the_principal_is_paid_by_the_bank(eco):
    eco.bonds.bond_buy(1, "normal", 2000, NOW)
    bond_id = eco.bonds.bond_list(1)["active"][0]["id"]
    late = NOW + 3 * DAY - 3600
    ok, got = eco.bonds.bond_cancel(1, bond_id, late)
    assert ok and got > 2000
    assert _cash(eco, late) == pytest.approx(1_000_000 - (got - 2000))


def test_a_default_leaves_the_principal_with_the_bank(eco):
    eco.bonds.bond_buy(1, "junk", 1000, NOW)
    eco.bonds.bond_settle(NOW + 3600, Always(0.0))
    s = eco.bank.bank_status(NOW + 3600)
    assert _cash(eco, NOW + 3600) == pytest.approx(1_001_000 - s["thermostat"]["burned"])
    assert s["bonds"] == 0


def test_the_bankruptcy_write_off_pays_the_seize_rate_from_the_bank(eco):
    eco.bonds.bond_buy(1, "junk", 1000, NOW)
    bond_id = eco.bonds.bond_list(1)["active"][0]["id"]
    with eco.db.tx():
        eco.bonds.bond_write_off_for(0, 1, bond_id, 500, NOW)
    s = eco.bank.bank_status(NOW)
    assert _cash(eco) == pytest.approx(1_000_500 - s["thermostat"]["burned"])
    assert eco.wallet.balance(1) == INITIAL_BALANCE - 1000 + 500
    row = eco.db.conn.execute("SELECT status, paid FROM bonds WHERE id=?", (bond_id,)).fetchone()
    assert tuple(row) == ("cancelled", 500)


def test_bond_flows_net_into_the_thermostat_only_as_the_banks_gain(eco):
    """元本の出し入れは熱量に数えず、倒産で残った元本だけが流入になる。"""
    eco.bonds.bond_buy(1, "junk", 1000, NOW)
    assert eco.bank.bank_status(NOW)["thermostat"]["load"] == pytest.approx(0)
    eco.bonds.bond_settle(NOW + 3600, Always(0.0))
    assert eco.bank.bank_status(NOW + 3600)["thermostat"]["load"] > 0


def test_panel_shows_the_bond_principal_outstanding(eco):
    from cogs.teiai.panel import bank_lines

    assert "帝愛社債" not in bank_lines(eco.bank.bank_status(NOW))
    eco.bonds.bond_buy(1, "normal", 2000, NOW)
    assert "帝愛社債の元本残高" in bank_lines(eco.bank.bank_status(NOW))
