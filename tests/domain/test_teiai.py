"""帝愛ファイナンス: 借金の天引き・ピンハネ・種銭・強制執行・身分・自己破産・ニックネーム。数字は domain/teiai.py。"""

import asyncio
import datetime
import time
from types import SimpleNamespace

import pytest

from cogs.shop import cog as shop_cog
from domain import shop, teiai
from domain.types import DAILY_BASE, INITIAL_BALANCE
from infra.db.repositories import open_repositories
from tests.fakes import as_guild, as_interaction, as_member, freeze_time, must
from tests.support import (
    give_test_kyosai_units,
    insert_test_loan,
    make_bond_cog,
    make_casino_deps,
    make_ranking_repos,
    make_shop_cog,
    make_test_bot,
    set_test_balance,
    set_test_debt,
    set_test_grace,
)

U, A, B = 1, 2, 3
DAY = 86400


@pytest.fixture
def eco(tmp_path):
    return open_repositories(str(tmp_path / "t.db"))


def indebt(eco, user_id=U, debt=10_000, balance=0):
    """借金のある人にする（種銭などの副作用なしに、状態だけ作る）。"""
    eco.wallet.balance(user_id)
    with eco.db.tx():
        set_test_balance(eco, user_id, balance)
        set_test_debt(eco, user_id, debt)


def earn(eco, user_id, amount):
    with eco.db.tx():
        eco.account.apply(0, user_id, amount, "収入", income=amount)


def play(eco, user_id, bet, payout, penalty=0):
    eid = eco.escrow.place_bet(user_id, bet, "テスト")
    return eco.escrow.settle_with_penalty(eid, payout, "テスト", penalty)


# ----------------------------- 天引き ----------------------------- #
def test_only_the_profit_of_a_game_is_withheld_and_the_stake_comes_back_whole(eco):
    indebt(eco, debt=10_000, balance=5_000)
    net, _, _ = play(eco, U, 1_000, 3_000)  # 賭け金1,000が、3,000になって戻る: 純利益2,000
    assert net == 2_000
    assert eco.debt.debt(U) == 10_000 - 600  # 純利益の3割（600）だけが、天引きされる
    assert eco.wallet.balance(U) == 5_000 - 1_000 + 3_000 - 600


def test_losses_and_stake_refunds_are_never_withheld(eco):
    indebt(eco, debt=10_000, balance=5_000)
    play(eco, U, 1_000, 0)  # 負け
    play(eco, U, 1_000, 1_000)  # 引き分け（賭け金が戻るだけ）
    assert eco.debt.debt(U) == 10_000
    eid = eco.escrow.place_bet(U, 1_000, "テスト")
    eco.escrow.refund(eid)  # エラー時の返金
    assert eco.debt.debt(U) == 10_000


def test_withholding_stops_when_the_debt_is_gone_and_a_non_debtor_keeps_everything(eco):
    indebt(eco, debt=100, balance=5_000)
    play(eco, U, 1_000, 3_000)
    assert eco.debt.debt(U) == 0 and eco.wallet.balance(U) == 5_000 - 1_000 + 3_000 - 100  # 借金100の分だけ
    before = eco.wallet.balance(U)
    play(eco, U, 1_000, 3_000)
    assert eco.wallet.balance(U) == before + 2_000  # 借金がなければ、何も引かれない


def test_income_other_than_games_is_also_withheld_but_grants_and_loans_are_not(eco):
    indebt(eco, debt=10_000, balance=1_000)
    eco.wallet.credit(U, 1_000, "付与")
    assert eco.debt.debt(U) == 10_000
    earn(eco, U, 1_000)  # デイリー・ミッション・クイズなどの収入
    assert eco.debt.debt(U) == 9_700


# ----------------------------- デイリー ----------------------------- #
def test_daily_for_a_debtor_is_skimmed_then_withheld(eco):
    indebt(eco, debt=5_000, balance=4_000)
    result = eco.wallet.claim_daily(U, datetime.date(2026, 11, 1))
    assert (result.skimmed, result.repaid, result.seed) == (200, 240, 0)
    assert eco.debt.debt(U) == 5_000 - 240  # ピンハネは、借金の元本を減らさない
    assert eco.wallet.balance(U) == 4_000 + 800 - 240


def test_daily_refills_the_seed_money_when_the_hand_is_below_the_limit(eco):
    indebt(eco, debt=5_000, balance=0)
    result = eco.wallet.claim_daily(U, datetime.date(2026, 11, 1))
    assert result.seed > 0 and eco.wallet.balance(U) == teiai.SEED  # 手元が枠まで補充される
    assert eco.debt.debt(U) == 5_000 - 240 + result.seed


def test_daily_for_a_normal_player_is_untouched(eco):
    result = eco.wallet.claim_daily(U, datetime.date(2026, 11, 1))
    assert (result.skimmed, result.repaid, result.seed) == (0, 0, 0) and result.amount == DAILY_BASE


# ----------------------------- 強制執行と種銭 ----------------------------- #
def test_a_shortfall_seizes_crypto_then_bonds_before_becoming_debt(eco):
    eco.wallet.credit(U, 50_000, "テスト")
    eco.market.market_set(time.time(), 100.0)
    assert eco.crypto.crypto_buy(U, 2_000, 100.0).ok
    assert eco.bonds.bond_buy(U, "normal", 3_000, time.time())[0]
    with eco.db.tx():
        set_test_balance(eco, U, 0)
        owed = eco.debt.owe_for(0, U, 5_000, "テスト")
    assert eco.crypto.crypto_holding(U)[0] == 0  # CSトークンは全部差し押さえ
    assert (
        eco.db.conn.execute("SELECT status FROM bonds WHERE user_id=?", (U,)).fetchone()[0] == "cancelled"
    )  # 通常債は強制解約
    assert eco.debt.status(U) == teiai.UNDERGROUND
    assert 0 < owed < 5_000  # 差し押さえた分だけ、借金は減る
    notes = eco.notices.take_notices(U)
    assert any(n.code == "seize_spot" for n in notes) and any(n.code == "seize_bond" for n in notes)


def test_assets_are_kept_when_cash_covers_the_shortfall(eco):
    eco.wallet.credit(U, 50_000, "テスト")
    eco.market.market_set(time.time(), 100.0)
    assert eco.crypto.crypto_buy(U, 2_000, 100.0).ok
    cash = eco.wallet.balance(U)
    with eco.db.tx():
        owed = eco.debt.owe_for(0, U, 1_000, "テスト")
    assert owed == 0 and eco.wallet.balance(U) == cash - 1_000
    assert eco.crypto.crypto_holding(U)[0] > 0 and eco.debt.debt(U) == 0  # 現金で足りれば、証券には手を付けない


