"""クイズの一括登録: 書き方の読み取り、誤りの報告、重複の防止、確認ボタンから登録まで。"""

import asyncio
from types import SimpleNamespace

import pytest

from cogs.approval_cog import ApprovalCog, bulk_preview_lines
from core import quizimport
from core.economy import Economy

OWNER, FRIEND = 1, 2

ONE = """問題: 首都は？
正解: 東京
不正解: 大阪
不正解: 京都
不正解: 名古屋
解説: 解説です
報酬: 150
日数: 3
"""


def test_parse_one_block_with_all_fields():
    quizzes, errors = quizimport.parse_bulk(ONE)
    assert errors == [] and quizzes == [
        {
            "question": "首都は？",
            "choices": ["東京", "大阪", "京都", "名古屋"],
            "explanation": "解説です",
            "reward": 150,
            "minutes": 3 * 1440,
        }
    ]


def test_defaults_comments_fullwidth_colons_bom_and_multiline():
    text = "﻿# メモ\n問題：1行目\n2行目\n正解：A\n不正解：B\n不正解：C\n不正解：D\n解説：説明\nつづき\n---\n---\n問題: 2問目\n正解: a\n不正解: b\n不正解: c\n不正解: d\n"
    quizzes, errors = quizimport.parse_bulk(text)
    assert errors == [] and len(quizzes) == 2  # 空のブロックは無視する
    assert quizzes[0]["question"] == "1行目\n2行目" and quizzes[0]["explanation"] == "説明\nつづき"
    assert (quizzes[1]["reward"], quizzes[1]["minutes"], quizzes[1]["explanation"]) == (
        100,
        None,
        "",
    )  # 既定: 報酬100・無期限


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("", "問題が見つかりませんでした"),
        (ONE.replace("不正解: 名古屋\n", ""), "「不正解:」は3つ必要です（いま2つ）"),
        (ONE + "不正解: 神戸\n", "「不正解:」は3つ必要です（いま4つ）"),
        (ONE.replace("正解: 東京\n", ""), "「正解:」がありません"),
        (ONE.replace("問題: 首都は？\n", ""), "「問題:」がありません"),
        (ONE.replace("不正解: 京都", "不正解: 大阪"), "同じ選択肢が2つあります"),
        (ONE.replace("不正解: 京都", "不正解: " + "あ" * 81), "選択肢は1つ80文字まで"),
        (ONE.replace("報酬: 150", "報酬: 0"), "報酬は 1〜10,000"),
        (ONE.replace("報酬: 150", "報酬: たくさん"), "報酬は 1〜10,000"),
        (ONE.replace("日数: 3", "日数: 1000"), "期間は 1分〜365日"),
        (ONE + "報酬: 200\n", "「報酬」が2回あります"),
        ("見出しのない行\n" + ONE, "1行目: 「問題:」「正解:」などの見出しで始めてください"),
        (ONE.replace("正解: 東京", "正解: 東京\nつづき"), "「正解」は1行で書いてください"),
        (ONE.replace("問題: 首都は？", "問題: " + "あ" * 501), "問題文は500文字まで"),
        (ONE.replace("解説: 解説です", "解説: " + "あ" * 801), "解説は800文字まで"),
    ],
)
def test_errors_are_reported_with_the_question_number(text, fragment):
    quizzes, errors = quizimport.parse_bulk(text)
    assert any(fragment in e for e in errors), errors


def test_error_names_the_question_number_and_line():
    _, errors = quizimport.parse_bulk(ONE + "---\n" + ONE.replace("報酬: 150", "報酬: 0"))
    assert errors and errors[0].startswith("2問目: ")


def test_too_many_and_duplicate_questions_are_rejected():
    _, errors = quizimport.parse_bulk(
        "---\n".join(ONE.replace("首都は？", f"問題{i}") for i in range(quizimport.MAX_QUESTIONS + 1))
    )
    assert any("30問まで" in e for e in errors)
    _, errors = quizimport.parse_bulk(ONE + "---\n" + ONE)
    assert any("2問目: 1問目と同じ問題文" in e for e in errors)


def test_the_bundled_draft_for_today_is_valid():
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "..", "quiz-drafts", "2026-11-06.txt")
    if not os.path.exists(path):
        pytest.skip("下書きのファイルがない環境")
    quizzes, errors = quizimport.parse_bulk(open(path, encoding="utf-8").read())
    assert errors == [] and len(quizzes) == 8


# ----------------------------- 保存と重複 ----------------------------- #
def test_add_many_registers_all_with_expiry_and_lists_existing(tmp_path):
    eco = Economy(str(tmp_path / "t.db"))
    quizzes, _ = quizimport.parse_bulk(ONE + "---\n" + ONE.replace("首都は？", "2問目").replace("日数: 3\n", ""))
    ids = eco.quiz_add_many(quizzes, 1000.0)
    assert ids == [1, 2]
    a, b = eco.quiz_get(1), eco.quiz_get(2)
    assert a["expires"] == 1000.0 + 3 * 86400 and b["expires"] is None and a["reward"] == 150
    assert eco.quiz_existing_questions() == {"首都は？", "2問目"}


