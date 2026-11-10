"""ショップ: 購入・装備・消耗品・保険券・デイリー2倍券・名前の色ロールの付け外し。"""

import asyncio
import datetime
import time
from types import SimpleNamespace

import discord
import pytest

from cogs import shop_cog
from core import shop
from core.economy import INITIAL_BALANCE, Economy


@pytest.fixture
def eco(tmp_path):
    return Economy(str(tmp_path / "t.db"))


def buy(eco, item_id, qty=1, uid=1):
    it = shop.BY_ID[item_id]
    return eco.purchase(uid, it.id, shop.label(it), it.price, it.unique, qty)


# ----------------------------- カタログ ----------------------------- #
def test_catalog_is_consistent():
    assert len({i.id for i in shop.CATALOG}) == len(shop.CATALOG)
    assert all(i.price > 0 for i in shop.CATALOG if not i.limited)  # 売り物には値段がある
    assert all(i.price == 0 for i in shop.CATALOG if i.limited)
    for kind in shop.KIND_LABEL:
        assert shop.of_kind(kind), kind
    # 色ロールの色は 0x000000（Discord では「色なし」扱い）にしない
    assert all(i.value != 0 for i in shop.of_kind(shop.COLOR_ROLE))
    assert shop.find("title_zawa") is shop.find("ざわ…ざわ…")
    assert shop.find("存在しない") is None
    # 同じ名前（「赤」）でも、種類つきなら区別して探せる
    assert shop.find("カードの色:赤").kind == shop.CARD_COLOR
    assert shop.find("名前の色（ロール）:赤").kind == shop.COLOR_ROLE


# ----------------------------- 購入 ----------------------------- #
def test_purchase_charges_and_adds_to_inventory(eco):
    res = buy(eco, "title_rookie")
    assert res.ok and eco.balance(1) == INITIAL_BALANCE - 2_000
    assert eco.inventory(1) == {"title_rookie": 1}


def test_cannot_buy_unique_item_twice_or_without_money(eco):
    assert buy(eco, "title_rookie").ok
    again = buy(eco, "title_rookie")
    assert not again.ok and "持って" in again.reason and eco.balance(1) == INITIAL_BALANCE - 2_000
    poor = buy(eco, "col_sunglasses")  # 100,000 は足りない
    assert not poor.ok and "足りません" in poor.reason and eco.balance(1) == INITIAL_BALANCE - 2_000
    assert "title_emperor" not in eco.inventory(1)  # 失敗したら持ち物も増えない


def test_consumables_stack_and_charge_per_unit(eco):
    eco.credit(1, 10_000, "テスト")
    assert buy(eco, shop.INSURANCE, qty=3).ok
    assert eco.inventory(1)[shop.INSURANCE] == 3 and eco.balance(1) == INITIAL_BALANCE + 10_000 - 2_000 * 3
    assert buy(eco, shop.INSURANCE).ok and eco.inventory(1)[shop.INSURANCE] == 4


def test_purchase_is_recorded_in_ledger(eco):
    buy(eco, "title_rookie")
    assert eco.history(1)[0]["delta"] == -2_000 and "購入" in eco.history(1)[0]["reason"]


# ----------------------------- 装備 ----------------------------- #
def test_equip_requires_ownership_and_can_be_removed(eco):
    assert not eco.equip(1, "title", "title_rookie")  # 持っていない
    buy(eco, "title_rookie")
    assert eco.equip(1, "title", "title_rookie")
    assert eco.get_profile(1)["title"] == "title_rookie"
    assert eco.equip(1, "title", None)
    assert eco.get_profile(1)["title"] is None
    with pytest.raises(ValueError):
        eco.equip(1, "balance; DROP TABLE users", None)  # スロット名は固定の3つだけ


def test_get_profile_does_not_create_accounts(eco):
    assert eco.get_profile(99)["title"] is None
    assert eco._conn.execute("SELECT COUNT(*) FROM users WHERE user_id=99").fetchone()[0] == 0


