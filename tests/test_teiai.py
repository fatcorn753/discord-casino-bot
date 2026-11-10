"""帝愛ファイナンス: 借金の天引き・ピンハネ・種銭・強制執行・身分・自己破産・ニックネーム。数字は core/teiai.py。"""

import asyncio
import datetime
import time
from types import SimpleNamespace

import pytest

from cogs import shop_cog
from core import shop, teiai
from core.economy import INITIAL_BALANCE, Economy
from core.session import Sessions

U, A, B = 1, 2, 3
DAY = 86400


@pytest.fixture
def eco(tmp_path):
    return Economy(str(tmp_path / "t.db"))


def indebt(eco, user_id=U, debt=10_000, balance=0):
    """借金のある人にする（種銭などの副作用なしに、状態だけ作る）。"""
    eco.balance(user_id)
    eco._conn.execute("UPDATE users SET debt=?, balance=? WHERE user_id=?", (debt, balance, user_id))
    eco._conn.commit()


def earn(eco, user_id, amount):
    eco._apply(user_id, amount, "収入", income=amount)
    eco._conn.commit()


def play(eco, user_id, bet, payout, penalty=0):
    eid = eco.place_bet(user_id, bet, "テスト")
    return eco.settle_with_penalty(eid, payout, "テスト", penalty)


# ----------------------------- 天引き ----------------------------- #
def test_only_the_profit_of_a_game_is_withheld_and_the_stake_comes_back_whole(eco):
    indebt(eco, debt=10_000, balance=5_000)
    net, _, _ = play(eco, U, 1_000, 3_000)  # 賭け金1,000が、3,000になって戻る: 純利益2,000
    assert net == 2_000
    assert eco.debt(U) == 10_000 - 600  # 純利益の3割（600）だけが、天引きされる
    assert eco.balance(U) == 5_000 - 1_000 + 3_000 - 600


def test_losses_and_stake_refunds_are_never_withheld(eco):
    indebt(eco, debt=10_000, balance=5_000)
    play(eco, U, 1_000, 0)  # 負け
    play(eco, U, 1_000, 1_000)  # 引き分け（賭け金が戻るだけ）
    assert eco.debt(U) == 10_000
    eid = eco.place_bet(U, 1_000, "テスト")
    eco.refund(eid)  # エラー時の返金
    assert eco.debt(U) == 10_000


def test_withholding_stops_when_the_debt_is_gone_and_a_non_debtor_keeps_everything(eco):
    indebt(eco, debt=100, balance=5_000)
    play(eco, U, 1_000, 3_000)
    assert eco.debt(U) == 0 and eco.balance(U) == 5_000 - 1_000 + 3_000 - 100  # 借金100の分だけ
    before = eco.balance(U)
    play(eco, U, 1_000, 3_000)
    assert eco.balance(U) == before + 2_000  # 借金がなければ、何も引かれない


def test_income_other_than_games_is_also_withheld_but_grants_and_loans_are_not(eco):
    indebt(eco, debt=10_000, balance=1_000)
    eco.credit(U, 1_000, "付与")
    assert eco.debt(U) == 10_000
    earn(eco, U, 1_000)  # デイリー・ミッション・クイズなどの収入
    assert eco.debt(U) == 9_700


# ----------------------------- デイリー ----------------------------- #
def test_daily_for_a_debtor_is_skimmed_then_withheld(eco):
    indebt(eco, debt=5_000, balance=4_000)
    result = eco.claim_daily(U, datetime.date(2026, 11, 1))
    assert (result.skimmed, result.repaid, result.seed) == (200, 240, 0)
    assert eco.debt(U) == 5_000 - 240  # ピンハネは、借金の元本を減らさない
    assert eco.balance(U) == 4_000 + 800 - 240


def test_daily_refills_the_seed_money_when_the_hand_is_below_the_limit(eco):
    indebt(eco, debt=5_000, balance=0)
    result = eco.claim_daily(U, datetime.date(2026, 11, 1))
    assert result.seed > 0 and eco.balance(U) == teiai.SEED  # 手元が枠まで補充される
    assert eco.debt(U) == 5_000 - 240 + result.seed


def test_daily_for_a_normal_player_is_untouched(eco):
    result = eco.claim_daily(U, datetime.date(2026, 11, 1))
    assert (result.skimmed, result.repaid, result.seed) == (0, 0, 0) and result.amount == Economy.DAILY_BASE


