"""プレイヤー間の取引（定価の市場と競り）: 値段の幅・手数料・入札・預かり・精算・送金の抜け道対策。"""

import asyncio
from types import SimpleNamespace

import pytest

from cogs import p2p_cog
from core import horse as horse_mod
from core import p2p, shop
from core.economy import Economy
from core.session import Sessions

pytestmark = pytest.mark.real_scope

G = 10
S, B, C = 1, 2, 3  # 売り手・買い手・もう1人
NOW = 1_800_000_000.0
CARROT = shop.CARROT
CARD = next(i.id for i in shop.CATALOG if i.kind == shop.CARD_COLOR and not i.limited)


@pytest.fixture
def eco(tmp_path):
    e = Economy(str(tmp_path / "t.db"), strict=True).for_guild(G)
    for uid in (S, B, C):
        e.credit(uid, 500_000, "テスト")
    return e


def give(eco, uid, item, qty=1):
    eco._conn.execute(
        "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty = qty + excluded.qty",
        (eco._g(), uid, item, qty),
    )
    eco._conn.commit()


def horse(eco, uid=S, name="ホース"):
    give(eco, uid, shop.HORSE_VOUCHER)
    ok, hid = eco.horse_create(uid, name, "栗毛", "🟥")
    assert ok
    return hid


# ----------------------------- 純粋なロジック ----------------------------- #
def test_only_consumables_and_cosmetics_are_tradable():
    assert p2p.tradable(CARROT) and p2p.tradable(CARD) and p2p.tradable(shop.GOLD_CARROT)
    assert not p2p.tradable("title_regular") and not p2p.tradable(shop.INSIDER_MEMO)
    assert not any(
        p2p.tradable(i.id)
        for i in shop.CATALOG
        if i.kind in (shop.COLLECTION, shop.TITLE) or (i.limited and i.kind != shop.RELIC)
    )
    assert all(p2p.tradable(i.id) for i in shop.CATALOG if i.kind == shop.RELIC)  # 発掘品は、買えないが、取引できる
    assert not p2p.tradable("ない品")


def test_price_range_follows_the_list_price_and_the_fee_and_increment_rules():
    lo, hi = p2p.price_range("item", CARROT, 3)
    assert lo == max(p2p.MIN_PRICE, int(3 * 1_000 * 0.10)) and hi == 3 * 1_000 * 5
    assert p2p.price_range("horse") == p2p.HORSE_PRICE_RANGE
    assert p2p.fee_of(10_000) == 500
    assert p2p.min_next_bid(1_000, 0) == 1_000  # 最初は開始価格
    assert p2p.min_next_bid(1_000, 1_000) == 1_100  # 5% か 100 の大きいほう
    assert p2p.min_next_bid(1_000, 10_000) == 10_500
    assert p2p.end_after_bid(1_000.0, 100.0) == 1_000.0  # 締切まで十分あれば、延びない
    assert p2p.end_after_bid(1_000.0, 900.0) == 1_200.0  # 締切の5分前を切ったら、5分延びる


# ----------------------------- 出品 ----------------------------- #
def test_listing_an_item_takes_it_from_the_inventory_and_cancel_returns_it(eco):
    give(eco, S, CARROT, 5)
    ok, lid = eco.p2p_list(S, "item", CARROT, 2, "fixed", 1_500, None, NOW)
    assert ok and eco.inventory(S)[CARROT] == 3  # 出した分は、預かられる
    assert eco.p2p_cancel(S, lid, NOW + 1)[0] and eco.inventory(S)[CARROT] == 5
    assert not eco.p2p_cancel(S, lid, NOW + 2)[0]  # もう取り消せない


def test_listing_validates_ownership_price_range_kind_and_the_active_limit(eco):
    give(eco, S, CARROT, 1)
    assert not eco.p2p_list(S, "item", CARROT, 5, "fixed", 1_000, None, NOW)[0]  # 持っていない数
    assert not eco.p2p_list(S, "item", CARROT, 1, "fixed", 10, None, NOW)[0]  # 安すぎる（送金の抜け道・投げ売り）
    assert not eco.p2p_list(S, "item", CARROT, 1, "fixed", 900_000, None, NOW)[0]  # 高すぎる（送金の抜け道）
    assert not eco.p2p_list(S, "item", "title_regular", 1, "fixed", 5_000, None, NOW)[0]  # 称号は売れない
    assert not eco.p2p_list(S, "item", CARROT, 1, "auction", 1_000, 5, NOW)[0]  # 競りの長さは決まった中から
    give(eco, S, shop.INSURANCE, 10)
    for _ in range(p2p.MAX_ACTIVE):
        assert eco.p2p_list(S, "item", shop.INSURANCE, 1, "fixed", 2_000, None, NOW)[0]
    ok, why = eco.p2p_list(S, "item", shop.INSURANCE, 1, "fixed", 2_000, None, NOW)
    assert not ok and f"{p2p.MAX_ACTIVE} 件" in why