# ------------------------- 消耗品の効果 ------------------------- #
def test_daily_double_ticket_doubles_next_daily_once(eco):
    buy(eco, shop.DAILY_DOUBLE)
    assert eco.arm(1, shop.DAILY_DOUBLE)
    assert not eco.arm(1, shop.DAILY_DOUBLE)  # 持っていないので2回目は使えない
    day = datetime.date(2026, 10, 20)
    r1 = eco.claim_daily(1, day)
    assert r1.boosted and r1.amount == Economy.DAILY_BASE * 2
    r2 = eco.claim_daily(1, day + datetime.timedelta(days=1))
    assert not r2.boosted and r2.amount == Economy.DAILY_BASE + Economy.DAILY_STREAK_BONUS  # 券は1回きり


def test_insurance_refunds_half_on_first_loss_only(eco):
    eco.credit(1, 10_000, "テスト")
    buy(eco, shop.INSURANCE)
    eco.arm(1, shop.INSURANCE)
    before = eco.balance(1)
    eid = eco.place_bet(1, 1000, "テスト")
    net, taken, insured = eco.settle_with_penalty(eid, 0, "テスト", 0)  # 負け
    assert insured == 500 and net == -500 and eco.balance(1) == before - 500
    eid = eco.place_bet(1, 1000, "テスト")
    net, _, insured = eco.settle_with_penalty(eid, 0, "テスト", 0)  # 券は使用済み
    assert insured == 0 and net == -1000


def test_insurance_is_not_used_on_a_win_or_draw(eco):
    eco.credit(1, 10_000, "テスト")
    buy(eco, shop.INSURANCE)
    eco.arm(1, shop.INSURANCE)
    for payout in (2000, 1000):
        eid = eco.place_bet(1, 1000, "テスト")
        assert eco.settle_with_penalty(eid, payout, "テスト", 0)[2] == 0
    assert eco.get_profile(1)["insurance"] == 1  # 負けるまで取っておかれる


def test_insurance_and_double_penalty_in_one_settlement(eco):
    eco.credit(1, 10_000, "テスト")
    buy(eco, shop.INSURANCE)
    eco.arm(1, shop.INSURANCE)
    before = eco.balance(1)
    eid = eco.place_bet(1, 500, "チンチロ")
    net, taken, insured = eco.settle_with_penalty(eid, 0, "チンチロ", 2000)  # 5倍払い: 追加で 2000
    assert (taken, insured) == (2000, 250)
    assert eco.balance(1) == before - 500 + 250 - 2000 and net == 250 - 500 - 2000


# ------------------------- 名前の色ロール ------------------------- #
class FakeRole:
    def __init__(self, name, position=1, colour=None, perms=0, managed=False):
        self.name, self.position = name, position
        self.colour = SimpleNamespace(value=getattr(colour, "value", colour if colour is not None else 0))
        self.permissions = SimpleNamespace(value=perms)
        self.managed = managed
        self.edited = []

    def __ge__(self, other):
        return self.position >= other.position

    def __hash__(self):
        return id(self)

    async def edit(self, **kw):
        self.edited.append(kw)
        self.position = kw.get("position", self.position)
        if "colour" in kw:
            self.colour = SimpleNamespace(value=kw["colour"].value)


class FakeMember:
    def __init__(self, roles):
        self.roles = list(roles)

    async def add_roles(self, *roles, **kw):
        self.roles.extend(r for r in roles if r not in self.roles)

    async def remove_roles(self, *roles, **kw):
        self.roles = [r for r in self.roles if r not in roles]


class FakeGuild:
    def __init__(self, roles, bot_pos=10, can_manage=True):
        self.roles = list(roles)
        self.me = SimpleNamespace(
            top_role=FakeRole("Bot", bot_pos), guild_permissions=SimpleNamespace(manage_roles=can_manage)
        )
        self.created = []

    async def create_role(self, name, colour, **kw):
        role = FakeRole(name, 1, colour)
        self.roles.append(role)
        self.created.append(role)
        return role


def run(coro):
    return asyncio.run(coro)


