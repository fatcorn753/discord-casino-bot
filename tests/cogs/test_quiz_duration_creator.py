"""クイズの期間（分・時間・日で書ける）、制作者の名前、取り下げの候補（自分の問題だけ）。"""

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from cogs.quiz.cog import AddModal, UserModal, creator_label, public_embed, question_embed
from domain import quizimport
from domain.duration import format_minutes, parse_duration
from domain.errors import Failure
from infra.db.repositories import open_repositories
from tests.fakes import as_interaction, call, must
from tests.support import make_quiz_cog, make_test_bot

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
    bot = make_test_bot(db_path=str(tmp_path / "t.db"), config={"owner_ids": [1]})
    return make_quiz_cog(bot), bot.repos


def fill(modal, **fields):
    reward, duration = fields.pop("reward", "100"), fields.pop("duration", "")
    modal.terms._value = f"{reward} / {duration}" if duration else reward  # 「報酬 / 期間」は1つの欄
    for name, value in {"question": "問題", "choices": "a\nb\nc\nd", "explanation": "", "verdicts": ""}.items():
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
        asyncio.run(
            fill(AddModal(cog._quiz, cog, announce=False), duration=text).on_submit(as_interaction(owner_inter(log)))
        )
        q = eco.quiz.quiz_list()[0]
        assert (q["expires"] is None) == (minutes is None)
        if minutes:
            assert abs(q["expires"] - q["created"] - minutes * 60) < 1
        eco.quiz.quiz_delete(q["id"])
    log.clear()
    asyncio.run(
        fill(AddModal(cog._quiz, cog, announce=False), duration="あした").on_submit(as_interaction(owner_inter(log)))
    )
    asyncio.run(
        fill(AddModal(cog._quiz, cog, announce=False), duration="1000日").on_submit(as_interaction(owner_inter(log)))
    )
    assert [x[1] for x in log] == ["❌ 入力エラー", "❌ 入力エラー"] and eco.quiz.quiz_list() == [] and time.time()


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
        asyncio.run(fill(UserModal(cog._quiz), duration=text).on_submit(as_interaction(inter)))
    assert [x[1] for x in log] == ["❌ 入力エラー"] * 3 and eco.quiz.quiz_list() == []
    asyncio.run(fill(UserModal(cog._quiz), duration="90分").on_submit(as_interaction(inter)))
    q = eco.quiz.quiz_list()[0]
    assert abs(q["expires"] - q["created"] - 90 * 60) < 1
    eco.quiz.quiz_delete(q["id"])
    inter.user = SimpleNamespace(id=OTHER, display_name="別の人")
    asyncio.run(fill(UserModal(cog._quiz), duration="").on_submit(as_interaction(inter)))
    assert abs(eco.quiz.quiz_list()[0]["expires"] - eco.quiz.quiz_list()[0]["created"] - 3 * 1440 * 60) < 1  # 既定は3日


# ----------------------------- 制作者の名前 ----------------------------- #
def test_creator_name_is_saved_and_shown_everywhere(tmp_path):
    cog, eco = make_cog(tmp_path)
    own = eco.quiz.quiz_add("持ち主の問題", ["a", "b", "c", "d"], "", 100, None, NOW, "持ち主さん")
    legacy = eco.quiz.quiz_add("名前なし", ["a", "b", "c", "d"], "", 100, None, NOW)
    ok, uid = eco.quiz.quiz_post(ME, "みんなの問題", ["a", "b", "c", "d"], "", 100, 60, 7, NOW, "出題者さん")
    ok, old_user = eco.quiz.quiz_post(OTHER, "古い問題", ["a", "b", "c", "d"], "", 100, 60, 7, NOW + 1)
    q = {i: eco.quiz.quiz_get(i) for i in (own, legacy, uid, old_user)}
    assert [creator_label(q[i]) for i in (own, legacy, uid, old_user)] == [
        "持ち主さん",
        "運営",
        "出題者さん",
        f"<@{OTHER}>",
    ]
    assert "出題: **持ち主さん**" in must(public_embed(q[own]).description) and "出題者は答えられません" not in must(
        public_embed(q[own]).description
    )
    e = public_embed(q[uid])
    assert "出題: **出題者さん**（出題者は答えられません。正解の報酬は出題者が払います）" in must(e.description)
    assert "出題: **運営**" in must(public_embed(q[legacy]).description)
    assert "出題: **出題者さん**" in must(
        question_embed(q[uid], q[uid]["order"], 1).description
    )  # /クイズ 挑戦 の画面にも出る


