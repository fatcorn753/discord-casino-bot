"""ショップの拡充: 高額称号・演出の変更品・コレクション。見た目だけが変わり、勝率と配当は変わらない。"""

import asyncio
from types import SimpleNamespace

import pytest

from cogs.casino import common
from cogs.economy.cog import EconomyCog
from cogs.shop.cog import SLOT_OF_KIND, UNEQUIP_NAMES, ShopCog, shop_slots
from domain import shop, skins
from infra.db.economy import Economy
from services.session import Sessions

U, G = 7, 10

pytestmark = pytest.mark.real_scope  # サーバーごとの記録を、本物の for_guild で扱う


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def eco(tmp_path):
    return Economy(str(tmp_path / "t.db"), strict=True).for_guild(G)


def own(eco, *item_ids, money=20_000_000):
    eco.credit(U, money, "テスト")
    for item_id in item_ids:
        item = shop.BY_ID[item_id]
        assert eco.purchase(U, item.id, shop.label(item), item.price, item.unique).ok


# ----------------------------- カタログ ----------------------------- #
def test_catalog_has_the_new_titles_collection_and_skins_with_the_agreed_prices():
    assert [shop.BY_ID[i].price for i in ("title_creditor", "title_fixer", "title_100oku")] == [
        500_000,
        2_000_000,
        10_000_000,
    ]
    assert [i.price for i in shop.of_kind(shop.COLLECTION)] == [
        13_500,
        25_000,
        50_000,
        100_000,
        250_000,
        500_000,
        1_000_000,
        3_000_000,
        10_000_000,
    ]
    assert {i.id: i.price for i in shop.of_kind(shop.SKIN_DICE)} == {
        "skin_kuro": 30_000,
        "skin_gold_dice": 150_000,
        "skin_glass_dice": 500_000,
    }
    assert {i.id: i.price for i in shop.of_kind(shop.SKIN_TABLE)} == {"skin_vip": 300_000, "skin_numa": 1_000_000}
    assert len({i.id for i in shop.CATALOG}) == len(shop.CATALOG)  # 商品の番号は重ならない
    assert all(len(shop.of_kind(k)) <= 25 for k in shop.KIND_LABEL)  # 選択メニューは25個まで
    assert all(i.value for i in shop.of_kind(shop.COLLECTION))  # コレクションは絵文字を持つ


def test_every_skin_item_has_a_look_and_every_look_has_an_item():
    assert skins.check_catalog() == []
    assert all(skin.games and skin.title_prefix and skin.win_line and skin.lose_line for skin in skins.SKINS.values())


def test_collection_value_is_the_sum_of_what_is_owned():
    owned, total = shop.collection_value({"col_beer": 1, "col_throne": 1, "title_rookie": 1, "col_gold_bar": 0})
    assert [i.id for i in owned] == ["col_beer", "col_throne"] and total == 13_500 + 10_000_000
    assert shop.collection_value({}) == ([], 0)


# ----------------------------- 装備 ----------------------------- #
def test_skins_are_equipped_into_two_slots_and_collection_is_not_equipped(eco):
    own(eco, "skin_kuro", "skin_vip", "col_beer")
    assert eco.equip(U, "skin_dice", "skin_kuro") and eco.equip(U, "skin_table", "skin_vip")
    prof = eco.get_profile(U)
    assert prof["skin_dice"] == "skin_kuro" and prof["skin_table"] == "skin_vip"
    assert not eco.equip(U, "skin_dice", "skin_gold_dice")  # 持っていないものは装備できない
    assert eco.equip(U, "skin_dice", None) and eco.get_profile(U)["skin_dice"] is None
    assert shop.COLLECTION not in SLOT_OF_KIND  # コレクションに、装備の枠はない
    assert "skin_dice" in shop_slots() and "skin_table" in shop_slots()
    assert set(UNEQUIP_NAMES) >= {"skin_dice", "skin_table"}


