"""クイズの期間（分・時間・日で書ける）、制作者の名前、取り下げの候補（自分の問題だけ）。"""

import asyncio
from types import SimpleNamespace

import pytest

from cogs.quiz.cog import AddModal, QuizCog, UserModal, creator_label, public_embed, question_embed
from domain import quizimport
from domain.duration import format_minutes, parse_duration
from infra.db.economy import Economy

NOW = 1_000_000.0
ME, OTHER = 500, 600


# ----------------------------- 期間の読み取り ----------------------------- #
@pytest.mark.parametrize(
    "text, expected",
    [
        ("90分", 90),
        ("2時間", 120),
        ("1時間30分", 90),
        ("3日", 4320),
        ("1日2時間30分", 1440 + 150),
        ("２時間", 120),
        ("2 時間 30 分", 150),
        ("90m", 90),
        ("2h", 120),
        ("1d", 1440),
        ("2H30M", 150),
        ("5", 300),  # 単位なしは、既定の単位（時間）
    ],
)
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


def test_parse_duration_default_unit_and_errors():
    assert parse_duration("5", "分") == 5 and parse_duration("5", "日") == 7200
    for bad in ("", "  ", "あした", "2時間 くらい", "1時間1時間", "-5分", "分"):
        with pytest.raises(ValueError):
            parse_duration(bad)


def test_format_minutes():
    assert [format_minutes(m) for m in (1, 90, 120, 1440, 4320, 4350, 0)] == [
        "1分",
        "1時間30分",
        "2時間",
        "1日",
        "3日",
        "3日30分",
        "0分",
    ]


# ----------------------------- 一括入力の期間 ----------------------------- #
BASE = "問題: 問\n正解: a\n不正解: b\n不正解: c\n不正解: d\n"


@pytest.mark.parametrize(
    "line, minutes",
    [
        ("期間: 90分", 90),
        ("期間: 2時間", 120),
        ("期間: 1時間30分", 90),
        ("期間: 3日", 4320),
        ("期間: 2", 120),
        ("時間: 2", 120),
        ("日数: 3", 4320),
        ("分: 45", 45),
        ("", None),
    ],
)
def test_bulk_duration_keys(line, minutes):
    quizzes, errors = quizimport.parse_bulk(BASE + line + "\n")
    assert errors == [] and quizzes[0]["minutes"] == minutes


def test_bulk_duration_errors():
    for line, fragment in (
        ("期間: あした", "期間は「90分」"),
        ("期間: 400日", "1分〜365日"),
        ("期間: 0分", "1分〜365日"),
    ):
        _, errors = quizimport.parse_bulk(BASE + line + "\n")
        assert any(fragment in e for e in errors), (line, errors)
    _, errors = quizimport.parse_bulk(BASE + "期間: 2時間\n日数: 3\n")
    assert any("どれか1つだけ" in e for e in errors)


# ----------------------------- モーダルの期間 ----------------------------- #
class Resp:
    def __init__(self, log):
        self.log = log

    async def send_message(self, **kw):
        self.log.append(("send", kw["embed"].title, kw["embed"].description))

    async def defer(self, **kw):
        pass


def make_cog(tmp_path):
    eco = Economy(str(tmp_path / "t.db"))
    bot = SimpleNamespace(config={"owner_ids": [1]}, guilds=[], economy=eco)
    return QuizCog(bot), eco


def fill(modal, **fields):
    reward, duration = fields.pop("reward", "100"), fields.pop("duration", "")
    modal.terms._value = f"{reward} / {duration}" if duration else reward  # 「報酬 / 期間」は1つの欄
    for name, value in dict(question="問題", choices="a\nb\nc\nd", explanation="", verdicts="").items():
        getattr(modal, name)._value = fields.get(name, value)
    return modal


def owner_inter(log):
    class F:
        async def send(self, **kw):
            log.append(("follow", kw["embed"].title, kw["embed"].description))

    return SimpleNamespace(user=SimpleNamespace(id=1, display_name="持ち主さん"), response=Resp(log), followup=F())


def test_owner_modal_accepts_minutes_hours_days_or_nothing(tmp_path):
    import time

    cog, eco = make_cog(tmp_path)
    log = []
    for text, minutes in (("45分", 45), ("6時間", 360), ("2日", 2880), ("", None)):
        asyncio.run(fill(AddModal(cog, announce=False), duration=text).on_submit(owner_inter(log)))
        q = eco.quiz_list()[0]
        assert (q["expires"] is None) == (minutes is None)
        if minutes:
            assert abs(q["expires"] - q["created"] - minutes * 60) < 1
        eco.quiz_delete(q["id"])
    log.clear()
    asyncio.run(fill(AddModal(cog, announce=False), duration="あした").on_submit(owner_inter(log)))
    asyncio.run(fill(AddModal(cog, announce=False), duration="1000日").on_submit(owner_inter(log)))
    assert [x[1] for x in log] == ["❌ 入力エラー", "❌ 入力エラー"] and eco.quiz_list() == [] and time.time()