def test_bulk_and_old_schema_keep_the_creator(tmp_path):
    import sqlite3

    eco = open_repositories(str(tmp_path / "t.db"))
    quizzes, _ = quizimport.parse_bulk(BASE + "期間: 2時間\n")
    ids = eco.quiz.quiz_add_many(quizzes, NOW, "持ち主さん")
    assert (
        must(eco.quiz.quiz_get(ids[0]))["creator_name"] == "持ち主さん"
        and abs(cast(float, must(eco.quiz.quiz_get(ids[0]))["expires"]) - NOW - 7200) < 1
    )
    eco.db.conn.commit()
    eco.db.close()
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.executescript(
        "DROP TABLE quiz_choices; DROP TABLE quiz_user_submissions; DROP TABLE quiz_questions; "
        "CREATE TABLE quiz_questions (id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL, "
        "choices TEXT NOT NULL, explanation TEXT NOT NULL DEFAULT '', shown_order TEXT NOT NULL DEFAULT '', "
        "reward INTEGER NOT NULL, expires REAL, created REAL NOT NULL, author_id INTEGER NOT NULL DEFAULT 0, "
        "guild_id INTEGER NOT NULL DEFAULT 0, channel_id INTEGER NOT NULL DEFAULT 0, "
        "message_id INTEGER NOT NULL DEFAULT 0, settled INTEGER NOT NULL DEFAULT 0, earned INTEGER NOT NULL DEFAULT 0);"
    )
    c.execute(
        "INSERT INTO quiz_questions (id, question, choices, reward, created) VALUES (?, '問', '[\"a\",\"b\",\"c\",\"d\"]', 100, ?)",
        (ids[0], NOW),
    )
    c.execute("PRAGMA user_version = 0")
    c.commit()
    c.close()
    assert (
        must(open_repositories(str(tmp_path / "t.db")).quiz.quiz_get(ids[0]))["creator_name"] == ""
    )  # 古い表には、空の名前の列が足される


# ----------------------------- 取り下げの候補 ----------------------------- #
@pytest.mark.real_scope
def test_withdraw_autocomplete_lists_only_my_open_questions_in_this_guild(tmp_path):
    import time

    eco = open_repositories(str(tmp_path / "t.db"))
    g1, g2 = eco.for_guild(10), eco.for_guild(20)
    now = time.time()
    for wallet, user_id in ((g1.wallet, ME), (g1.wallet, OTHER), (g2.wallet, ME)):
        wallet.balance(user_id)  # 口座を作る
    ok, mine1 = g1.quiz.quiz_post(ME, "私の問題その1", ["a", "b", "c", "d"], "", 100, 90, 7, now)
    assert not isinstance(mine1, Failure)
    ok, mine2 = g1.quiz.quiz_post(ME, "私の問題その2", ["a", "b", "c", "d"], "", 100, 600, 7, now)
    assert not isinstance(mine2, Failure)
    ok, theirs = g1.quiz.quiz_post(OTHER, "他の人の問題", ["a", "b", "c", "d"], "", 100, 90, 7, now)
    assert not isinstance(theirs, Failure)
    ok, other_guild = g2.quiz.quiz_post(ME, "別サーバーの問題", ["a", "b", "c", "d"], "", 100, 90, 7, now)
    eco.quiz.quiz_add("持ち主の問題", ["a", "b", "c", "d"], "", 100, None, now)
    cog = make_quiz_cog(make_test_bot(repos=eco))

    def candidates(uid, guild, current=""):
        inter = SimpleNamespace(user=SimpleNamespace(id=uid), guild_id=guild)
        return asyncio.run(call(cog.withdraw_autocomplete, as_interaction(inter), current))

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
    g1.quiz.quiz_withdraw(ME, mine1, now)
    assert [c.value for c in candidates(ME, 10)] == [mine2]  # 取り下げたものは候補から消える