def test_color_role_is_created_without_permissions_just_below_bot_role(monkeypatch):
    monkeypatch.setattr(discord.utils, "get", lambda seq, name: next((r for r in seq if r.name == name), None))
    guild, member = FakeGuild([]), FakeMember([])
    run(shop_cog.set_color_role(guild, member, shop.BY_ID["role_red"]))
    role = guild.created[0]
    assert role.name == "ペリカ・赤" and role.position == 9  # Bot(10) のすぐ下
    assert role in member.roles


def test_changing_color_removes_the_previous_color_role_only(monkeypatch):
    monkeypatch.setattr(discord.utils, "get", lambda seq, name: next((r for r in seq if r.name == name), None))
    secret = FakeRole("シークレット", 3)  # 他のロールは触らない
    old = FakeRole("ペリカ・赤", 9, 0xE74C3C)
    guild, member = FakeGuild([secret, old]), FakeMember([secret, old])
    run(shop_cog.set_color_role(guild, member, shop.BY_ID["role_blue"]))
    names = {r.name for r in member.roles}
    assert names == {"シークレット", "ペリカ・青"}
    run(shop_cog.set_color_role(guild, member, None))  # 外す
    assert {r.name for r in member.roles} == {"シークレット"}


def test_color_role_requires_manage_roles_permission():
    guild, member = FakeGuild([], can_manage=False), FakeMember([])
    with pytest.raises(shop_cog.ShopError) as exc:
        run(shop_cog.set_color_role(guild, member, shop.BY_ID["role_red"]))
    assert "ロールの管理" in str(exc.value) and not guild.created


def test_color_role_above_bot_role_is_rejected(monkeypatch):
    monkeypatch.setattr(discord.utils, "get", lambda seq, name: next((r for r in seq if r.name == name), None))
    high = FakeRole("ペリカ・赤", 20, 0xE74C3C)  # 位置が Bot より上で、動かせない

    async def stuck(**kw):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="x"), "no")

    high.edit = stuck
    guild, member = FakeGuild([high]), FakeMember([])
    with pytest.raises(shop_cog.ShopError) as exc:
        run(shop_cog.set_color_role(guild, member, shop.BY_ID["role_red"]))
    assert "Bot のロールより上" in str(exc.value) and not member.roles


# --------------------------- コマンドの流れ --------------------------- #
class FakeInteraction:
    def __init__(self, uid=1):
        self.user = SimpleNamespace(id=uid, display_name="テスト")
        self.guild = SimpleNamespace(id=10)
        self.sent = []
        self.response = SimpleNamespace(send_message=self._send, defer=self._defer)
        self.followup = SimpleNamespace(send=self._send)

    async def _send(self, *a, **kw):
        self.sent.append(kw)

    async def _defer(self, **kw):
        return None


@pytest.fixture
def cog(eco):
    from core.session import Sessions

    return shop_cog.ShopCog(SimpleNamespace(economy=eco, sessions=Sessions(cooldown=0)))


def test_cannot_buy_or_use_consumables_during_a_game(cog, eco):
    buy(eco, shop.INSURANCE)
    cog.bot.sessions.try_start(1)
    it = FakeInteraction()
    run(cog.buy_item(it, "title_rookie", 1))
    assert "遊んでいる" in it.sent[0]["embed"].description and "title_rookie" not in eco.inventory(1)
    it = FakeInteraction()
    run(cog.use_item(it, shop.INSURANCE))
    assert "ゲームの途中" in it.sent[0]["embed"].description
    assert eco.inventory(1)[shop.INSURANCE] == 1 and eco.get_profile(1)["insurance"] == 0  # 使われていない
    cog.bot.sessions.end(1)


def test_buy_and_use_flow(cog, eco):
    it = FakeInteraction()
    run(cog.buy_item(it, "title_rookie", 1))
    assert it.sent[-1]["ephemeral"] and "購入しました" in it.sent[-1]["embed"].title
    it = FakeInteraction()
    run(cog.use_item(it, "見習いギャンブラー"))  # 名前でも指定できる
    assert eco.get_profile(1)["title"] == "title_rookie"
    it = FakeInteraction()
    run(cog.use_item(it, "title_regular"))  # 持っていないものは使えない
    assert "持っていません" in it.sent[0]["embed"].title