def test_a_debtor_with_an_empty_hand_gets_the_seed_money(eco):
    eco.wallet.balance(U)
    with eco.db.tx():
        set_test_balance(eco, U, 0)
        owed = eco.debt.owe_for(0, U, 2_000, "テスト")
    assert owed == 2_000
    assert eco.wallet.balance(U) == teiai.SEED and eco.debt.debt(U) == 2_000 + teiai.SEED
    assert any(n.code == "seed_loan" for n in eco.notices.take_notices(U))


def test_a_shortfall_keeps_the_daily_streak(eco):
    eco.wallet.claim_daily(U, datetime.date(2026, 11, 1))
    eco.wallet.claim_daily(U, datetime.date(2026, 11, 2))
    assert eco.wallet.stats(U)["daily_streak"] == 2
    with eco.db.tx():
        set_test_balance(eco, U, 0)
        eco.debt.owe_for(0, U, 500, "テスト")
    assert eco.wallet.stats(U)["daily_streak"] == 2  # 地下労働者に落ちても、連続ログインは途切れない


# ----------------------------- 返済 ----------------------------- #
def test_repay_debt_rules(eco):
    indebt(eco, debt=1_000, balance=400)
    assert eco.debt.repay_debt(U, 2_000)[1].code == "repay_exceeds_debt"
    assert eco.debt.repay_debt(U, 500)[1].code == "insufficient_balance"
    assert eco.debt.repay_debt(U) == (True, 400) and eco.debt.debt(U) == 600 and eco.wallet.balance(U) == 0
    assert eco.debt.repay_debt(U)[0] is False  # 手元が空
    eco.wallet.credit(U, 5_000, "付与")
    assert eco.debt.repay_debt(U) == (True, 600) and eco.debt.debt(U) == 0
    assert eco.debt.repay_debt(U)[1].code == "repay_no_debt"


# ----------------------------- 身分と差し押さえ凍結 ----------------------------- #
def test_status_follows_debt_and_overdue_loans(eco):
    assert eco.debt.status(U) == teiai.KATAGI and eco.debt.status(999) == teiai.KATAGI  # 口座がなくても、カタギ
    indebt(eco, debt=10)
    assert eco.debt.status(U) == teiai.UNDERGROUND
    with eco.db.tx():
        set_test_debt(eco, U, 0)
    assert eco.debt.status(U) == teiai.KATAGI
    with eco.db.tx():
        insert_test_loan(eco, A, U, 100, status="overdue", accepted_at=0.0, due=1.0)
    assert eco.debt.status(U) == teiai.UNDERGROUND


def test_equipment_is_frozen_while_not_a_katagi_and_comes_back_after(eco):
    eco.wallet.credit(U, 50_000, "テスト")
    item = shop.BY_ID["title_regular"]
    assert eco.shop.purchase(U, item.id, item.name, item.price, True).ok
    assert eco.shop.equip(U, "title", item.id)
    assert eco.shop.get_profile(U)["title"] == item.id and eco.shop.get_profile(U)["frozen"] is False
    with eco.db.tx():
        set_test_debt(eco, U, 100)
    frozen = eco.shop.get_profile(U)
    assert frozen["title"] is None and frozen["frozen"] and frozen["status"] == teiai.UNDERGROUND  # 差し押さえ中
    assert eco.shop.get_profile(U, raw=True)["title"] == item.id  # 記録は残っている
    eco.debt.repay_debt(U)
    assert eco.shop.get_profile(U)["title"] == item.id  # 完済でカタギに戻ると、使える


# ----------------------------- 自己破産 ----------------------------- #
def make_bankrupt_case(eco, debt=40_000):
    eco.wallet.credit(U, 3_000_000, "テスト")
    for item_id in ("title_lucky", "col_sunglasses", "col_beer"):
        item = shop.BY_ID[item_id]
        assert eco.shop.purchase(U, item.id, item.name, item.price, True).ok
    eco.shop.equip(U, "title", "title_lucky")
    eco.wallet.balance(A)
    with eco.db.tx():
        set_test_balance(eco, U, 1_000)
        set_test_debt(eco, U, debt)
        insert_test_loan(eco, A, U, 5000, status="active", accepted_at=time.time(), due=time.time() + DAY)


SHOP_VALUE = int((20_000 + 100_000 + 13_500) * teiai.SEIZE_RATE)  # ショップの全品の換金（購入額の50%）


def test_bankruptcy_writes_off_unsellable_bonds_at_the_seize_rate(eco):
    """売れない債券（ジャンク債）は、元本の SEIZE_RATE で換金して、解約済みにする。"""
    eco.wallet.credit(U, 50_000, "テスト")
    assert eco.bonds.bond_buy(U, "junk", 1_000, time.time())[0]
    indebt(eco, debt=40_000, balance=0)
    ok, result = eco.debt.bankrupt(U)
    assert ok and result["seized"] == int(1_000 * teiai.SEIZE_RATE)
    assert eco.db.conn.execute("SELECT status FROM bonds WHERE user_id=?", (U,)).fetchone()[0] == "cancelled"
    reasons = [r[0] for r in eco.db.conn.execute("SELECT reason FROM ledger WHERE user_id=?", (U,))]
    assert "破産:債券の差し押さえ" in reasons


def test_bankruptcy_seizes_everything_and_pays_the_senior_then_the_junior_creditors(eco):
    make_bankrupt_case(eco, debt=40_000)
    ok, result = eco.debt.bankrupt(U)
    assert ok
    pool = 1_000 + SHOP_VALUE  # 現金と、ショップの全品の換金
    assert result["seized"] == pool and result["shop_value"] == SHOP_VALUE
    assert (
        result["senior_paid"] == 40_000 and result["junior_paid"] == 5_000 and result["forgiven"] == 0
    )  # 帝愛が先、貸付が次
    assert eco.wallet.balance(A) == INITIAL_BALANCE + 5_000
    assert eco.shop.inventory(U) == {} and eco.shop.get_profile(U, raw=True)["title"] is None  # ショップは全部没収
    assert eco.db.conn.execute("SELECT status FROM loans WHERE borrower_id=?", (U,)).fetchone()[0] == "repaid"
    assert eco.debt.status(U) == teiai.KATAGI and eco.debt.debt(U) == 0  # 借金がなければ、身分は破産者でない
    assert eco.debt.loan_blacklisted(U)  # 貸付のブラックリストは、完済から7日続く
    assert eco.wallet.balance(U) == pool - 45_000  # 借金をすべて払って、あまった分は手元に残る
    assert (
        eco.db.conn.execute("SELECT blacklist_until FROM teiai_bankruptcies WHERE user_id=?", (U,)).fetchone()[0]
        is not None
    )  # 完済済みなので、7日が始まる


