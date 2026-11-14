"""クイズの一括入力。テキストファイルを読んで、問題の一覧にする（Discord に依存しない）。

書き方（問題は `---` だけの行で区切る。`#` で始まる行はメモとして無視する）:

    問題: 2026年に開かれる〇〇は？
    正解: 正しい選択肢
    不正解: まちがいの選択肢1
    不正解: まちがいの選択肢2
    不正解: まちがいの選択肢3
    解説: 解説（なくてもよい。次の行に続けて書ける）
    報酬: 100        （なくてもよい。既定 100）
    期間: 2時間      （なくてもよい。なければ無期限。「90分」「2時間」「1時間30分」「3日」の形。
                      「時間: 2」「日数: 3」「分: 90」と、単位を見出しにして数字だけ書いてもよい）
    ---
    問題: …

全角の「：」も使える。1つでも誤りがあれば、1問も登録しない（誤りは何問目の何行目かを返す）。
"""

import re
from typing import Optional

from domain.duration import parse_duration
from domain.ports import get_catalogs

_QUIZ_CFG = get_catalogs().economy().quizpay
MAX_QUESTIONS = _QUIZ_CFG.max_questions
MAX_QUESTION_LEN = _QUIZ_CFG.max_question_len
MAX_CHOICE_LEN = _QUIZ_CFG.max_choice_len
MAX_EXPLANATION_LEN = _QUIZ_CFG.max_explanation_len
MAX_MINUTES = _QUIZ_CFG.max_minutes  # 有効期間の上限（1年）
DEFAULT_REWARD = _QUIZ_CFG.default_reward

KEYS = {
    "問題": "question",
    "正解": "answer",
    "不正解": "wrong",
    "解説": "explanation",
    "報酬": "reward",
    "期間": "duration",
    "時間": "hours",
    "日数": "days",
    "分": "mins",
}
KEY_LINE = re.compile(r"^\s*(問題|正解|不正解|解説|報酬|期間|時間|日数|分)\s*[:：]\s*(.*)$")
SEPARATOR = re.compile(r"^\s*-{3,}\s*$")


def _int(text: str) -> Optional[int]:
    try:
        return int(text.strip().replace(",", ""))
    except ValueError:
        return None


def _parse_block(lines: list, number: int, errors: list) -> Optional[dict]:
    """1問分。lines は (元ファイルの行番号, 文字列) の並び。誤りは errors に足す。"""
    fields: dict = {"wrong": []}
    where: dict = {}
    current = None
    problems = []
    for lineno, line in lines:
        if line.lstrip().startswith("#"):
            continue
        m = KEY_LINE.match(line)
        if m:
            key = KEYS[m.group(1)]
            value = m.group(2).strip()
            current = key
            where.setdefault(key, lineno)
            if key == "wrong":
                fields["wrong"].append(value)
            elif key in fields:
                problems.append(f"{lineno}行目: 「{m.group(1)}」が2回あります。")
            else:
                fields[key] = value
        elif line.strip():
            if current in ("question", "explanation"):
                fields[current] += "\n" + line.strip()  # 問題文と解説は、次の行に続けられる
            elif current is None:
                problems.append(f"{lineno}行目: 「問題:」「正解:」などの見出しで始めてください。")
            else:
                problems.append(
                    f"{lineno}行目: 「{[k for k, v in KEYS.items() if v == current][0]}」は1行で書いてください。"
                )
    if (
        not fields.get("question")
        and not problems
        and not any(fields.get(k) for k in ("answer", "explanation"))
        and not fields["wrong"]
    ):
        return None  # 空のブロック（`---` が続いたときなど）
    head = f"{number}問目: "
    errors.extend(head + p for p in problems)
    question = fields.get("question", "")
    answer = fields.get("answer", "")
    wrong = fields["wrong"]
    if not question:
        errors.append(head + "「問題:」がありません。")
    elif len(question) > MAX_QUESTION_LEN:
        errors.append(head + f"問題文は{MAX_QUESTION_LEN}文字までです（いま{len(question)}文字）。")
    choices = ([answer] if answer else []) + wrong
    if not answer:
        errors.append(head + "「正解:」がありません。")
    if len(wrong) != 3:
        errors.append(head + f"「不正解:」は3つ必要です（いま{len(wrong)}つ）。")
    if any(not c for c in choices):
        errors.append(head + "選択肢が空です。")
    elif len(set(choices)) != len(choices):
        errors.append(head + "同じ選択肢が2つあります。")
    elif any(len(c) > MAX_CHOICE_LEN for c in choices):
        errors.append(head + f"選択肢は1つ{MAX_CHOICE_LEN}文字までです。")
    explanation = fields.get("explanation", "")
    if len(explanation) > MAX_EXPLANATION_LEN:
        errors.append(head + f"解説は{MAX_EXPLANATION_LEN}文字までです（いま{len(explanation)}文字）。")
    reward = DEFAULT_REWARD
    if "reward" in fields:
        reward = _int(fields["reward"])
        if reward is None or reward < 1:
            errors.append(head + "報酬は 1 以上の数字で書いてください。")
            reward = DEFAULT_REWARD
    minutes = None
    given = [k for k in ("duration", "hours", "days", "mins") if k in fields]
    if len(given) > 1:
        errors.append(head + "期間は「期間」「時間」「日数」「分」のどれか1つだけで書いてください。")
    elif given:
        key = given[0]
        try:
            minutes = parse_duration(
                fields[key], {"duration": "時間", "hours": "時間", "days": "日", "mins": "分"}[key]
            )
            if not 1 <= minutes <= MAX_MINUTES:
                raise ValueError("期間は 1分〜365日 の範囲で書いてください。")
        except ValueError as exc:
            errors.append(head + str(exc) + "（無期限なら行ごと消します）")
            minutes = None
    return {"question": question, "choices": choices, "explanation": explanation, "reward": reward, "minutes": minutes}


def parse_bulk(text: str) -> tuple:
    """(問題のリスト, 誤りのリスト) を返す。誤りが1つでもあれば、呼び出し側は1問も登録しないこと。"""
    blocks, current = [], []
    for lineno, line in enumerate(text.lstrip("﻿").splitlines(), start=1):
        if SEPARATOR.match(line):
            blocks.append(current)
            current = []
        else:
            current.append((lineno, line))
    blocks.append(current)
    quizzes, errors = [], []
    for lines in blocks:
        if not any(text.strip() and not text.lstrip().startswith("#") for _, text in lines):
            continue
        quiz = _parse_block(lines, len(quizzes) + 1, errors)
        if quiz is not None:
            quizzes.append(quiz)
    if not quizzes and not errors:
        errors.append("問題が見つかりませんでした。")
    if len(quizzes) > MAX_QUESTIONS:
        errors.append(f"一度に登録できるのは{MAX_QUESTIONS}問までです（いま{len(quizzes)}問）。")
    seen = {}
    for i, q in enumerate(quizzes, start=1):
        if q["question"] in seen:
            errors.append(f"{i}問目: {seen[q['question']]}問目と同じ問題文です。")
        seen.setdefault(q["question"], i)
    return quizzes, errors