def test_unknown_item(cog, eco):
    it = FakeInteraction()
    run(cog.buy_item(it, "なにこれ", 1))
    assert "見つかりません" in it.sent[0]["embed"].title


# ---------------- レビュー指摘（抜け道）に対する回帰テスト ---------------- #
def test_insurance_ticket_can_never_make_money(eco):
    """保険券は値段より多くは戻らない。最大額を賭けて負け続けても、買うほど増えることはない。"""
    price = shop.BY_ID[shop.INSURANCE].price
    assert shop.INSURANCE_CAP < price
    eco.credit(1, 1_000_000, "テスト")
    buy(eco, shop.INSURANCE)
    eco.arm(1, shop.INSURANCE)
    eid = eco.place_bet(1, 100_000, "コイン")  # 上限いっぱいの賭け
    _, _, insured = eco.settle_with_penalty(eid, 0, "コイン", 0)
    assert insured == shop.INSURANCE_CAP  # 賭け金の半分(50,000)ではなく上限で頭打ち
    assert insured < price  # 券の値段を超えて戻ることはない


def test_insurance_cap_does_not_reduce_small_refunds(eco):
    eco.credit(1, 10_000, "テスト")
    buy(eco, shop.INSURANCE)
    eco.arm(1, shop.INSURANCE)
    eid = eco.place_bet(1, 400, "テスト")
    assert eco.settle_with_penalty(eid, 0, "テスト", 0)[2] == 200


def test_daily_double_ticket_is_never_profitable():
    max_daily = Economy.DAILY_BASE + Economy.DAILY_STREAK_BONUS * Economy.DAILY_STREAK_CAP
    assert (
        max_daily <= shop.BY_ID[shop.DAILY_DOUBLE].price
    )  # 増える分(最大3,000)より券(3,000)のほうが、安くはない（儲からない）


def test_purchase_rejects_nonsense_quantities_and_prices(eco):
    for qty, price in ((0, 100), (-3, 100), (1, 0), (1, -500)):
        assert not eco.purchase(1, "x", "x", price, False, qty).ok
    assert eco.balance(1) == INITIAL_BALANCE and eco.inventory(1) == {}


def test_arm_only_works_for_real_consumables(eco):
    eco.credit(1, 10_000, "テスト")
    buy(eco, "title_rookie")
    assert not eco.arm(1, "title_rookie")  # 称号は消耗品ではない
    assert not eco.arm(1, "なんでもない")
    buy(eco, shop.INSURANCE)
    assert (
        eco.arm(1, shop.INSURANCE) and eco.get_profile(1)["insurance"] == 1 and eco.get_profile(1)["daily_boost"] == 0
    )


@pytest.fixture
def get_by_name(monkeypatch):
    monkeypatch.setattr(discord.utils, "get", lambda seq, name: next((r for r in seq if r.name == name), None))


def test_privileged_role_with_same_name_is_never_reused(get_by_name):
    """運営が同じ名前で作った、管理者権限つきのロールを購入者に付与してしまわない。"""
    admin_like = FakeRole("ペリカ・赤", 5, 0, perms=8)  # 管理者権限
    guild, member = FakeGuild([admin_like]), FakeMember([])
    with pytest.raises(shop_cog.ShopError) as exc:
        run(shop_cog.set_color_role(guild, member, shop.BY_ID["role_red"]))
    assert "権限を持っている" in str(exc.value)
    assert admin_like not in member.roles and not admin_like.edited and not guild.created  # 付与せず、位置も動かさない