def test_what_the_seized_assets_cannot_cover_is_compressed_or_forgiven(eco):
    make_bankrupt_case(eco, debt=400_000)
    ok, result = eco.debt.bankrupt(U)
    assert ok and result["senior_paid"] == 1_000 + SHOP_VALUE and result["junior_paid"] == 0
    assert (
        result["forgiven"] == 5_000
        and eco.db.conn.execute("SELECT status FROM loans WHERE borrower_id=?", (U,)).fetchone()[0] == "discharged"
    )
    assert eco.wallet.balance(A) == INITIAL_BALANCE  # 貸した人は、貸し倒れ
    assert result["compressed"] == 400_000 - (1_000 + SHOP_VALUE) - teiai.BANKRUPT_FLOOR
    assert result["seed"] == 0
    assert eco.debt.debt(U) == teiai.BANKRUPT_FLOOR  # 圧縮のあと、破産直後の種銭は貸されない


def test_bankruptcy_keeps_the_senior_debt_up_to_the_floor_and_blocks_new_loans(eco):
    eco.wallet.balance(U)
    with eco.db.tx():
        set_test_balance(eco, U, 0)
        set_test_debt(eco, U, 500_000)
    ok, result = eco.debt.bankrupt(U)
    assert ok and result["compressed"] == 500_000 - teiai.BANKRUPT_FLOOR
    assert eco.debt.debt(U) == teiai.BANKRUPT_FLOOR  # 完全には消えず、破産直後の種銭も貸されない
    eco.wallet.balance(A)
    with eco.db.tx():
        insert_test_loan(eco, A, U, 100, status="offer", offer_expires=time.time() + DAY)
    assert eco.loans.loan_accept(1, U, time.time())[1].code == "loan_blacklisted"  # ブラックリストの間は、借りられない
    assert eco.debt.bankrupt(U)[1].code == "bankrupt_already"


def test_a_bankrupt_person_returns_to_katagi_when_the_debt_is_paid_but_the_loan_blacklist_lasts_seven_days(eco):
    eco.wallet.balance(U)
    with eco.db.tx():
        set_test_balance(eco, U, 0)
        set_test_debt(eco, U, 20_000)
    eco.debt.bankrupt(U)
    assert eco.debt.status(U) == teiai.BANKRUPT  # 借金が残っている間は、破産者
    eco.wallet.credit(U, 100_000, "付与")
    assert eco.debt.repay_debt(U)[0] and eco.debt.debt(U) == 0
    row = eco.db.conn.execute("SELECT blacklist_until FROM teiai_bankruptcies WHERE user_id=?", (U,)).fetchone()
    assert row[0] is not None  # 完済から7日が始まる
    assert eco.debt.status(U) == teiai.KATAGI  # 身分は、完済ですぐ、カタギに戻る
    assert eco.debt.loan_blacklisted(U) and eco.debt.loan_blacklisted(
        U, now=time.time() + 6 * DAY
    )  # 貸付は、7日たつまで借りられない
    assert not eco.debt.loan_blacklisted(U, now=time.time() + 8 * DAY)


def test_a_bankrupt_person_with_debt_left_is_still_blacklisted_for_loans_however_long_it_takes(eco):
    eco.wallet.balance(U)
    with eco.db.tx():
        set_test_balance(eco, U, 0)
        set_test_debt(eco, U, 500_000)
    eco.debt.bankrupt(U)
    assert eco.debt.debt(U) > 0 and eco.debt.status(U) == teiai.BANKRUPT
    assert eco.debt.loan_blacklisted(U, now=time.time() + 365 * DAY)  # 借金が残っている間は、期間なしで続く


def test_no_bankruptcy_without_any_debt(eco):
    eco.wallet.balance(U)
    assert eco.debt.bankrupt(U)[0] is False


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
    return make_shop_cog(make_test_bot(repos=eco))


def test_status_tag_is_added_for_a_debtor_and_removed_after_paying_off(eco):
    cog, guild, member = make_cog(eco), guild_for(), Member(nick_="あだ名")
    indebt(eco, debt=500, balance=0)
    asyncio.run(cog.sync_status_nick(as_guild(guild), as_member(member)))
    assert member.nick == "あだ名【地下労働者】"  # 称号の表示を選んでいなくても、身分は付く
    assert eco.guild.nick_managed(0, U) and not eco.guild.nick_pref(0, U)
    asyncio.run(cog.sync_status_nick(as_guild(guild), as_member(member)))
    assert len(member.edits) == 1  # 変わらないなら、書き換えない
    with eco.db.tx():
        set_test_debt(eco, U, 0)
        set_test_balance(eco, U, 100)
    asyncio.run(cog.sync_status_nick(as_guild(guild), as_member(member)))
    assert member.nick == "あだ名" and not eco.guild.nick_managed(0, U)  # 元のニックネームに戻り、記録も消える


def test_status_tag_restores_no_nickname_and_never_touches_a_normal_player(eco):
    cog, guild = make_cog(eco), guild_for()
    normal = Member(uid=B, nick_="ふつう")
    eco.wallet.balance(B)
    asyncio.run(cog.sync_status_nick(as_guild(guild), as_member(normal)))
    assert normal.edits == [] and normal.nick == "ふつう"  # カタギで、称号の表示も選んでいない人には触らない
    member = Member()
    indebt(eco, debt=500, balance=0)
    asyncio.run(cog.sync_status_nick(as_guild(guild), as_member(member)))
    assert member.nick == "たつ【地下労働者】"
    with eco.db.tx():
        set_test_debt(eco, U, 0)
    asyncio.run(cog.sync_status_nick(as_guild(guild), as_member(member)))
    assert member.nick is None  # もともとニックネームがなければ、なしに戻る


def test_status_replaces_the_title_tag_and_the_title_comes_back_for_those_who_chose_it(eco):
    eco.wallet.credit(U, 50_000, "テスト")
    item = shop.BY_ID["title_regular"]
    eco.shop.purchase(U, item.id, item.name, item.price, True)
    eco.shop.equip(U, "title", item.id)
    eco.guild.set_nick_pref(0, U, True)  # 称号の表示を、自分で選んだ人
    cog, guild, member = make_cog(eco), guild_for(), Member()
    asyncio.run(cog.sync_status_nick(as_guild(guild), as_member(member)))
    assert member.nick == "たつ【常連】"
    with eco.db.tx():
        set_test_debt(eco, U, 500)
    asyncio.run(cog.sync_status_nick(as_guild(guild), as_member(member)))
    assert member.nick == "たつ【地下労働者】"  # 差し押さえ中は、称号ではなく身分
    with eco.db.tx():
        set_test_debt(eco, U, 0)
    asyncio.run(cog.sync_status_nick(as_guild(guild), as_member(member)))
    assert member.nick == "たつ【常連】" and eco.guild.nick_pref(0, U)  # 完済すると、称号に戻る


