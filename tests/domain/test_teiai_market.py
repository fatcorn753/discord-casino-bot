"""帝愛の出品(#72): 期間限定の競売・定価販売と、闇市品の中身の入れ替わり。"""

import asyncio
import random
from types import SimpleNamespace

import discord
import pytest

from cogs.shop import actions as shop_actions
from cogs.shop import cog as shop_cog
from domain import shop, teiai
from domain import teiai_market as tm
from infra.db.auction import listable
from infra.db.repositories import open_repositories
from tests.fakes import as_interaction, must
from tests.support import make_shop_cog, make_test_bot, set_test_debt

pytestmark = pytest.mark.real_market

DAY = 1_800_000_000.0 - 1_800_000_000.0 % 86400  # UTC の0時
POOL = tm.pool(listable)


@pytest.fixture
def eco(tmp_path):
    return open_repositories(str(tmp_path / "t.db"))


class Rng(random.Random):
    """random() は決めた値を順に返す。choice() は先頭、uniform() は下限、randint() は下限を返す。"""

    def __init__(self, *values):
        super().__init__()
        self.values = list(values)

    def random(self):
        return self.values.pop(0) if self.values else 0.5

    def choice(self, seq):
        return seq[0]

    def uniform(self, a, b):
        return a

    def randint(self, a, b):
        return a


def _insert(eco, item_id, *, mode="fixed", qty=3, start=100, end=100, black=False, at=DAY, hours=6.0):
    gid = eco.shop.guild_id  # テストでは for_guild がそのままの値を返すので、既定のサーバーで出す
    with eco.db.tx():
        eco.db.conn.execute("INSERT OR IGNORE INTO teiai_market_days (guild_id, day) VALUES (?, ?)", (gid, DAY))
        cur = eco.db.conn.execute(
            "INSERT INTO teiai_market (guild_id, day, item_id, mode, qty, start_price, end_price, black, "
            "listed_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (gid, DAY, item_id, mode, qty, start, end, int(black), at, at + hours * 3600),
        )
        return cur.lastrowid


# ----------------------------- 計画・値段（domain） ----------------------------- #
def test_plan_day_makes_the_configured_number_of_listings_inside_the_day():
    plans = tm.plan_day(DAY, random.Random(3), POOL)
    assert len(plans) == teiai.MARKET_SLOTS_PER_DAY == 3
    assert all(DAY <= p.listed_at < DAY + 86400 for p in plans)
    assert [p.listed_at for p in plans] == sorted(p.listed_at for p in plans)
    for p in plans:
        it = shop.BY_ID[p.item_id]
        assert listable(p.item_id)
        if p.mode == tm.AUCTION:
            assert p.qty == 1 and p.end_price == max(1, round(p.start_price * teiai.MARKET_AUCTION_END))
            lo, hi = teiai.MARKET_AUCTION_HOURS
        else:
            assert p.start_price == p.end_price and p.expires_at - p.listed_at == teiai.MARKET_FIXED_HOURS * 3600
            assert p.qty == 1 if it.unique else teiai.MARKET_FIXED_QTY[0] <= p.qty <= teiai.MARKET_FIXED_QTY[1]
            lo = hi = teiai.MARKET_FIXED_HOURS
        assert lo * 3600 <= p.expires_at - p.listed_at <= hi * 3600


def test_plan_day_mode_and_black_market_follow_their_probabilities():
    rng = random.Random(7)
    plans = [p for _ in range(800) for p in tm.plan_day(DAY, rng, POOL)]
    auctions = sum(p.mode == tm.AUCTION for p in plans) / len(plans)
    blacks = sum(p.black for p in plans) / len(plans)
    assert auctions == pytest.approx(teiai.MARKET_AUCTION_PROB, abs=0.05)
    assert blacks == pytest.approx(teiai.MARKET_BLACK_PROB, abs=0.04)


def test_auction_price_falls_linearly_and_fixed_price_stays():
    start, end, at, until = 1000, 600, DAY, DAY + 4 * 3600
    assert tm.price_at(start, end, at, until, at) == 1000
    assert tm.price_at(start, end, at, until, at + 2 * 3600) == 800
    assert tm.price_at(start, end, at, until, until + 999) == 600  # 期限のあとも、下限で止まる
    assert tm.price_at(500, 500, at, until, at + 3600) == 500


