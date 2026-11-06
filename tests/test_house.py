"""運営口座: お金の流れが口座を通ること、マイナスを許すこと、資金追加・横領、毎日の公開。"""

import asyncio
import datetime
from types import SimpleNamespace

import pytest

from cogs.house_cog import HouseCog, day_range, report_embed
from core.eco_house import INITIAL_FUNDS
from core.economy import INITIAL_BALANCE, Economy

JST = datetime.timezone(datetime.timedelta(hours=9))


@pytest.fixture
def eco(tmp_path):
    return Economy(str(tmp_path / "t.db"))


def rows(eco, start=0, end=9e12):
    return eco.house_summary(start, end)["rows"]


def conserved(eco):
    """運営口座の増減が、ユーザーの帳簿と口座を通す動きだけで説明できること（口座の帳簿の合計 = 残高 - 初期資金）。"""
    total = eco._conn.execute("SELECT COALESCE(SUM(delta), 0) FROM house_ledger").fetchone()[0]
    return eco.house_balance() == total


def test_account_opens_once_with_initial_funds_and_a_ledger_line(eco):
    assert eco.house_balance() == INITIAL_FUNDS == 100_000
    assert eco.house_balance() == INITIAL_FUNDS                                # 二重には開かない
    assert rows(eco) == {"初期資金": (1, INITIAL_FUNDS)} and conserved(eco)


def test_new_user_gift_is_issued_by_the_system_and_leaves_the_account_alone(eco):
    eco.balance(1)
    assert eco.house_balance() == INITIAL_FUNDS and "初期支給" not in rows(eco)
    assert eco.house_summary(0, 9e12)["issued"] == {"初期支給": (1, INITIAL_BALANCE)}       # 発行として別に記録される
    eco.balance(1)
    assert eco.house_summary(0, 9e12)["issued"]["初期支給"][0] == 1                          # 2回目は発行しない


def test_a_loss_goes_in_and_a_win_comes_out(eco):
    eco.balance(1)
    base = eco.house_balance()
    eid = eco.place_bet(1, 1000, "スロット")
    eco.settle(eid, 0, "スロット")                                              # 負け
    assert eco.house_balance() == base + 1000
    eid = eco.place_bet(1, 1000, "スロット")
    eco.settle(eid, 3000, "スロット")                                           # 勝ち（払い戻し3000）
    assert eco.house_balance() == base + 1000 + 1000 - 3000 and rows(eco)["ゲーム:スロット"] == (3, 1000 + 1000 - 3000)
    assert conserved(eco)


def test_the_house_can_go_negative(eco):
    eco.credit(1, 10_000_000, "テスト")                                          # 口座を通さない付与（テスト用）
    eid = eco.place_bet(1, 100_000, "パチンコ")
    eco.settle(eid, 100_000 * 100, "パチンコ")                                  # 100倍
    assert eco.house_balance() < 0 and conserved(eco)


def test_refund_returns_the_bet_from_the_house(eco):
    eco.balance(1)
    base = eco.house_balance()
    eid = eco.place_bet(1, 500, "ルーレット")
    assert eco.house_balance() == base + 500
    eco.refund(eid)
    assert eco.house_balance() == base and conserved(eco)                       # エラー時の返金で、口座も元に戻る


def test_double_down_and_penalty_and_insurance(eco):
    eco.credit(1, 20_000, "テスト")                                              # 口座を通さない入金（残高を増やすだけ）
    base = eco.house_balance()
    eid = eco.place_bet(1, 1000, "ブラックジャック")
    assert eco.add_to_escrow(eid, 1000)                                         # ダブル
    net, taken, insured = eco.settle_with_penalty(eid, 0, "ブラックジャック", 0)
    assert eco.house_balance() == base + 2000
    eid = eco.place_bet(1, 1000, "チンチロ")
    eco.settle_with_penalty(eid, 0, "チンチロ", 500)                            # 負け + 追加の支払い500
    assert eco.house_balance() == base + 2000 + 1000 + 500 and conserved(eco)


def test_debt_repayment_goes_to_the_house(eco):
    eco.balance(1)
    eid = eco.place_bet(1, 3000, "チンチロ")
    eco.settle_with_penalty(eid, 0, "チンチロ", 5000)                           # 残高0で、倍払い5000 → 借金5000
    assert eco.debt(1) == 5000
    before = eco.house_balance()
    eco.credit(1, 2000, "テスト入金")                                            # 入金は先に借金へ回る
    assert eco.debt(1) == 3000 and eco.house_balance() == before + 2000 and rows(eco)["借金回収"] == (1, 2000)
    assert conserved(eco)


