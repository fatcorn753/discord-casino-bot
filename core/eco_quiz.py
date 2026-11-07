"""クイズの記録（Economy に混ぜ込む部品）。問題・回答・出題者への報酬の支払い。

表の定義と移行は core/economy.py の _init_schema にある。
"""

import json
from typing import Optional

from core import quizpay as quizpay_mod
from core.schema import new_quiz_order


class QuizMixin:
    """クイズの問題・回答・出題者への報酬。"""

    # ------------------------------------------------------------------ #
    # クイズ（持ち主が作った4択。1問につき1人1回、正解で報酬）            #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _quiz_row(row) -> Optional[dict]:
        if row is None:
            return None
        d = dict(row)
        d["choices"] = json.loads(d["choices"])
        d["order"] = (
            [d["choices"][i] for i in json.loads(d["shown_order"])] if d.get("shown_order") else d["choices"][:]
        )  # 表示する選択肢の並び
        return d

    def quiz_add(
        self,
        question: str,
        choices: list,
        explanation: str,
        reward: int,
        expires: Optional[float],
        now: float,
        creator_name: str = "",
        msg_right: str = "",
        msg_wrong: str = "",
    ) -> int:
        """問題を登録する（choices の先頭が正解）。登録した問題の番号を返す。creator_name は問題に出す制作者の名前。msg_right / msg_wrong は、正解・不正解の見出しの文言（空なら既定）。"""
        with self._lock, self._conn:
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
            return cur.lastrowid

    def quiz_add_many(self, items: list, now: float, creator_name: str = "") -> list:
        """まとめて登録する（全部成功するか、全部なしか）。items は {question, choices, explanation, reward, minutes} の並び（minutes が None なら無期限）。番号のリストを返す。"""
        with self._lock, self._conn:
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

    def quiz_existing_questions(self) -> set:
        """登録済みの問題文（同じファイルを2回登録してしまわないための照合用）。"""
        with self._lock:
            return {r[0] for r in self._conn.execute("SELECT question FROM quiz_questions")}

    def quiz_get(self, quiz_id: int) -> Optional[dict]:
        with self._lock:
            return self._quiz_row(self._conn.execute("SELECT * FROM quiz_questions WHERE id=?", (quiz_id,)).fetchone())

    def quiz_list(self) -> list:
        """全問題（新しい順）。回答数と正解数つき。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT q.*, COUNT(a.user_id) AS answered, COALESCE(SUM(a.correct), 0) AS right_count "
                "FROM quiz_questions q LEFT JOIN quiz_answers a ON a.question_id = q.id GROUP BY q.id ORDER BY q.id DESC"
            ).fetchall()
        return [self._quiz_row(r) for r in rows]

    def quiz_delete(self, quiz_id: int) -> bool:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM quiz_answers WHERE question_id=?", (quiz_id,))
            return self._conn.execute("DELETE FROM quiz_questions WHERE id=?", (quiz_id,)).rowcount > 0

    def quiz_next(self, user_id: int, now: float) -> tuple:
        """まだ答えていない、期限内の一番古い問題と、その残り数（その問題を含む）を返す。無ければ (None, 0)。

        ユーザーの問題は、このサーバーのもので、自分の問題ではなく、出題者がまだ報酬を払える間だけ出る。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM quiz_questions q WHERE (q.expires IS NULL OR q.expires > ?) "
                "AND (q.author_id=0 OR (q.guild_id=? AND q.author_id!=? AND COALESCE((SELECT u.balance FROM users u WHERE u.guild_id=q.guild_id AND u.user_id=q.author_id), 0) >= q.reward)) "
                "AND NOT EXISTS (SELECT 1 FROM quiz_answers a WHERE a.question_id=q.id AND a.guild_id=? AND a.user_id=?) ORDER BY q.id",
                (now, self._g(), user_id, self._g(), user_id),
            ).fetchall()
        return (self._quiz_row(rows[0]), len(rows)) if rows else (None, 0)

    def quiz_answer(self, user_id: int, quiz_id: int, chosen: str, now: float) -> tuple:
        """答えを記録する。(成功か, 理由か結果) を返す。結果は correct / reward（実際に入った額）/ answer / explanation。

        ユーザーの問題の報酬は、出題者の残高から引いて渡す（出題者の残高が報酬に足りなければ、その問題は終了）。
        """
        with self._lock, self._conn:
            q = self._quiz_row(self._conn.execute("SELECT * FROM quiz_questions WHERE id=?", (quiz_id,)).fetchone())
            if q is None or (q["expires"] is not None and now >= q["expires"]):
                return False, "この問題は終了しました。"
            author = q["author_id"]
            if author:
                if q["guild_id"] != self._g():
                    return False, "この問題は、出題されたサーバーでだけ答えられます。"
                if user_id == author:
                    return False, "自分が出した問題には答えられません。"
                if self._balance(author) < q["reward"]:
                    return False, "出題者の残高が尽きたので、この問題は終了しました。"
            self._ensure(user_id)
            if self._conn.execute(
                "SELECT 1 FROM quiz_answers WHERE question_id=? AND guild_id=? AND user_id=?",
                (quiz_id, self._g(), user_id),
            ).fetchone():
                return False, "この問題にはもう答えています。"
            correct = chosen == q["choices"][0]
            self._conn.execute(
                "INSERT INTO quiz_answers (question_id, guild_id, user_id, correct, ts) VALUES (?, ?, ?, ?, ?)",
                (quiz_id, self._g(), user_id, int(correct), now),
            )
            paid = 0
            if correct:
                if author:
                    self._apply(author, -q["reward"], "クイズ報酬の支払い")
                paid = self._apply(
                    user_id, q["reward"], "クイズ報酬"
                )  # 持ち主の問題の報酬は、システムから出る（増える）。みんなの問題は出題者から
            result = {
                "correct": correct,
                "reward": paid,
                "answer": q["choices"][0],
                "explanation": q["explanation"],
                "author_id": author,
                "author_delta": 0,
                "author_total": 0,
            }
            if author:
                result["author_delta"], result["author_total"] = self._quiz_pay_author(q)
            return True, result

    def _quiz_pay_author(self, q: dict) -> tuple:
        """いまの回答状況での出題者の受け取り額（基本+ボーナス+参加報酬）と、すでに払った累計の差を払う（減るときは戻す）。

        (動いた額, 累計) を返す。戻すときは、出題者の残高の範囲まで。期限のときの最終合計は、回答のたびに払った累計と同じになる。
        """
        n, c = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(correct), 0) FROM quiz_answers WHERE question_id=? AND guild_id=?",
            (q["id"], self._g()),
        ).fetchone()
        entitled = sum(quizpay_mod.author_payout(n, c, q["reward"]))
        earned = self._conn.execute("SELECT earned FROM quiz_questions WHERE id=?", (q["id"],)).fetchone()[0]
        diff = entitled - earned
        if diff > 0:
            self._apply(q["author_id"], diff, "クイズ出題報酬")
            moved = diff
        elif diff < 0:
            moved = -self.debit_up_to(q["author_id"], -diff, "クイズ出題報酬（調整）")
        else:
            moved = 0
        self._conn.execute("UPDATE quiz_questions SET earned=? WHERE id=?", (earned + moved, q["id"]))
        return moved, earned + moved

    # ---- ユーザーが出す問題 ---- #
    def quiz_post(
        self,
        author_id: int,
        question: str,
        choices: list,
        explanation: str,
        reward: int,
        minutes: int,
        channel_id: int,
        now: float,
        creator_name: str = "",
        msg_right: str = "",
        msg_wrong: str = "",
    ) -> tuple:
        """ユーザーが問題を出す。(成功か, 理由か問題の番号) を返す。"""
        with self._lock, self._conn:
            self._ensure(author_id)
            if self._debt(author_id) > 0:
                return False, f"借金が {self._debt(author_id):,}ペリカ 残っているので、出題できません。"
            mine = self._conn.execute(
                "SELECT created, expires, settled FROM quiz_questions WHERE author_id=? AND guild_id=?",
                (author_id, self._g()),
            ).fetchall()
            if sum(1 for r in mine if not r["settled"] and r["expires"] > now) >= quizpay_mod.OPEN_LIMIT:
                return (
                    False,
                    f"同時に出せる問題は {quizpay_mod.OPEN_LIMIT} つまでです。`/クイズ 取り下げ` で下げてから出してください。",
                )
            if sum(1 for r in mine if r["created"] > now - 86400) >= quizpay_mod.DAILY_LIMIT:
                return False, f"出題は、24時間に {quizpay_mod.DAILY_LIMIT} 問までです。"
            if self._balance(author_id) < reward:
                return (
                    False,
                    f"残高が報酬（{reward:,}ペリカ）に足りません。正解者への報酬は、あなたの残高から払われます。",
                )
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
                    self._g(),
                    channel_id,
                    creator_name,
                    msg_right,
                    msg_wrong,
                ),
            )
            return True, cur.lastrowid

    def quiz_own_open(self, user_id: int, now: float) -> list:
        """このサーバーで自分が出した、終了前の問題（古い順）。取り下げの候補に使う。"""
        with self._lock:
            return [
                self._quiz_row(r)
                for r in self._conn.execute(
                    "SELECT * FROM quiz_questions WHERE author_id=? AND guild_id=? AND settled=0 AND expires>? ORDER BY id",
                    (user_id, self._g(), now),
                )
            ]

    def quiz_set_message(self, quiz_id: int, message_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE quiz_questions SET message_id=? WHERE id=?", (message_id, quiz_id))

    def quiz_settle(self, now: float) -> list:
        """期限を過ぎたユーザーの問題の、出題者への報酬を決着させる（このサーバーの分）。決着した問題を、成績つきで返す。"""
        out = []
        with self._lock, self._conn:
            for row in self._conn.execute(
                "SELECT * FROM quiz_questions WHERE author_id!=0 AND guild_id=? AND settled=0 AND expires<=? ORDER BY id",
                (self._g(), now),
            ).fetchall():
                n, c = self._conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(correct), 0) FROM quiz_answers WHERE question_id=? AND guild_id=?",
                    (row["id"], self._g()),
                ).fetchone()
                base, bonus, part = quizpay_mod.author_payout(n, c, row["reward"])
                self._quiz_pay_author(
                    self._quiz_row(row)
                )  # 回答のたびに払っているので、通常は差がない（念のための最終調整）
                self._conn.execute("UPDATE quiz_questions SET settled=1 WHERE id=?", (row["id"],))
                who = self._conn.execute(
                    "SELECT user_id, correct FROM quiz_answers WHERE question_id=? AND guild_id=? ORDER BY ts, rowid",
                    (row["id"], self._g()),
                ).fetchall()
                out.append(
                    {
                        **self._quiz_row(row),
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

    def quiz_withdraw(self, user_id: int, quiz_id: int, now: float) -> Optional[dict]:
        """出題者が自分の問題を取り下げる。すぐ終了して、出題者の報酬を決着させる。自分の問題でない・終了済みなら None。"""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT 1 FROM quiz_questions WHERE id=? AND author_id=? AND guild_id=? AND settled=0",
                (quiz_id, user_id, self._g()),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute("UPDATE quiz_questions SET expires=? WHERE id=? AND expires>?", (now, quiz_id, now))
            return next((r for r in self.quiz_settle(now) if r["id"] == quiz_id), None)