def test_status_nick_targets_list_debtors_and_those_to_restore(eco):
    indebt(eco, user_id=U, debt=500)
    eco.wallet.balance(A)
    eco.guild.ensure_nick_row(0, B)  # 身分だけで付けている人（あとで外すかもしれない）
    eco.guild.set_nick_pref(0, A, True)  # 称号の表示を選んだだけの人は、対象にならない
    assert sorted(eco.guild.status_nick_targets()) == [U, B]


def test_run_status_nick_skips_people_it_cannot_change_and_keeps_going(eco, monkeypatch):
    members = {U: Member(uid=U), A: Member(uid=A)}
    guild = guild_for()
    guild.owner_id = U  # U は、サーバーの持ち主（ニックネームを変えられない）
    guild.get_member = lambda uid: members.get(uid)
    bot = make_test_bot(repos=eco)
    bot.guilds = [guild]
    cog = make_shop_cog(bot)
    monkeypatch.setattr("domain.approval.is_allowed", lambda *a: True)
    indebt(eco, user_id=U, debt=500)
    indebt(eco, user_id=A, debt=500)
    asyncio.run(cog.run_status_nick())
    assert members[U].nick is None and members[A].nick == "たつ【地下労働者】"  # 持ち主は飛ばして、次の人へ進む


def test_old_nick_prefs_rows_are_treated_as_the_title_choice(tmp_path):
    path = str(tmp_path / "old.db")
    e = open_repositories(path, strict=True)
    with e.db.tx():
        e.db.conn.execute("DROP TABLE nick_prefs")
        e.db.conn.execute(
            "CREATE TABLE nick_prefs (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, original_nick TEXT, applied_nick TEXT, "
            "PRIMARY KEY (guild_id, user_id))"
        )
        e.db.conn.execute("INSERT INTO nick_prefs VALUES (10, 1, NULL, NULL)")
        e.db.conn.execute("PRAGMA user_version = 0")
    e.db.conn.close()
    again = open_repositories(path, strict=True).for_guild(10)
    assert again.guild.nick_pref(10, 1) is True  # 既存の行は、称号の表示を自分で選んだ人


# ----------------------------- 画面への反映 ----------------------------- #
def test_chinchiro_result_shows_what_was_seized_and_the_seed_money(tmp_path):
    from cogs.admin import cog as approval_cog  # noqa: F401  (コマンドの読み込みを確かめるため)
    from cogs.casino import chinchiro as play_ch
    from cogs.casino import common
    from domain.games import chinchiro as ch

    bot = make_test_bot(str(tmp_path / "t.db"))
    eco = bot.repos
    eco.wallet.credit(U, 50_000, "テスト")
    eco.market.market_set(time.time(), 100.0)
    assert eco.crypto.crypto_buy(U, 20_000, 100.0).ok
    with eco.db.tx():
        set_test_balance(eco, U, 3000)

    class It:
        user = SimpleNamespace(id=U, display_name="たろう")
        guild_id = 0
        response = SimpleNamespace(send_message=None)

    async def send(**kw):
        pass

    it = It()
    it.response = SimpleNamespace(send_message=send)
    ctx = asyncio.run(common.start_game(as_interaction(it), make_casino_deps(bot), 3_000, "チンチロ"))
    view = play_ch.ChinchiroView(
        must(ctx), as_interaction(It()), [((1, 1, 1), ch.classify((1, 1, 1)))]
    )  # 親がピンゾロ（5倍払い）
    view.player_rolls = [((2, 2, 3), ch.classify((2, 2, 3)))]
    text = view.final_embed().description
    assert "CSトークンが差し押さえられました" in must(text)  # 結果の画面に、差し押さえたものが出る
    assert (
        eco.crypto.crypto_holding(U)[0] == 0 and eco.debt.debt(U) == 0
    )  # 12,000の倍払いは、CSトークンの差し押さえで足りた
    assert eco.wallet.balance(U) > 0 and eco.debt.status(U) == teiai.KATAGI


def test_ranking_marks_people_who_are_not_katagi(eco):
    from cogs.shared import ranking

    eco.wallet.balance(U)
    eco.wallet.balance(A)
    indebt(eco, user_id=A, debt=100, balance=900)
    text = ranking.build_embed(make_ranking_repos(eco), None, "t").description
    assert f"<@{A}>🟤" in must(text) and f"<@{U}>🟤" not in must(text)


def _set_balance(eco, user_id, balance):
    eco.wallet.balance(user_id)
    with eco.db.tx():
        set_test_balance(eco, user_id, balance)


def test_wealthy_is_display_only_and_needs_katagi_with_enough_assets(eco):
    _set_balance(eco, U, teiai.WEALTHY_ASSETS)
    _set_balance(eco, A, teiai.WEALTHY_ASSETS - 1)
    assert eco.wallet.display_status(U) == teiai.WEALTHY
    assert eco.debt.status(U) == teiai.KATAGI  # ゲームの条件に使う status() は変わらない
    assert eco.wallet.display_status(A) == teiai.KATAGI
    assert eco.wallet.wealthy_ids() == {U}
    assert U in eco.guild.status_nick_targets()


def test_wealthy_with_debt_is_not_wealthy(eco):
    indebt(eco, user_id=U, debt=10, balance=teiai.WEALTHY_ASSETS * 2)
    assert eco.wallet.display_status(U) == teiai.UNDERGROUND
    assert U not in eco.wallet.wealthy_ids()


def test_ranking_marks_the_wealthy(eco):
    from cogs.shared import ranking

    _set_balance(eco, U, teiai.WEALTHY_ASSETS)
    assert f"<@{U}>🟡" in must(ranking.build_embed(make_ranking_repos(eco), None, "t").description)


@pytest.mark.real_scope
def test_old_users_table_without_the_new_columns_is_upgraded(tmp_path):
    import sqlite3

    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.execute(
        "CREATE TABLE users (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, balance INTEGER NOT NULL, "
        "daily_streak INTEGER NOT NULL DEFAULT 0, last_daily TEXT, games_played INTEGER NOT NULL DEFAULT 0, "
        "total_wagered INTEGER NOT NULL DEFAULT 0, total_won INTEGER NOT NULL DEFAULT 0, biggest_win INTEGER NOT NULL DEFAULT 0, "
        "created_at REAL NOT NULL, last_slot INTEGER, debt INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (guild_id, user_id))"
    )
    c.execute("INSERT INTO users (guild_id, user_id, balance, created_at, debt) VALUES (10, 1, 500, 0, 700)")
    c.execute("PRAGMA user_version = 0")
    c.commit()
    c.close()
    again = open_repositories(path, strict=True).for_guild(10)
    assert again.db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'").fetchone() is None
    migrated = again.db.conn.execute(
        "SELECT debt, principal FROM teiai_debts WHERE guild_id=10 AND user_id=1"
    ).fetchone()
    assert migrated["debt"] == 700 and migrated["principal"] == 700
    assert again.wallet.stats(1)["grace_penalty"] == 0 and again.wallet.stats(1)["grace_penalty_at"] == 0
    assert again.debt.debt(1) == 700 and again.wallet.balance(1) == 500  # 既存の借金は、そのまま帝愛の借金
    assert again.debt.status(1) == teiai.UNDERGROUND
    assert again.debt.repay_debt(1, 500) == (True, 500)
    assert (
        again.db.conn.execute("SELECT principal FROM teiai_debts WHERE guild_id=10 AND user_id=1").fetchone()[0] == 200
    )


