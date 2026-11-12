"""ファイナル総資産: 馬・持ち物・発掘品・競りの預かり・貸し借りまで数える。"""

import time

import pytest

from core import horse as horse_mod
from core import ranking, shop
from core.economy import Economy

pytestmark = pytest.mark.real_scope

G = 10
A, B = 1, 2


@pytest.fixture
def eco(tmp_path):
    e = Economy(str(tmp_path / "t.db"), strict=True).for_guild(G)
    for uid in (A, B):
        e.balance(uid)
        e._conn.execute("UPDATE users SET balance=1000 WHERE user_id=?", (uid,))
    e._conn.commit()
    return e


def give(eco, uid, item, qty=1):
    eco._conn.execute(
        "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty = qty + excluded.qty",
        (eco._g(), uid, item, qty),
    )
    eco._conn.commit()


def parts(eco, uid):
    return next(p for u, _t, p in eco.final_assets() if u == uid)


def test_items_count_at_price_and_relics_at_their_value(eco):
    card = next(i for i in shop.CATALOG if i.kind == shop.CARD_COLOR and not i.limited and i.price > 0)
    relic, value = next(iter(shop.RELIC_VALUE.items()))
    give(eco, A, card.id)
    give(eco, A, relic, 2)
    p = parts(eco, A)
    assert p["items"] == card.price and p["relics"] == value * 2 and p["cash"] == 1000
    assert parts(eco, B)["items"] == 0


def test_horse_is_worth_price_plus_retire_value_and_stays_while_listed(eco):
    eco._conn.execute(
        "INSERT INTO horses (guild_id, owner_id, name, coat, silks, speed, stamina, power, guts, races, wins, places, created) "
        "VALUES (?, ?, 'h', 'c', 's', 50, 50, 50, 50, 10, 2, 4, 0)",
        (G, A),
    )
    eco._conn.commit()
    want = max(0, eco.HORSE_PRICE + horse_mod.retire_value(10, 2, 4))
    assert parts(eco, A)["horses"] == want
    eco._conn.execute("UPDATE horses SET owner_id=-3")  # 出品中の預かり
    eco._conn.execute(
        "INSERT INTO market_listings (guild_id, seller_id, kind, ref, qty, mode, price, bid, bidder_id, created, end_at) "
        "VALUES (?, ?, 'horse', '1', 1, 'auction', 100, 300, ?, 0, 9)",
        (G, A, B),
    )
    eco._conn.commit()
    assert parts(eco, A)["horses"] == want  # 出品中も持ち主のもの
    assert parts(eco, B)["escrow"] == 300  # 入札中のお金は入札した人のもの


def test_loans_net_out_between_lender_and_borrower(eco):
    eco._conn.execute(
        "INSERT INTO loans (guild_id, lender_id, borrower_id, principal, rate_tenths, term_days, status, created, "
        "offer_expires, accepted_at, due, paid) VALUES (?, ?, ?, 1000, 0, 7, 'active', 0, 0, ?, ?, 0)",
        (G, A, B, time.time(), time.time() + 86400),
    )
    eco._conn.commit()
    assert parts(eco, A)["loans"] == 1000 and parts(eco, B)["loans"] == -1000


def test_final_ranking_and_breakdown_embeds(eco):
    relic = next(iter(shop.RELIC_VALUE))
    give(eco, B, relic, 3)
    assert eco.final_top(2)[0][1] == B
    assert eco.final_rank_of(A) == (2, 1000, 2)
    text = ranking.build_final_embed(eco, None, "t", A).description
    assert text.index(f"<@{B}>") < text.index(f"<@{A}>")
    d = ranking.build_breakdown_embed(eco, B, "b").description
    assert "発掘品" in d and "ファイナル総資産" in d


def test_ranking_view_can_switch_back_and_forth(eco):
    import asyncio
    from types import SimpleNamespace

    from cogs.economy_cog import RankingView

    seen = []

    class Resp:
        async def defer(self):
            pass

    async def edit(**kw):
        seen.append(kw["embed"].title)

    it = SimpleNamespace(user=SimpleNamespace(id=A), response=Resp(), edit_original_response=edit)
    view = RankingView(eco, A)

    async def go():
        await view.final.callback(it)
        await view.normal.callback(it)

    asyncio.run(go())
    import discord

    assert view.normal.style == discord.ButtonStyle.primary and view.final.style == discord.ButtonStyle.secondary
    assert "ファイナル" in seen[0] and "ファイナル" not in seen[1]