# ----------------------------- コマンドの流れ ----------------------------- #
class FakeFile:
    def __init__(self, text=ONE, name="q.txt", size=None, content_type="text/plain", raw=None):
        self._raw = raw if raw is not None else text.encode("utf-8")
        self.filename, self.content_type = name, content_type
        self.size = size if size is not None else len(self._raw)

    async def read(self):
        return self._raw


def make(tmp_path, broadcasts=None):
    eco = Economy(str(tmp_path / "t.db"))
    quiz_cog = SimpleNamespace(broadcast=None)

    async def broadcast(qid, guild=None, channel=None):
        (broadcasts if broadcasts is not None else []).append(qid)
        return 2

    quiz_cog.broadcast = broadcast
    bot = SimpleNamespace(
        config={"owner_ids": [OWNER]}, economy=eco, guilds=[], get_guild=lambda g: None, get_cog=lambda n: quiz_cog
    )
    return ApprovalCog(bot), eco


def run_cmd(cog, uid, file, announce=None):
    log = {"sent": [], "follow": [], "edits": []}

    class Resp:
        async def send_message(self, **kw):
            log["sent"].append(kw["embed"].title)

        async def defer(self, **kw):
            pass

        async def edit_message(self, **kw):
            log["edits"].append(kw["embeds"][0].title)

    class Follow:
        async def send(self, **kw):
            log["follow"].append(kw)

    async def edit_original(**kw):
        log["edits"].append(kw["embeds"][0].title + "|" + (kw["embeds"][0].description or ""))

    inter = SimpleNamespace(
        user=SimpleNamespace(id=uid, display_name="持ち主さん"),
        response=Resp(),
        followup=Follow(),
        edit_original_response=edit_original,
    )
    asyncio.run(cog.quiz_bulk.callback(cog, inter, file, announce))
    return log, inter


def test_command_is_owner_only_and_checks_the_file(tmp_path):
    cog, eco = make(tmp_path)
    log, _ = run_cmd(cog, FRIEND, FakeFile())
    assert log["sent"] == ["❌ 権限がありません"]
    for bad in (FakeFile(name="q.pdf", content_type="application/pdf"), FakeFile(size=300 * 1024)):
        log, _ = run_cmd(cog, OWNER, bad)
        assert log["sent"] == ["❌ ファイルを使えません"]
    log, _ = run_cmd(cog, OWNER, FakeFile(raw="あ".encode("shift_jis")))
    assert log["follow"][0]["embed"].title == "❌ 文字コードを読めません"
    assert eco.quiz_list() == []


def test_errors_register_nothing_and_list_the_problems(tmp_path):
    cog, eco = make(tmp_path)
    log, _ = run_cmd(
        cog, OWNER, FakeFile(ONE + "---\n" + ONE.replace("首都は？", "2問目").replace("不正解: 京都\n", ""))
    )
    e = log["follow"][0]["embed"]
    assert e.title.startswith("❌ 登録できません（誤り1件") and "2問目" in e.description and eco.quiz_list() == []


def test_preview_then_register_then_the_same_file_is_refused(tmp_path):
    broadcasts = []
    cog, eco = make(tmp_path, broadcasts)
    log, inter = run_cmd(cog, OWNER, FakeFile(ONE + "---\n" + ONE.replace("首都は？", "2問目")))
    preview, view = log["follow"][0]["embeds"][0], log["follow"][0]["view"]
    assert (
        "2問登録します" in preview.title
        and "正解「東京」" in preview.description
        and "告知: しません" in preview.description
    )
    assert eco.quiz_list() == []  # 確認ボタンを押すまで登録しない
    assert asyncio.run(view.interaction_check(SimpleNamespace(user=SimpleNamespace(id=FRIEND)))) is False
    asyncio.run(view.register.callback(inter))
    assert len(eco.quiz_list()) == 2 and broadcasts == [] and "クイズを2問登録しました" in log["edits"][0]
    log2, _ = run_cmd(cog, OWNER, FakeFile(ONE))  # 同じファイルをもう一度
    assert (
        log2["follow"][0]["embed"].title.startswith("❌ 登録できません")
        and "すでに登録されている" in log2["follow"][0]["embed"].description
    )
    assert len(eco.quiz_list()) == 2


def test_cancel_registers_nothing_and_announce_posts_each_question(tmp_path):
    broadcasts = []
    cog, eco = make(tmp_path, broadcasts)
    log, inter = run_cmd(cog, OWNER, FakeFile())
    asyncio.run(log["follow"][0]["view"].cancel.callback(inter))
    assert eco.quiz_list() == [] and log["edits"] == ["📰 クイズの一括登録"]
    log, inter = run_cmd(
        cog, OWNER, FakeFile(ONE + "---\n" + ONE.replace("首都は？", "2問目")), SimpleNamespace(value=1)
    )
    assert "告知: 全サーバー" in log["follow"][0]["embeds"][0].description
    asyncio.run(log["follow"][0]["view"].register.callback(inter))
    assert broadcasts == [1, 2] and "のべ4件" in log["edits"][0]


def test_preview_lines_are_short():
    quizzes, _ = quizimport.parse_bulk(ONE.replace("首都は？", "あ" * 100))
    line = bulk_preview_lines(quizzes)
    assert "…" in line and "報酬" not in line and "150ペリカ" in line and line.endswith("3日")