def test_system_payouts_come_out_and_user_to_user_money_does_not(eco):
    eco.balance(1)
    eco.balance(2)
    base = eco.house_balance()
    eco.credit(1, 1000, "付与", house="付与")
    assert eco.house_balance() == base - 1000
    ok, _ = eco.bond_buy(1, "normal", 2000, 1000.0)
    assert ok and eco.house_balance() == base - 1000 + 2000                       # 元本は口座に預かる
    eco.bond_settle(1000.0 + 3 * 86400)
    assert eco.house_balance() == base - 1000 + 2000 - 2080                      # 満期に元本＋利息を払う
    mid = eco.house_balance()
    qid = eco.quiz_add("問", ["a", "b", "c", "d"], "", 100, None, 0.0)
    eco.quiz_answer(1, qid, "a", 1.0)
    assert eco.house_balance() == mid - 100                                      # 持ち主のクイズの報酬は口座から
    mid = eco.house_balance()
    ok, uid = eco.quiz_post(1, "みんなの問題", ["a", "b", "c", "d"], "", 100, 600, 7, 5.0, "名前")
    eco.quiz_answer(2, uid, "a", 6.0)                                            # みんなの問題の正解報酬は出題者から（口座は動かない）
    assert eco.house_balance() == mid
    mid = eco.house_balance()
    assert eco.transfer(1, 2, 1000, 0.10, 20_000, 0.0).ok
    assert eco.house_balance() == mid + 100 and rows(eco)["送金手数料"] == (1, 100)      # 送金そのものは口座を通らず、手数料だけが入る
    assert conserved(eco)


def test_shop_and_transfer_fee_go_in(eco):
    eco.balance(1)
    eco.balance(2)
    base = eco.house_balance()
    eco.purchase(1, "title_test", "テスト称号", 500, False)
    assert eco.house_balance() == base + 500 and rows(eco)["ショップ"] == (1, 500)


def test_crypto_and_raffle_do_not_touch_the_house(eco):
    eco.balance(1)
    eco.balance(2)
    base = eco.house_balance()
    eco.raffle_open(9, 1000, 9e12, 1)
    eco.raffle_join(1, 1.0)
    eco.raffle_join(2, 1.0)
    eco.raffle_close()
    assert eco.house_balance() == base                                          # 抽選はユーザー同士


# ----------------------------- 資金追加・横領 ----------------------------- #
def test_add_funds_and_embezzle(eco):
    assert eco.house_add_funds(500) == INITIAL_FUNDS + 500
    eco.balance(1)
    base_house, base_user = eco.house_balance(), eco.balance(1)
    ok, moved = eco.house_embezzle(1, 100_000)
    assert ok and moved == 100_000 and eco.house_balance() == base_house - 100_000 and eco.balance(1) == base_user + 100_000
    assert rows(eco)["横領"] == (1, -100_000) and rows(eco)["資金追加"] == (1, 500) and conserved(eco)
    with pytest.raises(ValueError):
        eco.house_add_funds(0)


def test_embezzle_is_limited_to_a_positive_balance(eco):
    eco.balance(1)
    bal = eco.house_balance()
    for amount, fragment in ((0, "1以上"), (bal + 1, "超えています")):
        ok, reason = eco.house_embezzle(1, amount)
        assert not ok and fragment in reason
    eco.credit(1, 5_000_000, "付与", house="付与")                              # 口座をマイナスにする
    assert eco.house_balance() < 0
    ok, reason = eco.house_embezzle(1, 1)
    assert not ok and "残高がありません" in reason


# ----------------------------- サーバーごと ----------------------------- #
@pytest.mark.real_scope
def test_each_guild_has_its_own_account(tmp_path):
    eco = Economy(str(tmp_path / "t.db"))
    a, b = eco.for_guild(10), eco.for_guild(20)
    a.balance(1)
    eid = a.place_bet(1, 1000, "スロット")
    a.settle(eid, 0, "スロット")
    assert a.house_balance() == INITIAL_FUNDS + 1000 and b.house_balance() == INITIAL_FUNDS
    b.house_add_funds(7)
    assert a.house_balance() != b.house_balance()