def test_listing_an_equipped_cosmetic_unequips_it(eco):
    give(eco, S, CARD)
    assert eco.equip(S, "card_color", CARD)
    assert eco.get_profile(S)["card_color"] == CARD
    assert eco.p2p_list(S, "item", CARD, 1, "fixed", shop.BY_ID[CARD].price, None, NOW)[0]
    assert eco.get_profile(S)["card_color"] is None and CARD not in eco.inventory(S)


# ----------------------------- 定価で買う ----------------------------- #
def test_buying_pays_the_seller_minus_the_fee_and_moves_the_item(eco):
    give(eco, S, CARROT, 2)
    _, lid = eco.p2p_list(S, "item", CARROT, 2, "fixed", 2_000, None, NOW)
    s0, b0, cash0 = eco.balance(S), eco.balance(B), eco.bank_status()["cash"]
    ok, paid = eco.p2p_buy(B, lid, NOW + 1)
    assert ok and paid == 2_000
    assert eco.balance(B) == b0 - 2_000 and eco.inventory(B)[CARROT] == 2
    assert eco.balance(S) == s0 + 2_000 - p2p.fee_of(2_000)  # 手数料を引いた額
    assert eco.bank_status()["cash"] > cash0  # 手数料は帝愛銀行の収入
    assert eco.p2p_get(lid)["status"] == "sold" and not eco.p2p_buy(C, lid, NOW + 2)[0]


def test_buy_checks_self_balance_debt_ownership_and_expiry(eco):
    give(eco, S, CARD)
    _, lid = eco.p2p_list(S, "item", CARD, 1, "fixed", shop.BY_ID[CARD].price, None, NOW)
    assert "自分の出品" in eco.p2p_buy(S, lid, NOW + 1)[1]
    give(eco, B, CARD)
    assert "すでに持っています" in eco.p2p_buy(B, lid, NOW + 1)[1]  # 見た目の品は、1つだけ
    eco._conn.execute("UPDATE users SET balance=10 WHERE user_id=?", (C,))
    assert "足りません" in eco.p2p_buy(C, lid, NOW + 1)[1] and eco.inventory(S).get(CARD) is None
    assert not eco.p2p_buy(C, lid, NOW + p2p.FIXED_DAYS * 86400 + 1)[0]  # 期限切れ


def test_a_debtor_cannot_buy(eco):
    give(eco, S, CARROT)
    _, lid = eco.p2p_list(S, "item", CARROT, 1, "fixed", 1_000, None, NOW)
    with eco._lock, eco._conn:
        eco._conn.execute("UPDATE users SET debt=5000 WHERE user_id=?", (B,))
    ok, why = eco.p2p_buy(B, lid, NOW + 1)
    assert not ok and "カタギ" in why


# ----------------------------- 馬の売買 ----------------------------- #
def test_horse_is_escrowed_while_listed_and_moves_to_the_buyer(eco):
    hid = horse(eco, S)
    ok, lid = eco.p2p_list(S, "horse", str(hid), 1, "fixed", 40_000, None, NOW)
    assert ok and eco.horse_list(S) == [] and eco.horse_get(hid)["owner_id"] == p2p.ESCROW_OWNER
    assert eco._horse_count(S) == 1  # 出品中も、持ち数に数える
    assert eco.p2p_buy(B, lid, NOW + 1)[0]
    assert [h["id"] for h in eco.horse_list(B)] == [hid] and eco.horse_get(hid)[
        "name"
    ] == "ホース"  # 名前・ステータスは、そのまま


def test_a_horse_scheduled_for_a_race_cannot_be_listed_and_listing_does_not_free_a_slot(eco):
    hid = horse(eco, S)
    rid = eco.race_create_special(NOW, 10, 10)
    assert eco.race_register(S, hid, rid, NOW + 1)[0]
    ok, why = eco.p2p_list(S, "horse", str(hid), 1, "fixed", 40_000, None, NOW + 2)
    assert not ok and "出走予定" in why
    eco.race_withdraw(S, hid, rid, NOW + 3)
    others = [horse(eco, S, f"馬{i}") for i in range(horse_mod.MAX_HORSES - 1)]
    assert len(others) == horse_mod.MAX_HORSES - 1
    assert eco.p2p_list(S, "horse", str(hid), 1, "fixed", 40_000, None, NOW + 4)[0]
    give(eco, S, shop.HORSE_VOUCHER)
    ok, why = eco.horse_create(S, "4頭目", "栗毛", "🟥")  # 出品中の馬も数えるので、3頭を超えて持てない
    assert not ok and "3 頭" in why