# ----------------------------- 強制執行と種銭 ----------------------------- #
def test_a_shortfall_seizes_crypto_then_bonds_before_becoming_debt(eco):
    eco.credit(U, 50_000, "テスト")
    eco.market_set(time.time(), 100.0)
    assert eco.crypto_buy(U, 2_000, 100.0).ok
    assert eco.bond_buy(U, "normal", 3_000, time.time())[0]
    eco._conn.execute("UPDATE users SET balance=0 WHERE user_id=?", (U,))
    owed = eco._owe(U, 5_000, "テスト")
    assert eco.crypto_holding(U)[0] == 0  # CSトークンは全部差し押さえ
    assert (
        eco._conn.execute("SELECT status FROM bonds WHERE user_id=?", (U,)).fetchone()[0] == "cancelled"
    )  # 通常債は強制解約
    assert eco.status(U) == teiai.UNDERGROUND
    assert 0 < owed < 5_000  # 差し押さえた分だけ、借金は減る
    notes = eco.take_notices(U)
    assert any("差し押さえ" in n for n in notes) and any("通常債" in n for n in notes)


def test_assets_are_kept_when_cash_covers_the_shortfall(eco):
    eco.credit(U, 50_000, "テスト")
    eco.market_set(time.time(), 100.0)
    assert eco.crypto_buy(U, 2_000, 100.0).ok
    cash = eco.balance(U)
    assert eco._owe(U, 1_000, "テスト") == 0 and eco.balance(U) == cash - 1_000
    assert eco.crypto_holding(U)[0] > 0 and eco.debt(U) == 0  # 現金で足りれば、証券には手を付けない


def test_a_debtor_with_an_empty_hand_gets_the_seed_money(eco):
    eco.balance(U)
    eco._conn.execute("UPDATE users SET balance=0 WHERE user_id=?", (U,))
    assert eco._owe(U, 2_000, "テスト") == 2_000
    assert eco.balance(U) == teiai.SEED and eco.debt(U) == 2_000 + teiai.SEED
    assert any("種銭" in n for n in eco.take_notices(U))


def test_a_shortfall_keeps_the_daily_streak(eco):
    eco.claim_daily(U, datetime.date(2026, 11, 1))
    eco.claim_daily(U, datetime.date(2026, 11, 2))
    assert eco.stats(U)["daily_streak"] == 2
    eco._conn.execute("UPDATE users SET balance=0 WHERE user_id=?", (U,))
    eco._owe(U, 500, "テスト")
    assert eco.stats(U)["daily_streak"] == 2  # 地下労働者に落ちても、連続ログインは途切れない


# ----------------------------- 返済 ----------------------------- #
def test_repay_debt_rules(eco):
    indebt(eco, debt=1_000, balance=400)
    assert eco.repay_debt(U, 2_000) == (False, "返す額が借金を超えています（借金: 1,000ペリカ）。")
    assert eco.repay_debt(U, 500) == (False, "残高が足りません（いまの残高: 400ペリカ）。")
    assert eco.repay_debt(U) == (True, 400) and eco.debt(U) == 600 and eco.balance(U) == 0
    assert eco.repay_debt(U)[0] is False  # 手元が空
    eco.credit(U, 5_000, "付与")
    assert eco.repay_debt(U) == (True, 600) and eco.debt(U) == 0
    assert eco.repay_debt(U) == (False, "帝愛への借金はありません。")


# ----------------------------- 身分と差し押さえ凍結 ----------------------------- #
def test_status_follows_debt_and_overdue_loans(eco):
    assert eco.status(U) == teiai.KATAGI and eco.status(999) == teiai.KATAGI  # 口座がなくても、カタギ
    indebt(eco, debt=10)
    assert eco.status(U) == teiai.UNDERGROUND
    eco._conn.execute("UPDATE users SET debt=0 WHERE user_id=?", (U,))
    assert eco.status(U) == teiai.KATAGI
    eco._conn.execute(
        "INSERT INTO loans (guild_id, lender_id, borrower_id, principal, rate_tenths, term_days, status, created, offer_expires, accepted_at, due, paid) "
        "VALUES (0, ?, ?, 100, 0, 5, 'overdue', 0, 0, 0, 1, 0)",
        (A, U),
    )
    assert eco.status(U) == teiai.UNDERGROUND