# ----------------------------- 集計と公開 ----------------------------- #
def test_summary_has_opening_and_closing_and_the_report_text(eco):
    eco._conn.execute("INSERT INTO house (guild_id, balance, created, last_report) VALUES (0, 1000000, 100, '')")
    ledger = [(150, "初期資金", 1_000_000, 1_000_000), (1000, "ゲーム:スロット", 3000, 1_003_000), (1100, "ゲーム:スロット", -1000, 1_002_000),
              (1200, "デイリー", -1600, 1_000_400), (1300, "資金追加", 500, 1_000_900), (1400, "横領", -900, 1_000_000), (9000, "ゲーム:スロット", 50, 1_000_050)]
    eco._conn.executemany("INSERT INTO house_ledger (guild_id, ts, category, delta, balance_after) VALUES (0, ?, ?, ?, ?)", ledger)
    s = eco.house_summary(1000, 2000)
    assert s["opening"] == 1_000_000 and s["closing"] == 1_000_000 and s["rows"]["ゲーム:スロット"] == (2, 2000)    # 期間外（9000）は入らない
    e = report_embed(s, "🏦 運営口座の収支（11月6日）")
    assert "収入 **+2,000**　支出 **-1,600**　収支 **+400**" in e.description and "1,000,000ペリカ → **1,000,000ペリカ**" in e.description
    names = [f.name for f in e.fields]
    assert names == ["🎰 ゲーム別（ユーザーの負け − 勝ち）", "📒 そのほか", "🏦 資金の動き（収支には入れません）"]
    assert "スロット: **+2,000**（2件）" in e.fields[0].value and "デイリー: **-1,600**" in e.fields[1].value
    assert "資金追加: **+500**" in e.fields[2].value and "横領: **-900**" in e.fields[2].value               # 横領も公開に出る
    assert report_embed(eco.house_summary(5000, 6000), "t").description.endswith("お金の動きがありませんでした。")


def test_day_range_is_midnight_to_midnight_in_the_given_timezone():
    start, end = day_range(datetime.date(2026, 11, 6), JST)
    assert end - start == 86400 and datetime.datetime.fromtimestamp(start, JST).hour == 0


class Chan:
    def __init__(self):
        self.sent = []

    def permissions_for(self, _me):
        return SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)

    async def send(self, **kw):
        self.sent.append(kw["embed"])


def make_cog(tmp_path, monkeypatch):
    eco = Economy(str(tmp_path / "t.db"))
    chan = Chan()
    guild = SimpleNamespace(id=1, me=object(), system_channel=chan, get_channel=lambda cid: None)
    bot = SimpleNamespace(guilds=[guild], config={}, economy=SimpleNamespace(for_guild=lambda gid: eco))
    monkeypatch.setattr("cogs.house_cog.approval.is_allowed", lambda *a: True)
    return HouseCog(bot), eco, chan, guild


def test_daily_report_is_posted_once_per_day_starting_the_day_after_opening(tmp_path, monkeypatch):
    cog, eco, chan, guild = make_cog(tmp_path, monkeypatch)
    day1 = datetime.datetime(2026, 11, 6, 10, 0, tzinfo=JST)
    eco.balance(1)
    eco.house_balance()                                  # 先に口座を開く（開く前の UPDATE は空振りし、実際の日付に左右される）
    eco._conn.execute("UPDATE house SET created=?", (day1.timestamp(),))
    asyncio.run(cog.report_guild(guild, day1))
    assert chan.sent == []                                                       # 開いた日の当日は、まだ出さない
    day2 = datetime.datetime(2026, 11, 7, 0, 5, tzinfo=JST)
    asyncio.run(cog.report_guild(guild, day2))
    asyncio.run(cog.report_guild(guild, day2 + datetime.timedelta(hours=3)))
    assert len(chan.sent) == 1 and "11月6日" in chan.sent[0].title            # 翌日の0時過ぎに、前日分を1回だけ
    assert eco.house_info()["last_report"] == "2026-11-06"


def test_missed_days_are_posted_one_by_one_up_to_the_limit(tmp_path, monkeypatch):
    cog, eco, chan, guild = make_cog(tmp_path, monkeypatch)
    eco.balance(1)
    eco.house_balance()                                  # 先に口座を開く（開く前の UPDATE は空振りし、実際の日付に左右される）
    eco._conn.execute("UPDATE house SET created=?, last_report='2026-11-01'", (datetime.datetime(2026, 11, 1, tzinfo=JST).timestamp(),))
    asyncio.run(cog.report_guild(guild, datetime.datetime(2026, 11, 9, 8, 0, tzinfo=JST)))        # 2日〜8日が未公開 → 最新の3日分だけ
    assert [e.title for e in chan.sent] == ["🏦 運営口座の収支（11月6日）", "🏦 運営口座の収支（11月7日）", "🏦 運営口座の収支（11月8日）"]
    assert eco.house_info()["last_report"] == "2026-11-08"