def test_cancelling_a_horse_listing_returns_the_horse(eco):
    hid = horse(eco, S)
    _, lid = eco.p2p_list(S, "horse", str(hid), 1, "auction", 30_000, 6, NOW)
    assert eco.p2p_cancel(S, lid, NOW + 1)[0] and eco.horse_get(hid)["owner_id"] == S


def test_buyer_with_three_horses_cannot_buy_another(eco):
    for i in range(horse_mod.MAX_HORSES):
        horse(eco, B, f"持ち馬{i}")
    hid = horse(eco, S)
    _, lid = eco.p2p_list(S, "horse", str(hid), 1, "fixed", 40_000, None, NOW)
    ok, why = eco.p2p_buy(B, lid, NOW + 1)
    assert not ok and "3 頭" in why


# ----------------------------- 競り ----------------------------- #
def test_bidding_escrows_money_refunds_the_outbid_and_extends_near_the_end(eco):
    give(eco, S, CARROT, 3)
    _, lid = eco.p2p_list(S, "item", CARROT, 3, "auction", 1_000, 1, NOW)
    b0, c0 = eco.balance(B), eco.balance(C)
    assert "1,000" in eco.p2p_bid(B, lid, 900, NOW + 10)[1]  # 開始価格より低い
    ok, end = eco.p2p_bid(B, lid, 1_000, NOW + 10)
    assert ok and eco.balance(B) == b0 - 1_000  # お金は預かられる
    assert "最高入札者" in eco.p2p_bid(B, lid, 1_500, NOW + 11)[1]
    assert "1,100" in eco.p2p_bid(C, lid, 1_050, NOW + 12)[1]  # 5%（最低100）以上
    ok, _ = eco.p2p_bid(C, lid, 1_200, NOW + 3_500)  # 締切（NOW+3600）の5分前を切ってから
    assert ok and eco.balance(B) == b0 and eco.balance(C) == c0 - 1_200  # 抜かれた人には、返る
    assert eco.p2p_get(lid)["end_at"] == NOW + 3_500 + p2p.SNIPE_SECONDS  # 締切が延びた
    assert "競り" in eco.p2p_buy(B, lid, NOW + 3_501)[1]  # 競りは「買う」では買えない
    assert not eco.p2p_cancel(S, lid, NOW + 3_502)[0]  # 入札が入ったら、取り消せない


def test_auction_settles_to_the_highest_bidder_and_pays_the_seller_minus_the_fee(eco):
    give(eco, S, CARROT, 3)
    _, lid = eco.p2p_list(S, "item", CARROT, 3, "auction", 1_000, 1, NOW)
    eco.p2p_bid(B, lid, 1_000, NOW + 10)
    eco.p2p_bid(C, lid, 1_300, NOW + 20)
    s0 = eco.balance(S)
    assert eco.p2p_settle(NOW + 100) == []  # まだ締切前
    out = eco.p2p_settle(NOW + 3_601)
    assert [(r["id"], res) for r, res in out] == [(lid, "sold")]
    assert eco.inventory(C)[CARROT] == 3 and eco.balance(S) == s0 + 1_300 - p2p.fee_of(1_300)
    done = eco.p2p_get(lid)
    assert done["status"] == "sold" and done["buyer_id"] == C and done["sold_price"] == 1_300
    assert eco.p2p_settle(NOW + 7_000) == []  # 二重に精算しない
    assert any("落札" in n for n in eco.take_notices(C)) and any("売れました" in n for n in eco.take_notices(S))


def test_unsold_listings_return_to_the_seller(eco):
    give(eco, S, CARROT, 2)
    _, a = eco.p2p_list(S, "item", CARROT, 1, "auction", 1_000, 1, NOW)
    _, b = eco.p2p_list(S, "item", CARROT, 1, "fixed", 1_000, None, NOW)
    assert eco.inventory(S).get(CARROT, 0) == 0
    out = eco.p2p_settle(NOW + p2p.FIXED_DAYS * 86400 + 1)
    assert {res for _, res in out} == {"expired"} and eco.inventory(S)[CARROT] == 2  # 売れ残りは、戻る
    assert eco.p2p_get(a)["status"] == "expired" and eco.p2p_get(b)["status"] == "expired"