# ----------------------------- 債券の満期と借金 ----------------------------- #
def test_junk_bond_principal_goes_to_the_debt_when_it_matures_while_indebted(eco):
    eco.wallet.balance(U)
    with eco.db.tx():
        eco.bank.bank_sync_for(0, 1.0)
        eco.bank.bank_set_for(0, 1_000_000, 0)  # 利息の原資
    assert eco.bonds.bond_buy(U, "junk", 1_000, 1.0)[0]  # 元本1,000・利息+20%
    with eco.db.tx():
        set_test_debt(eco, U, 5_000)
        set_test_balance(eco, U, 0)

    class Never:
        def random(self):
            return 1.0  # 倒産しない

    eco.bonds.bond_settle(1e12, Never())
    # 償還 1,200 = 元本1,000 + 利息200。利息の3割（60）を天引き、元本は全額（1,000）を返済に充てる
    assert eco.debt.debt(U) == 5_000 - 60 - 1_000 and eco.wallet.balance(U) == 1_200 - 60 - 1_000


def test_bond_maturity_without_debt_pays_out_whole(eco):
    eco.wallet.balance(U)
    with eco.db.tx():
        eco.bank.bank_sync_for(0, 1.0)
        eco.bank.bank_set_for(0, 1_000_000, 0)  # 利息の原資
    assert eco.bonds.bond_buy(U, "junk", 1_000, 1.0)[0]
    before = eco.wallet.balance(U)

    class Never:
        def random(self):
            return 1.0

    eco.bonds.bond_settle(1e12, Never())
    assert eco.wallet.balance(U) == before + 1_200 and eco.debt.debt(U) == 0


def test_bond_principal_pays_the_overdue_loan_when_there_is_no_senior_debt(eco, monkeypatch):
    freeze_time(monkeypatch, eco.db.clock, 1000 + 86400)  # 期限ちょうど（遅延損害金は、まだ付かない）
    eco.wallet.balance(U)
    assert eco.bonds.bond_buy(U, "junk", 1_000, 1.0)[0]
    eco.wallet.balance(A)
    with eco.db.tx():
        insert_test_loan(eco, A, U, 500, status="overdue", accepted_at=1000.0, due=1000.0 + 86400)
    a_before = eco.wallet.balance(A)

    class Never:
        def random(self):
            return 1.0

    eco.bonds.bond_settle(1e12, Never())
    assert eco.wallet.balance(A) == a_before + 500  # 満期の元本から、延滞中の貸付が回収される


# ----------------------------- 破産の直後の反映 ----------------------------- #
def test_after_bankruptcy_removes_the_color_role_and_tags_the_nick(tmp_path):
    cog = make_shop_cog(make_test_bot(str(tmp_path / "t.db")))
    calls = []

    async def fake_role(guild, member, item):
        calls.append(("role", item))

    async def fake_nick(guild, member):
        calls.append(("nick", member.id))

    import cogs.shop.cog as sc

    orig = sc.set_color_role
    sc.set_color_role = fake_role
    cog.sync_status_nick = fake_nick
    try:
        asyncio.run(cog.after_bankruptcy(as_guild(SimpleNamespace(id=7)), as_member(SimpleNamespace(id=U))))
    finally:
        sc.set_color_role = orig
    assert calls == [("role", None), ("nick", U)]


def test_after_bankruptcy_never_raises_on_discord_failures(tmp_path):
    cog = make_shop_cog(make_test_bot(str(tmp_path / "t.db")))

    async def fail_nick(guild, member):
        raise shop_cog.ShopError("権限なし")

    import cogs.shop.cog as sc

    async def fail_role(guild, member, item):
        raise shop_cog.ShopError("権限なし")

    orig = sc.set_color_role
    sc.set_color_role = fail_role
    cog.sync_status_nick = fail_nick
    try:
        asyncio.run(
            cog.after_bankruptcy(as_guild(SimpleNamespace(id=7)), as_member(SimpleNamespace(id=U)))
        )  # 例外にならない
    finally:
        sc.set_color_role = orig


# ----------------------------- 海外逃亡 ----------------------------- #
class _Roll:
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


def _in_debt(eco, debt=20_000):
    eco.wallet.credit(U, 10_000, "テスト")
    with eco.db.tx():
        set_test_balance(eco, U, 1_000)
        set_test_debt(eco, U, debt)


def test_escape_success_wipes_the_whole_debt_but_takes_everything(eco):
    _in_debt(eco)
    ok, r = eco.debt.escape_attempt(U, now=NOW_E, rng=_Roll(0.0))
    assert ok and r["escaped"] and eco.debt.debt(U) == 0 and eco.wallet.balance(U) == 0  # 種銭も貸さない
    assert eco.debt.status(U) == teiai.KATAGI and eco.debt.loan_blacklisted(
        U
    )  # 借金がゼロなので、身分はカタギ。貸付だけ、ブラックリスト
    assert r["bankrupt"]["compressed"] > teiai.BANKRUPT_FLOOR - 1  # 圧縮ではなく全額ゼロ


def test_escape_failure_adds_a_fine_and_starts_the_cooldown(eco):
    _in_debt(eco, 20_000)
    ok, r = eco.debt.escape_attempt(U, now=NOW_E, rng=_Roll(0.99))
    assert ok and not r["escaped"] and r["fine"] == 2_000 and eco.debt.debt(U) == 22_000
    assert eco.wallet.balance(U) == 1_000  # 失敗では、財産は取られない
    ok, reason = eco.debt.escape_attempt(U, now=NOW_E + 3600, rng=_Roll(0.0))
    assert not ok and reason.code == "escape_cooldown" and eco.debt.debt(U) == 22_000
    ok, r = eco.debt.escape_attempt(U, now=NOW_E + teiai.ESCAPE_COOLDOWN_HOURS * 3600 + 1, rng=_Roll(0.0))
    assert ok and r["escaped"]


