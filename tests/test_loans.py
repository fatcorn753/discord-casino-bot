"""貸し借り: 日割り単利、承諾でお金が移る、返済、期限の自動回収、延滞の強制徴収、ボタンと通知。"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from cogs.loan_cog import LoanButton, LoanCog, handle_button, offer_embed, who
from core import loans, teiai
from core.economy import INITIAL_BALANCE, Economy

NOW = 1_000_000.0
DAY = 86400
A, B, C, OWNER = 11, 22, 33, 99


@pytest.fixture
def eco(tmp_path):
    return Economy(str(tmp_path / "t.db"))


def offer(eco, lender=A, borrower=B, principal=1000, rate=20, days=10, now=NOW):
    ok, result = eco.loan_offer(lender, borrower, principal, rate, days, 777, now)
    assert ok, result
    return result


def active(eco, **kw):
    lid = offer(eco, **kw)
    ok, reason = eco.loan_accept(lid, kw.get("borrower", B), NOW + 1)
    assert ok, reason
    return lid


# ----------------------------- 計算 ----------------------------- #
def test_interest_is_simple_per_day_with_rounding_up_and_a_term_cap():
    assert loans.interest(1000, 20, 1) == 20 and loans.interest(1000, 20, 10) == 200  # 日利2.0%
    assert (
        loans.elapsed_days(0, 1, 10) == 1
        and loans.elapsed_days(0, DAY, 10) == 1
        and loans.elapsed_days(0, DAY + 1, 10) == 2
    )
    assert loans.elapsed_days(0, 99 * DAY, 10) == 10  # 期間が上限（延滞の利息は付かない）
    assert loans.total_owed(1000, 20, 10, 0, 3 * DAY - 5) == 1000 + 3 * 20
    assert loans.full_term_total(1000, 20, 10) == 1200 and loans.rate_text(25) == "2.5%" and loans.rate_text(0) == "0%"


# ----------------------------- 提案 ----------------------------- #
def test_offer_validation(eco):
    cases = [
        ((A, A, 1000, 20, 10), "自分には"),
        ((A, B, 99, 20, 10), "金額は"),
        ((A, B, 1000, -1, 10), "日利は"),
        ((A, B, 1000, 20, 0), "期間は"),
        ((A, B, 1000, 20, 31), "期間は"),
        ((A, B, INITIAL_BALANCE + 1, 20, 10), "残高が足りません"),
    ]
    for args, fragment in cases:
        ok, reason = eco.loan_offer(*args, 777, NOW)
        assert not ok and fragment in reason, (args, reason)
    assert eco.loan_offer(A, B, 1000, 1000, 10, 777, NOW)[0]  # 日利100%まで出せる


def test_open_offer_limit(eco):
    for _ in range(loans.MAX_LENDER_OPEN):
        offer(eco, principal=100)
    ok, reason = eco.loan_offer(A, B, 100, 20, 10, 777, NOW)
    assert not ok and "承諾待ち" in reason


# ----------------------------- 承諾 ----------------------------- #
def test_accept_moves_the_money_and_starts_the_clock(eco):
    lid = offer(eco)
    eco.balance(B)
    assert eco.loan_get(lid)["status"] == "offer" and eco.balance(A) == INITIAL_BALANCE  # 承諾までは動かない
    ok, _ = eco.loan_accept(lid, B, NOW + 5)
    loan = eco.loan_get(lid)
    assert ok and eco.balance(A) == INITIAL_BALANCE - 1000 and eco.balance(B) == INITIAL_BALANCE + 1000
    assert loan["status"] == "active" and loan["due"] == NOW + 5 + 10 * DAY and loan["accepted_at"] == NOW + 5


def test_accept_guards(eco):
    lid = offer(eco)
    assert eco.loan_accept(lid, C, NOW + 1) == (False, "この提案を承諾できるのは、借りる本人だけです。")
    assert eco.loan_accept(lid, B, NOW + loans.OFFER_HOURS * 3600) == (False, "承諾の期限が過ぎました。")
    eco.balance(B)
    eco._conn.execute("UPDATE users SET bankrupt_at=?, blacklist_until=NULL WHERE user_id=?", (NOW, B))
    assert "破産者" in eco.loan_accept(lid, B, NOW + 1)[1]  # 破産者のブラックリスト中は、借りられない
    eco._conn.execute("UPDATE users SET bankrupt_at=NULL WHERE user_id=?", (B,))
    eco._conn.execute("UPDATE users SET balance=10 WHERE user_id=?", (A,))  # 貸す側の残高が減った
    assert "貸す側の残高" in eco.loan_accept(lid, B, NOW + 1)[1]
    eco._conn.execute("UPDATE users SET balance=?", (INITIAL_BALANCE,))
    assert eco.loan_accept(lid, B, NOW + 1)[0] and eco.loan_accept(lid, B, NOW + 2) == (
        False,
        "この提案はもう終わっています。",
    )  # 二重に承諾できない


def test_there_is_no_cap_on_how_many_loans_one_can_borrow_but_overdue_blocks_new_ones(eco):
    for i in range(5):  # 以前は同時に3件まで。いまは上限がない
        active(eco, lender=100 + i, principal=100)
    lid = offer(eco, lender=200, principal=100)
    assert eco.loan_accept(lid, B, NOW + 5) == (True, "")
    eco._conn.execute("UPDATE loans SET status='overdue' WHERE borrower_id=? AND id=1", (B,))
    lid2 = offer(eco, lender=201, principal=100)
    assert "延滞中" in eco.loan_accept(lid2, B, NOW + 5)[1]


def test_principal_and_rate_have_no_upper_limit_but_the_lenders_balance_still_applies(eco):
    eco.credit(A, 5_000_000, "テスト")
    assert eco.loan_offer(A, B, 4_000_000, 5000, 10, 777, NOW)[0]  # 400万を、日利500%で
    assert not eco.loan_offer(A, C, 9_000_000, 20, 10, 777, NOW)[0]  # 貸す人の残高を超える額は、出せない
    assert loans.total_owed(4_000_000, 5000, 10, 0, 10 * 86400) == 4_000_000 + 4_000_000 * 5000 * 10 // 1000


def test_decline_and_cancel(eco):
    lid = offer(eco)
    assert eco.loan_decline(lid, C) == (False, "断れるのは、借りる本人だけです。")
    assert eco.loan_cancel(lid, B) == (False, "取り下げられるのは、貸す側だけです。")
    assert eco.loan_cancel(lid, A) == (True, "") and eco.loan_get(lid)["status"] == "cancelled"
    lid2 = offer(eco)
    assert eco.loan_decline(lid2, B) == (True, "") and eco.loan_get(lid2)["status"] == "declined"
    assert eco.loan_accept(lid2, B, NOW + 1)[0] is False and eco.balance(A) == INITIAL_BALANCE  # お金は動かない


# ----------------------------- 返済 ----------------------------- #
def test_repay_in_full_pays_daily_interest_and_closes(eco):
    lid = active(eco)  # 1000 / 日利2% / 10日
    day3 = NOW + 1 + 2 * DAY + 100  # 経過 2日と少し → 3日分
    ok, paid = eco.loan_repay(lid, B, None, day3)
    assert ok and paid == 1000 + 3 * 20 and eco.loan_get(lid)["status"] == "repaid"
    assert (
        eco.balance(A) == INITIAL_BALANCE + 60 and eco.balance(B) == INITIAL_BALANCE - 60
    )  # 貸した人が利息ぶん増え、借りた人が減る
    assert eco.loan_repay(lid, B, None, day3) == (False, "返す貸付が見つかりません。")


def test_partial_repayment_goes_to_principal_first_and_early_repay_saves_interest(eco):
    lid = active(eco)
    assert eco.loan_repay(lid, B, 400, NOW + 10)[0] and eco.loan_get(lid)["paid"] == 400
    loan = eco.loan_get(lid)
    assert eco.loan_remaining(loan, NOW + 10) == 1020 - 400  # 1日目: 元本1000 + 利息20 − 400
    assert eco.loan_repay(lid, B, 5000, NOW + 10) == (False, "返す額が残りを超えています（残り 620ペリカ）。")
    assert eco.loan_repay(lid, B, 620, NOW + 10)[0] and eco.loan_get(lid)["status"] == "repaid"
    assert eco.balance(B) == INITIAL_BALANCE + 1000 - 1020


def test_repay_guards(eco):
    lid = active(eco)
    assert eco.loan_repay(lid, C, None, NOW + 5) == (False, "返せるのは、借りた本人だけです。")
    eco._conn.execute("UPDATE users SET balance=0 WHERE user_id=?", (B,))
    assert "残高がありません" in eco.loan_repay(lid, B, None, NOW + 5)[1]
    eco._conn.execute("UPDATE users SET balance=100 WHERE user_id=?", (B,))
    assert "残高が足りません" in eco.loan_repay(lid, B, 500, NOW + 5)[1]
    ok, paid = eco.loan_repay(lid, B, None, NOW + 5)  # 省略なら、残高の範囲で
    assert ok and paid == 100 and eco.loan_get(lid)["status"] == "active"


# ----------------------------- 期限の自動回収・延滞 ----------------------------- #
def test_due_date_collects_automatically(eco):
    lid = active(eco)
    assert eco.loan_collect_due(NOW + 5 * DAY) == []  # まだ
    out = eco.loan_collect_due(NOW + 1 + 10 * DAY)
    assert [(o["id"], o["status"], o["collected"], o["left"]) for o in out] == [(lid, "repaid", 1200, 0)]
    assert eco.balance(A) == INITIAL_BALANCE + 200 and eco.loan_collect_due(NOW + 99 * DAY) == []


def earn(eco, user_id, amount):
    """収入（ゲームの利益・デイリーなど）が入ったことにする。借金や延滞があれば、一部が天引きされる。"""
    eco._apply(user_id, amount, "収入", income=amount)


def test_shortfall_becomes_overdue_then_is_collected_from_income(eco):
    lid = active(eco)  # 借りた1000を使い切った想定
    eco._conn.execute("UPDATE users SET balance=300 WHERE user_id=?", (B,))
    out = eco.loan_collect_due(NOW + 1 + 10 * DAY)
    assert out[0]["status"] == "overdue" and out[0]["collected"] == 300 and out[0]["left"] == 900
    assert eco.balance(B) == 0 and eco.loan_get(lid)["status"] == "overdue"
    assert eco.status(B) == teiai.UNDERGROUND  # 延滞で、地下労働者に落ちる
    earn(eco, B, 500)  # 収入の3割（150）が、天引きで回収される。残りは手元に残る
    assert eco.balance(B) == 350 and eco.loan_remaining(eco.loan_get(lid), NOW + 99 * DAY) == 750
    assert eco.balance(A) == INITIAL_BALANCE - 1000 + 300 + 150
    eco.credit(B, 1000, "付与")  # 収入ではない入金からは、天引きしない
    assert eco.loan_remaining(eco.loan_get(lid), NOW + 99 * DAY) == 750
    earn(eco, B, 10_000)  # 天引き3,000で、残り750を回収して完済
    assert eco.loan_get(lid)["status"] == "repaid" and eco.balance(A) == INITIAL_BALANCE + 200
    assert eco.status(B) == teiai.KATAGI  # 延滞がなくなれば、カタギに戻る
    ledger_total = eco._conn.execute("SELECT SUM(delta) FROM ledger WHERE user_id IN (?, ?)", (A, B)).fetchone()[0]
    assert ledger_total >= 0


def test_teiai_debt_is_withheld_before_overdue_loans(eco):
    lid = active(eco)
    eco._conn.execute("UPDATE users SET balance=0, debt=200 WHERE user_id=?", (B,))
    eco.loan_collect_due(NOW + 1 + 10 * DAY)
    assert eco.loan_get(lid)["status"] == "overdue"
    earn(eco, B, 500)  # 天引き150は、全部が帝愛（シニア債権）へ
    assert eco.debt(B) == 50 and eco.loan_get(lid)["paid"] == 0
    earn(eco, B, 500)  # 天引き150: 帝愛の残り50を払い、残りの100が貸付（ジュニア債権）へ
    assert eco.debt(B) == 0 and eco.loan_get(lid)["paid"] == 100


def test_a_debtor_can_still_borrow_from_other_players(eco):
    eco.balance(B)
    eco._conn.execute("UPDATE users SET debt=500 WHERE user_id=?", (B,))
    lid = offer(eco)
    assert eco.loan_accept(lid, B, NOW + 1) == (True, "")  # 借金中でも借りられる（借り換え）


def test_repayment_does_not_chain_into_the_receivers_own_overdue_loans(eco):
    l1 = active(eco, lender=A, borrower=B, principal=500)
    eco._conn.execute(
        "INSERT INTO loans (guild_id, lender_id, borrower_id, principal, rate_tenths, term_days, status, created, offer_expires, accepted_at, due, paid) "
        "VALUES (0, ?, ?, 400, 0, 5, 'overdue', 0, 0, 0, 1, 0)",
        (C, A),
    )  # A は C に400を延滞中
    eco.balance(C)
    eco.loan_repay(l1, B, 500, NOW + 5)
    assert eco.loan_get(2)["paid"] == 0  # 回収の連鎖は起きない（A の入金で、すぐ C へは払わない）


# ----------------------------- 一覧・通知 ----------------------------- #
def test_lists_reminders_and_expiry(eco):
    o = offer(eco)
    a = active(eco, lender=A, borrower=C)
    data = eco.loan_list(A)
    assert [x["id"] for x in data["lent"]] == [o, a] and data["borrowed"] == []
    assert [x["id"] for x in eco.loan_list(C)["borrowed"]] == [a]
    assert eco.loan_due_soon(NOW + 5 * DAY) == []
    soon = eco.loan_due_soon(NOW + 1 + 9 * DAY + 100)  # 期限の24時間前を切った
    assert [x["id"] for x in soon] == [a] and eco.loan_due_soon(NOW + 1 + 9 * DAY + 200) == []  # 1回だけ
    assert [x["id"] for x in eco.loan_expire_offers(NOW + 25 * 3600)] == [o] and eco.loan_get(o)["status"] == "expired"


# ----------------------------- ボタンとコマンド ----------------------------- #
class Resp:
    def __init__(self, log):
        self.log = log

    async def send_message(self, *a, **kw):
        self.log.append(("send", (kw.get("embed") or kw["embeds"][0]).title))

    async def edit_message(self, **kw):
        self.log.append(("edit", kw["embed"].title, kw["view"]))


def click(uid, log):
    return SimpleNamespace(user=SimpleNamespace(id=uid), guild_id=0, response=Resp(log))


def make_bot(eco, owners=()):
    return SimpleNamespace(economy=eco, config={"owner_ids": list(owners)}, guilds=[])


def test_buttons_accept_decline_cancel_with_the_right_people(eco, monkeypatch):
    monkeypatch.setattr("cogs.loan_cog.approval.is_allowed", lambda *a: True)
    monkeypatch.setattr("cogs.loan_cog.time.time", lambda: NOW + 5)
    bot = make_bot(eco, owners=[OWNER])
    lid = offer(eco)
    log = []
    asyncio.run(handle_button(bot, click(C, log), "accept", lid))  # 関係ない人
    assert log[-1] == ("send", "❌ できません") and eco.loan_get(lid)["status"] == "offer"
    asyncio.run(handle_button(bot, click(B, log), "accept", lid))
    assert log[-1][:2] == ("edit", "🤝 貸付 #1 成立") and log[-1][2] is None and eco.loan_get(lid)["status"] == "active"
    lid2 = offer(eco)
    asyncio.run(handle_button(bot, click(B, log), "cancel", lid2))  # 貸す側以外は、取り下げられない
    assert log[-1][0] == "send" and eco.loan_get(lid2)["status"] == "offer"
    asyncio.run(handle_button(bot, click(A, log), "cancel", lid2))
    assert log[-1][1].endswith("取り下げられました") and eco.loan_get(lid2)["status"] == "cancelled"
    lid3 = offer(eco, principal=100)
    asyncio.run(handle_button(bot, click(B, log), "decline", lid3))
    assert log[-1][1].endswith("辞退されました")


def test_dynamic_button_id_round_trip():
    import re

    item = LoanButton("accept", 42)
    assert item.item.custom_id == "loan:accept:42"
    m = re.fullmatch(LoanButton.__discord_ui_compiled_template__.pattern, "loan:cancel:7")
    restored = asyncio.run(LoanButton.from_custom_id(None, None, m))
    assert (restored.act, restored.loan_id) == ("cancel", 7)


def test_offer_embed_and_names(eco):
    lid = offer(eco)
    e = offer_embed(eco.loan_get(lid))
    assert (
        f"<@{A}>" in e.description
        and "1,200ペリカ" in e.description
        and "日利: **2%**" in e.description
        and who(5) == "<@5>"
    )


class Chan:
    id = 777

    def __init__(self):
        self.sent, self.edits = [], []

    def permissions_for(self, _me):
        return SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)

    async def send(self, content=None, **kw):
        self.sent.append((content, kw.get("embed")))
        return SimpleNamespace(id=5000)

    def get_partial_message(self, mid):
        chan = self

        class M:
            async def edit(self, **kw):
                chan.edits.append(kw["embed"].title)

        return M()


def test_run_once_collects_notifies_and_expires(eco, monkeypatch):
    monkeypatch.setattr("cogs.loan_cog.approval.is_allowed", lambda *a: True)
    chan = Chan()
    guild = SimpleNamespace(id=0, me=object(), system_channel=chan, get_channel=lambda cid: chan)
    bot = SimpleNamespace(economy=SimpleNamespace(for_guild=lambda gid: eco), config={}, guilds=[guild])
    cog = LoanCog(bot)
    now = 1_700_000_000.0
    monkeypatch.setattr("cogs.loan_cog.time.time", lambda: now)
    pending = eco.loan_offer(A, B, 100, 20, 5, 777, now - 2 * DAY)[1]  # とっくに承諾期限切れ
    eco.loan_set_message(pending, 4242)
    soon = eco.loan_offer(A, C, 500, 10, 3, 777, now - 3 * DAY)[1]
    assert eco.loan_accept(soon, C, now - 3 * DAY + 10)[0]  # 期限まで数分
    eco._conn.execute("UPDATE loans SET due=? WHERE id=?", (now + 3600, soon))
    asyncio.run(cog.run_once())
    assert chan.edits == ["🤝 貸付 #1 承諾の期限が切れました"] and any(
        "⏰" in (t or "") and "貸付 #2" in t for t, _ in chan.sent
    )
    eco._conn.execute("UPDATE users SET balance=0 WHERE user_id=?", (C,))
    asyncio.run(cog.run_once())
    eco._conn.execute("UPDATE loans SET due=1 WHERE id=?", (soon,))
    asyncio.run(cog.run_once())
    assert any("⚠️" in (t or "") and "延滞" in t for t, _ in chan.sent) and eco.loan_get(soon)["status"] == "overdue"


def test_commands_offer_repay_and_list(eco, monkeypatch):
    monkeypatch.setattr("cogs.loan_cog.approval.is_allowed", lambda *a: True)
    bot = SimpleNamespace(economy=SimpleNamespace(for_guild=lambda gid: eco), config={}, guilds=[])
    cog = LoanCog(bot)
    log = []

    class R:
        async def send_message(self, *a, **kw):
            log.append(kw)

    async def original():
        return SimpleNamespace(id=6001)

    def inter(uid):
        return SimpleNamespace(
            user=SimpleNamespace(id=uid, display_name=f"ユーザー{uid}"),
            guild_id=0,
            channel_id=777,
            response=R(),
            original_response=original,
        )

    bad = SimpleNamespace(id=B, bot=True)
    asyncio.run(cog.offer.callback(cog, inter(A), bad, 1000, 2.0, 10))
    assert log[-1]["embed"].title == "❌ Bot には貸せません"
    member = SimpleNamespace(id=B, bot=False)
    asyncio.run(cog.offer.callback(cog, inter(A), member, 1000, 2.5, 10))
    assert (
        log[-1]["embed"].title == "🤝 貸付の提案 #1"
        and log[-1]["content"] == f"<@{B}>"
        and eco.loan_get(1)["message_id"] == 6001
        and eco.loan_get(1)["rate_tenths"] == 25
    )
    asyncio.run(cog.offer.callback(cog, inter(A), SimpleNamespace(id=A, bot=False), 1000, 2.0, 10))
    assert log[-1]["embed"].title == "❌ 提案できません"
    assert eco.loan_accept(1, B, NOW)[0]
    asyncio.run(cog.listing.callback(cog, inter(B)))
    e = log[-1]["embed"]
    assert "ephemeral" not in log[-1] and e.title == f"🤝 ユーザー{B} の貸し借り"  # 一覧はみんなに見える
    assert f"<@{A}>" in e.fields[0].value and "元本1,000" in e.fields[0].value and e.fields[1].value == "なし"
    asyncio.run(cog.repay.callback(cog, inter(B), 1, 100))
    assert (
        log[-1]["embed"].title.startswith("✅ #1 を 100ペリカ 返しました") and "ephemeral" not in log[-1]
    )  # 返済の完了もみんなに見える
    assert f"借りた人: <@{B}>　貸した人: <@{A}>" in log[-1]["embed"].description
    choices = asyncio.run(cog.repay_autocomplete(inter(B), ""))
    assert [c.value for c in choices] == [1] and choices[0].name.startswith("#1 残り")
    assert asyncio.run(cog.repay_autocomplete(inter(C), "")) == []
    asyncio.run(cog.repay.callback(cog, inter(C), 1, None))
    assert log[-1]["embed"].title == "❌ 返せません" and log[-1]["ephemeral"] is True  # 失敗は本人だけ


def test_daily_rate_up_to_one_hundred_percent_is_computed_and_collected(eco):
    assert (
        loans.rate_text(1000) == "100%"
        and loans.interest(1000, 1000, 3) == 3000
        and loans.full_term_total(1000, 1000, 30) == 31_000
    )
    lid = active(eco, principal=1000, rate=1000, days=2)
    eco.credit(B, 10_000, "テスト")  # 残高を増やして、返せるようにする
    ok, paid = eco.loan_repay(lid, B, None, NOW + 1 + 100)  # 1日目: 利息 100% = 1000
    assert ok and paid == 2000 and eco.loan_get(lid)["status"] == "repaid"


# ----------------------------- 募集（借りたい人が出す） ----------------------------- #
def request(eco, borrower=B, principal=1000, rate=20, days=10, now=NOW):
    ok, result = eco.loan_request(borrower, principal, rate, days, 777, now)
    assert ok, result
    return result


def test_request_then_first_lender_gets_it_and_money_moves(eco):
    lid = request(eco)
    assert eco.loan_get(lid)["status"] == "request" and eco.loan_get(lid)["lender_id"] == 0
    assert eco.loan_lend(lid, A, NOW + 5) == (True, "")
    loan = eco.loan_get(lid)
    assert loan["status"] == "active" and loan["lender_id"] == A and loan["due"] == NOW + 5 + 10 * DAY
    assert eco.balance(A) == INITIAL_BALANCE - 1000 and eco.balance(B) == INITIAL_BALANCE + 1000
    ok, reason = eco.loan_lend(lid, C, NOW + 6)  # 早い者勝ち: 2人目は貸せない
    assert not ok and "もう終わって" in reason


def test_request_rules(eco):
    lid = request(eco)
    ok, reason = eco.loan_request(B, 1000, 20, 10, 777, NOW)  # 同時に出せるのは1件
    assert not ok and "1 件" in reason
    assert eco.loan_lend(lid, B, NOW)[1] == "自分の募集には、貸せません。"
    assert "期限" in eco.loan_lend(lid, A, NOW + loans.OFFER_HOURS * 3600)[1]
    ok, reason = eco.loan_request(C, 50, 20, 10, 777, NOW)
    assert not ok and "以上" in reason
    ok, reason = eco.loan_request(C, 1000, 20, 99, 777, NOW)
    assert not ok and "期間" in reason


def test_lender_needs_balance_and_borrower_state_is_rechecked(eco):
    lid = request(eco, principal=INITIAL_BALANCE + 1)
    assert "残高が足りません" in eco.loan_lend(lid, A, NOW)[1]
    lid2 = request(eco, borrower=C)
    eco._conn.execute("UPDATE users SET bankrupt_at=?, blacklist_until=? WHERE user_id=?", (NOW, time.time() + DAY, C))
    ok, reason = eco.loan_lend(lid2, A, NOW + 1)  # 募集のあとで破産者になった
    assert not ok and "借りられない状態" in reason
    assert eco.balance(A) == INITIAL_BALANCE  # お金は動いていない


def test_request_cancel_expire_and_borrower_block(eco):
    lid = request(eco)
    assert eco.loan_cancel(lid, A) == (False, "取り下げられるのは、募集を出した人だけです。")
    assert eco.loan_cancel(lid, B) == (True, "")
    assert eco.loan_get(lid)["status"] == "cancelled"
    lid = request(eco)
    assert [r["id"] for r in eco.loan_expire_offers(NOW + loans.OFFER_HOURS * 3600 + 1)] == [lid]
    active(eco, lender=A, borrower=C)
    eco._conn.execute("UPDATE loans SET status='overdue' WHERE borrower_id=?", (C,))
    ok, reason = eco.loan_request(C, 1000, 20, 10, 777, NOW)
    assert not ok and "延滞中" in reason


def test_credit_info_counts_history_and_hides_assets(eco):
    lid = active(eco, lender=A, borrower=B)
    eco.loan_repay(lid, B, None, NOW + 2)
    active(eco, lender=A, borrower=B, principal=500)
    c = eco.loan_credit(B)
    assert c["repaid"] == 1 and c["active"] == 1 and c["owed"] == 500 and c["overdue"] == 0
    assert c["status"] == teiai.KATAGI and "balance" not in c
    eco._conn.execute("UPDATE loans SET status='discharged' WHERE borrower_id=? AND status='active'", (B,))
    assert eco.loan_credit(B)["discharged"] == 1