def test_winner_who_can_no_longer_receive_gets_a_refund_and_the_item_goes_back(eco):
    give(eco, S, CARD)
    _, lid = eco.p2p_list(S, "item", CARD, 1, "auction", shop.BY_ID[CARD].price, 1, NOW)
    price = shop.BY_ID[CARD].price
    b0 = eco.balance(B)
    assert eco.p2p_bid(B, lid, price, NOW + 10)[0]
    give(eco, B, CARD)  # 入札のあと、別の方法で同じ品を手に入れた
    out = eco.p2p_settle(NOW + 3_601)
    assert [(res) for _, res in out] == ["failed"]
    assert eco.balance(B) == b0 and eco.inventory(S)[CARD] == 1 and eco.p2p_get(lid)["status"] == "expired"


# ----------------------------- 送金の抜け道 ----------------------------- #
def test_same_pair_trades_are_capped_per_day_so_the_market_is_not_a_transfer_loophole(eco):
    give(eco, S, shop.INSURANCE, 30)
    price = shop.BY_ID[shop.INSURANCE].price * 5  # 定価の幅の上限いっぱい
    total = 0
    bought = 0
    while True:
        ok, lid = eco.p2p_list(S, "item", shop.INSURANCE, 1, "fixed", price, None, NOW)
        assert ok
        ok, why = eco.p2p_buy(B, lid, NOW + bought + 1)
        if not ok:
            assert "24時間" in why and f"{p2p.PAIR_DAILY_LIMIT:,}" in why
            break
        total += price
        bought += 1
        assert bought < 30
    assert total <= p2p.PAIR_DAILY_LIMIT
    ok, lid = eco.p2p_list(S, "item", shop.INSURANCE, 1, "fixed", price, None, NOW)
    assert eco.p2p_buy(C, lid, NOW + 100)[0]  # 別の相手なら、買える（上限は、2人の組ごと）


def test_the_pair_cap_resets_after_24_hours(eco):
    give(eco, S, shop.INSURANCE, 30)
    price = shop.BY_ID[shop.INSURANCE].price * 5
    n = p2p.PAIR_DAILY_LIMIT // price
    for i in range(n):
        _, lid = eco.p2p_list(S, "item", shop.INSURANCE, 1, "fixed", price, None, NOW)
        assert eco.p2p_buy(B, lid, NOW + i + 1)[0]
    _, lid = eco.p2p_list(S, "item", shop.INSURANCE, 1, "fixed", price, None, NOW)
    assert not eco.p2p_buy(B, lid, NOW + 100)[0]
    assert eco.p2p_buy(B, lid, NOW + 86_400 + 100)[0]


# ----------------------------- 一覧 ----------------------------- #
def test_active_and_mine_lists(eco):
    give(eco, S, CARROT, 2)
    hid = horse(eco, S)
    _, a = eco.p2p_list(S, "item", CARROT, 1, "fixed", 1_000, None, NOW)
    _, b = eco.p2p_list(S, "horse", str(hid), 1, "auction", 30_000, 6, NOW)
    assert {r["id"] for r in eco.p2p_active(None, NOW + 1)} == {a, b}
    assert [r["id"] for r in eco.p2p_active("horse", NOW + 1)] == [b]
    eco.p2p_bid(B, b, 30_000, NOW + 2)
    assert {r["id"] for r in eco.p2p_mine(S, NOW + 3)["selling"]} == {a, b}
    assert [r["id"] for r in eco.p2p_mine(B, NOW + 3)["bidding"]] == [b]
    assert eco.p2p_active(None, NOW + 10 * 86400) == []  # 期限を過ぎたものは出ない


# ----------------------------- Cog ----------------------------- #
class Chan:
    def __init__(self):
        self.sent = []

    async def send(self, **kw):
        self.sent.append(kw)


class It:
    def __init__(self, uid, gid=G, values=None):
        self.user = SimpleNamespace(id=uid)
        self.guild_id = gid
        self.data = {"values": values or []}
        self.sent = []
        self.response = SimpleNamespace(send_message=self._send, send_modal=self._modal)
        self.followup = SimpleNamespace(send=self._send)

    async def _send(self, *a, **kw):
        self.sent.append(kw)

    async def _modal(self, modal):
        self.sent.append({"modal": modal})


