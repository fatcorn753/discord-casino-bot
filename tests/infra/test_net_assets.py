"""貸借対照表（B/S）の純資産: 馬・持ち物・発掘品・競りの預かり・貸し借りまで数える。"""

import time

import pytest

from cogs.shared import ranking
from domain import horse as horse_mod
from domain import shop
from infra.db.repositories import open_repositories
from tests.fakes import invoke, must
from tests.support import insert_test_loan, make_ranking_repos, set_test_balance, set_test_debt

pytestmark = pytest.mark.real_scope

G = 10
A, B = 1, 2


@pytest.fixture
def eco(tmp_path):
    e = open_repositories(str(tmp_path / "t.db"), strict=True).for_guild(G)
    for uid in (A, B):
        e.wallet.balance(uid)
        set_test_balance(e, uid, 1000)
    e.db.conn.commit()
    return e


def give(eco, uid, item, qty=1):
    eco.db.conn.execute(
        "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty = qty + excluded.qty",
        (eco.wallet._g(), uid, item, qty),
    )
    eco.db.conn.commit()


def parts(eco, uid):
    return next(p for u, _t, p in eco.assets.net_assets() if u == uid)


def test_items_count_at_price_and_relics_at_their_value(eco):
    card = next(i for i in shop.CATALOG if i.kind == shop.CARD_COLOR and not i.limited and i.price > 0)
    relic, value = next(iter(shop.RELIC_VALUE.items()))
    give(eco, A, card.id)
    give(eco, A, relic, 2)
    p = parts(eco, A)
    assert p["items"] == card.price and p["relics"] == value * 2 and p["cash"] == 1000
    assert parts(eco, B)["items"] == 0


def test_horse_is_worth_price_plus_retire_value_and_stays_while_listed(eco):
    cur = eco.db.conn.execute(
        "INSERT INTO horses (guild_id, status, name, coat, silks, speed, stamina, power, guts, races, wins, places, created) "
        "VALUES (?, 'owned', 'h', 'c', 's', 50, 50, 50, 50, 10, 2, 4, 0)",
        (G,),
    )
    hid = cur.lastrowid
    eco.db.conn.execute("INSERT INTO horse_ownership (horse_id, guild_id, owner_id) VALUES (?, ?, ?)", (hid, G, A))
    eco.db.conn.commit()
    want = max(0, eco.horses.HORSE_PRICE + horse_mod.retire_value(10, 2, 4))
    assert parts(eco, A)["horses"] == want
    eco.db.conn.execute("UPDATE horses SET status='escrow' WHERE id=?", (hid,))  # 出品中の預かり
    eco.db.conn.execute("DELETE FROM horse_ownership WHERE horse_id=?", (hid,))
    cur = eco.db.conn.execute(
        "INSERT INTO market_listings (guild_id, seller_id, kind, ref, qty, mode, price, created, end_at) "
        "VALUES (?, ?, 'horse', ?, 1, 'auction', 100, 0, 9)",
        (G, A, str(hid)),
    )
    eco.db.conn.execute(
        "INSERT INTO market_auction_bids (listing_id, bidder_id, amount) VALUES (?, ?, 300)", (cur.lastrowid, B)
    )
    eco.db.conn.commit()
    assert parts(eco, A)["horses"] == want  # 出品中も持ち主のもの
    assert parts(eco, B)["escrow"] == 300  # 入札中のお金は入札した人のもの


def test_loans_are_split_into_lent_asset_and_owed_liability(eco):
    insert_test_loan(eco, A, B, 1000, guild_id=G, term_days=7, accepted_at=time.time(), due=time.time() + 86400)
    eco.db.conn.commit()
    assert parts(eco, A)["loans_lent"] == 1000 and parts(eco, B)["loans_owed"] == -1000


def test_net_ranking_and_breakdown_embeds(eco):
    relic = next(iter(shop.RELIC_VALUE))
    give(eco, B, relic, 3)
    assert eco.assets.net_top(2)[0][1] == B
    assert eco.assets.net_rank_of(A) == (2, 1000, 2)
    repos = make_ranking_repos(eco)
    text = must(ranking.build_embed(repos, None, "t", A).description)
    assert text.index(f"<@{B}>") < text.index(f"<@{A}>")
    d = ranking.build_breakdown_embed(repos, B, "b").description
    assert "発掘品" in must(d) and "総資産" in must(d)


def test_ranking_view_can_switch_back_and_forth(eco):
    import asyncio
    from types import SimpleNamespace

    from cogs.economy.cog import RankingView

    seen = []

    class Resp:
        async def defer(self):
            pass

    async def edit(**kw):
        seen.append(kw["embed"].title)

    it = SimpleNamespace(user=SimpleNamespace(id=A), response=Resp(), edit_original_response=edit)
    view = RankingView(make_ranking_repos(eco), A, G)

    async def go():
        await invoke(view.liquid, it)
        await invoke(view.net, it)

    asyncio.run(go())
    import discord

    assert view.net.style == discord.ButtonStyle.primary and view.liquid.style == discord.ButtonStyle.secondary
    assert "流動資産" in seen[0] and "純資産" in seen[1]


def test_labor_tool_upgrades_count_toward_assets(eco):
    from domain import labor as lab

    eco.db.conn.execute(
        "INSERT INTO labor_upgrades (guild_id, user_id, pick, glove, dorm) VALUES (?, ?, 2, 1, 1) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET pick=excluded.pick, glove=excluded.glove, dorm=excluded.dorm",
        (G, A),
    )
    eco.db.conn.commit()
    want = lab.upgrade_spent(2) + lab.upgrade_spent(1) + lab.dorm_spent(1)
    assert want > 0
    assert parts(eco, A)["tools"] == want
    assert parts(eco, B)["tools"] == 0


