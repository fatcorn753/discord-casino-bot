"""クイズ（出題・挑戦・回答・取り下げ・期限の精算・運営の追加と削除）のアプリケーションサービス。"""

from typing import Any, Optional

from domain.errors import Outcome
from domain.ports.clock import IClock
from domain.ports.repos import IQuizRepo


class QuizService:
    """クイズのユースケース。時刻は IClock から取り、Cog に Repository と時刻を扱わせない。"""

    def __init__(self, quiz: IQuizRepo, clock: IClock) -> None:
        self._quiz = quiz
        self._clock = clock

    def now_ts(self) -> float:
        return self._clock.now_ts()

    # ------------------------------ 全サーバー共通の問題（運営） ------------------------------ #
    def get(self, quiz_id: int) -> Optional[dict[str, Any]]:
        return self._quiz.quiz_get(quiz_id)

    def list_all(self) -> list[dict[str, Any]]:
        return self._quiz.quiz_list()

    def delete(self, quiz_id: int) -> bool:
        return self._quiz.quiz_delete(quiz_id)

    def existing_questions(self) -> set[str]:
        return self._quiz.quiz_existing_questions()

    def add(
        self,
        question: str,
        choices: list[str],
        explanation: str,
        reward: int,
        minutes: Optional[int],
        creator_name: str = "",
        msg_right: str = "",
        msg_wrong: str = "",
    ) -> int:
        """運営の問題を登録する。minutes が無ければ無期限。登録した番号を返す。"""
        now = self._clock.now_ts()
        expires = now + minutes * 60 if minutes else None
        return self._quiz.quiz_add(
            question, choices, explanation, reward, expires, now, creator_name, msg_right, msg_wrong
        )

    def add_many(self, items: list[dict[str, Any]], creator_name: str = "") -> list[int]:
        return self._quiz.quiz_add_many(items, self._clock.now_ts(), creator_name)

    # ------------------------------ サーバーごとの挑戦・出題 ------------------------------ #
    def next_for(self, guild_id: int, user_id: int) -> tuple[Optional[dict[str, Any]], int]:
        """(次の問題 or None, 残りの問題数)。"""
        return self._quiz.for_guild(guild_id).quiz_next(user_id, self._clock.now_ts())

    def answer(self, guild_id: int, user_id: int, quiz_id: int, chosen: str) -> Outcome[dict[str, Any]]:
        return self._quiz.for_guild(guild_id).quiz_answer(user_id, quiz_id, chosen, self._clock.now_ts())

    def post(
        self,
        guild_id: int,
        author_id: int,
        question: str,
        choices: list[str],
        explanation: str,
        reward: int,
        minutes: int,
        channel_id: int,
        creator_name: str = "",
        msg_right: str = "",
        msg_wrong: str = "",
    ) -> Outcome[int]:
        """プレイヤーの出題（報酬は出題者のペリカから預かる）。"""
        return self._quiz.for_guild(guild_id).quiz_post(
            author_id,
            question,
            choices,
            explanation,
            reward,
            minutes,
            channel_id,
            self._clock.now_ts(),
            creator_name,
            msg_right,
            msg_wrong,
        )

    def get_in(self, guild_id: int, quiz_id: int) -> Optional[dict[str, Any]]:
        return self._quiz.for_guild(guild_id).quiz_get(quiz_id)

    def set_message(self, guild_id: int, quiz_id: int, message_id: int) -> None:
        self._quiz.for_guild(guild_id).quiz_set_message(quiz_id, message_id)

    def own_open(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        return self._quiz.for_guild(guild_id).quiz_own_open(user_id, self._clock.now_ts())

    def withdraw(self, guild_id: int, user_id: int, quiz_id: int) -> Optional[dict[str, Any]]:
        return self._quiz.for_guild(guild_id).quiz_withdraw(user_id, quiz_id, self._clock.now_ts())

    def settle(self, guild_id: int) -> list[dict[str, Any]]:
        """期限の来たプレイヤーの問題を精算する（出題者への報酬）。"""
        return self._quiz.for_guild(guild_id).quiz_settle(self._clock.now_ts())