@pytest.fixture
def cog(eco):
    bot = SimpleNamespace(
        economy=SimpleNamespace(for_guild=lambda gid: eco), sessions=Sessions(cooldown=0), config={}, guilds=[]
    )
    return p2p_cog.P2PCog(bot)


def _auction_view(cog_shop, uid):
    from cogs import shop_cog

    return shop_cog.ShopView(cog_shop, uid, G, screen="auction")


@pytest.fixture
def shop_cog_obj(eco, cog):
    from cogs import shop_cog

    bot = SimpleNamespace(
        economy=SimpleNamespace(for_guild=lambda gid: eco),
        sessions=cog.bot.sessions,
        config={},
        guilds=[],
        get_cog=lambda name: cog if name == "P2PCog" else None,
    )
    return shop_cog.ShopCog(bot)


def test_sell_view_lists_tradable_items_and_horses_and_the_modal_lists_it(cog, eco):
    give(eco, S, CARROT, 2)
    give(eco, S, "title_regular")  # 称号は、候補に出ない
    hid = horse(eco, S)
    view = p2p_cog.SellView(cog, S, G)
    values = [o.value for o in view.options]
    assert f"item:{CARROT}" in values and f"horse:{hid}" in values and "item:title_regular" not in values
    go = view.children[1]
    no_pick = It(S)
    asyncio.run(go.callback(no_pick))
    assert "先に選んでください" in no_pick.sent[-1]["embed"].title  # 選ぶ前に押すと、案内
    asyncio.run(view._on_pick(_PickIt(S, f"item:{CARROT}")))
    opened = It(S)
    asyncio.run(go.callback(opened))
    modal = opened.sent[-1]["modal"]
    assert (
        isinstance(modal, p2p_cog.SellModal) and modal.qty is not None and modal.hours is None
    )  # 定価・消耗品: 個数あり
    asyncio.run(view._on_pick(_PickIt(S, f"horse:{hid}")))
    horse_modal = It(S)
    asyncio.run(view.children[2].callback(horse_modal))
    m2 = horse_modal.sent[-1]["modal"]
    assert m2.qty is None and m2.hours is not None  # 競り・馬: 個数なし・長さあり


class _PickIt(It):
    def __init__(self, uid, value):
        super().__init__(uid, values=[value])
        self.response = SimpleNamespace(send_message=self._send, send_modal=self._modal, edit_message=self._edit)

    async def _edit(self, **kw):
        self.sent.append(kw)


def test_sell_modal_lists_the_item_and_validates_numbers(cog, eco):
    give(eco, S, CARROT, 3)
    view = p2p_cog.SellView(cog, S, G)
    view.target = f"item:{CARROT}"
    modal = p2p_cog.SellModal(view, "fixed")
    modal.price._value = "abc"
    bad = It(S)
    asyncio.run(modal.on_submit(bad))
    assert "数字で入れて" in bad.sent[-1]["embed"].description and eco.inventory(S)[CARROT] == 3
    modal.price._value = "1,500"
    modal.qty._value = "2"
    done = It(S)
    asyncio.run(modal.on_submit(done))
    assert "出品しました" in done.sent[-1]["embed"].title and eco.inventory(S)[CARROT] == 1
    listing = eco.p2p_active(None)[0]
    assert listing["price"] == 1_500 and listing["qty"] == 2 and listing["mode"] == "fixed"
    view.target = f"item:{CARROT}"
    auction = p2p_cog.SellModal(view, "auction")
    auction.price._value = "1000"
    auction.qty._value = "1"
    auction.hours._value = "6"
    asyncio.run(auction.on_submit(It(S)))
    assert any(r["mode"] == "auction" and r["end_at"] > 0 for r in eco.p2p_active(None))


def test_auction_screen_shows_player_listings_with_sell_and_mine_buttons(shop_cog_obj, eco):
    give(eco, S, CARROT, 2)
    _, lid = eco.p2p_list(S, "item", CARROT, 1, "fixed", 1_500, None, time_now())
    view = _auction_view(shop_cog_obj, B)
    labels = [getattr(c, "label", "") for c in view.children]
    assert "📤 出品する" in labels and "📋 自分の取引" in labels and "🛍️ ショップ" in labels
    assert view.auction_tab == "players"  # 出品があるので、最初に、プレイヤーの出品のタブが開く
    assert any(isinstance(c, p2p_cog.discord.ui.Select) and "見る出品を選ぶ" in c.placeholder for c in view.children)
    text = "\n".join(f.value for f in view.embed().fields)
    assert "にんじん" in text and f"#{lid}" in text  # 競売の画面に、プレイヤーの出品が並ぶ
    assert all(c.row <= 4 for c in view.children)  # 画面の段に収まっている