def test_using_a_collection_item_only_explains_and_a_skin_equips(eco):
    own(eco, "skin_gold_dice", "col_beer")
    sent = []

    class It:
        user = SimpleNamespace(id=U)
        guild = SimpleNamespace(id=G)
        guild_id = G
        response = SimpleNamespace(send_message=None)
        followup = SimpleNamespace(send=None)

    async def send(**kw):
        sent.append(kw["embed"].title + "|" + (kw["embed"].description or ""))

    it = It()
    it.response = SimpleNamespace(send_message=send, defer=send)
    cog = ShopCog(SimpleNamespace(economy=eco, sessions=Sessions(cooldown=0), config={}, get_guild=lambda g: None))
    cog.bot.economy = SimpleNamespace(for_guild=lambda g: eco)
    run(cog.use_item(it, "col_beer"))
    run(cog.use_item(it, "skin_gold_dice"))
    assert (
        "コレクション" in sent[0] and eco.get_profile(U)["skin_dice"] == "skin_gold_dice" and "装備しました" in sent[1]
    )


# ----------------------------- ゲームの見た目 ----------------------------- #
def active(eco, game, **equipped):
    prof = {"skin_dice": None, "skin_table": None, **equipped}
    return skins.active_skin(prof, game)


def test_a_skin_only_works_in_the_games_it_is_for(eco):
    assert active(eco, skins.CHINCHIRO, skin_dice="skin_gold_dice").item_id == "skin_gold_dice"
    assert active(eco, skins.PACHINKO, skin_table="skin_vip") and active(eco, skins.BLACKJACK, skin_table="skin_vip")
    assert (
        active(eco, skins.PACHINKO, skin_table="skin_numa")
        and active(eco, skins.BLACKJACK, skin_table="skin_numa") is None
    )  # 沼はパチンコだけ
    assert active(eco, skins.CHINCHIRO, skin_table="skin_vip") is None  # 別の枠のものは、チンチロには効かない
    assert active(eco, "スロット", skin_dice="skin_kuro", skin_table="skin_vip") is None
    assert active(eco, skins.CHINCHIRO) is None and skins.active_skin({}, skins.CHINCHIRO) is None


def make_bot(eco):
    return SimpleNamespace(economy=SimpleNamespace(for_guild=lambda g: eco), sessions=Sessions(cooldown=0), config={})


def play_ctx(eco, game, payout):
    bot = make_bot(eco)
    escrow = eco.place_bet(U, 1000, game)
    ctx = common.GameContext(bot, U, escrow, game, 1000, G, "たろう")
    ctx.skin = skins.active_skin(eco.get_profile(U), game)
    net = ctx.settle(payout)
    return ctx, net


def test_skin_changes_title_and_adds_one_line_but_never_the_payout(eco):
    own(eco, "skin_gold_dice")
    ctx, net = play_ctx(eco, skins.CHINCHIRO, 3000)
    assert net == 2000 and common.game_title("🎲 地下チンチロ", ctx) == "🎲 地下チンチロ（たろう）"  # 装備前
    base_body = common.result_lines(ctx, 3000, net)
    eco.equip(U, "skin_dice", "skin_gold_dice")
    ctx2, net2 = play_ctx(eco, skins.CHINCHIRO, 3000)
    assert net2 == net  # 配当は同じ
    assert common.game_title("🎲 地下チンチロ", ctx2) == "✨ 🎲 地下チンチロ（たろう）"
    body = common.result_lines(ctx2, 3000, net2)
    assert skins.SKINS["skin_gold_dice"].win_line in body and skins.SKINS["skin_gold_dice"].win_line not in base_body
    lose_ctx, lose_net = play_ctx(eco, skins.CHINCHIRO, 0)
    assert skins.SKINS["skin_gold_dice"].lose_line in common.result_lines(lose_ctx, 0, lose_net)
    draw_ctx, draw_net = play_ctx(eco, skins.CHINCHIRO, 1000)  # 引き分けは、一言なし
    draw_body = common.result_lines(draw_ctx, 1000, draw_net)
    assert (
        skins.SKINS["skin_gold_dice"].win_line not in draw_body
        and skins.SKINS["skin_gold_dice"].lose_line not in draw_body
    )