def test_user_modal_duration_range_and_default(tmp_path):
    cog, eco = make_cog(tmp_path)
    log = []

    class R(Resp):
        async def send_message(self, **kw):
            self.log.append(("send", kw["embed"].title, kw["embed"].description))

    async def original():
        return SimpleNamespace(id=1234)

    inter = SimpleNamespace(
        user=SimpleNamespace(id=ME, display_name="出題者さん"),
        guild_id=1,
        channel_id=9,
        response=R(log),
        original_response=original,
    )
    for text in ("8日", "0分", "ふつう"):
        asyncio.run(fill(UserModal(cog), duration=text).on_submit(inter))
    assert [x[1] for x in log] == ["❌ 入力エラー"] * 3 and eco.quiz_list() == []
    asyncio.run(fill(UserModal(cog), duration="90分").on_submit(inter))
    q = eco.quiz_list()[0]
    assert abs(q["expires"] - q["created"] - 90 * 60) < 1
    eco.quiz_delete(q["id"])
    inter.user = SimpleNamespace(id=OTHER, display_name="別の人")
    asyncio.run(fill(UserModal(cog), duration="").on_submit(inter))
    assert abs(eco.quiz_list()[0]["expires"] - eco.quiz_list()[0]["created"] - 3 * 1440 * 60) < 1  # 既定は3日


# ----------------------------- 制作者の名前 ----------------------------- #
def test_creator_name_is_saved_and_shown_everywhere(tmp_path):
    cog, eco = make_cog(tmp_path)
    own = eco.quiz_add("持ち主の問題", ["a", "b", "c", "d"], "", 100, None, NOW, "持ち主さん")
    legacy = eco.quiz_add("名前なし", ["a", "b", "c", "d"], "", 100, None, NOW)
    ok, uid = eco.quiz_post(ME, "みんなの問題", ["a", "b", "c", "d"], "", 100, 60, 7, NOW, "出題者さん")
    ok, old_user = eco.quiz_post(OTHER, "古い問題", ["a", "b", "c", "d"], "", 100, 60, 7, NOW + 1)
    q = {i: eco.quiz_get(i) for i in (own, legacy, uid, old_user)}
    assert [creator_label(q[i]) for i in (own, legacy, uid, old_user)] == [
        "持ち主さん",
        "運営",
        "出題者さん",
        f"<@{OTHER}>",
    ]
    assert (
        "出題: **持ち主さん**" in public_embed(q[own]).description
        and "出題者は答えられません" not in public_embed(q[own]).description
    )
    e = public_embed(q[uid])
    assert "出題: **出題者さん**（出題者は答えられません。正解の報酬は出題者が払います）" in e.description
    assert "出題: **運営**" in public_embed(q[legacy]).description
    assert (
        "出題: **出題者さん**" in question_embed(q[uid], q[uid]["order"], 1).description
    )  # /クイズ 挑戦 の画面にも出る


def test_bulk_and_old_schema_keep_the_creator(tmp_path):
    import sqlite3

    eco = Economy(str(tmp_path / "t.db"))
    quizzes, _ = quizimport.parse_bulk(BASE + "期間: 2時間\n")
    ids = eco.quiz_add_many(quizzes, NOW, "持ち主さん")
    assert (
        eco.quiz_get(ids[0])["creator_name"] == "持ち主さん" and abs(eco.quiz_get(ids[0])["expires"] - NOW - 7200) < 1
    )
    eco._conn.commit()
    eco._conn.close()
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.executescript(
        "ALTER TABLE quiz_questions RENAME TO old_q; CREATE TABLE quiz_questions AS SELECT id, question, choices, explanation, shown_order, reward, expires, created, author_id, guild_id, channel_id, message_id, settled, earned FROM old_q; DROP TABLE old_q;"
    )
    c.commit()
    c.close()
    assert Economy(str(tmp_path / "t.db")).quiz_get(ids[0])["creator_name"] == ""  # 古い表には、空の名前の列が足される


# ----------------------------- 取り下げの候補 ----------------------------- #
@pytest.mark.real_scope
def test_withdraw_autocomplete_lists_only_my_open_questions_in_this_guild(tmp_path):
    import time

    eco = Economy(str(tmp_path / "t.db"))
    g1, g2 = eco.for_guild(10), eco.for_guild(20)
    now = time.time()
    g1.balance(ME), g1.balance(OTHER), g2.balance(ME)
    ok, mine1 = g1.quiz_post(ME, "私の問題その1", ["a", "b", "c", "d"], "", 100, 90, 7, now)
    ok, mine2 = g1.quiz_post(ME, "私の問題その2", ["a", "b", "c", "d"], "", 100, 600, 7, now)
    ok, theirs = g1.quiz_post(OTHER, "他の人の問題", ["a", "b", "c", "d"], "", 100, 90, 7, now)
    ok, other_guild = g2.quiz_post(ME, "別サーバーの問題", ["a", "b", "c", "d"], "", 100, 90, 7, now)
    eco.quiz_add("持ち主の問題", ["a", "b", "c", "d"], "", 100, None, now)
    cog = QuizCog(SimpleNamespace(config={}, guilds=[], economy=eco))

    def candidates(uid, guild, current=""):
        inter = SimpleNamespace(user=SimpleNamespace(id=uid), guild_id=guild)
        return asyncio.run(cog.withdraw_autocomplete(inter, current))

    got = candidates(ME, 10)
    assert [c.value for c in got] == [mine1, mine2]  # 自分の問題だけ（他人・別サーバー・持ち主の問題は出ない）
    assert (
        got[0].name.startswith(f"#{mine1} 私の問題その1（あと") and "1時間" in got[0].name
    )  # 90分 → あと1時間29分前後
    assert [c.value for c in candidates(ME, 10, "その2")] == [mine2] and candidates(ME, 10, "zzz") == []
    assert [c.value for c in candidates(OTHER, 10)] == [theirs] and [c.value for c in candidates(ME, 20)] == [
        other_guild
    ]
    assert candidates(ME, None) == []
    g1.quiz_withdraw(ME, mine1, now)
    assert [c.value for c in candidates(ME, 10)] == [mine2]  # 取り下げたものは候補から消える