def test_picking_a_player_listing_opens_the_detail_and_the_button_buys(shop_cog_obj, cog, eco, monkeypatch):
    give(eco, S, CARROT, 2)
    _, lid = eco.p2p_list(S, "item", CARROT, 2, "fixed", 1_500, None, time_now())

    async def yes(*a, **k):
        return True

    monkeypatch.setattr(p2p_cog, "confirm_action", yes)
    view = _auction_view(shop_cog_obj, B)
    pick = It(B, values=[str(lid)])
    asyncio.run(view._on_player(pick))
    sent = pick.sent[-1]
    assert "にんじん" in sent["embed"].title and sent["view"].children and sent["ephemeral"] is True
    asyncio.run(sent["view"]._on_go(It(B)))
    assert eco.inventory(B)[CARROT] == 2
    seller_pick = It(S, values=[str(eco.p2p_active(None)[0]["id"])]) if eco.p2p_active(None) else None
    assert seller_pick is None  # 売れたので、もう出ていない


def test_own_listing_has_no_buy_button_and_mine_view_cancels(cog, eco):
    give(eco, S, CARROT)
    _, lid = eco.p2p_list(S, "item", CARROT, 1, "fixed", 1_000, None, time_now())
    own = It(S)
    asyncio.run(cog.open_detail(own, lid))
    assert not own.sent[-1]["view"].children  # 自分の出品は、買えない
    mine = p2p_cog.MineView(cog, S, G)
    assert f"#{lid}" in mine.embed().fields[0].value and mine.children
    gone = It(S, values=[str(lid)])
    asyncio.run(mine._on_cancel(gone))
    assert "取り消しました" in gone.sent[-1]["embed"].title and eco.inventory(S)[CARROT] == 1
    empty = p2p_cog.MineView(cog, S, G)
    assert not empty.children and "出していません" in empty.embed().fields[0].value


def test_sell_screen_without_anything_to_sell_says_so(cog, eco):
    nothing = It(C)
    asyncio.run(cog.open_sell(nothing))
    assert "出せるものがありません" in nothing.sent[-1]["embed"].title


def time_now():
    import time

    return time.time()


def test_bid_through_the_modal_and_settlement_announces_the_winner(cog, eco, monkeypatch):
    give(eco, S, CARROT, 2)
    _, lid = eco.p2p_list(S, "item", CARROT, 2, "auction", 1_000, 1, NOW)
    it = It(B)
    asyncio.run(cog.do_bid(it, lid, 1_200))
    assert "入札しました" in it.sent[-1]["embed"].title and eco.p2p_get(lid)["bid"] == 1_200
    chan = Chan()
    guild = SimpleNamespace(id=G)
    monkeypatch.setattr(p2p_cog, "announce_channel", lambda g, e: chan)
    monkeypatch.setattr(eco, "p2p_settle", lambda now=None: eco.__class__.p2p_settle(eco, NOW + 5_000))
    asyncio.run(cog._settle_guild(guild))
    assert chan.sent and "落札" in chan.sent[0]["embed"].title and f"<@{B}>" in chan.sent[0]["embed"].description


# ----------------------------- 発掘品の取引（#24） ----------------------------- #
def test_relics_can_be_traded_within_their_reference_value_range(eco):
    rid = "relic_vase"
    value = shop.RELIC_VALUE[rid]
    give(eco, S, rid, 2)
    lo, hi = p2p.price_range("item", rid, 1)
    assert lo == max(p2p.MIN_PRICE, int(value * 0.10)) and hi == min(p2p.PAIR_DAILY_LIMIT, value * 5)
    assert not eco.p2p_list(S, "item", rid, 1, "fixed", lo - 1, None, NOW)[0]  # 安すぎる
    ok, lid = eco.p2p_list(S, "item", rid, 1, "fixed", value, None, NOW)
    assert ok and eco.inventory(S)[rid] == 1
    assert eco.p2p_buy(B, lid, NOW + 1)[0] and eco.inventory(B)[rid] == 1  # 発掘品は、いくつでも持てる
    ok, lid2 = eco.p2p_list(S, "item", rid, 1, "auction", value, 6, NOW)
    assert ok and eco.p2p_bid(B, lid2, value, NOW + 10)[0]
    assert eco.p2p_settle(NOW + 7 * 3600)[0][1] == "sold" and eco.inventory(B)[rid] == 2