def test_unrelated_role_with_the_prefix_is_not_removed(get_by_name):
    vip = FakeRole("ペリカ・VIP", 4, 0xFF0000, perms=0)  # 接頭辞が同じだけの別用途のロール
    guild, member = FakeGuild([vip]), FakeMember([vip])
    run(shop_cog.set_color_role(guild, member, shop.BY_ID["role_blue"]))
    assert vip in member.roles and any(r.name == "ペリカ・青" for r in member.roles)
    run(shop_cog.set_color_role(guild, member, None))
    assert vip in member.roles  # 外すときも、触らない


def test_privileged_same_name_role_is_not_removed_either(get_by_name):
    staff_role = FakeRole("ペリカ・青", 4, 0x3498DB, perms=8)  # 権限つき: Bot の管理対象ではない
    guild, member = FakeGuild([staff_role]), FakeMember([staff_role])
    run(shop_cog.set_color_role(guild, member, None))
    assert staff_role in member.roles


def test_picks_the_harmless_role_when_duplicates_exist(get_by_name):
    risky = FakeRole("ペリカ・赤", 5, 0, perms=8)
    plain = FakeRole("ペリカ・赤", 9, 0xE74C3C, perms=0)
    guild, member = FakeGuild([risky, plain]), FakeMember([])
    run(shop_cog.set_color_role(guild, member, shop.BY_ID["role_red"]))
    # 権限なしのロールがあるので、そちらを使う（権限つきの同名ロールは付けない）
    assert plain in member.roles and risky not in member.roles


def test_wrong_colour_on_existing_role_is_repaired(get_by_name):
    drifted = FakeRole("ペリカ・赤", 9, 0x123456, perms=0)  # 誰かが色を変えてしまった
    guild, member = FakeGuild([drifted]), FakeMember([])
    run(shop_cog.set_color_role(guild, member, shop.BY_ID["role_red"]))
    assert drifted.colour.value == 0xE74C3C


def test_concurrent_first_purchases_create_one_role(cog, get_by_name):
    """同時に2人が初めて同じ色を使っても、同名ロールが二重に作られない（サーバーごとの鍵）。"""
    guild = FakeGuild([])
    original = guild.create_role

    async def slow_create(name, colour, **kw):
        await asyncio.sleep(0.01)
        return await original(name, colour, **kw)

    guild.create_role = slow_create

    async def both():
        async def one(member):
            async with cog._role_lock(1):
                await shop_cog.set_color_role(guild, member, shop.BY_ID["role_green"])

        await asyncio.gather(one(FakeMember([])), one(FakeMember([])))

    run(both())
    assert len(guild.created) == 1


def test_purchase_holds_the_session_while_waiting_for_confirmation(cog, eco, monkeypatch):
    """確認ボタンの待ち時間に、ゲームを始めて残高を動かす抜け道がない。"""
    eco.credit(1, 200_000, "テスト")
    seen = {}

    async def fake_confirm(interaction, *a, **k):
        seen["active"] = cog.bot.sessions.is_active(1)  # 確認待ちの間は「プレイ中」
        seen["start_blocked"] = cog.bot.sessions.try_start(1) is not None  # この間はゲームを始められない
        return True

    monkeypatch.setattr(shop_cog, "confirm_action", fake_confirm)

    class It(FakeInteraction):
        def __init__(self):
            super().__init__()
            self.followup = SimpleNamespace(send=self._send)

    it = It()
    run(cog.buy_item(it, "col_sunglasses", 1))  # 100,000: 確認ボタンが出る価格
    assert seen == {"active": True, "start_blocked": True}
    assert "col_sunglasses" in eco.inventory(1)
    assert not cog.bot.sessions.is_active(1)  # 終わったら解除される


def test_use_prefers_the_owned_item_when_names_collide(cog, eco):
    eco.credit(1, 30_000, "テスト")
    buy(eco, "role_red")  # 色ロールの「赤」だけ持っている
    guild = FakeGuild([])
    guild.id = 10
    member = FakeMember([])
    member.id, member.display_name = 1, "テスト"
    it = FakeInteraction()
    it.guild, it.user = guild, member
    it.response.defer = lambda **k: asyncio.sleep(0)
    it.followup = SimpleNamespace(send=it._send)
    run(cog.use_item(it, "赤"))
    # 「赤」はカードの色にも同名があるが、持っている色ロールのほうを選ぶ（「持っていません」にならず、ロールが付く）
    assert [r.name for r in member.roles] == ["ペリカ・赤"]