def test_escape_has_a_minimum_fine_and_a_minimum_debt(eco):
    _in_debt(eco, 5_000)
    ok, r = eco.debt.escape_attempt(U, now=NOW_E, rng=_Roll(0.99))
    assert ok and r["fine"] == teiai.ESCAPE_FINE_MIN  # 500 ではなく、最低 1,000
    _in_debt(eco, 4_999)
    with eco.db.tx():
        eco.db.conn.execute("UPDATE teiai_escapes SET last_attempt_at=NULL WHERE user_id=?", (U,))
    ok, reason = eco.debt.escape_attempt(U, now=NOW_E, rng=_Roll(0.0))
    assert not ok and reason.code == "escape_min_debt"


def test_escape_is_one_percent_and_only_two_tries_in_total(eco):
    assert teiai.ESCAPE_CHANCE == 0.01 and teiai.ESCAPE_MAX_TRIES == 2
    _in_debt(eco)
    day = teiai.ESCAPE_COOLDOWN_HOURS * 3600 + 1
    assert eco.debt.escape_left(U) == 2
    assert eco.debt.escape_attempt(U, now=NOW_E, rng=_Roll(0.99))[1]["escaped"] is False
    assert eco.debt.escape_attempt(U, now=NOW_E + day, rng=_Roll(0.99))[1]["escaped"] is False
    assert eco.debt.escape_left(U) == 0
    ok, reason = eco.debt.escape_attempt(U, now=NOW_E + 3 * day, rng=_Roll(0.0))
    assert not ok and reason.code == "escape_max_tries" and reason.params["max_tries"] == 2


def test_escape_is_not_available_to_bankrupts(eco):
    _in_debt(eco)
    assert eco.debt.escape_attempt(U, now=NOW_E, rng=_Roll(0.0))[0]
    ok, reason = eco.debt.escape_attempt(U, now=NOW_E + 10 * 86400, rng=_Roll(0.0))
    assert not ok  # 借金がゼロなので、逃亡は試せない（完済していなければ、破産者は逃亡できない）


NOW_E = 1_000_000.0


# ----------------------------- 帝愛の利息（トイチ） ----------------------------- #
def _interest_clock(monkeypatch, eco, now):
    freeze_time(monkeypatch, eco.db.clock, lambda: now[0])


def test_debt_grows_by_daily_interest_only_after_each_full_day(eco, monkeypatch):
    now = [1_800_000_000.0]
    _interest_clock(monkeypatch, eco, now)
    indebt(eco, U, debt=5_000)
    assert eco.debt.debt(U) == 5_000  # 借金ができた時点から数える
    now[0] += DAY - 60
    assert eco.debt.debt(U) == 5_000  # 1日に満たない
    now[0] += 120
    assert eco.debt.debt(U) == 5_000 + 50  # 1日ぶん: 日利1%
    now[0] += 3 * DAY
    assert eco.debt.debt(U) == 5_050 + int(5_050 * 0.01 * 3)  # 3日ぶん。端数の時刻は、失われない
    assert any(
        "帝愛の利息" in r["reason"] for r in eco.db.conn.execute("SELECT reason FROM ledger WHERE user_id=?", (U,))
    )


def test_interest_accrues_all_elapsed_days_up_to_twice_the_principal(eco, monkeypatch):
    now = [1_800_000_000.0]
    _interest_clock(monkeypatch, eco, now)
    indebt(eco, U, debt=2_000)
    eco.debt.debt(U)
    now[0] += 90 * DAY  # 10日打ち止め廃止: 90日分まるごと単利で加算される
    assert eco.debt.debt(U) == 2_000 + int(2_000 * 0.01 * 90)
    now[0] += 30 * DAY  # 元本の2倍（4,000）が上限
    assert eco.debt.debt(U) == teiai.debt_interest_cap(2_000) == 4_000
    now[0] += 30 * DAY
    assert eco.debt.debt(U) == 4_000


def test_principal_excludes_existing_interest_and_updates_on_partial_repayment(eco, monkeypatch):
    now = [1_800_000_000.0]
    _interest_clock(monkeypatch, eco, now)
    eco.wallet.balance(U)
    with eco.db.tx():
        set_test_balance(eco, U, 0)
        eco.debt.owe_for(0, U, 3_000, "テスト")
    # owe_for 時点では黒リスト外なので種銭 3,000 が追加され、元本は 6,000 ではなく明示的に 3,000 で試す
    eco.wallet.set_balance(U, -3_000, "テスト")
    assert eco.debt.debt(U) == 3_000
    now[0] += 20 * DAY  # 利息 +600 → 借金 3,600（元本 3,000 のまま）
    assert eco.debt.debt(U) == 3_600
    with eco.db.tx():
        set_test_balance(eco, U, 0)
        eco.debt.owe_for(0, U, 2_000, "追加借入")  # 追加 2,000（種銭 3,000 も乗らないよう残高を戻す前）
    # balance=0 なので種銭 3,000 が追加 → 追加元本は 2,000 + 3,000 = 5,000、元本合計 8,000・借金 8,600
    row = eco.db.conn.execute("SELECT debt, principal FROM teiai_debts WHERE user_id=?", (U,)).fetchone()
    assert row["debt"] == 8_600 and row["principal"] == 8_000  # 既発生利息 600 は元本に組み込まれない
    eco.wallet.credit(U, 10_000, "テスト")
    assert eco.debt.repay_debt(U, 300) == (True, 300)  # 利息分の一部返済では元本は減らない
    assert eco.db.conn.execute("SELECT principal FROM teiai_debts WHERE user_id=?", (U,)).fetchone()[0] == 8_000
    assert eco.debt.repay_debt(U, 3_300) == (True, 3_300)  # 残高 5,000 < 旧元本 8,000 → 元本も 5,000 へ縮む
    assert eco.db.conn.execute("SELECT principal FROM teiai_debts WHERE user_id=?", (U,)).fetchone()[0] == 5_000
    assert eco.debt.repay_debt(U) == (True, 5_000)
    assert eco.db.conn.execute("SELECT debt, principal, interest_at FROM teiai_debts WHERE user_id=?", (U,)).fetchone() is None
    st = eco.wallet.stats(U)
    assert st["debt"] == 0 and st["debt_principal"] == 0