def test_the_most_valuable_relic_still_fits_under_the_pair_cap(eco):
    top = max(shop.RELIC_VALUE, key=shop.RELIC_VALUE.get)
    lo, hi = p2p.price_range("item", top, 1)
    assert hi <= p2p.PAIR_DAILY_LIMIT and lo < shop.RELIC_VALUE[top] <= hi


# ----------------------------- 競売の画面（タブ・絞り込み・並び順） ----------------------------- #
def test_auction_screen_opens_on_the_shelf_when_no_player_listings_and_tabs_switch(shop_cog_obj, eco):
    view = _auction_view(shop_cog_obj, B)
    assert view.auction_tab == "teiai"  # 出品がなければ、帝愛の棚
    labels = [getattr(c, "label", "") for c in view.children]
    assert any(label.startswith("🤝 プレイヤーの出品（0）") for label in labels)
    give(eco, S, CARROT)
    eco.p2p_list(S, "item", CARROT, 1, "fixed", 1_000, None, time_now())
    again = _auction_view(shop_cog_obj, B)
    assert again.auction_tab == "players"
    labels = [getattr(c, "label", "") for c in again.children]
    assert any(label.startswith("🤝 プレイヤーの出品（1）") for label in labels)
    tab_it = _PickIt(B, "")
    asyncio.run(again._make_auction_tab("teiai")(tab_it))
    assert again.auction_tab == "teiai" and "帝愛の棚" in again.embed().title
    assert all(c.row <= 4 for c in again.children)


def test_player_tab_filters_sorts_and_shows_two_line_entries(shop_cog_obj, eco):
    import time

    now = time.time()
    give(eco, S, CARROT, 2)
    give(eco, S, shop.INSURANCE)
    hid = horse(eco, S)
    _, cheap = eco.p2p_list(S, "item", CARROT, 1, "fixed", 1_000, None, now)
    _, pricey = eco.p2p_list(S, "item", shop.INSURANCE, 1, "auction", 3_000, 6, now + 1)
    _, mount = eco.p2p_list(S, "horse", str(hid), 1, "fixed", 40_000, None, now + 2)
    view = _auction_view(shop_cog_obj, B)
    text = view.embed().fields[0].value
    assert (
        f"#{cheap}" in text and f"#{pricey}" in text and f"#{mount}" in text and "出品 <@1>" in text and "締切" in text
    )
    assert text.index(f"#{pricey}") < text.index(f"#{cheap}")  # 締切が近い順（競りの6時間が、定価の7日より先）
    horse_only = _PickIt(B, "")
    asyncio.run(view._make_p2p_filter("horse")(horse_only))
    assert f"#{mount}" in view.embed().fields[0].value and f"#{cheap}" not in view.embed().fields[0].value
    asyncio.run(view._make_p2p_filter("auction")(_PickIt(B, "")))
    only_auction = view.embed().fields[0].value
    assert f"#{pricey}" in only_auction and f"#{cheap}" not in only_auction
    asyncio.run(view._make_p2p_filter("all")(_PickIt(B, "")))
    asyncio.run(view._on_p2p_order(_PickIt(B, "price_desc")))
    ordered = view.embed().fields[0].value
    assert ordered.index(f"#{mount}") < ordered.index(f"#{pricey}") < ordered.index(f"#{cheap}")  # 高い順
    asyncio.run(view._on_p2p_order(_PickIt(B, "new")))
    newest = view.embed().fields[0].value
    assert newest.index(f"#{mount}") < newest.index(f"#{pricey}") < newest.index(f"#{cheap}")


def test_empty_player_tab_invites_the_first_seller(shop_cog_obj, eco):
    view = _auction_view(shop_cog_obj, B)
    asyncio.run(view._make_auction_tab("players")(_PickIt(B, "")))
    assert "最初の出品者" in view.embed().fields[0].value
    assert not any(isinstance(c, p2p_cog.discord.ui.Select) and "見る出品" in c.placeholder for c in view.children)