def test_a_failed_post_is_retried_next_time(tmp_path, monkeypatch):
    import discord
    cog, eco, chan, guild = make_cog(tmp_path, monkeypatch)
    eco.balance(1)
    eco.house_balance()                                  # 先に口座を開く（開く前の UPDATE は空振りし、実際の日付に左右される）
    eco._conn.execute("UPDATE house SET created=?", (datetime.datetime(2026, 11, 6, tzinfo=JST).timestamp(),))

    async def boom(**kw):
        raise discord.HTTPException(SimpleNamespace(status=500, reason="x"), "err")
    chan.send = boom
    now = datetime.datetime(2026, 11, 7, 1, 0, tzinfo=JST)
    asyncio.run(cog.report_guild(guild, now))
    assert eco.house_info()["last_report"] == ""                                 # 印を付けない
    chan.send = Chan.send.__get__(chan)
    asyncio.run(cog.report_guild(guild, now))
    assert len(chan.sent) == 1 and eco.house_info()["last_report"] == "2026-11-06"


# ----------------------------- /運営 口座 ----------------------------- #
def make_admin(tmp_path):
    from cogs.approval_cog import ApprovalCog
    eco = Economy(str(tmp_path / "t.db"))
    guild = SimpleNamespace(id=5, name="友達鯖", members=[])
    bot = SimpleNamespace(config={"owner_ids": [1], "utc_offset_hours": 9}, economy=eco, guilds=[guild], get_guild=lambda gid: guild if gid == 5 else None, get_cog=lambda n: None)
    return ApprovalCog(bot), eco


def inter(uid, log):
    class R:
        async def send_message(self, **kw):
            log.append((kw["embeds"][0] if "embeds" in kw else kw["embed"]))
    return SimpleNamespace(user=SimpleNamespace(id=uid), guild_id=None, response=R(), namespace=SimpleNamespace())


@pytest.mark.real_scope
def test_house_commands_are_owner_only_and_move_the_right_money(tmp_path, monkeypatch):
    monkeypatch.setattr("cogs.approval_cog.approval.is_allowed", lambda *a: True)
    cog, eco = make_admin(tmp_path)
    g = eco.for_guild(5)
    log = []
    asyncio.run(cog.house_add.callback(cog, inter(2, log), "5", 1000))
    asyncio.run(cog.house_embezzle.callback(cog, inter(2, log), "5", 1000))
    assert [e.title for e in log] == ["❌ 権限がありません"] * 2 and g.house_balance() == INITIAL_FUNDS
    log.clear()
    asyncio.run(cog.house_add.callback(cog, inter(1, log), "5", 250_000))
    assert log[-1].title == "✅ 資金を足しました" and g.house_balance() == INITIAL_FUNDS + 250_000
    asyncio.run(cog.house_embezzle.callback(cog, inter(1, log), "5", 100_000))
    assert log[-1].title == "✅ 横領しました" and g.balance(1) == INITIAL_BALANCE + 100_000 and g.house_balance() == INITIAL_FUNDS + 150_000
    asyncio.run(cog.house_embezzle.callback(cog, inter(1, log), "5", 999_999_999))              # 残高を超える
    assert log[-1].title == "❌ 横領できません"
    asyncio.run(cog.house_embezzle.callback(cog, inter(1, log), "999", 1))                       # 不明なサーバー
    assert log[-1].title == "❌ サーバーが見つかりません"


@pytest.mark.real_scope
def test_house_status_shows_today_for_chosen_or_all_guilds(tmp_path, monkeypatch):
    monkeypatch.setattr("cogs.approval_cog.approval.is_allowed", lambda *a: True)
    cog, eco = make_admin(tmp_path)
    g = eco.for_guild(5)
    g.balance(1)
    eid = g.place_bet(1, 1000, "スロット")
    g.settle(eid, 0, "スロット")
    log = []
    asyncio.run(cog.house_status.callback(cog, inter(2, log), None))
    assert log[-1].title == "❌ 権限がありません"
    asyncio.run(cog.house_status.callback(cog, inter(1, log), "5"))
    e = log[-1]
    assert e.title == "🏦 友達鯖 の運営口座（総合）" and "スロット: **+1,000**" in e.fields[0].value and e.fields[1].name.startswith("🪙 システムの発行") and "初期支給: **+3,000**" in e.fields[1].value