def test_equipment_is_frozen_while_not_a_katagi_and_comes_back_after(eco):
    eco.credit(U, 50_000, "テスト")
    item = shop.BY_ID["title_regular"]
    assert eco.purchase(U, item.id, item.name, item.price, True).ok
    assert eco.equip(U, "title", item.id)
    assert eco.get_profile(U)["title"] == item.id and eco.get_profile(U)["frozen"] is False
    eco._conn.execute("UPDATE users SET debt=100 WHERE user_id=?", (U,))
    frozen = eco.get_profile(U)
    assert frozen["title"] is None and frozen["frozen"] and frozen["status"] == teiai.UNDERGROUND  # 差し押さえ中
    assert eco.get_profile(U, raw=True)["title"] == item.id  # 記録は残っている
    eco.repay_debt(U)
    assert eco.get_profile(U)["title"] == item.id  # 完済でカタギに戻ると、使える


# ----------------------------- 自己破産 ----------------------------- #
def make_bankrupt_case(eco, debt=40_000):
    eco.credit(U, 3_000_000, "テスト")
    for item_id in ("title_lucky", "col_sunglasses", "col_beer"):
        item = shop.BY_ID[item_id]
        assert eco.purchase(U, item.id, item.name, item.price, True).ok
    eco.equip(U, "title", "title_lucky")
    eco._conn.execute("UPDATE users SET balance=1_000, debt=? WHERE user_id=?", (debt, U))
    eco.balance(A)
    eco._conn.execute(
        "INSERT INTO loans (guild_id, lender_id, borrower_id, principal, rate_tenths, term_days, status, created, offer_expires, accepted_at, due, paid) "
        "VALUES (0, ?, ?, 5000, 0, 5, 'active', 0, 0, ?, ?, 0)",
        (A, U, time.time(), time.time() + DAY),
    )
    eco._conn.commit()


SHOP_VALUE = int((20_000 + 100_000 + 13_500) * teiai.SEIZE_RATE)  # ショップの全品の換金（購入額の50%）


def test_bankruptcy_seizes_everything_and_pays_the_senior_then_the_junior_creditors(eco):
    make_bankrupt_case(eco, debt=40_000)
    ok, result = eco.bankrupt(U)
    assert ok
    pool = 1_000 + SHOP_VALUE  # 現金と、ショップの全品の換金
    assert result["seized"] == pool and result["shop_value"] == SHOP_VALUE
    assert (
        result["senior_paid"] == 40_000 and result["junior_paid"] == 5_000 and result["forgiven"] == 0
    )  # 帝愛が先、貸付が次
    assert eco.balance(A) == INITIAL_BALANCE + 5_000
    assert eco.inventory(U) == {} and eco.get_profile(U, raw=True)["title"] is None  # ショップは全部没収
    assert eco._conn.execute("SELECT status FROM loans WHERE borrower_id=?", (U,)).fetchone()[0] == "repaid"
    assert eco.status(U) == teiai.BANKRUPT and eco.debt(U) == 0
    assert eco.balance(U) == pool - 45_000  # 借金をすべて払って、あまった分は手元に残る
    assert (
        eco._conn.execute("SELECT blacklist_until FROM users WHERE user_id=?", (U,)).fetchone()[0] is not None
    )  # 完済済みなので、7日が始まる


def test_what_the_seized_assets_cannot_cover_is_compressed_or_forgiven(eco):
    make_bankrupt_case(eco, debt=400_000)
    ok, result = eco.bankrupt(U)
    assert ok and result["senior_paid"] == 1_000 + SHOP_VALUE and result["junior_paid"] == 0
    assert (
        result["forgiven"] == 5_000
        and eco._conn.execute("SELECT status FROM loans WHERE borrower_id=?", (U,)).fetchone()[0] == "discharged"
    )
    assert eco.balance(A) == INITIAL_BALANCE  # 貸した人は、貸し倒れ
    assert result["compressed"] == 400_000 - (1_000 + SHOP_VALUE) - teiai.BANKRUPT_FLOOR
    assert eco.debt(U) == teiai.BANKRUPT_FLOOR + teiai.SEED  # 圧縮のあと、手元が空なので種銭が加わる