def test_debt_is_a_separate_liability_from_cash(eco):
    set_test_debt(eco, A, 300, guild_id=G)
    eco.db.conn.commit()
    p = parts(eco, A)
    assert p["cash"] == 1000 and p["debt"] == -300  # 現金は借金を引かず、借金は別の負債項目になる


def test_asset_groups_match_gross_minus_liabilities_equals_net(eco):
    from domain.types import asset_groups

    set_test_balance(eco, A, 1000, guild_id=G)
    set_test_debt(eco, A, 400, guild_id=G)
    insert_test_loan(eco, B, A, 500, guild_id=G, term_days=7, accepted_at=time.time(), due=time.time() + 86400)
    eco.db.conn.commit()
    p = parts(eco, A)
    g = asset_groups(p)
    assert g["gross"] - g["liabilities"] == g["net"]
    assert g["liabilities"] == 400 + 500  # 帝愛への借金 + 借りている貸付
    assert g["net"] == sum(p.values())


def test_gross_and_net_rankings_differ_by_liabilities(eco):
    # A は借金 900 で総資産 1,900・純資産 1,000、B は借金なしで 1,500。総資産では A、純資産では B が上
    set_test_balance(eco, A, 1900, guild_id=G)
    set_test_debt(eco, A, 900, guild_id=G)
    set_test_balance(eco, B, 1500, guild_id=G)
    eco.db.conn.commit()
    repos = make_ranking_repos(eco)
    assert [(u, t) for u, t, _g in repos.ranked_assets("gross")] == [(A, 1900), (B, 1500)]
    assert [(u, t) for u, t, _g in repos.ranked_assets("net")] == [(B, 1500), (A, 1000)]
    net = must(ranking.build_embed(repos, None, "t").description)
    gross = must(ranking.build_embed(repos, None, "t", measure="gross").description)
    assert net.index(f"<@{B}>") < net.index(f"<@{A}>") and gross.index(f"<@{A}>") < gross.index(f"<@{B}>")
    assert "**1,000ペリカ**（流動 1,900・負債 900）" in net  # 区分の小計が、内訳を開かずに見える
    assert "**1,900ペリカ**（流動 1,900・負債 900）" in gross


def test_balance_card_shows_bs_groups_and_net_rank(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from cogs.economy.cog import EconomyCog
    from tests.support import make_economy_cog, make_test_bot

    repos = open_repositories(str(tmp_path / "t.db"), strict=True)
    a = repos.for_guild(G)
    for uid, bal, debt in ((A, 1900, 900), (B, 1500, 0)):
        a.wallet.balance(uid)
        set_test_balance(a, uid, bal, guild_id=G)
        set_test_debt(a, uid, debt, guild_id=G)
    relic = next(iter(shop.RELIC_VALUE))
    give(a, A, relic)
    a.db.conn.commit()
    cog = make_economy_cog(make_test_bot(repos=repos))
    sent = []

    async def send(*args, **kw):
        sent.append(kw)

    member = SimpleNamespace(id=A, display_name="a", display_avatar=SimpleNamespace(url="https://example.com/a.png"))
    it = SimpleNamespace(user=member, guild=SimpleNamespace(id=G), response=SimpleNamespace(send_message=send))
    asyncio.run(invoke(EconomyCog.balance, cog, it, None))
    fields = {f.name: f.value for f in sent[0]["embed"].fields}
    bs = fields["🧾 資産（貸借対照表）"]
    worth = shop.RELIC_VALUE[relic]
    assert f"固定資産 {worth:,}ペリカ" in bs and f"負債 {900:,}ペリカ" in bs
    assert f"純資産 {1000 + worth:,}ペリカ" in bs
    want_rank = 1 if 1000 + worth > 1500 else 2
    assert fields["このサーバーでの順位（純資産）"] == f"{want_rank}位 / 2人"


def test_net_ranking_counts_unbooked_debt_interest(eco):
    from domain import teiai

    two_days_ago = time.time() - 2 * 86400 - 60
    set_test_debt(eco, A, 1000, interest_at=two_days_ago, guild_id=G)
    eco.db.conn.commit()
    want = 1000 + int(1000 * teiai.DEBT_RATE_PER_DAY * 2)  # 2日分のトイチが、負債に入ってから並ぶ
    (_uid, _net, g) = next(t for t in make_ranking_repos(eco).ranked_assets("net") if t[0] == A)
    assert g["liabilities"] == want


def test_liquid_ranking_leaves_out_fixed_assets(eco):
    # B は発掘品（固定資産）で純資産が上、流動資産（現金）だけなら同額で、ユーザーID順に A が上
    relic = next(iter(shop.RELIC_VALUE))
    give(eco, B, relic)
    repos = make_ranking_repos(eco)
    assert [u for u, _t, _g in repos.ranked_assets("net")] == [B, A]
    assert [(u, t) for u, t, _g in repos.ranked_assets("liquid")] == [(A, 1000), (B, 1000)]
    text = must(ranking.build_embed(repos, None, "t", measure="liquid").description)
    assert text.index(f"<@{A}>") < text.index(f"<@{B}>") and "**1,000ペリカ**" in text