def test_reveal_is_mostly_the_shown_item_and_sometimes_cheaper_or_dearer_of_the_same_kind():
    shown = next(it for it in POOL if any(o.kind == it.kind and o.price < it.price for o in POOL))
    assert tm.reveal(shown.id, Rng(0.99), POOL) == (shown.id, tm.SAME)
    got, outcome = tm.reveal(shown.id, Rng(0.0), POOL)
    assert outcome == tm.WORSE and shop.BY_ID[got].price < shown.price and shop.BY_ID[got].kind == shown.kind
    dear = next(it for it in POOL if any(o.kind == it.kind and o.price > it.price for o in POOL))
    got, outcome = tm.reveal(dear.id, Rng(teiai.MARKET_BLACK_WORSE + 0.001), POOL)
    assert outcome == tm.BETTER and shop.BY_ID[got].price > dear.price


def test_reveal_falls_back_to_the_shown_item_when_nothing_cheaper_exists():
    cheapest = min(POOL, key=lambda it: it.price)
    if any(it.price < cheapest.price for it in POOL):
        pytest.skip("同額の品がある")
    assert tm.reveal(cheapest.id, Rng(0.0), POOL) == (cheapest.id, tm.SAME)


def test_rates_over_many_purchases_match_the_config():
    rng = random.Random(11)
    shown = next(it for it in POOL if any(o.price < it.price for o in POOL) and any(o.price > it.price for o in POOL))
    outs = [tm.reveal(shown.id, rng, POOL)[1] for _ in range(20000)]
    assert outs.count(tm.WORSE) / len(outs) == pytest.approx(teiai.MARKET_BLACK_WORSE, abs=0.01)
    assert outs.count(tm.BETTER) / len(outs) == pytest.approx(teiai.MARKET_BLACK_BETTER, abs=0.01)


# ----------------------------- 棚と購入（infra） ----------------------------- #
def test_market_list_plans_once_per_day_and_shows_only_open_listings(eco):
    a = eco.for_guild(10)
    rng = random.Random(5)
    noon = DAY + 12 * 3600
    a.auction.market_list(noon, rng)
    gid = eco.shop.guild_id
    count = eco.db.conn.execute("SELECT COUNT(*) FROM teiai_market WHERE guild_id=?", (gid,)).fetchone()[0]
    assert count == teiai.MARKET_SLOTS_PER_DAY
    a.auction.market_list(noon + 60, rng)  # 同じ日にもう一度見ても、計画は増えない
    assert eco.db.conn.execute("SELECT COUNT(*) FROM teiai_market WHERE guild_id=?", (gid,)).fetchone()[0] == count
    rows = eco.db.conn.execute("SELECT * FROM teiai_market WHERE guild_id=?", (gid,)).fetchall()
    shown = {x["id"] for x in a.auction.market_list(noon, rng)}
    assert shown == {r["id"] for r in rows if r["listed_at"] <= noon < r["expires_at"]}


def test_market_list_without_slots_shows_nothing(eco, monkeypatch):
    monkeypatch.setattr(teiai, "MARKET_SLOTS_PER_DAY", 0)
    assert eco.for_guild(10).auction.market_list(DAY + 3600, random.Random(1)) == []


def test_buying_a_fixed_listing_charges_reduces_stock_and_feeds_the_bank(eco, monkeypatch):
    a = eco.for_guild(10)
    drink = shop.LABOR_DRINK
    lid = _insert(eco, drink, qty=3, start=250, end=250)
    a.wallet.credit(1, 10_000, "テスト")
    bal = a.wallet.balance(1)
    paid_in = []
    monkeypatch.setattr(
        type(a.auction._bank), "bank_income_for", lambda self, gid, amount, share, now=None: paid_in.append(amount)
    )
    res = a.auction.market_buy(1, lid, 2, DAY + 60, Rng())
    assert paid_in == [500]  # 売上は帝愛銀行の収入になる（差し押さえ品の競売と同じ）
    assert res.ok and res.total == 500 and res.got == drink and res.outcome == tm.SAME
    assert a.wallet.balance(1) == bal - 500 and a.shop.inventory(1)[drink] == 2
    assert eco.db.conn.execute("SELECT qty FROM teiai_market WHERE id=?", (lid,)).fetchone()[0] == 1
    assert not a.auction.market_buy(1, lid, 2, DAY + 60, Rng()).ok  # 残り1つなので2つは買えない


def test_buying_is_for_katagi_only_and_refuses_after_expiry(eco):
    a = eco.for_guild(10)
    lid = _insert(eco, shop.LABOR_DRINK)
    a.wallet.credit(1, 10_000, "テスト")
    with eco.db.tx():
        set_test_debt(eco, 1, 5000, guild_id=eco.shop.guild_id)
    res = a.auction.market_buy(1, lid, 1, DAY + 60, Rng())
    assert not res.ok and res.reason.code == "auction_katagi_only"
    a.wallet.credit(2, 10_000, "テスト")
    res = a.auction.market_buy(2, lid, 1, DAY + 7 * 3600, Rng())  # 6時間の販売が終わったあと
    assert not res.ok and res.reason.code == "auction_item_unavailable"