@pytest.mark.real_scope
def test_accrue_all_debt_interest_and_bond_cog_tick_update_idle_debtors(tmp_path, monkeypatch):
    monkeypatch.setattr("domain.approval.is_allowed", lambda *a: True)
    now = [1_800_000_000.0]
    bot = make_test_bot(str(tmp_path / "tick.db"), strict=True)
    freeze_time(monkeypatch, bot.repos.db.clock, lambda: now[0])
    eco = bot.repos.for_guild(10)
    bot.guilds = [SimpleNamespace(id=10)]
    eco.wallet.balance(U)
    eco.wallet.balance(A)
    eco.wallet.balance(B)  # 借金なし
    with eco.db.tx():
        set_test_debt(eco, U, 4_000, guild_id=10)
        set_test_balance(eco, U, 0, guild_id=10)
        set_test_debt(eco, A, 2_000, guild_id=10)
        set_test_balance(eco, A, 0, guild_id=10)
    # 初回ティックで起点を記録
    cog = make_bond_cog(bot)
    cog.run_once()
    assert eco.debt.debt(U) == 4_000 and eco.debt.debt(A) == 2_000
    now[0] += 2 * DAY + 10
    # ユーザーが一切操作しなくても BondCog.run_once() で定期加算される
    cog.run_once()
    assert eco.db.conn.execute("SELECT debt FROM teiai_debts WHERE guild_id=10 AND user_id=?", (U,)).fetchone()[0] == 4_080
    assert eco.db.conn.execute("SELECT debt FROM teiai_debts WHERE guild_id=10 AND user_id=?", (A,)).fetchone()[0] == 2_040
    assert eco.db.conn.execute("SELECT debt FROM teiai_debts WHERE guild_id=10 AND user_id=?", (B,)).fetchone() is None


def test_interest_auto_seizure_protects_company_shop_and_kyosai(eco, monkeypatch):
    now = [1_800_000_000.0]
    _interest_clock(monkeypatch, eco, now)
    eco.wallet.credit(U, 5_000_000, "テスト")
    ok, _ = eco.company.company_found(U, "帝愛対策商事", "mine", now[0])
    assert ok
    item = shop.BY_ID["title_regular"]
    assert eco.shop.purchase(U, item.id, item.name, item.price, True).ok
    with eco.db.tx():
        give_test_kyosai_units(eco, U, 100, guild_id=0, now=now[0])
    assert eco.bank.bank_deposit(U, 3_000, now[0])[0]
    with eco.db.tx():
        set_test_balance(eco, U, 2_000)
        set_test_debt(eco, U, 14_900, principal=14_900, interest_at=now[0])
    now[0] += DAY + 1  # 利息 +149 → 15,049 (>= GRACE_DEBT) → 猶予入り前に A-1 発動
    added = eco.debt.accrue_all_debt_interest(now[0])
    assert added == 149
    # 現金 2,000 + 信託 3,000 = 5,000 が強制回収され、借金は 10,049 (< GRACE_DEBT) となって猶予回避
    assert eco.debt.debt(U) == 10_049
    assert eco.wallet.balance(U) == 0 and eco.bank.deposit_of(U) == 0
    assert eco.bank.grace_until(U) is None and eco.debt.status(U) == teiai.UNDERGROUND
    # 会社・ショップ・共済口は守られる
    assert eco.company.company_get(U, now[0]) is not None
    assert eco.shop.inventory(U).get("title_regular") == 1
    assert eco.kyosai.units_of_for(0, U) == 100
    notes = eco.notices.take_notices(U)
    assert any(n.code == "debt_auto_seized" and n.params == {"seized": 5_000, "debt": 10_049} for n in notes)
    assert any(
        "強制回収:帝愛の利息超過（5,000ペリカ）" in r["reason"]
        for r in eco.db.conn.execute("SELECT reason FROM ledger WHERE user_id=?", (U,))
    )


def test_interest_auto_seizure_enters_grace_when_exhausted_and_skips_when_already_in_grace(eco, monkeypatch):
    now = [1_800_000_000.0]
    _interest_clock(monkeypatch, eco, now)
    eco.wallet.credit(A, 10_000, "テスト")
    eco.wallet.balance(U)
    assert eco.bank.cds_buy(A, U, 1_000, now[0])[0]
    with eco.db.tx():
        set_test_balance(eco, U, 100)
        set_test_debt(eco, U, 14_900, principal=14_900, interest_at=now[0])
    now[0] += 2 * DAY + 1  # 利息 +298 → 15,198。手元 100 全額差し押さえでも 15,098 >= GRACE_DEBT → 猶予入り
    eco.debt.accrue_all_debt_interest(now[0])
    assert eco.wallet.balance(U) == 0 and eco.debt.debt(U) == 15_098
    assert eco.bank.grace_until(U) is not None
    # すでに猶予中の場合、手元に現金があっても A-1 は不発で、CDS 発火と干渉しない
    eco.wallet.credit(U, 5_000, "付与")
    now[0] += DAY
    eco.debt.accrue_all_debt_interest(now[0])
    assert eco.wallet.balance(U) == 5_000
    assert eco.bank.grace_until(U) is not None
    done = eco.bank.grace_expire(now[0] + teiai.GRACE_HOURS * 3600 + 1)
    assert [uid for uid, _ in done] == [U] and done[0][1]["cds_paid"] > 0


def test_grace_penalty_accumulates_halves_duration_and_recovers_with_decay(eco, monkeypatch):
    assert teiai.effective_grace_hours(0.0) == pytest.approx(24.0)
    assert teiai.effective_grace_hours(1.0) == pytest.approx(12.0)
    assert teiai.effective_grace_hours(10.0) == pytest.approx(teiai.GRACE_MIN_HOURS)
    assert teiai.decay_grace_penalty(2.0, teiai.GRACE_PENALTY_HALF_LIFE_DAYS) == pytest.approx(1.0)

    now = [1_800_000_000.0]
    _interest_clock(monkeypatch, eco, now)
    eco.wallet.balance(U)
    durations: list[float] = []
    for _ in range(6):
        with eco.db.tx():
            set_test_debt(eco, U, 20_000, principal=20_000)
            eco.debt.after_debt_change(0, U)
        until = must(eco.bank.grace_until(U))
        durations.append((until - now[0]) / 3600.0)
        with eco.db.tx():
            set_test_debt(eco, U, 1_000, principal=1_000)
            eco.debt.after_debt_change(0, U)
        now[0] += 60  # ほぼ経過時間ゼロで再突入
    assert durations[0] == pytest.approx(24.0)
    assert durations[1] == pytest.approx(12.0, abs=0.01)
    assert durations[2] == pytest.approx(6.0, abs=0.01)
    assert durations[3] == pytest.approx(3.0, abs=0.01)
    assert durations[4] == pytest.approx(1.5, abs=0.01)
    assert durations[5] == pytest.approx(teiai.GRACE_MIN_HOURS)

    # 半減期（14日）経過するとペナルティが減衰し、猶予時間が回復する
    with eco.db.tx():
        set_test_grace(eco, U, None, penalty=2.0, penalty_at=now[0])
    now[0] += teiai.GRACE_PENALTY_HALF_LIFE_DAYS * DAY
    with eco.db.tx():
        set_test_debt(eco, U, 20_000, principal=20_000)
        eco.debt.after_debt_change(0, U)
    recovered_hours = (must(eco.bank.grace_until(U)) - now[0]) / 3600.0
    assert recovered_hours == pytest.approx(12.0, abs=0.01)