def test_bankruptcy_keeps_the_senior_debt_up_to_the_floor_and_blocks_new_loans(eco):
    eco.balance(U)
    eco._conn.execute("UPDATE users SET balance=0, debt=500_000 WHERE user_id=?", (U,))
    ok, result = eco.bankrupt(U)
    assert ok and result["compressed"] == 500_000 - teiai.BANKRUPT_FLOOR
    assert eco.debt(U) == teiai.BANKRUPT_FLOOR + teiai.SEED  # 完全には消えない
    eco.balance(A)
    eco._conn.execute(
        "INSERT INTO loans (guild_id, lender_id, borrower_id, principal, rate_tenths, term_days, status, created, offer_expires) "
        "VALUES (0, ?, ?, 100, 0, 5, 'offer', 0, ?)",
        (A, U, time.time() + DAY),
    )
    assert "破産者" in eco.loan_accept(1, U, time.time())[1]  # ブラックリストの間は、借りられない
    assert eco.bankrupt(U) == (False, "すでに破産者です。")


def test_a_bankrupt_person_returns_to_katagi_seven_days_after_paying_off(eco):
    eco.balance(U)
    eco._conn.execute("UPDATE users SET balance=0, debt=20_000 WHERE user_id=?", (U,))
    eco.bankrupt(U)
    eco.credit(U, 100_000, "付与")
    assert eco.repay_debt(U)[0] and eco.debt(U) == 0
    row = eco._conn.execute("SELECT blacklist_until FROM users WHERE user_id=?", (U,)).fetchone()
    assert row[0] is not None  # 完済から7日が始まる
    assert eco.status(U) == teiai.BANKRUPT
    assert eco.status(U, now=time.time() + 6 * DAY) == teiai.BANKRUPT
    assert eco.status(U, now=time.time() + 8 * DAY) == teiai.KATAGI


def test_no_bankruptcy_without_any_debt(eco):
    eco.balance(U)
    assert eco.bankrupt(U)[0] is False


# ----------------------------- ニックネーム ----------------------------- #
class Role:
    def __init__(self, position):
        self.position = position

    def __ge__(self, other):
        return self.position >= other.position


class Member:
    def __init__(self, uid=U, nick_=None):
        self.id, self.nick, self.global_name, self.name = uid, nick_, "たつ", "tatu"
        self.top_role, self.edits = Role(1), []

    async def edit(self, **kw):
        self.edits.append(kw)
        self.nick = kw["nick"]


def guild_for(gid=0):
    return SimpleNamespace(
        id=gid,
        owner_id=999,
        me=SimpleNamespace(top_role=Role(10), guild_permissions=SimpleNamespace(manage_nicknames=True)),
    )


def make_cog(eco):
    return shop_cog.ShopCog(SimpleNamespace(economy=eco, sessions=Sessions(cooldown=0), get_guild=lambda g: None))


def test_status_tag_is_added_for_a_debtor_and_removed_after_paying_off(eco):
    cog, guild, member = make_cog(eco), guild_for(), Member(nick_="あだ名")
    indebt(eco, debt=500, balance=0)
    asyncio.run(cog.sync_status_nick(guild, member))
    assert member.nick == "あだ名【地下労働者】"  # 称号の表示を選んでいなくても、身分は付く
    assert eco.nick_managed(0, U) and not eco.nick_pref(0, U)
    asyncio.run(cog.sync_status_nick(guild, member))
    assert len(member.edits) == 1  # 変わらないなら、書き換えない
    eco._conn.execute("UPDATE users SET debt=0, balance=100 WHERE user_id=?", (U,))
    asyncio.run(cog.sync_status_nick(guild, member))
    assert member.nick == "あだ名" and not eco.nick_managed(0, U)  # 元のニックネームに戻り、記録も消える


def test_status_tag_restores_no_nickname_and_never_touches_a_normal_player(eco):
    cog, guild = make_cog(eco), guild_for()
    normal = Member(uid=B, nick_="ふつう")
    eco.balance(B)
    asyncio.run(cog.sync_status_nick(guild, normal))
    assert normal.edits == [] and normal.nick == "ふつう"  # カタギで、称号の表示も選んでいない人には触らない
    member = Member()
    indebt(eco, debt=500, balance=0)
    asyncio.run(cog.sync_status_nick(guild, member))
    assert member.nick == "たつ【地下労働者】"
    eco._conn.execute("UPDATE users SET debt=0 WHERE user_id=?", (U,))
    asyncio.run(cog.sync_status_nick(guild, member))
    assert member.nick is None  # もともとニックネームがなければ、なしに戻る