def test_black_market_item_can_turn_into_another_item_and_is_not_refunded(eco):
    a = eco.for_guild(10)
    shown = next(it for it in POOL if not it.unique and any(o.kind == it.kind and o.price < it.price for o in POOL))
    lid = _insert(eco, shown.id, black=True, start=shown.price, end=shown.price)
    a.wallet.credit(1, 1_000_000, "テスト")
    res = a.auction.market_buy(1, lid, 1, DAY + 60, Rng(0.0))  # 安い品になる
    assert res.ok and res.outcome == tm.WORSE and res.shown == shown.id and res.got != shown.id
    inv = a.shop.inventory(1)
    assert inv.get(res.got) == 1 and shown.id not in inv


def test_black_market_unique_outcome_already_owned_becomes_the_shown_item(eco, monkeypatch):
    a = eco.for_guild(10)
    uniques = [it for it in POOL if it.unique]
    if len(uniques) < 2:
        pytest.skip("見た目の品が2つ以上ない")
    shown, other = uniques[0], uniques[1]
    monkeypatch.setattr(tm, "reveal", lambda item_id, rng, items: (other.id, tm.BETTER))
    with eco.db.tx():
        eco.db.conn.execute(
            "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, 1, ?, 1)", (eco.shop.guild_id, other.id)
        )
    lid = _insert(eco, shown.id, black=True, qty=1, start=10, end=10)
    a.wallet.credit(1, 10_000, "テスト")
    res = a.auction.market_buy(1, lid, 1, DAY + 60, Rng())
    assert res.ok and res.got == shown.id and res.outcome == tm.SAME


# ----------------------------- 画面（cogs） ----------------------------- #
class FakeInteraction:
    def __init__(self, uid=2):
        self.user = SimpleNamespace(id=uid, display_name="テスト")
        self.guild = SimpleNamespace(id=10)
        self.sent = []
        self.response = SimpleNamespace(send_message=self._send, defer=self._defer)
        self.followup = SimpleNamespace(send=self._send)

    async def _send(self, *a, **kw):
        self.sent.append(kw)

    async def _defer(self, **kw):
        return None


def test_market_listing_appears_on_the_teiai_shelf_and_can_be_bought(eco, monkeypatch):
    import time as time_mod

    now = time_mod.time()
    monkeypatch.setattr(teiai, "MARKET_SLOTS_PER_DAY", 0)
    with eco.db.tx():
        eco.db.conn.execute(
            "INSERT OR IGNORE INTO teiai_market_days (guild_id, day) VALUES (?, ?)",
            (eco.shop.guild_id, tm.day_start(now)),
        )
        cur = eco.db.conn.execute(
            "INSERT INTO teiai_market (guild_id, day, item_id, mode, qty, start_price, end_price, black, "
            "listed_at, expires_at) VALUES (?, ?, ?, 'fixed', 2, 300, 300, 1, ?, ?)",
            (eco.shop.guild_id, tm.day_start(now), shop.LABOR_DRINK, now - 60, now + 3600),
        )
        lid = cur.lastrowid
    cog = make_shop_cog(make_test_bot(repos=eco))
    eco.wallet.credit(2, 50_000, "テスト")
    view = shop_cog.ShopView(cog, 2, eco.shop.guild_id, screen="auction")
    e = view.embed()
    text = "\n".join(must(f.value) for f in e.fields)
    assert "帝愛の出品" in must(e.fields[0].name) and "闇市品" in text and "未鑑定" in text
    select = next(c for c in view.children if isinstance(c, discord.ui.Select) and c.placeholder == "買う品を選ぶ")
    assert f"tm:{lid}" in [o.value for o in select.options]
    buy = [c for c in view.children if isinstance(c, discord.ui.Button) and (c.label or "").startswith("🛒 購入")]
    assert buy, "帝愛の出品だけの日も、購入ボタンが出る"
    it = FakeInteraction()
    asyncio.run(shop_actions.buy_teiai_market(cog, as_interaction(it), lid, 1, confirm_fn=_yes))
    title = it.sent[-1]["embed"].title
    assert "帝愛の出品を買いました" in title and "鑑定の結果" in it.sent[-1]["embed"].description


async def _yes(*a, **kw):
    """確認ダイアログで「買う」を押したことにする。"""
    return True