def test_b2_bankrupt_and_escape_block_seed_and_credit_until_blacklist_expires(eco, monkeypatch):
    now = [1_800_000_000.0]
    _interest_clock(monkeypatch, eco, now)
    indebt(eco, U, debt=50_000, balance=0)
    ok, res = eco.debt.bankrupt(U)
    assert ok and res["seed"] == 0 and eco.wallet.balance(U) == 0 and eco.debt.debt(U) == teiai.BANKRUPT_FLOOR
    assert eco.debt.borrow_seed(U)[1].code == "seed_bankrupt_blocked"
    assert eco.debt.can_borrow_for_bet(U, 500)[1].code == "credit_bankrupt_blocked"
    # 破産中のデイリーや追加借金でも種銭は出ない
    daily_res = eco.wallet.claim_daily(U, datetime.date(2026, 11, 1))
    assert daily_res.seed == 0
    with eco.db.tx():
        set_test_balance(eco, U, 0)
        eco.debt.owe_for(0, U, 500, "テスト")
    assert eco.wallet.balance(U) == 0

    # 完済してカタギに戻っても、ブラックリスト 7 日間は種銭・信用が遮断される
    eco.wallet.credit(U, 50_000, "テスト")
    assert eco.debt.repay_debt(U)[0]
    eco.wallet.debit_up_to(U, eco.wallet.balance(U) - 500, "テスト")
    assert eco.debt.status(U) == teiai.KATAGI and eco.debt.loan_blacklisted(U)
    assert eco.debt.borrow_seed(U)[1].code == "seed_blacklisted"
    assert eco.debt.can_borrow_for_bet(U, 500)[1].code == "credit_blacklisted"
    eid = eco.escrow.place_bet(U, 500, "ブラックジャック")
    assert not eco.escrow.add_to_escrow(eid, 1_000, credit=True)
    eco.escrow.refund(eid)

    # ブラックリスト経過後に種銭・信用が復帰する
    now[0] += (teiai.BLACKLIST_DAYS + 1) * DAY
    assert not eco.debt.loan_blacklisted(U)
    assert eco.debt.borrow_seed(U) == (True, teiai.SEED - 500)
    eid2 = eco.escrow.place_bet(U, teiai.SEED, "ブラックジャック")
    assert eco.escrow.add_to_escrow(eid2, 1_000, credit=True)

    # 海外逃亡成功後も同様にブラックリスト期間中は種銭・信用が使えない
    indebt(eco, A, debt=20_000, balance=0)
    ok_esc, res_esc = eco.debt.escape_attempt(A, now=now[0], rng=_Roll(0.0))
    assert ok_esc and res_esc["escaped"] and res_esc["bankrupt"]["seed"] == 0
    assert eco.debt.borrow_seed(A)[1].code == "seed_blacklisted"
    assert eco.debt.can_borrow_for_bet(A, 500)[1].code == "credit_blacklisted"


def test_ui_embeds_and_messages_reflect_principal_cap_and_safety_notices(eco):
    from cogs.economy.embeds import balance_embed
    from cogs.shared.messages import format_failure, format_notice
    from cogs.teiai.panel import panel_embed
    from domain.errors import fail, notice

    indebt(eco, U, debt=5_000, balance=1_000)
    bot = make_test_bot(repos=eco)
    panel = panel_embed(bot.services.teiai, 0, U)
    you_field = next(f for f in panel.fields if f.name == "あなた").value
    assert f"元本の{teiai.DEBT_INTEREST_CAP_MULT:g}倍まで" in must(you_field)
    card = must(bot.services.economy.balance_card(0, U, U))
    bal_e = balance_embed(card, "たつ", "https://example.com/a.png")
    debt_field = next(f for f in bal_e.fields if f.name == "帝愛への借金").value
    assert f"元本の{teiai.DEBT_INTEREST_CAP_MULT:g}倍まで" in must(debt_field)
    assert "ブラックリスト" in format_failure(fail("seed_blacklisted"))
    assert "ブラックリスト" in format_failure(fail("credit_blacklisted"))
    notice_text = format_notice(notice("debt_auto_seized", seized=5_000, debt=10_049))
    assert "5,000ペリカ" in notice_text and "10,049ペリカ" in notice_text


def test_net_worth_ranking_accrues_untouched_debt_interest_so_it_matches_the_balance_display(eco, monkeypatch):
    now = [1_800_000_000.0]
    _interest_clock(monkeypatch, eco, now)
    indebt(eco, U, debt=5_000, balance=5_000)
    eco.debt.debt(U)  # 借金ができた時点を記録（まだ利息は付かない）
    now[0] += 3 * DAY  # 本人は、しばらく残高を見ていない
    rank = eco.wallet.rank_of(U)
    assert rank is not None
    expected_debt = 5_000 + int(5_000 * 0.01 * 3)  # 3日ぶんの利息（日利1%）
    assert rank[1] == eco.wallet.balance(U) - expected_debt  # 利息を加える前の、古い借金で計算していない
    assert eco.debt.debt(U) == expected_debt  # 残高表示側（/ペリカ 残高）と、二重に利息を付けていない


def test_a_paid_off_debt_forgets_the_interest_clock(eco, monkeypatch):
    now = [1_800_000_000.0]
    _interest_clock(monkeypatch, eco, now)
    indebt(eco, U, debt=1_000, balance=5_000)
    eco.debt.debt(U)
    now[0] += 2 * DAY
    assert eco.debt.repay_debt(U, None)[0]  # 利息ぶんも含めて、完済
    assert eco.debt.debt(U) == 0
    now[0] += 30 * DAY  # 長い間、借金なし
    with eco.db.tx():
        set_test_debt(eco, U, 500)  # 新しい借金
    assert eco.debt.debt(U) == 500  # 昔の時刻から数えず、いまから
    now[0] += DAY + 1
    assert eco.debt.debt(U) == 505


@pytest.mark.real_scope
def test_daily_skim_goes_to_the_bank_and_other_servers_have_their_own(eco):
    indebt(eco, U, debt=5_000, balance=4_000)
    res = eco.wallet.claim_daily(U, datetime.date(2026, 11, 1))
    assert res.skimmed == 200
    s = eco.bank.bank_status(eco.db.now_ts())
    assert s["cash"] + s["thermostat"]["burned"] == pytest.approx(res.skimmed)
    assert eco.bank.house_account() == {"balance": 0, "skimmed": 0}  # 帝愛物産プールには入らない
    other = eco.for_guild(99)
    assert other.bank.bank_status(other.db.now_ts())["cash"] == 0  # サーバーごとに別々
    assert other.bank.house_account() == {"balance": 0, "skimmed": 0}