def test_status_replaces_the_title_tag_and_the_title_comes_back_for_those_who_chose_it(eco):
    eco.credit(U, 50_000, "テスト")
    item = shop.BY_ID["title_regular"]
    eco.purchase(U, item.id, item.name, item.price, True)
    eco.equip(U, "title", item.id)
    eco.set_nick_pref(0, U, True)  # 称号の表示を、自分で選んだ人
    cog, guild, member = make_cog(eco), guild_for(), Member()
    asyncio.run(cog.sync_status_nick(guild, member))
    assert member.nick == "たつ【常連】"
    eco._conn.execute("UPDATE users SET debt=500 WHERE user_id=?", (U,))
    asyncio.run(cog.sync_status_nick(guild, member))
    assert member.nick == "たつ【地下労働者】"  # 差し押さえ中は、称号ではなく身分
    eco._conn.execute("UPDATE users SET debt=0 WHERE user_id=?", (U,))
    asyncio.run(cog.sync_status_nick(guild, member))
    assert member.nick == "たつ【常連】" and eco.nick_pref(0, U)  # 完済すると、称号に戻る


def test_status_nick_targets_list_debtors_and_those_to_restore(eco):
    indebt(eco, user_id=U, debt=500)
    eco.balance(A)
    eco.ensure_nick_row(0, B)  # 身分だけで付けている人（あとで外すかもしれない）
    eco.set_nick_pref(0, A, True)  # 称号の表示を選んだだけの人は、対象にならない
    assert sorted(eco.status_nick_targets()) == [U, B]


def test_run_status_nick_skips_people_it_cannot_change_and_keeps_going(eco, monkeypatch):
    members = {U: Member(uid=U), A: Member(uid=A)}
    guild = guild_for()
    guild.owner_id = U  # U は、サーバーの持ち主（ニックネームを変えられない）
    guild.get_member = lambda uid: members.get(uid)
    cog = shop_cog.ShopCog(
        SimpleNamespace(economy=eco, sessions=Sessions(cooldown=0), get_guild=lambda g: None, guilds=[guild], config={})
    )
    monkeypatch.setattr("cogs.shop_cog.approval.is_allowed", lambda *a: True)
    indebt(eco, user_id=U, debt=500)
    indebt(eco, user_id=A, debt=500)
    asyncio.run(cog.run_status_nick())
    assert members[U].nick is None and members[A].nick == "たつ【地下労働者】"  # 持ち主は飛ばして、次の人へ進む


def test_old_nick_prefs_rows_are_treated_as_the_title_choice(tmp_path):
    path = str(tmp_path / "old.db")
    e = Economy(path, strict=True)
    e._conn.execute("DROP TABLE nick_prefs")
    e._conn.execute(
        "CREATE TABLE nick_prefs (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, original_nick TEXT, applied_nick TEXT, "
        "PRIMARY KEY (guild_id, user_id))"
    )
    e._conn.execute("INSERT INTO nick_prefs VALUES (10, 1, NULL, NULL)")
    e._conn.commit()
    e._conn.close()
    again = Economy(path, strict=True).for_guild(10)
    assert again.nick_pref(10, 1) is True  # 既存の行は、称号の表示を自分で選んだ人


# ----------------------------- 画面への反映 ----------------------------- #
def test_chinchiro_result_shows_what_was_seized_and_the_seed_money(tmp_path):
    from cogs import approval_cog  # noqa: F401  (コマンドの読み込みを確かめるため)
    from games import chinchiro as ch
    from play import chinchiro as play_ch
    from play import common

    eco = Economy(str(tmp_path / "t.db"))
    bot = SimpleNamespace(economy=eco, sessions=Sessions(cooldown=0), config={})
    eco.credit(U, 50_000, "テスト")
    eco.market_set(time.time(), 100.0)
    assert eco.crypto_buy(U, 20_000, 100.0).ok
    eco._conn.execute("UPDATE users SET balance=3000 WHERE user_id=?", (U,))
    eco._conn.commit()

    class It:
        user = SimpleNamespace(id=U, display_name="たろう")
        guild_id = 0
        response = SimpleNamespace(send_message=None)

    async def send(**kw):
        pass

    it = It()
    it.response = SimpleNamespace(send_message=send)
    ctx = asyncio.run(common.start_game(it, bot, 3_000, "チンチロ"))
    view = play_ch.ChinchiroView(ctx, It(), [((1, 1, 1), ch.classify((1, 1, 1)))])  # 親がピンゾロ（5倍払い）
    view.player_rolls = [((2, 2, 3), ch.classify((2, 2, 3)))]
    text = view.final_embed().description
    assert "CSトークンが差し押さえられました" in text  # 結果の画面に、差し押さえたものが出る
    assert eco.crypto_holding(U)[0] == 0 and eco.debt(U) == 0  # 12,000の倍払いは、CSトークンの差し押さえで足りた
    assert eco.balance(U) > 0 and eco.status(U) == teiai.KATAGI


