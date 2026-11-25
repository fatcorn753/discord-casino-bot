"""クイズの記録。問題・回答・出題者への報酬の支払い。

表の定義と移行は infra/db/schema/tables.py・migrations.py にある。
"""

import json
from collections.abc import Mapping
from typing import Any, cast

from domain import quizpay as quizpay_mod
from domain.errors import Outcome, fail
from domain.ports.bridges import IDebtOps
from domain.ports.contracts import QuizAdminView, QuizAnswerResult, QuizImportItem, QuizSettleRow, QuizView
from domain.ports.repos import IQuizRepo
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo
from infra.db.schema import new_quiz_order


def _quiz_dict(row: Any) -> QuizView:
    d = dict(row)
    d["choices"] = json.loads(d["choices"])
    d["order"] = (
        [d["choices"][i] for i in json.loads(d["shown_order"])] if d.get("shown_order") else d["choices"][:]
    )  # 表示する選択肢の並び
    return cast("QuizView", d)


def _quiz_row(row: Any) -> QuizView | None:
    return None if row is None else _quiz_dict(row)


class QuizRepo(ScopedRepo, IQuizRepo):
    """クイズの問題・回答・出題者への報酬。"""

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        debt: IDebtOps,
        guild_id: int = 0,
        strict: bool = False,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._debt = debt

    def quiz_add(
        self,
        question: str,
        choices: list[str],
        explanation: str,
        reward: int,
        expires: float | None,
        now: float,
        creator_name: str = "",
        msg_right: str = "",
        msg_wrong: str = "",
    ) -> int:
        """問題を登録する（choices の先頭が正解）。登録した問題の番号を返す。creator_name は問題に出す制作者の名前。msg_right / msg_wrong は、正解・不正解の見出しの文言（空なら既定）。"""
        with self._db.tx():
            cur = self._conn.execute(
                "INSERT INTO quiz_questions (question, choices, explanation, shown_order, reward, expires, created, creator_name, msg_right, msg_wrong) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    question,
                    json.dumps(choices, ensure_ascii=False),
                    explanation,
                    json.dumps(new_quiz_order()),
                    reward,
                    expires,
                    now,
                    creator_name,
                    msg_right,
                    msg_wrong,
                ),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def quiz_add_many(self, items: list[QuizImportItem], now: float, creator_name: str = "") -> list[int]:
        """まとめて登録する（全部成功するか、全部なしか）。items は {question, choices, explanation, reward, minutes} の並び（minutes が None なら無期限）。番号のリストを返す。"""
        with self._db.tx():
            return [
                self.quiz_add(
                    i["question"],
                    i["choices"],
                    i["explanation"],
                    i["reward"],
                    now + i["minutes"] * 60 if i["minutes"] else None,
                    now,
                    creator_name,
                )
                for i in items
            ]

    def quiz_existing_questions(self) -> set[str]:
        """登録済みの問題文（同じファイルを2回登録してしまわないための照合用）。"""
        with self._lock:
            return {r["question"] for r in self._all("SELECT question FROM quiz_questions")}

    def quiz_get(self, quiz_id: int) -> QuizView | None:
        with self._lock:
            return _quiz_row(self._one("SELECT * FROM quiz_questions WHERE id=?", (quiz_id,)))

    def quiz_list(self) -> list[QuizAdminView]:
        """全問題（新しい順）。回答数と正解数つき。"""
        with self._lock:
            rows = self._all(
                "SELECT q.*, COUNT(a.user_id) AS answered, COALESCE(SUM(a.correct), 0) AS right_count "
                "FROM quiz_questions q LEFT JOIN quiz_answers a ON a.question_id = q.id GROUP BY q.id ORDER BY q.id DESC"
            )
        return [cast("QuizAdminView", _quiz_dict(r)) for r in rows]

    def quiz_delete(self, quiz_id: int) -> bool:
        with self._db.tx():
            self._conn.execute("DELETE FROM quiz_answers WHERE question_id=?", (quiz_id,))
            return self._conn.execute("DELETE FROM quiz_questions WHERE id=?", (quiz_id,)).rowcount > 0

    def quiz_next(self, user_id: int, now: float) -> tuple[QuizView | None, int]:
        """まだ答えていない、期限内の一番古い問題と、その残り数（その問題を含む）を返す。無ければ (None, 0)。

        ユーザーの問題は、このサーバーのもので、自分の問題ではないものだけ出る（出題者の残高が尽きても、借金で報酬を払うので出続ける）。
        """
        with self._lock:
            gid = self._g()
            rows = self._all(
                "SELECT * FROM quiz_questions q WHERE (q.expires IS NULL OR q.expires > ?) "
                "AND (q.author_id=0 OR (q.guild_id=? AND q.author_id!=?)) "
                "AND NOT EXISTS (SELECT 1 FROM quiz_answers a WHERE a.question_id=q.id AND a.guild_id=? AND a.user_id=?) ORDER BY q.id",
                (now, gid, user_id, gid, user_id),
            )
        return (_quiz_dict(rows[0]), len(rows)) if rows else (None, 0)

    def quiz_answer(self, user_id: int, quiz_id: int, chosen: str, now: float) -> Outcome[QuizAnswerResult]:
        """答えを記録する。(成功か, 理由か結果) を返す。結果は correct / reward（実際に入った額）/ answer / explanation。

        ユーザーの問題の報酬は、出題者の残高から引いて渡す。残高が報酬に足りないときは、足りない分を出題者の借金にして、
        回答者には報酬を全額払う（借金は、出題者に入るお金から、先に自動で返済される）。結果の author_debt_added が、増えた借金。
        """
        with self._db.tx():
            gid = self._g()
            q = _quiz_row(self._one("SELECT * FROM quiz_questions WHERE id=?", (quiz_id,)))
            if q is None or (q["expires"] is not None and now >= q["expires"]):
                return False, fail("quiz_ended")
            author = q["author_id"]
            if author:
                if q["guild_id"] != gid:
                    return False, fail("quiz_wrong_guild")
                if user_id == author:
                    return False, fail("quiz_self_answer")
            self._account.ensure(gid, user_id)
            if self._one(
                "SELECT 1 FROM quiz_answers WHERE question_id=? AND guild_id=? AND user_id=?",
                (quiz_id, gid, user_id),
            ):
                return False, fail("quiz_already_answered")
            correct = chosen == q["choices"][0]
            # 出題者がいる問題は、ゲームを3回以上遊んだ人だけが報酬をもらえる（正解にはなる）。遊んでいない人の回答は、出題者の報酬にも数えない
            eligible = not author or self._games_played(gid, user_id) >= quizpay_mod.MIN_GAMES_FOR_REWARD
            self._conn.execute(
                "INSERT INTO quiz_answers (question_id, guild_id, user_id, correct, ts, counted) VALUES (?, ?, ?, ?, ?, ?)",
                (quiz_id, gid, user_id, int(correct), now, int(eligible)),
            )
            paid = 0
            debt_added = 0
            if correct and eligible:
                if author:
                    taken = -self._account.apply(gid, author, -q["reward"], "クイズ報酬の支払い")
                    # 残高で払えなかった分は、強制執行したうえで、残りが出題者の借金になる
                    debt_added = self._debt.owe_for(gid, author, q["reward"] - taken, "クイズの報酬の支払い")
                paid = self._account.apply(
                    gid, user_id, q["reward"], "クイズ報酬", income=q["reward"]
                )  # 持ち主の問題の報酬は、システムから出る（増える）。みんなの問題は出題者から
            result: QuizAnswerResult = {
                "correct": correct,
                "reward": paid,
                "answer": q["choices"][0],
                "explanation": q["explanation"],
                "author_id": author,
                "author_delta": 0,
                "author_total": 0,
                "author_debt_added": debt_added,
                "reward_blocked": correct and not eligible,
            }
            if author and eligible:
                result["author_delta"], result["author_total"] = self._quiz_pay_author(gid, q)
            return True, result

    def _games_played(self, guild_id: int, user_id: int) -> int:
        v = self._scalar(
            "SELECT games_played FROM user_play_stats WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )
        return v if v is not None else 0

    def _quiz_pay_author(self, guild_id: int, q: Mapping[str, Any]) -> tuple[int, int]:
        """いまの回答状況での出題者の受け取り額（基本+ボーナス+参加報酬）と、すでに払った累計の差を払う（減るときは戻す）。

        (動いた額, 累計) を返す。戻すときは、出題者の残高の範囲まで。期限のときの最終合計は、回答のたびに払った累計と同じになる。
        """
        row = self._one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(correct), 0) AS c FROM quiz_answers WHERE question_id=? AND guild_id=? AND counted=1",
            (q["id"], guild_id),
        )
        assert row is not None  # 集計なので必ず1行返る
        n, c = row["n"], row["c"]
        entitled = sum(quizpay_mod.author_payout(n, c, q["reward"]))
        earned = self._scalar("SELECT earned FROM quiz_questions WHERE id=?", (q["id"],))
        diff = entitled - earned
        if diff > 0:
            self._account.apply(guild_id, q["author_id"], diff, "クイズ出題報酬", income=diff)
            moved = diff
        elif diff < 0:
            self._account.ensure(guild_id, q["author_id"])
            moved = self._account.apply(guild_id, q["author_id"], diff, "クイズ出題報酬（調整）")
        else:
            moved = 0
        self._conn.execute("UPDATE quiz_questions SET earned=? WHERE id=?", (earned + moved, q["id"]))
        return moved, earned + moved

    def quiz_post(
        self,
        author_id: int,
        question: str,
        choices: list[str],
        explanation: str,
        reward: int,
        minutes: int,
        channel_id: int,
        now: float,
        creator_name: str = "",
        msg_right: str = "",
        msg_wrong: str = "",
    ) -> Outcome[int]:
        """ユーザーが問題を出す。(成功か, 理由か問題の番号) を返す。"""
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, author_id)
            debt = self._account.debt(gid, author_id)
            if debt > 0:
                return False, fail("debt_blocks", debt=debt, verb="出題できません")
            mine = self._all(
                "SELECT created, expires, settled FROM quiz_questions WHERE author_id=? AND guild_id=?",
                (author_id, gid),
            )
            if sum(1 for r in mine if not r["settled"] and r["expires"] > now) >= quizpay_mod.OPEN_LIMIT:
                return False, fail("quiz_open_limit", limit=quizpay_mod.OPEN_LIMIT)
            if sum(1 for r in mine if r["created"] > now - 86400) >= quizpay_mod.DAILY_LIMIT:
                return False, fail("quiz_daily_limit", limit=quizpay_mod.DAILY_LIMIT)
            if self._account.raw_balance(gid, author_id) < reward:
                return False, fail("quiz_insufficient_balance", reward=reward)
            cur = self._conn.execute(
                "INSERT INTO quiz_questions (question, choices, explanation, shown_order, reward, expires, created, author_id, guild_id, channel_id, creator_name, msg_right, msg_wrong) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    question,
                    json.dumps(choices, ensure_ascii=False),
                    explanation,
                    json.dumps(new_quiz_order()),
                    reward,
                    now + minutes * 60,
                    now,
                    author_id,
                    gid,
                    channel_id,
                    creator_name,
                    msg_right,
                    msg_wrong,
                ),
            )
            lastrowid = cur.lastrowid
            assert lastrowid is not None
            return True, lastrowid

    def quiz_own_open(self, user_id: int, now: float) -> list[QuizView]:
        """このサーバーで自分が出した、終了前の問題（古い順）。取り下げの候補に使う。"""
        with self._lock:
            return [
                _quiz_dict(r)
                for r in self._all(
                    "SELECT * FROM quiz_questions WHERE author_id=? AND guild_id=? AND settled=0 AND expires>? ORDER BY id",
                    (user_id, self._g(), now),
                )
            ]

    def quiz_set_message(self, quiz_id: int, message_id: int) -> None:
        with self._db.tx():
            self._conn.execute("UPDATE quiz_questions SET message_id=? WHERE id=?", (message_id, quiz_id))

    def quiz_settle(self, now: float) -> list[QuizSettleRow]:
        """期限を過ぎたユーザーの問題の、出題者への報酬を決着させる（このサーバーの分）。決着した問題を、成績つきで返す。"""
        out = []
        with self._db.tx():
            gid = self._g()
            for row in self._all(
                "SELECT * FROM quiz_questions WHERE author_id!=0 AND guild_id=? AND settled=0 AND expires<=? ORDER BY id",
                (gid, now),
            ):
                tally = self._one(
                    "SELECT COUNT(*) AS n, COALESCE(SUM(correct), 0) AS c FROM quiz_answers WHERE question_id=? AND guild_id=? AND counted=1",
                    (row["id"], gid),
                )
                assert tally is not None  # 集計なので必ず1行返る
                n, c = tally["n"], tally["c"]
                base, bonus, part = quizpay_mod.author_payout(n, c, row["reward"])
                q = _quiz_dict(row)
                self._quiz_pay_author(gid, q)  # 回答のたびに払っているので、通常は差がない（念のための最終調整）
                self._conn.execute("UPDATE quiz_questions SET settled=1 WHERE id=?", (row["id"],))
                who = self._all(
                    "SELECT user_id, correct FROM quiz_answers WHERE question_id=? AND guild_id=? ORDER BY ts, rowid",
                    (row["id"], gid),
                )
                out.append(
                    {
                        **q,
                        "participants": n,
                        "correct_count": c,
                        "base": base,
                        "bonus": bonus,
                        "part": part,
                        "right_users": [w["user_id"] for w in who if w["correct"]],
                        "wrong_users": [w["user_id"] for w in who if not w["correct"]],
                    }
                )
        return out

    def quiz_withdraw(self, user_id: int, quiz_id: int, now: float) -> QuizSettleRow | None:
        """出題者が自分の問題を取り下げる。すぐ終了して、出題者の報酬を決着させる。自分の問題でない・終了済みなら None。"""
        with self._db.tx():
            gid = self._g()
            row = self._one(
                "SELECT 1 FROM quiz_questions WHERE id=? AND author_id=? AND guild_id=? AND settled=0",
                (quiz_id, user_id, gid),
            )
            if row is None:
                return None
            self._conn.execute("UPDATE quiz_questions SET expires=? WHERE id=? AND expires>?", (now, quiz_id, now))
            return next((r for r in self.quiz_settle(now) if r["id"] == quiz_id), None)