def test_list_does_not_create_an_account(cog, eco):
    it = FakeInteraction(uid=77)
    cog._catalog_embed(it.guild.id, it.user.id)
    assert eco._conn.execute("SELECT COUNT(*) FROM users WHERE user_id=77").fetchone()[0] == 0


def test_buying_without_enough_pelica_shows_a_clear_message(cog, eco):
    it = FakeInteraction()
    run(cog.buy_item(it, "col_sunglasses", 1))  # 100,000: 初期の 3,000 では足りない
    embed = it.sent[-1]["embed"]
    assert "買えませんでした" in embed.title and "足りません" in embed.description and "100,000" in embed.description
    assert eco.balance(1) == INITIAL_BALANCE and eco.inventory(1) == {}
    assert not cog.bot.sessions.is_active(1)  # 失敗してもロックが残らない


# ----------------------------- 連続守り券 ----------------------------- #
def _days(eco, *days):
    eco.credit(1, 100_000, "テスト")
    return [eco.claim_daily(1, datetime.date(2026, 11, d)) for d in days]


def test_streak_guard_fills_missed_days_one_ticket_per_day(eco):
    _days(eco, 1, 2)  # 連続2日
    assert buy(eco, shop.STREAK_GUARD, qty=2).ok
    r = eco.claim_daily(1, datetime.date(2026, 11, 5))  # 3日・4日を逃した（2日ぶん）
    assert r.streak == 3 and r.guarded == 2
    assert eco.inventory(1).get(shop.STREAK_GUARD, 0) == 0


def test_streak_guard_not_used_when_tickets_are_short(eco):
    _days(eco, 1, 2)
    assert buy(eco, shop.STREAK_GUARD).ok
    r = eco.claim_daily(1, datetime.date(2026, 11, 5))  # 2日ぶん逃したが、券は1枚
    assert r.streak == 1 and r.guarded == 0
    assert eco.inventory(1)[shop.STREAK_GUARD] == 1  # 足りないときは1枚も使わない


def test_streak_guard_not_used_when_nothing_missed(eco):
    _days(eco, 1)
    assert buy(eco, shop.STREAK_GUARD).ok
    r = eco.claim_daily(1, datetime.date(2026, 11, 2))
    assert r.streak == 2 and r.guarded == 0 and eco.inventory(1)[shop.STREAK_GUARD] == 1


def test_streak_guard_price_and_use_message(cog, eco):
    assert shop.BY_ID[shop.STREAK_GUARD].price == 16_000
    eco.credit(1, 100_000, "テスト")
    buy(eco, shop.STREAK_GUARD)
    it = FakeInteraction()
    run(cog.use_item(it, shop.STREAK_GUARD))
    assert "ここでは使いません" in it.sent[0]["embed"].title and eco.inventory(1)[shop.STREAK_GUARD] == 1


# ----------------------------- 帝愛の競売 ----------------------------- #
from core import teiai  # noqa: E402

CARD = next(i.id for i in shop.CATALOG if i.kind == shop.CARD_COLOR and not i.limited)


def _seized(eco, uid=1):
    """uid が品物を買って、破産する。"""
    eco.credit(uid, 100_000, "テスト")
    assert buy(eco, shop.STREAK_GUARD, qty=2, uid=uid).ok
    assert buy(eco, "title_regular", uid=uid).ok  # 称号は、没収するだけで、競売に出さない
    assert buy(eco, CARD, uid=uid).ok
    eco._conn.execute("UPDATE users SET balance=0 WHERE user_id=?", (uid,))
    eco._owe(uid, 500, "テスト")
    assert eco.bankrupt(uid)[0]