def test_ranking_marks_people_who_are_not_katagi(eco):
    from core import ranking

    eco.balance(U)
    eco.balance(A)
    indebt(eco, user_id=A, debt=100, balance=900)
    text = ranking.build_embed(eco, None, "t").description
    assert f"<@{A}>🟡" in text and f"<@{U}>🟡" not in text


@pytest.mark.real_scope
def test_old_users_table_without_the_new_columns_is_upgraded(tmp_path):
    path = str(tmp_path / "old.db")
    e = Economy(path, strict=True)
    e._conn.execute("ALTER TABLE users RENAME TO users_new")
    e._conn.execute(
        "CREATE TABLE users (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, balance INTEGER NOT NULL, "
        "daily_streak INTEGER NOT NULL DEFAULT 0, last_daily TEXT, games_played INTEGER NOT NULL DEFAULT 0, "
        "total_wagered INTEGER NOT NULL DEFAULT 0, total_won INTEGER NOT NULL DEFAULT 0, biggest_win INTEGER NOT NULL DEFAULT 0, "
        "created_at REAL NOT NULL, last_slot INTEGER, debt INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (guild_id, user_id))"
    )
    e._conn.execute("INSERT INTO users (guild_id, user_id, balance, created_at, debt) VALUES (10, 1, 500, 0, 700)")
    e._conn.execute("DROP TABLE users_new")
    e._conn.commit()
    e._conn.close()
    again = Economy(path, strict=True).for_guild(10)
    assert again.debt(1) == 700 and again.balance(1) == 500  # 既存の借金は、そのまま帝愛の借金
    assert again.status(1) == teiai.UNDERGROUND
    assert again.repay_debt(1, 500) == (True, 500)


# ----------------------------- 債券の満期と借金 ----------------------------- #
def test_junk_bond_principal_goes_to_the_debt_when_it_matures_while_indebted(eco):
    eco.balance(U)
    assert eco.bond_buy(U, "junk", 1_000, 1.0)[0]  # 元本1,000・利息+20%
    eco._conn.execute("UPDATE users SET debt=5_000, balance=0 WHERE user_id=?", (U,))
    eco._conn.commit()

    class Never:
        def random(self):
            return 1.0  # 倒産しない

    eco.bond_settle(1e12, Never())
    # 償還 1,200 = 元本1,000 + 利息200。利息の3割（60）を天引き、元本は全額（1,000）を返済に充てる
    assert eco.debt(U) == 5_000 - 60 - 1_000 and eco.balance(U) == 1_200 - 60 - 1_000


def test_bond_maturity_without_debt_pays_out_whole(eco):
    eco.balance(U)
    assert eco.bond_buy(U, "junk", 1_000, 1.0)[0]
    before = eco.balance(U)

    class Never:
        def random(self):
            return 1.0

    eco.bond_settle(1e12, Never())
    assert eco.balance(U) == before + 1_200 and eco.debt(U) == 0


def test_bond_principal_pays_the_overdue_loan_when_there_is_no_senior_debt(eco, monkeypatch):
    monkeypatch.setattr("core.eco_loans.time.time", lambda: 1000 + 86400)  # 期限ちょうど（遅延損害金は、まだ付かない）
    eco.balance(U)
    assert eco.bond_buy(U, "junk", 1_000, 1.0)[0]
    eco.balance(A)
    eco._conn.execute(
        "INSERT INTO loans (guild_id, lender_id, borrower_id, principal, rate_tenths, term_days, status, created, offer_expires, accepted_at, due, paid) "
        "VALUES (0, ?, ?, 500, 0, 5, 'overdue', 0, 0, 1000, 1000 + 86400, 0)",
        (A, U),
    )
    eco._conn.commit()
    a_before = eco.balance(A)

    class Never:
        def random(self):
            return 1.0

    eco.bond_settle(1e12, Never())
    assert eco.balance(A) == a_before + 500  # 満期の元本から、延滞中の貸付が回収される