def test_start_game_reads_the_equipped_skin(eco):
    own(eco, "skin_vip")
    eco.equip(U, "skin_table", "skin_vip")
    bot = make_bot(eco)
    it = SimpleNamespace(user=SimpleNamespace(id=U, display_name="たろう"), guild_id=G, response=SimpleNamespace())

    async def send(**kw):
        raise AssertionError("始められるはず")

    it.response.send_message = send
    ctx = run(common.start_game(it, bot, 1000, "パチンコ"))
    assert ctx is not None and ctx.skin.item_id == "skin_vip"
    ctx.abort()
    ctx2 = run(common.start_game(it, bot, 1000, "チンチロ"))  # チンチロ用の枠は空
    assert ctx2 is not None and ctx2.skin is None
    ctx2.abort()


# ----------------------------- 残高カード・移行 ----------------------------- #
def test_balance_card_lists_the_collection_with_its_value(tmp_path):
    eco = Economy(str(tmp_path / "t.db"), strict=True)
    g = eco.for_guild(G)
    own(g, "col_beer", "col_gold_bar")
    cog = EconomyCog(SimpleNamespace(economy=eco, sessions=Sessions(cooldown=0), config={}))
    sent = []

    async def send(*args, **kw):
        sent.append(kw)

    member = SimpleNamespace(
        id=U, display_name="テスト", display_avatar=SimpleNamespace(url="https://example.com/a.png")
    )
    it = SimpleNamespace(user=member, guild=SimpleNamespace(id=G), response=SimpleNamespace(send_message=send))
    run(EconomyCog.balance.callback(cog, it, None))
    fields = {f.name: f.value for f in sent[0]["embed"].fields}
    key = next(k for k in fields if k.startswith("🏆 コレクション"))
    assert "1,013,500" in key and fields[key] == "🍺 🪙"


def test_old_profile_table_without_skin_columns_is_upgraded(tmp_path):
    path = str(tmp_path / "old.db")
    e = Economy(path, strict=True)
    e._conn.execute("DROP TABLE profile")
    e._conn.execute(
        "CREATE TABLE profile (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, title TEXT, card_color TEXT, "
        "card_icon TEXT, daily_boost INTEGER NOT NULL DEFAULT 0, insurance INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY (guild_id, user_id))"
    )
    e._conn.execute("INSERT INTO profile (guild_id, user_id, title) VALUES (10, 7, 'title_rookie')")
    e._conn.commit()
    e._conn.close()
    again = Economy(path, strict=True).for_guild(G)
    prof = again.get_profile(U)
    assert (
        prof["title"] == "title_rookie" and prof["skin_dice"] is None and prof["skin_table"] is None
    )  # 既存の装備は残る
    again._conn.execute("UPDATE profile SET skin_dice='skin_kuro' WHERE user_id=7")  # 新しい列に書ける


def test_pachinko_banner_comes_from_the_skin_only_for_big_hits():
    from cogs.casino import pachinko as play_pa

    big = pachinko_spin((10,))
    huge = pachinko_spin((30, 2))
    small = pachinko_spin((2,))
    vip, numa = skins.SKINS["skin_vip"], skins.SKINS["skin_numa"]
    assert play_pa.banner_for(10, None) == play_pa.BANNER[10] and play_pa.banner_for(30, None) == play_pa.BANNER[30]
    assert play_pa.banner_for(10, vip) == vip.banner and play_pa.banner_for(30, numa) == numa.banner
    assert play_pa.banner_for(2, vip) == ""  # 小当たりには、見出しを付けない
    assert (
        vip.banner in play_pa.reveal_frames(big, skin=vip)[0].text
        and numa.banner in play_pa.reveal_frames(huge, skin=numa)[0].text
    )
    assert "黒薔薇" not in play_pa.reveal_frames(big)[0].text  # 装備していなければ、いつもの見出し
    assert "当たり" in play_pa.reveal_frames(small, skin=vip)[0].text  # 小当たりは、いつもの見出し


def pachinko_spin(rounds):
    from domain.games import pachinko

    return pachinko.SpinInfo("", "", True, rounds, 0.1)