def test_bankruptcy_lists_seized_items_but_not_collection_or_limited(eco):
    _seized(eco)
    listed = {e["item_id"]: e for e in eco.auction_list()}
    assert listed[shop.STREAK_GUARD]["qty"] == 2 and CARD in listed
    assert listed[shop.STREAK_GUARD]["price"] == round(16_000 * teiai.AUCTION_START)
    assert not any(
        shop.BY_ID[i].kind in (shop.COLLECTION, shop.TITLE) or shop.BY_ID[i].limited for i in listed
    )  # 称号・コレクション・限定品は出ない（称号は、そのまま没収）
    assert "title_regular" not in listed and "title_regular" not in eco.inventory(1)


def test_auction_price_falls_daily_and_stops_at_floor():
    day = 86400
    assert teiai.auction_rate(0, 0) == teiai.AUCTION_START
    assert round(teiai.auction_rate(0, day), 2) == 0.8
    assert teiai.auction_rate(0, 30 * day) == teiai.AUCTION_FLOOR > teiai.SEIZE_RATE


def test_auction_buy_moves_item_pays_bank_and_blocks_non_katagi(eco):
    _seized(eco, uid=1)
    eco.credit(2, 100_000, "テスト")
    cash_before = eco.bank_status()["cash"]
    res = eco.auction_buy(2, shop.STREAK_GUARD, qty=2)
    assert res.ok and eco.inventory(2)[shop.STREAK_GUARD] == 2
    assert eco.balance(2) == INITIAL_BALANCE + 100_000 - 2 * round(16_000 * teiai.AUCTION_START)
    assert not any(e["item_id"] == shop.STREAK_GUARD for e in eco.auction_list())
    assert eco.bank_status()["cash"] > cash_before  # 売上は帝愛の収入
    # 破産者（カタギでない）は、買えない。持っているものは買えない
    assert not eco.auction_buy(1, CARD).ok
    assert eco.auction_buy(2, CARD).ok and not eco.auction_buy(2, CARD).ok


def test_auction_buy_rejects_missing_or_unaffordable(eco):
    _seized(eco, uid=1)
    assert not eco.auction_buy(2, shop.STREAK_GUARD, qty=3).ok  # 在庫は2
    eco.balance(2)
    eco._conn.execute("UPDATE users SET balance=100 WHERE user_id=2")
    res = eco.auction_buy(2, CARD)
    assert not res.ok and "足りません" in res.reason  # 払えないときは、買えない


def test_auction_screen_and_buy_flow(cog, eco):
    _seized(eco, uid=1)
    eco.credit(2, 50_000, "テスト")
    view = shop_cog.ShopView(cog, 2, eco.guild_id if hasattr(eco, "guild_id") else 0, screen="auction")
    text = view.embed().fields[0].value
    assert "連続守り券" in text and "×2" in text
    it = FakeInteraction()
    it.user = SimpleNamespace(id=2)
    run(cog.buy_auction(it, CARD, 1))
    assert "競売で買いました" in it.sent[0]["embed"].title and CARD in eco.inventory(2)


# ----------------------------- 在庫（1人ずつ別々） ----------------------------- #
DRINK = shop.LABOR_DRINK
T0 = 1_800_000_000.0


def buy_at(eco, qty, now, uid=1):
    it = shop.BY_ID[DRINK]
    return eco.purchase(uid, it.id, shop.label(it), it.price, it.unique, qty, now)


def test_stock_math_is_pure_and_capped_at_the_max():
    rule = shop.STOCK_RULES[DRINK]
    assert shop.stock_now(rule, 3.0, T0, T0 + 3600) == pytest.approx(3.0 + rule.per_hour)  # 1時間で per_hour ぶん
    assert shop.stock_now(rule, 3.0, T0, T0 + 99 * 3600) == rule.max  # 上限まで
    assert shop.stock_now(rule, 3.0, T0, T0 - 100) == 3.0  # 時計が戻っても、減らない
    assert shop.next_refill_at(rule, float(rule.max), T0) is None  # 満タンなら、次の補充はない
    assert shop.next_refill_at(rule, 3.0, T0) == pytest.approx(T0 + 3600 / rule.per_hour)


