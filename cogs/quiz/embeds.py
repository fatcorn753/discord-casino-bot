"""クイズの入力検証・Embed 構築・結果整形（プレゼンテーション部品）。"""

import re
from typing import Optional

import discord

from cogs.shared import ui
from domain import quizpay
from infra import catalogs

_QUIZ_CFG = catalogs.economy().quizpay
MAX_CHOICE_LEN = _QUIZ_CFG.max_choice_len
MAX_VERDICT_LEN = _QUIZ_CFG.max_verdict_len  # 正解・不正解の見出しの文言の最大文字数
DEFAULT_RIGHT, DEFAULT_WRONG = _QUIZ_CFG.default_right, _QUIZ_CFG.default_wrong
DEFAULT_REWARD = _QUIZ_CFG.default_reward
LETTERS = _QUIZ_CFG.letters


def parse_choices(text: str) -> list:
    """入力欄から選択肢を取り出す。4行・空行なし・重複なし。問題があれば ValueError。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) != 4:
        raise ValueError(f"選択肢は4行で入力してください（いま {len(lines)} 行）。1行目が正解です。")
    if len(set(lines)) != 4:
        raise ValueError("同じ選択肢が2つあります。")
    if any(len(ln) > MAX_CHOICE_LEN for ln in lines):
        raise ValueError(f"選択肢は1つ{MAX_CHOICE_LEN}文字までです。")
    return lines


def parse_int(text: str, label: str) -> int:
    try:
        return int(text.strip().replace(",", ""))
    except ValueError:
        raise ValueError(f"{label}は数字で入力してください。") from None


def creator_label(q: dict) -> str:
    """問題に出す制作者の名前。登録時の名前、なければ（古い問題）出題者のメンション、持ち主の問題なら「運営」。"""
    if q.get("creator_name"):
        return q["creator_name"]
    return f"<@{q['author_id']}>" if q.get("author_id") else "運営"


def parse_terms(text: str) -> tuple:
    """「報酬 / 期間」の入力欄を、(報酬, 期間の文字列) に分ける。期間は省略できる（空文字を返す）。"""
    parts = re.split(r"[/／]", text, maxsplit=1)
    return parse_int(parts[0], "報酬"), (parts[1].strip() if len(parts) > 1 else "")


def parse_verdicts(text: str) -> tuple:
    """「正解・不正解の文言」の入力欄を、(正解のとき, 不正解のとき) にする。1行目が正解、2行目が不正解。省略した方は空文字（既定になる）。"""
    lines = [ln.strip() for ln in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    if len(lines) > 2:
        raise ValueError("文言は2行まで（1行目: 正解のとき、2行目: 不正解のとき）です。")
    right, wrong = (lines + ["", ""])[:2]
    if len(right) > MAX_VERDICT_LEN or len(wrong) > MAX_VERDICT_LEN:
        raise ValueError(f"文言は1つ{MAX_VERDICT_LEN}文字までです。")
    return right, wrong


def verdict(q: Optional[dict], correct: bool) -> str:
    """正解・不正解の見出しの言葉。出題者が決めていなければ既定。"""
    if q:
        text = (q.get("msg_right") if correct else q.get("msg_wrong")) or ""
        if text:
            return text
    return DEFAULT_RIGHT if correct else DEFAULT_WRONG


def question_embed(q: dict, order: list, remaining: int) -> discord.Embed:
    body = (
        q["question"]
        + "\n\n"
        + "\n".join(f"**{LETTERS[i]}.** {c}" for i, c in enumerate(order))
        + f"\n\n出題: **{creator_label(q)}**"
    )
    e = ui.embed(f"📰 クイズ #{q['id']}", body, ui.INFO)
    e.set_footer(text=f"正解で {q['reward']:,}ペリカ ｜ 1問1回だけ ｜ あと{remaining}問")
    return e


def result_embed(result: dict, more: int, quiz: Optional[dict] = None) -> discord.Embed:
    if result["correct"] and result["reward_blocked"]:
        e = ui.embed(
            f"⭕ {verdict(quiz, True)}",
            f"**報酬はありません**（ゲームを{quizpay.MIN_GAMES_FOR_REWARD}回以上遊ぶと、みんなの問題の報酬をもらえます）",
            ui.OK,
        )
    elif result["correct"]:
        e = ui.embed(f"⭕ {verdict(quiz, True)}", f"**+{ui.pelica(result['reward'])}**", ui.OK)
    else:
        e = ui.embed(f"❌ {verdict(quiz, False)}", f"正解は **{result['answer']}** でした。", ui.ERROR)
    if result["explanation"]:
        e.add_field(name="解説", value=result["explanation"][:1000], inline=False)
    if more:
        e.set_footer(text=f"まだ{more}問あります。/クイズ 挑戦")
    return e


def public_embed(q: dict) -> discord.Embed:
    body = q["question"] + "\n\n" + "\n".join(f"**{LETTERS[i]}.** {c}" for i, c in enumerate(q["order"]))
    body += f"\n\n出題: **{creator_label(q)}**" + (
        "（出題者は答えられません。正解の報酬は出題者が払います）" if q["author_id"] else ""
    )
    if q["expires"] is not None:
        body += f"\n⏳ 期限 <t:{int(q['expires'])}:R>"
    e = ui.embed(f"📰 {'みんなのクイズ' if q['author_id'] else '新しいクイズ'} #{q['id']}", body, ui.GOLD)
    e.set_footer(text=f"下のボタンで答える。正解で {q['reward']:,}ペリカ ｜ 1人1回だけ")
    return e


def names_field(user_ids: list, limit: int = 900) -> str:
    """メンションを並べる（埋め込みの中なので通知は飛ばない）。長すぎるときは、残りを人数で示す。"""
    if not user_ids:
        return "なし"
    out, used = [], 0
    for i, uid in enumerate(user_ids):
        part = f"<@{uid}>"
        if used + len(part) + 1 > limit:
            return " ".join(out) + f" ほか{len(user_ids) - i}人"
        out.append(part)
        used += len(part) + 1
    return " ".join(out)


def settle_text(q: dict) -> str:
    n, c = q["participants"], q["correct_count"]
    total = q["base"] + q["bonus"] + q["part"]
    if total == 0:
        return f"回答者が{quizpay.MIN_PARTICIPANTS}人に満たなかった（{n}人）ので、出題報酬はありません。"
    return (
        f"参加 **{n}人** ／ 正解 **{c}人**（正答率 **{c / n:.0%}**）\n"
        f"出題報酬: 基本 {ui.pelica(q['base'])} ＋ 正答率ボーナス {ui.pelica(q['bonus'])} ＋ 参加報酬 {ui.pelica(q['part'])}"
        f" ＝ **{ui.pelica(total)}**（回答のたびに受け取り済み）\n（正答率{quizpay.TARGET_RATE:.0%}に近いほど、ボーナスと参加報酬が大きくなります）"
    )


def result_with_names(q: dict) -> discord.Embed:
    e = ui.embed(f"📰 クイズ #{q['id']} 終了", settle_text(q), ui.INFO)
    e.add_field(name=f"⭕ 正解者（{len(q['right_users'])}人）", value=names_field(q["right_users"]), inline=False)
    e.add_field(name=f"❌ 不正解者（{len(q['wrong_users'])}人）", value=names_field(q["wrong_users"]), inline=False)
    return e