# ----------------------------- 破産の直後の反映 ----------------------------- #
def test_after_bankruptcy_removes_the_color_role_and_tags_the_nick(tmp_path):
    eco = Economy(str(tmp_path / "t.db"))
    bot = SimpleNamespace(economy=eco, sessions=Sessions(cooldown=0), config={}, guilds=[])
    cog = shop_cog.ShopCog(bot)
    calls = []

    async def fake_role(guild, member, item):
        calls.append(("role", item))

    async def fake_nick(guild, member):
        calls.append(("nick", member.id))

    import cogs.shop_cog as sc

    orig = sc.set_color_role
    sc.set_color_role = fake_role
    cog.sync_status_nick = fake_nick
    try:
        asyncio.run(cog.after_bankruptcy(SimpleNamespace(id=7), SimpleNamespace(id=U)))
    finally:
        sc.set_color_role = orig
    assert calls == [("role", None), ("nick", U)]


def test_after_bankruptcy_never_raises_on_discord_failures(tmp_path):
    eco = Economy(str(tmp_path / "t.db"))
    bot = SimpleNamespace(economy=eco, sessions=Sessions(cooldown=0), config={}, guilds=[])
    cog = shop_cog.ShopCog(bot)

    async def fail_nick(guild, member):
        raise shop_cog.ShopError("権限なし")

    import cogs.shop_cog as sc

    async def fail_role(guild, member, item):
        raise shop_cog.ShopError("権限なし")

    orig = sc.set_color_role
    sc.set_color_role = fail_role
    cog.sync_status_nick = fail_nick
    try:
        asyncio.run(cog.after_bankruptcy(SimpleNamespace(id=7), SimpleNamespace(id=U)))  # 例外にならない
    finally:
        sc.set_color_role = orig


# ----------------------------- 海外逃亡 ----------------------------- #
class _Roll:
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


def _in_debt(eco, debt=20_000):
    eco.credit(U, 10_000, "テスト")
    eco._conn.execute("UPDATE users SET balance=1_000, debt=? WHERE user_id=?", (debt, U))
    eco._conn.commit()


def test_escape_success_wipes_the_whole_debt_but_takes_everything(eco):
    _in_debt(eco)
    ok, r = eco.escape_attempt(U, now=NOW_E, rng=_Roll(0.0))
    assert ok and r["escaped"] and eco.debt(U) == 0 and eco.balance(U) == 0  # 種銭も貸さない
    assert eco.status(U) == teiai.BANKRUPT  # 破産者のブラックリスト
    assert r["bankrupt"]["compressed"] > teiai.BANKRUPT_FLOOR - 1  # 圧縮ではなく全額ゼロ


def test_escape_failure_adds_a_fine_and_starts_the_cooldown(eco):
    _in_debt(eco, 20_000)
    ok, r = eco.escape_attempt(U, now=NOW_E, rng=_Roll(0.99))
    assert ok and not r["escaped"] and r["fine"] == 2_000 and eco.debt(U) == 22_000
    assert eco.balance(U) == 1_000  # 失敗では、財産は取られない
    ok, reason = eco.escape_attempt(U, now=NOW_E + 3600, rng=_Roll(0.0))
    assert not ok and "次に試せる" in reason and eco.debt(U) == 22_000
    ok, r = eco.escape_attempt(U, now=NOW_E + teiai.ESCAPE_COOLDOWN_HOURS * 3600 + 1, rng=_Roll(0.0))
    assert ok and r["escaped"]


def test_escape_has_a_minimum_fine_and_a_minimum_debt(eco):
    _in_debt(eco, 5_000)
    ok, r = eco.escape_attempt(U, now=NOW_E, rng=_Roll(0.99))
    assert ok and r["fine"] == teiai.ESCAPE_FINE_MIN  # 500 ではなく、最低 1,000
    _in_debt(eco, 4_999)
    eco._conn.execute("UPDATE users SET escape_at=NULL WHERE user_id=?", (U,))
    ok, reason = eco.escape_attempt(U, now=NOW_E, rng=_Roll(0.0))
    assert not ok and "以上" in reason


