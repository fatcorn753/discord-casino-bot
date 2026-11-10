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


def test_command_sells_and_the_list_view_shows_and_buys(cog, eco, monkeypatch):
    give(eco, S, CARROT, 2)

    async def yes(*a, **k):
        return True

    monkeypatch.setattr(p2p_cog, "confirm_action", yes)
    it = It(S)
    asyncio.run(p2p_cog.P2PCog.sell.callback(cog, it, f"item:{CARROT}", SimpleNamespace(value="fixed"), 1_500, 2, None))
    assert "出品しました" in it.sent[-1]["embed"].title and eco.inventory(S).get(CARROT, 0) == 0
    lid = eco.p2p_active(None)[0]["id"]
    view = p2p_cog.ListView(cog, B, G)
    assert "にんじん" in view.embed().description and str(lid) in view.embed().description
    pick = It(B, values=[str(lid)])
    asyncio.run(view._on_pick(pick))
    assert "にんじん" in pick.sent[-1]["embed"].title and pick.sent[-1]["view"].children  # 買うボタンがある
    asyncio.run(pick.sent[-1]["view"]._on_go(It(B)))
    assert eco.inventory(B)[CARROT] == 2
    mine = It(S)
    asyncio.run(p2p_cog.P2PCog.mine.callback(cog, mine))
    assert mine.sent[-1]["ephemeral"] is True


def test_seller_sees_no_buy_button_on_own_listing_and_empty_list_has_no_select(cog, eco):
    empty = p2p_cog.ListView(cog, B, G)
    assert not empty.children and "出ているものはありません" in empty.embed().description
    give(eco, S, CARROT)
    _, lid = eco.p2p_list(S, "item", CARROT, 1, "fixed", 1_000, None, NOW)
    view = p2p_cog.ListView(cog, S, G)
    it = It(S, values=[str(lid)])
    asyncio.run(view._on_pick(it))
    assert not it.sent[-1]["view"].children


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