# ----------------------------- 出品の告知（チャンネルへの投稿） ----------------------------- #
class PostChan:
    id = 77

    def __init__(self):
        self.sent, self.edits = [], []

    async def send(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(id=500 + len(self.sent))

    def get_partial_message(self, mid):
        chan = self

        class Partial:
            async def edit(self, **kw):
                chan.edits.append((mid, kw))

        return Partial()


def _guild(chan):
    return SimpleNamespace(id=G, get_channel=lambda cid: chan if cid == chan.id else None, me=object())


def test_listing_is_announced_in_the_channel_and_the_post_follows_bids_and_the_sale(cog, eco, monkeypatch):
    chan = PostChan()
    monkeypatch.setattr(p2p_cog, "announce_channel", lambda g, e: chan)
    guild = _guild(chan)
    give(eco, S, CARROT, 2)
    _, lid = eco.p2p_list(S, "item", CARROT, 2, "auction", 1_000, 6, NOW)
    assert asyncio.run(cog.announce(guild, lid)) is True
    post = chan.sent[0]
    assert "出品" in post["embed"].title and "にんじん" in post["embed"].title
    assert post["view"].children[0].custom_id == f"p2p:open:{lid}"  # 「見る・買う・入札」のボタン
    row = eco.p2p_get(lid)
    assert row["channel_id"] == chan.id and row["message_id"] == 501
    eco.p2p_bid(B, lid, 1_200, NOW + 10)
    asyncio.run(cog.refresh_post(guild, lid))
    mid, kw = chan.edits[-1]
    assert (
        mid == 501
        and "入札あり" not in kw["embed"].title
        and "1,200" in kw["embed"].description + kw["embed"].title + "".join(f.value for f in kw["embed"].fields)
    )
    assert "view" not in kw  # 出ている間は、ボタンを残す
    eco.p2p_settle(NOW + 7 * 3600)
    asyncio.run(cog.refresh_post(guild, lid))
    mid, kw = chan.edits[-1]
    assert "売れました" in kw["embed"].title and kw["view"] is None  # 売れたら、結果に書き換わり、ボタンが外れる


def test_cancelled_and_expired_posts_are_rewritten_and_missing_channels_are_ignored(cog, eco, monkeypatch):
    chan = PostChan()
    monkeypatch.setattr(p2p_cog, "announce_channel", lambda g, e: chan)
    guild = _guild(chan)
    give(eco, S, CARROT, 2)
    _, a = eco.p2p_list(S, "item", CARROT, 1, "fixed", 1_000, None, NOW)
    asyncio.run(cog.announce(guild, a))
    eco.p2p_cancel(S, a, NOW + 1)
    asyncio.run(cog.refresh_post(guild, a))
    assert "取り消されました" in chan.edits[-1][1]["embed"].title
    _, b = eco.p2p_list(S, "item", CARROT, 1, "auction", 1_000, 1, NOW)
    asyncio.run(cog.announce(guild, b))
    eco.p2p_settle(NOW + 3_601)
    asyncio.run(cog.refresh_post(guild, b))
    assert "売れ残り" in chan.edits[-1][1]["embed"].title
    assert asyncio.run(cog.announce(None, b)) is False  # サーバーがなければ、何もしない
    monkeypatch.setattr(p2p_cog, "announce_channel", lambda g, e: None)
    assert asyncio.run(cog.announce(guild, b)) is False  # 告知チャンネルがなければ、投稿しない（出品は、そのまま）
    asyncio.run(cog.refresh_post(None, b))  # 投稿を覚えていなくても、落ちない


def test_selling_through_the_modal_posts_the_announcement(cog, eco, monkeypatch):
    chan = PostChan()
    monkeypatch.setattr(p2p_cog, "announce_channel", lambda g, e: chan)
    give(eco, S, CARROT, 2)
    view = p2p_cog.SellView(cog, S, G)
    view.target = f"item:{CARROT}"
    modal = p2p_cog.SellModal(view, "fixed")
    modal.price._value = "1500"
    modal.qty._value = "1"
    done = It(S)
    done.guild = _guild(chan)
    asyncio.run(modal.on_submit(done))
    assert chan.sent and "告知チャンネルに" in done.sent[-1]["embed"].description
    assert eco.p2p_active(None)[0]["message_id"] == 501


def test_the_announce_button_opens_the_detail_for_anyone(cog, eco):
    give(eco, S, CARROT)
    _, lid = eco.p2p_list(S, "item", CARROT, 1, "fixed", 1_000, None, time_now())
    button = p2p_cog.ListingButton(lid)
    it = It(B)
    it.client = SimpleNamespace(get_cog=lambda name: cog)
    asyncio.run(button.callback(it))
    assert "にんじん" in it.sent[-1]["embed"].title and it.sent[-1]["view"].children
    gone = It(B)
    gone.client = SimpleNamespace(get_cog=lambda name: None)
    asyncio.run(button.callback(gone))
    assert "いまは使えません" in gone.sent[-1]["embed"].title
