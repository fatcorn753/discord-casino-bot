"""クイズ: 登録、1問1回の回答、報酬、期限、サーバーごとの回答、操作できる人。"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from cogs.quiz_cog import AddModal, AnswerView, PublicButton, QuizCog, parse_choices, public_embed, question_embed, record_answer
from core.economy import INITIAL_BALANCE, Economy

NOW = 1_000_000.0


@pytest.fixture
def eco(tmp_path):
    return Economy(str(tmp_path / "t.db"))


def add(eco, q="首都は？", reward=100, expires=None, choices=("東京", "大阪", "京都", "名古屋")):
    return eco.quiz_add(q, list(choices), "解説です", reward, expires, NOW)


def test_correct_answer_pays_once_and_wrong_pays_nothing(eco):
    qid = add(eco)
    ok, res = eco.quiz_answer(1, qid, "東京", NOW)
    assert ok and res["correct"] and res["reward"] == 100 and eco.balance(1) == INITIAL_BALANCE + 100
    assert eco.quiz_answer(1, qid, "東京", NOW) == (False, "この問題にはもう答えています。")       # 二重に受け取れない
    ok, res = eco.quiz_answer(2, qid, "大阪", NOW)
    assert ok and not res["correct"] and res["answer"] == "東京" and eco.balance(2) == INITIAL_BALANCE
    assert not eco.quiz_answer(2, qid, "東京", NOW)[0]                                           # 間違えたあとのやり直し不可


def test_next_skips_answered_and_expired_in_order(eco):
    a, b, c = add(eco, "A"), add(eco, "B", expires=NOW + 10), add(eco, "C")
    q, n = eco.quiz_next(1, NOW)
    assert (q["id"], n) == (a, 3)
    eco.quiz_answer(1, a, "東京", NOW)
    assert eco.quiz_next(1, NOW)[0]["id"] == b
    assert eco.quiz_next(1, NOW + 11)[0]["id"] == c and eco.quiz_next(1, NOW + 11)[1] == 1       # 期限切れは出ない
    assert eco.quiz_next(2, NOW)[1] == 3                                                          # 別の人には全部残る
    assert not eco.quiz_answer(1, b, "東京", NOW + 11)[0]                                         # 期限後の回答は不可


def test_empty_bank(eco):
    assert eco.quiz_next(1, NOW) == (None, 0)


@pytest.mark.real_scope
def test_answers_are_per_guild_but_questions_are_shared(tmp_path):
    eco = Economy(str(tmp_path / "t.db"))
    qid = add(eco)
    g1, g2 = eco.for_guild(10), eco.for_guild(20)
    assert g1.quiz_answer(1, qid, "東京", NOW)[0]
    assert g1.quiz_next(1, NOW)[0] is None and g2.quiz_next(1, NOW)[0]["id"] == qid             # 別サーバーではまた答えられる


@pytest.mark.real_scope
def test_reward_goes_to_the_guild_account_where_answered(tmp_path):
    eco = Economy(str(tmp_path / "t.db"))
    qid = add(eco)
    eco.for_guild(10).quiz_answer(1, qid, "東京", NOW)
    assert eco.for_guild(10).balance(1) == INITIAL_BALANCE + 100 and eco.for_guild(20).balance(1) == INITIAL_BALANCE


def test_list_counts_and_delete_removes_answers(eco):
    qid = add(eco)
    eco.quiz_answer(1, qid, "東京", NOW)
    eco.quiz_answer(2, qid, "大阪", NOW)
    row = eco.quiz_list()[0]
    assert (row["answered"], row["right_count"]) == (2, 1)
    assert eco.quiz_delete(qid) and not eco.quiz_delete(qid) and eco.quiz_list() == []


def test_reward_repays_debt(eco):
    qid = add(eco, reward=300)
    eco.stats(1)
    eco._conn.execute("UPDATE users SET debt=200 WHERE user_id=1")
    eco.quiz_answer(1, qid, "東京", NOW)
    assert eco._conn.execute("SELECT debt FROM users WHERE user_id=1").fetchone()[0] == 0


def test_parse_choices_rules():
    assert parse_choices("a\n b \n\nc\nd") == ["a", "b", "c", "d"]
    for bad in ("a\nb\nc", "a\nb\nc\nc", "a\nb\nc\nd\ne", "a\nb\nc\n" + "x" * 81):
        with pytest.raises(ValueError):
            parse_choices(bad)


def test_question_embed_lists_shuffled_choices(eco):
    qid = add(eco)
    q = eco.quiz_get(qid)
    e = question_embed(q, ["大阪", "東京", "京都", "名古屋"], 3)
    assert "**B.** 東京" in e.description and "あと3問" in e.footer.text


class Resp:
    def __init__(self, log):
        self.log = log

    async def send_message(self, **kw):
        self.log.append(("send", kw["embed"].title))

    async def edit_message(self, **kw):
        self.log.append(("edit", kw["embed"].title, kw["view"]))

    async def defer(self, **kw):
        self.log.append(("defer",))

    async def send_modal(self, modal):
        self.log.append(("modal", type(modal).__name__))


def make_cog(eco, owners=(99,)):
    bot = SimpleNamespace(config={"owner_ids": list(owners)}, guilds=[],
                          economy=SimpleNamespace(for_guild=lambda gid: eco, quiz_add=eco.quiz_add, quiz_list=eco.quiz_list, quiz_delete=eco.quiz_delete, quiz_get=eco.quiz_get))
    return QuizCog(bot)


def test_play_flow_buttons_pay_the_player_only(eco):
    cog, log = make_cog(eco), []
    qid = add(eco)
    inter = SimpleNamespace(user=SimpleNamespace(id=1), guild_id=1, response=Resp(log))
    asyncio.run(cog.play.callback(cog, inter))
    assert log == [("send", "📰 クイズ #1")] and qid == 1

    async def run():
        view = AnswerView(cog, 1, eco.quiz_get(qid), ["大阪", "東京", "京都", "名古屋"])
        stranger = SimpleNamespace(user=SimpleNamespace(id=2), response=Resp(log))
        assert await view.interaction_check(stranger) is False                       # 他人は押せない
        mine = SimpleNamespace(user=SimpleNamespace(id=1), guild_id=1, response=Resp(log))
        await view.children[1].callback(mine)                                          # B = 東京（正解）
    asyncio.run(run())
    assert log[-1][:2] == ("edit", "⭕ 正解！") and log[-1][2] is None                 # ボタンは外れる
    assert eco.balance(1) == INITIAL_BALANCE + 100 and eco.balance(2) == INITIAL_BALANCE


def test_play_with_empty_bank(eco):
    cog, log = make_cog(eco), []
    asyncio.run(cog.play.callback(cog, SimpleNamespace(user=SimpleNamespace(id=1), guild_id=1, response=Resp(log))))
    assert log == [("send", "📰 クイズ")]


def test_admin_helpers_list_and_remove(eco):
    cog = make_cog(eco)
    assert "まだありません" in cog.listing_embed().description
    qid = add(eco)
    eco.quiz_answer(1, qid, "東京", NOW)
    assert f"#{qid}" in cog.listing_embed().description and "1/1人正解" in cog.listing_embed().description
    assert isinstance(cog.add_modal(False), AddModal) and cog.add_modal(False).announce is False
    assert cog.remove(qid) and not cog.remove(qid)


def test_modal_validates_and_saves(eco):
    cog, log = make_cog(eco), []

    def modal(choices, reward="150", days="3日"):
        m = AddModal(cog, announce=False)
        for field, value in ((m.question, "問題"), (m.choices, choices), (m.explanation, "解説"), (m.terms, f"{reward} / {days}"), (m.verdicts, "")):
            field._value = value
        return m

    followups = []

    class Follow:
        async def send(self, **kw):
            followups.append(kw["embed"].title)

    inter = SimpleNamespace(user=SimpleNamespace(id=99, display_name="持ち主"), response=Resp(log), followup=Follow())
    asyncio.run(modal("a\nb\nc").on_submit(inter))                                    # 選択肢が足りない
    asyncio.run(modal("a\nb\nc\nd", reward="abc").on_submit(inter))                   # 数字ではない
    asyncio.run(modal("a\nb\nc\nd", reward="0").on_submit(inter))                     # 範囲外
    assert log == [("send", "❌ 入力エラー")] * 3 and eco.quiz_list() == []
    log.clear()
    asyncio.run(modal("a\nb\nc\nd").on_submit(inter))
    assert log == [("defer",)] and followups == ["✅ クイズ #1 を登録しました"]
    q = eco.quiz_list()[0]
    assert (q["choices"][0], q["reward"]) == ("a", 150) and q["expires"] is not None


def test_display_order_is_fixed_at_registration_and_keeps_the_answer(eco):
    qid = add(eco)
    first = eco.quiz_get(qid)["order"]
    assert sorted(first) == sorted(["東京", "大阪", "京都", "名古屋"]) and eco.quiz_get(qid)["order"] == first
    assert eco.quiz_list()[0]["order"] == first
    seen = {tuple(eco.quiz_get(add(eco))["order"]) for _ in range(30)}
    assert len(seen) > 1                                                 # 登録のたびに並びが変わる


def test_old_questions_without_a_saved_order_get_one_on_upgrade(tmp_path):
    import sqlite3
    path = str(tmp_path / "t.db")
    Economy(path)
    c = sqlite3.connect(path)
    c.executescript("""DROP TABLE quiz_questions; CREATE TABLE quiz_questions (id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL,
        choices TEXT NOT NULL, explanation TEXT NOT NULL DEFAULT '', reward INTEGER NOT NULL, expires REAL, created REAL NOT NULL);
        INSERT INTO quiz_questions (question, choices, reward, created) VALUES ('q', '["a","b","c","d"]', 100, 1);""")
    c.commit()
    c.close()
    first = Economy(path).quiz_get(1)["order"]
    assert sorted(first) == ["a", "b", "c", "d"] and Economy(path).quiz_get(1)["order"] == first        # 一度決めたら変わらない


def test_public_post_has_four_buttons_that_answer_for_the_clicker(eco):
    cog, log = make_cog(eco), []
    qid = add(eco, expires=9e12)
    q = eco.quiz_get(qid)
    assert "新しいクイズ" in public_embed(q).title and "⏳ 期限" in public_embed(q).description
    pos = q["order"].index("東京")
    btn = PublicButton(qid, pos)
    assert btn.item.custom_id == f"quiz:{qid}:{pos}"

    public = []

    class Follow:
        async def send(self, text, **kw):
            public.append(text)

    def click(uid):
        return SimpleNamespace(user=SimpleNamespace(id=uid), guild_id=1, client=cog.bot, response=Resp(log), followup=Follow())
    asyncio.run(btn.callback(click(1)))
    assert log[-1] == ("send", "⭕ 正解！") and eco.balance(1) == INITIAL_BALANCE + 100
    assert public == ["⭕ <@1> が #1 に **正解！**（+100ペリカ）"]                       # みんなに見える形で、その場で出る
    asyncio.run(btn.callback(click(1)))                                                 # 二重に押しても入らない・公開もしない
    assert log[-1] == ("send", "❌ 答えられません") and eco.balance(1) == INITIAL_BALANCE + 100 and len(public) == 1
    asyncio.run(PublicButton(qid, (pos + 1) % 4).callback(click(2)))                    # 不正解
    assert log[-1] == ("send", "❌ 不正解…") and eco.balance(2) == INITIAL_BALANCE
    assert public[-1] == "❌ <@2> が #1 に **不正解…**" and "東京" not in public[-1]       # 正解は公開しない
    eco.quiz_delete(qid)
    asyncio.run(record_answer(cog.bot, click(3), qid, 0))
    assert log[-1] == ("send", "❌ この問題は終了しました")


def test_broadcast_posts_to_every_allowed_guild_channel(eco, monkeypatch):
    cog = make_cog(eco)
    qid = add(eco)
    sent = []

    class Chan:
        async def send(self, **kw):
            sent.append((kw["embed"].title, len(kw["view"].children)))

    cog.bot.guilds = [SimpleNamespace(id=1), SimpleNamespace(id=2), SimpleNamespace(id=3)]
    cog.bot.economy.is_approved = lambda *a: True
    monkeypatch.setattr("cogs.quiz_cog.approval.is_allowed", lambda eco_, cfg, gid, uid: gid != 2)            # 2は許可されていない
    monkeypatch.setattr("cogs.quiz_cog.announce_channel", lambda guild, e: None if guild.id == 3 else Chan())  # 3は投稿先がない
    assert asyncio.run(cog.broadcast(qid)) == 1 and sent == [("📰 新しいクイズ #1", 4)]


# ----------------------------- ユーザーが出す問題 ----------------------------- #
from core import quizpay  # noqa: E402

AUTHOR = 500


def post(eco, reward=100, days=3, now=NOW, author=AUTHOR, choices=("東京", "大阪", "京都", "名古屋"), minutes=None, name=""):
    return eco.quiz_post(author, "首都は？", list(choices), "解説", reward, minutes if minutes is not None else days * 1440, 777, now, name)


def test_bonus_peaks_at_forty_percent_and_fades_to_zero():
    assert quizpay.bonus(10, 4) == 200 and quizpay.bonus(5, 2) == 200
    assert quizpay.bonus(10, 2) == 100 and quizpay.bonus(10, 6) == 100          # 20%・60% は半分
    assert quizpay.bonus(10, 0) == 0 and quizpay.bonus(10, 8) == 0 and quizpay.bonus(10, 10) == 0
    assert quizpay.bonus(0, 0) == 0
    assert quizpay.author_payout(1, 0, 100) == (0, 0, 0) and quizpay.author_payout(0, 0, 100) == (0, 0, 0)
    assert quizpay.author_payout(2, 1, 100) == (100, quizpay.bonus(2, 1), quizpay.participation(2, 1, 100))


def test_correct_answer_is_paid_from_the_authors_balance(eco):
    ok, qid = post(eco, reward=300)
    assert ok
    assert eco.quiz_answer(1, qid, "東京", NOW)[1]["reward"] == 300
    assert eco.balance(1) == INITIAL_BALANCE + 300 and eco.balance(AUTHOR) == INITIAL_BALANCE - 300      # 出題者から移るだけ
    assert eco.balance(AUTHOR) == INITIAL_BALANCE - 300                                                   # 1人目では出題報酬はまだ
    assert eco.quiz_answer(2, qid, "大阪", NOW)[1]["correct"] is False
    assert eco.balance(AUTHOR) == INITIAL_BALANCE - 300 + sum(quizpay.author_payout(2, 1, 300))            # 2人目で出題報酬（報酬300なので倍率√3）。不正解では報酬は払わない


def test_author_cannot_answer_and_does_not_see_it_in_next(eco):
    ok, qid = post(eco)
    assert eco.quiz_answer(AUTHOR, qid, "東京", NOW) == (False, "自分が出した問題には答えられません。")
    assert eco.quiz_next(AUTHOR, NOW) == (None, 0) and eco.quiz_next(1, NOW)[0]["id"] == qid


def test_question_closes_when_the_author_can_no_longer_pay(eco):
    ok, qid = post(eco, reward=1000)
    for uid in (1, 2, 3):
        assert eco.quiz_answer(uid, qid, "東京", NOW)[0]
    assert eco.balance(AUTHOR) == quizpay.base_reward(1000)               # 3,000 から 3,000 払い、出題報酬(基本316)を受け取った
    assert eco.quiz_next(4, NOW) == (None, 0)                              # 出なくなる
    assert eco.quiz_answer(4, qid, "東京", NOW) == (False, "出題者の残高が尽きたので、この問題は終了しました。")
    assert eco.balance(4) == INITIAL_BALANCE and eco.balance(AUTHOR) == quizpay.base_reward(1000)   # マイナスにはならない


def test_post_limits(eco):
    assert not post(eco, reward=INITIAL_BALANCE + 1)[0]                    # 報酬に足りない残高
    assert post(eco)[0] and post(eco)[0]
    ok, reason = post(eco)
    assert not ok and "同時に出せる問題は 2" in reason
    eco.stats(7)
    eco._conn.execute("UPDATE users SET debt=100 WHERE user_id=7")
    assert not post(eco, author=7)[0]                                       # 借金中
    # 24時間に3問: 古い2問を取り下げても、3問目まで
    other = 600
    assert post(eco, author=other, days=1)[0] and post(eco, author=other, days=1)[0]
    eco.quiz_withdraw(other, 4, NOW + 10)
    assert post(eco, author=other, now=NOW + 20)[0]
    eco.quiz_withdraw(other, 5, NOW + 30)
    ok, reason = post(eco, author=other, now=NOW + 40)
    assert not ok and "24時間に 3" in reason


def test_settle_pays_base_plus_bonus_once_after_expiry(eco):
    ok, qid = post(eco, reward=10, days=1)
    for uid, pick in ((1, "東京"), (2, "東京"), (3, "大阪"), (4, "大阪"), (5, "京都")):      # 5人中2人正解 = 40%
        eco.quiz_answer(uid, qid, pick, NOW)
    assert eco.quiz_settle(NOW + DAY - 1) == []                             # まだ期限前
    done = eco.quiz_settle(NOW + DAY)
    assert [(d["participants"], d["correct_count"], d["base"], d["bonus"], d["part"]) for d in done] == [(5, 2, 32, 63, 50)]    # 報酬10 → 倍率√0.1。参加報酬 = 1.0×10×5
    assert eco.balance(AUTHOR) == INITIAL_BALANCE - 20 + 145
    assert eco.quiz_settle(NOW + 9 * DAY) == []                             # 二重に払わない


def test_settle_needs_two_participants_and_lists_who_answered(eco):
    ok, none_q = post(eco, days=1)
    ok, two_q = post(eco, days=1)
    ok, one_q = post(eco, days=1, author=600)
    eco.quiz_answer(3, one_q, "東京", NOW)
    eco.quiz_answer(1, two_q, "東京", NOW)
    eco.quiz_answer(2, two_q, "大阪", NOW)                                  # 2人中1人正解 = 50%
    done = {d["id"]: d for d in eco.quiz_settle(NOW + DAY)}
    assert (done[none_q]["base"], done[none_q]["bonus"], done[none_q]["part"]) == (0, 0, 0)           # 参加者0人は報酬なし
    assert (done[one_q]["base"], done[one_q]["bonus"], done[one_q]["part"]) == (0, 0, 0) and eco.balance(600) == INITIAL_BALANCE - 100      # 1人では出題報酬なし（正解者への100だけ払った）
    assert (done[two_q]["right_users"], done[two_q]["wrong_users"]) == ([1], [2])      # 誰が正解・不正解か
    assert (done[two_q]["base"], done[two_q]["bonus"], done[two_q]["part"]) == (100, 150, 150) and quizpay.bonus(2, 1) == 150      # 50%: f=0.75、参加報酬 0.75×100×2
    assert eco.balance(AUTHOR) == INITIAL_BALANCE - 100 + 100 + 150 + 150     # 払った報酬 100、受け取った 100+150+150


def test_withdraw_only_own_question_and_settles_now(eco):
    ok, qid = post(eco)
    for uid in (1, 2, 3):
        eco.quiz_answer(uid, qid, "東京" if uid == 1 else "大阪", NOW)
    assert eco.quiz_withdraw(7, qid, NOW + 5) is None                       # 他人は取り下げられない
    done = eco.quiz_withdraw(AUTHOR, qid, NOW + 5)
    assert (done["participants"], done["base"]) == (3, 100) and eco.quiz_next(9, NOW + 6) == (None, 0)
    assert eco.quiz_withdraw(AUTHOR, qid, NOW + 6) is None                  # 二重に取り下げられない
    assert not eco.quiz_answer(9, qid, "東京", NOW + 6)[0]


@pytest.mark.real_scope
def test_user_questions_stay_in_their_guild(tmp_path):
    eco = Economy(str(tmp_path / "t.db"))
    g1, g2 = eco.for_guild(10), eco.for_guild(20)
    ok, qid = post(g1)
    assert g2.quiz_next(1, NOW) == (None, 0) and g1.quiz_next(1, NOW)[0]["id"] == qid
    assert g2.quiz_answer(1, qid, "東京", NOW)[0] is False
    eco.quiz_add("全体", ["a", "b", "c", "d"], "", 50, None, NOW)             # 持ち主の問題は全サーバー
    assert g2.quiz_next(1, NOW)[0]["author_id"] == 0


def test_owner_questions_unchanged_and_not_paid_by_anyone(eco):
    qid = add(eco)
    assert eco.quiz_answer(AUTHOR, qid, "東京", NOW)[0]                      # 持ち主の問題は、誰でも答えられる
    assert eco.quiz_settle(NOW + 9 * DAY) == []


def test_old_schema_gets_author_columns(tmp_path):
    import sqlite3
    path = str(tmp_path / "t.db")
    Economy(path)
    c = sqlite3.connect(path)
    c.executescript("""DROP TABLE quiz_questions; CREATE TABLE quiz_questions (id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL,
        choices TEXT NOT NULL, explanation TEXT NOT NULL DEFAULT '', shown_order TEXT NOT NULL DEFAULT '', reward INTEGER NOT NULL, expires REAL, created REAL NOT NULL);
        INSERT INTO quiz_questions (question, choices, reward, created) VALUES ('q', '["a","b","c","d"]', 100, 1);""")
    c.commit()
    c.close()
    q = Economy(path).quiz_get(1)
    assert (q["author_id"], q["guild_id"], q["settled"]) == (0, 0, 0)


DAY = 86400


class Msg:
    def __init__(self):
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)


class Channel:
    def __init__(self):
        self.sent, self.embeds, self.msg = [], [], Msg()

    def get_partial_message(self, mid):
        return self.msg

    async def send(self, content=None, **kw):
        self.sent.append((content, kw["embed"].title))
        self.embeds.append(kw["embed"])


def user_inter(log, uid=AUTHOR, guild_id=1, channel_id=777, guild=None):
    sent = {}

    class R(Resp):
        async def send_message(self, **kw):
            log.append(("send", kw["embed"].title))
            sent["kw"] = kw

    async def original():
        return SimpleNamespace(id=9090)

    return SimpleNamespace(user=SimpleNamespace(id=uid, display_name="出題者さん"), guild_id=guild_id, channel_id=channel_id, response=R(log), original_response=original, guild=guild), sent


def test_user_modal_posts_publicly_and_saves_the_message(eco):
    from cogs.quiz_cog import UserModal
    cog, log = make_cog(eco), []
    m = UserModal(cog)
    for field, value in ((m.question, "問題"), (m.choices, "a\nb\nc\nd"), (m.explanation, ""), (m.terms, "250"), (m.verdicts, "")):
        field._value = value
    inter, sent = user_inter(log)
    asyncio.run(m.on_submit(inter))
    q = eco.quiz_list()[0]
    assert log == [("send", "📰 みんなのクイズ #1")] and len(sent["kw"]["view"].children) == 4
    assert (q["author_id"], q["reward"], q["message_id"], q["channel_id"]) == (AUTHOR, 250, 9090, 777)
    assert abs(q["expires"] - q["created"] - 3 * DAY) < 1                       # 日数の既定は3日
    m.terms._value = "1001"                                                    # 上限超え
    log.clear()
    asyncio.run(m.on_submit(inter))
    assert log == [("send", "❌ 入力エラー")] and len(eco.quiz_list()) == 1


def test_public_button_rejects_the_author(eco):
    cog, log = make_cog(eco), []
    ok, qid = post(eco, now=time.time(), days=3)                              # 期限は十分先
    pos = eco.quiz_get(qid)["order"].index("東京")
    click = SimpleNamespace(user=SimpleNamespace(id=AUTHOR), guild_id=1, client=cog.bot, response=Resp(log))
    asyncio.run(PublicButton(qid, pos).callback(click))
    assert log[-1] == ("send", "❌ 答えられません") and eco.balance(AUTHOR) == INITIAL_BALANCE


def test_run_once_settles_and_announces_the_result(eco, monkeypatch):
    monkeypatch.setattr("cogs.quiz_cog.approval.is_allowed", lambda *a: True)
    cog, ch = make_cog(eco), Channel()
    cog.bot.guilds = [SimpleNamespace(id=1, get_channel=lambda cid: ch)]
    ok, qid = post(eco, reward=10, days=1, now=1.0)                              # とっくに期限切れ
    eco.quiz_set_message(qid, 4242)
    for uid, pick in ((1, "東京"), (2, "東京"), (3, "大阪"), (4, "大阪"), (5, "京都")):
        eco.quiz_answer(uid, qid, pick, 1.0)
    asyncio.run(cog.run_once())
    assert ch.sent and ch.sent[0][0] == f"<@{AUTHOR}>" and "終了" in ch.sent[0][1]
    shown = ch.embeds[0]
    assert [f.name for f in shown.fields] == ["⭕ 正解者（2人）", "❌ 不正解者（3人）"] and shown.fields[0].value == "<@1> <@2>" and shown.fields[1].value == "<@3> <@4> <@5>"
    assert ch.msg.edits and ch.msg.edits[0]["view"] is None
    assert eco.balance(AUTHOR) == INITIAL_BALANCE - 20 + 145
    ch.sent.clear()
    asyncio.run(cog.run_once())
    assert ch.sent == []                                                        # 二重には精算・告知しない


def test_withdraw_command(eco, monkeypatch):
    cog, log, ch = make_cog(eco), [], Channel()
    ok, qid = post(eco)
    inter, _ = user_inter(log, guild=SimpleNamespace(id=1, get_channel=lambda cid: ch))
    asyncio.run(cog.withdraw.callback(cog, inter, qid + 5))
    assert log[-1] == ("send", "❌ 取り下げられません")
    asyncio.run(cog.withdraw.callback(cog, inter, qid))
    assert log[-1] == ("send", f"✅ クイズ #{qid} を取り下げました") and ch.sent


def test_names_field_truncates_long_lists():
    from cogs.quiz_cog import names_field
    assert names_field([]) == "なし" and names_field([1, 2]) == "<@1> <@2>"
    long = names_field(list(range(10**17, 10**17 + 200)))
    assert len(long) <= 1000 and "ほか" in long


def test_author_profits_with_popularity_near_forty_percent_and_participation_has_no_cap():
    # 報酬100・参加10人・正答率40%: 支払い 400、受け取り 100+200+1000 → +900 の黒字
    assert quizpay.author_payout(10, 4, 100) == (100, 200, 1000)
    assert quizpay.author_payout(10, 2, 100) == (100, 100, 500)             # 正答率20%: f=0.5
    assert quizpay.author_payout(10, 6, 100) == (100, 100, 500)             # 60%: f=0.5。支払い 600 を上回る
    assert quizpay.author_payout(10, 0, 100)[1:] == (0, 0)                  # 誰も解けない問題で稼げない
    assert quizpay.author_payout(10, 8, 100)[1:] == (0, 0)
    assert quizpay.participation(100, 40, 1000) == 100_000                  # 上限なし: 1.0×1000×100人


def test_base_and_bonus_grow_with_the_reward_but_gently():
    assert [quizpay.scale(r) for r in (25, 100, 400)] == [0.5, 1.0, 2.0]
    assert [(quizpay.base_reward(r), quizpay.bonus(10, 4, r)) for r in (25, 100, 400, 1000)] == [(50, 100), (100, 200), (200, 400), (316, 632)]
    assert quizpay.base_reward(1) == 10 and quizpay.bonus(10, 4, 1) == 20          # 報酬1でも少しは出る
    assert quizpay.author_payout(10, 8, 1000)[1:] == (0, 0)                        # 係数 f が 0 なら、倍率が高くてもボーナスはなし


def test_author_is_paid_on_every_answer_and_the_sum_matches_the_final_payout(eco):
    ok, qid = post(eco, reward=100, days=1)
    deltas = []
    for uid, pick in ((1, "東京"), (2, "大阪"), (3, "東京"), (4, "大阪"), (5, "京都")):
        res = eco.quiz_answer(uid, qid, pick, NOW)[1]
        deltas.append((res["author_delta"], res["author_total"]))
    assert deltas[0] == (0, 0)                                              # 1人目はまだ
    assert deltas[1] == (100 + 150 + 150, 400)                               # 2人目: 50% → 100 + 150 + 0.75×100×2
    assert sum(d for d, _ in deltas) == deltas[-1][1] == sum(quizpay.author_payout(5, 2, 100))    # 5人中2人正解: 100+200+100
    pay = 2 * 100
    assert eco.balance(AUTHOR) == INITIAL_BALANCE - pay + deltas[-1][1]
    before = eco.balance(AUTHOR)
    done = eco.quiz_settle(NOW + DAY)
    assert done and eco.balance(AUTHOR) == before                           # 期限では二重に払わない


def test_author_payout_goes_back_when_accuracy_moves_away_from_forty_percent(eco):
    ok, qid = post(eco, reward=100, days=1)
    results = [eco.quiz_answer(uid, qid, pick, NOW)[1] for uid, pick in ((1, "東京"), (2, "大阪"), (3, "大阪"), (4, "京都"), (5, "京都"), (6, "京都"))]
    deltas = [r["author_delta"] for r in results]
    assert deltas[2] > 0 and deltas[3] < 0 and deltas[4] < 0                 # 33%まで上がったあと、25%・20%と離れると戻す
    assert results[-1]["author_total"] == sum(deltas) == sum(quizpay.author_payout(6, 1, 100))
    assert eco.balance(AUTHOR) == INITIAL_BALANCE - 100 + results[-1]["author_total"]


def test_clawback_is_limited_to_the_authors_balance(eco):
    ok, qid = post(eco, reward=10, days=1)
    eco.quiz_answer(1, qid, "東京", NOW)
    eco.quiz_answer(2, qid, "大阪", NOW)
    eco._conn.execute("UPDATE users SET balance=0 WHERE user_id=?", (AUTHOR,))      # 稼いだ分を使い切った
    for uid in (3, 4, 5, 6, 7, 8):
        eco.quiz_answer(uid, qid, "京都", NOW)                                       # 正答率が下がり、本来は受け取りが減る
    assert eco.balance(AUTHOR) >= 0                                                  # マイナスにはならない


def test_public_line_shows_the_authors_earnings_right_away(eco):
    cog = make_cog(eco)
    ok, qid = post(eco, reward=100, days=3, now=time.time())
    q = eco.quiz_get(qid)
    right, wrong = q["order"].index("東京"), (q["order"].index("東京") + 1) % 4
    public, log = [], []

    class Follow:
        async def send(self, text, **kw):
            public.append(text)

    def click(uid):
        return SimpleNamespace(user=SimpleNamespace(id=uid), guild_id=1, client=cog.bot, response=Resp(log), followup=Follow())
    asyncio.run(PublicButton(qid, right).callback(click(1)))
    assert "💰" not in public[0]                                              # 1人目は出題者の受け取りがまだない
    asyncio.run(PublicButton(qid, wrong).callback(click(2)))
    assert public[1] == f"❌ <@2> が #{qid} に **不正解…**\n💰 出題者 <@{AUTHOR}> +400ペリカ（累計 400ペリカ）"


# ----------------------------- 正解・不正解の文言（出題者が決める） ----------------------------- #
def test_parse_terms_and_verdicts():
    from cogs.quiz_cog import parse_terms, parse_verdicts
    assert parse_terms("100") == (100, "") and parse_terms("100 / 3日") == (100, "3日") and parse_terms("１００／90分") == (100, "90分") and parse_terms("100 /") == (100, "")
    with pytest.raises(ValueError):
        parse_terms("abc / 3日")
    assert parse_verdicts("") == ("", "") and parse_verdicts("お見事！") == ("お見事！", "") and parse_verdicts("お見事！\nおしい！") == ("お見事！", "おしい！")
    assert parse_verdicts("\nおしい！") == ("", "おしい！") and parse_verdicts("a\nb\n") == ("a", "b")                  # 片方だけでもよい
    for bad in ("a\nb\nc", "a\n\nb", "あ" * 21):
        with pytest.raises(ValueError):
            parse_verdicts(bad)


def test_default_wording_is_used_when_the_author_leaves_it_blank(eco):
    from cogs.quiz_cog import verdict, result_embed
    qid = eco.quiz_add("問題", ["東京", "大阪", "京都", "名古屋"], "", 100, None, time.time())
    q = eco.quiz_get(qid)
    assert (q["msg_right"], q["msg_wrong"]) == ("", "") and verdict(q, True) == "正解！" and verdict(q, False) == "不正解…"
    assert result_embed({"correct": True, "reward": 100, "explanation": ""}, 100, 0, q).title == "⭕ 正解！"
    assert result_embed({"correct": False, "answer": "東京", "explanation": ""}, 100, 0, q).title == "❌ 不正解…"
    assert result_embed({"correct": True, "reward": 100, "explanation": ""}, 100, 0).title == "⭕ 正解！"            # 問題を渡さなくても既定


def test_custom_wording_replaces_only_the_headline_everywhere(eco):
    from cogs.quiz_cog import verdict, result_embed
    qid = eco.quiz_add("問題", ["東京", "大阪", "京都", "名古屋"], "解説", 100, None, time.time(), "作者", "お見事！", "")
    q = eco.quiz_get(qid)
    assert verdict(q, True) == "お見事！" and verdict(q, False) == "不正解…"                                       # 片方だけ決めたら、もう片方は既定
    e = result_embed({"correct": True, "reward": 100, "explanation": "解説"}, 100, 0, q)
    assert e.title == "⭕ お見事！" and "+100" in e.description                                                  # 報酬の表示は残る
    e = result_embed({"correct": False, "answer": "東京", "explanation": "解説"}, 100, 0, q)
    assert e.title == "❌ 不正解…" and "東京" in e.description


def test_user_modal_saves_the_wording_and_public_answer_line_uses_it(eco):
    from cogs.quiz_cog import UserModal
    cog, log = make_cog(eco), []
    m = UserModal(cog)
    for field, value in ((m.question, "問題"), (m.choices, "a\nb\nc\nd"), (m.explanation, ""), (m.terms, "100 / 1日"), (m.verdicts, "さすが！\nざんねん…")):
        field._value = value
    inter, _ = user_inter(log)
    asyncio.run(m.on_submit(inter))
    q = eco.quiz_list()[0]
    assert (q["msg_right"], q["msg_wrong"]) == ("さすが！", "ざんねん…")
    m2 = UserModal(cog)
    for field, value in ((m2.question, "問題2"), (m2.choices, "a\nb\nc\nd"), (m2.explanation, ""), (m2.terms, "100"), (m2.verdicts, "")):
        field._value = value
    asyncio.run(m2.on_submit(inter))
    assert all((x["msg_right"], x["msg_wrong"]) == ("", "") for x in eco.quiz_list() if x["id"] != q["id"])         # 省略なら空（既定）


def test_public_answer_line_and_result_use_the_custom_wording(eco):
    cog, log = make_cog(eco), []
    qid = eco.quiz_add("問題", ["東京", "大阪", "京都", "名古屋"], "", 100, 9e12, time.time(), "作者", "お見事！", "おしい！")
    q = eco.quiz_get(qid)
    pos = q["order"].index("東京")
    public = []

    class Follow:
        async def send(self, text, **kw):
            public.append(text)

    def click(uid):
        return SimpleNamespace(user=SimpleNamespace(id=uid), guild_id=1, client=cog.bot, response=Resp(log), followup=Follow())
    asyncio.run(PublicButton(qid, pos).callback(click(1)))
    assert log[-1] == ("send", "⭕ お見事！") and public[-1] == f"⭕ <@1> が #{qid} に **お見事！**（+100ペリカ）"
    asyncio.run(PublicButton(qid, (pos + 1) % 4).callback(click(2)))
    assert log[-1] == ("send", "❌ おしい！") and public[-1] == f"❌ <@2> が #{qid} に **おしい！**"