def test_everyone_starts_with_a_full_stock_and_buying_reduces_only_their_own(eco):
    rule = shop.STOCK_RULES[DRINK]
    eco.credit(1, 100_000, "テスト")
    eco.credit(2, 100_000, "テスト")
    assert eco.shop_stock(1, DRINK, T0)["stock"] == rule.max and eco.shop_stock(2, DRINK, T0)["stock"] == rule.max
    assert buy_at(eco, 4, T0, 1).ok
    assert eco.shop_stock(1, DRINK, T0)["stock"] == rule.max - 4
    assert eco.shop_stock(2, DRINK, T0)["stock"] == rule.max  # 別の人の在庫は、減らない
    assert eco.shop_stock(1, "title_rookie", T0) is None  # 在庫のない品は、None


def test_buying_more_than_the_stock_is_refused_and_changes_nothing(eco):
    rule = shop.STOCK_RULES[DRINK]
    eco.credit(1, 100_000, "テスト")
    assert buy_at(eco, rule.max, T0).ok
    before = eco.balance(1)
    res = buy_at(eco, 1, T0)
    assert not res.ok and "在庫が足りません" in res.reason and "補充" in res.reason
    assert eco.balance(1) == before and eco.inventory(1)[DRINK] == rule.max
    res = buy_at(eco, rule.max + 1, T0 + 10 * 3600)  # 補充されて満タンでも、上限を超えては買えない
    assert not res.ok


def test_stock_refills_over_time_and_keeps_the_fraction(eco):
    rule = shop.STOCK_RULES[DRINK]
    eco.credit(1, 100_000, "テスト")
    assert buy_at(eco, rule.max, T0).ok  # 0にする
    half = 1800 / 3600 * rule.per_hour  # 30分ぶん
    assert eco.shop_stock(1, DRINK, T0 + 1800)["stock"] == int(half)
    s = eco.shop_stock(1, DRINK, T0 + 900)  # 15分: まだ整数に届かない（0.5個）
    assert s["stock"] == 0 and s["next_at"] == pytest.approx(T0 + 1800)
    assert buy_at(eco, 1, T0 + 1800).ok  # 30分たつと、1本買える
    assert eco.shop_stock(1, DRINK, T0 + 1800)["stock"] == 0
    assert eco.shop_stock(1, DRINK, T0 + 1800 + 3600 * 5)["stock"] == rule.max  # 買ってから5時間で満タン


def test_a_failed_purchase_does_not_use_up_the_stock(eco):
    eco.set_balance(1, 0, "テスト")
    res = buy_at(eco, 3, T0)  # ペリカが足りない
    assert not res.ok and "足りません" in res.reason
    assert eco.shop_stock(1, DRINK, T0)["stock"] == shop.STOCK_RULES[DRINK].max


def test_shop_screen_shows_the_players_own_stock(eco):
    from types import SimpleNamespace as NS

    eco.credit(1, 100_000, "テスト")
    buy_at(eco, 3, time.time())
    cog = shop_cog.ShopCog(NS(economy=NS(for_guild=lambda g: eco)))
    text = "\n".join(f.value for f in cog._catalog_embed(0, 1, "item").fields)
    assert "📦 在庫 7/10" in text


def test_titles_are_confiscated_not_auctioned_and_old_title_listings_are_purged(eco):
    _seized(eco)
    assert not any(shop.BY_ID[e["item_id"]].kind == shop.TITLE for e in eco.auction_list())
    with eco._lock, eco._conn:  # 古い版が出品していた称号
        eco._conn.execute(
            "INSERT INTO seized_items (guild_id, item_id, qty, listed_at) VALUES (?, ?, 1, 0)", (eco._g(), "title_zawa")
        )
        assert eco._purge_unlistable_auctions() == 1
    assert "title_zawa" not in {e["item_id"] for e in eco.auction_list()}
    assert shop.STREAK_GUARD in {e["item_id"] for e in eco.auction_list()}  # 出せる品は、残る