# ----------------------------- 収支の表示は、いまは止めている ----------------------------- #
def test_public_account_display_is_switched_off():
    import cogs.house_cog as hc
    assert hc.DAILY_REPORT is False and not hasattr(HouseCog, "show")                    # /運営口座 は登録されない


def test_daily_report_loop_is_not_started_while_switched_off(tmp_path, monkeypatch):
    cog, eco, chan, guild = make_cog(tmp_path, monkeypatch)
    started = []
    monkeypatch.setattr(type(cog).tick, "start", lambda *a, **k: started.append(1), raising=False)
    asyncio.run(cog.cog_load())
    assert started == []


def test_daily_and_missions_are_issued_and_do_not_touch_the_account(eco):
    import datetime as dt
    eco.balance(1)
    base = eco.house_balance()
    day = dt.date(2026, 11, 6)
    result = eco.claim_daily(1, day, 1)
    assert result.ok and eco.house_balance() == base                                      # デイリーで口座は減らない
    issued = eco.house_summary(0, 9e12)["issued"]
    assert issued["デイリー"][0] == 1 and issued["デイリー"][1] > 0 and "デイリー" not in rows(eco)
    eco._apply(1, 500, "ミッション報酬:テスト", issue="ミッション")             # ミッション報酬と同じ呼び方
    assert eco.house_balance() == base and eco.house_summary(0, 9e12)["issued"]["ミッション"] == (1, 500)
    assert eco.house_balance() == sum(r[1] for r in rows(eco).values())                    # 口座の帳簿は、発行を含まない


def test_issue_is_shown_in_the_report_but_not_in_income_or_spending():
    s = {"rows": {"ゲーム:スロット": (2, 500)}, "opening": 1_000_000, "closing": 1_000_500, "issued": {"デイリー": (9, 12_000), "初期支給": (1, 3000)}}
    e = report_embed(s, "t")
    assert "収入 **+500**" in e.description and "収支 **+500**" in e.description
    names = [f.name for f in e.fields]
    assert names == ["🎰 ゲーム別（ユーザーの負け − 勝ち）", "🪙 システムの発行（口座の残高には影響しません）"]
    assert "デイリー: **+12,000**（9件）" in e.fields[1].value and e.fields[1].value.index("デイリー") < e.fields[1].value.index("初期支給")      # 多い順
    only_issue = report_embed({"rows": {}, "opening": 5, "closing": 5, "issued": {"デイリー": (1, 100)}}, "t")
    assert "お金の動きがありませんでした" not in only_issue.description                      # 発行だけの日も「動きなし」とは書かない


def test_deposit_moves_money_from_the_personal_account_without_creating_any(eco):
    eco.balance(1)
    base_house, base_user = eco.house_balance(), eco.balance(1)
    ok, moved = eco.house_deposit(1, 2000)
    assert ok and moved == 2000 and eco.balance(1) == base_user - 2000 and eco.house_balance() == base_house + 2000
    assert rows(eco)["入金"] == (1, 2000) and conserved(eco)
    for amount, fragment in ((0, "1以上"), (base_user, "残高が足りません")):
        ok, reason = eco.house_deposit(1, amount)
        assert not ok and fragment in reason
    ok, moved = eco.house_embezzle(1, 2000)                                      # 入金と横領で、元に戻る
    assert ok and eco.balance(1) == base_user and eco.house_balance() == base_house


def test_deposit_is_a_fund_movement_not_income(eco):
    e = report_embed({"rows": {"入金": (1, 500), "ゲーム:スロット": (1, 100)}, "opening": 0, "closing": 600, "issued": {}}, "t")
    assert "収入 **+100**" in e.description and "収支 **+100**" in e.description                    # 入金は収支に入れない
    assert "入金: **+500**" in e.fields[-1].value and e.fields[-1].name.startswith("🏦 資金の動き")


def test_summary_after_the_ledger_is_wiped_starts_from_the_current_balance(eco):
    eco.house_add_funds(500)
    eco._conn.execute("DELETE FROM house_ledger")
    eco._conn.execute("DELETE FROM house_issue")
    eco._conn.commit()
    s = eco.house_summary(0, 10**12)
    assert s["rows"] == {} and s["opening"] == s["closing"] == eco.house_balance()