def test_escape_success_rate_is_about_seven_percent(eco):
    import random

    rng = random.Random(1)
    wins = sum(rng.random() < teiai.ESCAPE_CHANCE for _ in range(20_000))
    assert 0.06 < wins / 20_000 < 0.08 and teiai.ESCAPE_CHANCE == 0.07


def test_escape_is_not_available_to_bankrupts(eco):
    _in_debt(eco)
    assert eco.escape_attempt(U, now=NOW_E, rng=_Roll(0.0))[0]
    ok, reason = eco.escape_attempt(U, now=NOW_E + 10 * 86400, rng=_Roll(0.0))
    assert not ok and "破産者" in reason


NOW_E = 1_000_000.0


# ----------------------------- 帝愛の利息（トイチ） ----------------------------- #
def _interest_clock(monkeypatch, now):
    monkeypatch.setattr("core.eco_debt.time.time", lambda: now[0])


def test_debt_grows_by_daily_interest_only_after_each_full_day(eco, monkeypatch):
    now = [1_800_000_000.0]
    _interest_clock(monkeypatch, now)
    indebt(eco, U, debt=5_000)
    assert eco.debt(U) == 5_000  # 借金ができた時点から数える
    now[0] += DAY - 60
    assert eco.debt(U) == 5_000  # 1日に満たない
    now[0] += 120
    assert eco.debt(U) == 5_000 + 50  # 1日ぶん: 日利1%
    now[0] += 3 * DAY
    assert eco.debt(U) == 5_050 + int(5_050 * 0.01 * 3)  # 3日ぶん。端数の時刻は、失われない
    assert any(
        "帝愛の利息" in r["reason"] for r in eco._conn.execute("SELECT reason FROM ledger WHERE user_id=?", (U,))
    )


def test_interest_per_settlement_is_capped_so_a_long_absence_does_not_explode(eco, monkeypatch):
    now = [1_800_000_000.0]
    _interest_clock(monkeypatch, now)
    indebt(eco, U, debt=2_000)
    eco.debt(U)
    now[0] += 90 * DAY  # 3か月来なかった
    assert eco.debt(U) == 2_000 + int(2_000 * 0.01 * teiai.DEBT_INTEREST_MAX_DAYS)  # 数えるのは10日分まで
    now[0] += 60  # 超えた日は捨てられていて、すぐ追加で付かない
    assert eco.debt(U) == 2_200


def test_interest_stops_just_before_the_bankruptcy_grace_line(eco, monkeypatch):
    now = [1_800_000_000.0]
    _interest_clock(monkeypatch, now)
    indebt(eco, U, debt=14_900)
    eco.debt(U)
    now[0] += 10 * DAY
    assert eco.debt(U) == teiai.DEBT_INTEREST_CEILING  # 猶予（GRACE_DEBT）の直前で止まる
    assert eco.grace_until(U) is None  # 利息のせいで、猶予のカウントダウンは始まらない
    now[0] += 10 * DAY
    assert eco.debt(U) == teiai.DEBT_INTEREST_CEILING


def test_a_paid_off_debt_forgets_the_interest_clock(eco, monkeypatch):
    now = [1_800_000_000.0]
    _interest_clock(monkeypatch, now)
    indebt(eco, U, debt=1_000, balance=5_000)
    eco.debt(U)
    now[0] += 2 * DAY
    assert eco.repay_debt(U, None)[0]  # 利息ぶんも含めて、完済
    assert eco.debt(U) == 0
    now[0] += 30 * DAY  # 長い間、借金なし
    eco._conn.execute("UPDATE users SET debt=500 WHERE user_id=?", (U,))  # 新しい借金
    assert eco.debt(U) == 500  # 昔の時刻から数えず、いまから
    now[0] += DAY + 1
    assert eco.debt(U) == 505


@pytest.mark.real_scope
def test_daily_skim_goes_to_the_house_account_and_other_servers_have_their_own(eco):
    indebt(eco, U, debt=5_000, balance=4_000)
    res = eco.claim_daily(U, datetime.date(2026, 11, 1))
    assert res.skimmed == 200
    assert eco.house_account() == {"balance": res.skimmed, "skimmed": res.skimmed}
    other = eco.for_guild(99)
    assert other.house_account() == {"balance": 0, "skimmed": 0}  # サーバーごとに別々
