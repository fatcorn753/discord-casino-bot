"""クイズの回答ボタン・出題モーダル・公開パネル。"""

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

import discord

from cogs.quiz.embeds import (
    DEFAULT_REWARD,
    LETTERS,
    parse_choices,
    parse_terms,
    parse_verdicts,
    public_embed,
    result_embed,
    verdict,
)
from cogs.shared import ui
from cogs.shared.messages import format_failure
from domain import approval, quizpay
from domain.duration import format_minutes, parse_duration
from infra import logthrottle

if TYPE_CHECKING:
    from cogs.quiz.cog import QuizCog


async def record_answer(bot, interaction: discord.Interaction, quiz_id: int, pos: int) -> None:
    """公開された問題のボタン。押した人が答える（結果は本人だけに見える）。"""
    quiz = bot.economy.quiz_get(quiz_id)
    if quiz is None:
        await interaction.response.send_message(embed=ui.error_embed("この問題は終了しました"), ephemeral=True)
        return
    guild_id = interaction.guild_id
    if guild_id is None or not approval.is_allowed(bot.economy, bot.config, guild_id, interaction.user.id):
        await interaction.response.send_message(embed=ui.error_embed("利用できません"), ephemeral=True)
        return
    eco = bot.economy.for_guild(guild_id)
    ok, result = eco.quiz_answer(interaction.user.id, quiz_id, quiz["order"][pos], time.time())
    if not ok:
        await interaction.response.send_message(
            embed=ui.error_embed("答えられません", format_failure(result)), ephemeral=True
        )
        return
    await interaction.response.send_message(
        embed=result_embed(result, eco.quiz_next(interaction.user.id, time.time())[1], quiz), ephemeral=True
    )
    if result["correct"]:
        paid = "報酬なし" if result["reward_blocked"] else f"+{ui.pelica(result['reward'])}"
        line = f"⭕ <@{interaction.user.id}> が #{quiz_id} に **{verdict(quiz, True)}**（{paid}）"
    else:
        line = f"❌ <@{interaction.user.id}> が #{quiz_id} に **{verdict(quiz, False)}**"
    if result["author_debt_added"]:
        line += (
            f"\n⚠️ 出題者 <@{result['author_id']}> の残高が足りないため、不足 "
            f"{ui.pelica(result['author_debt_added'])} は出題者の借金になりました。"
        )
    if result["author_delta"]:
        sign = "+" if result["author_delta"] > 0 else "−"
        line += f"\n💰 出題者 <@{result['author_id']}> {sign}{ui.pelica(abs(result['author_delta']))}（累計 {ui.pelica(result['author_total'])}）"
    try:
        await interaction.followup.send(line, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as exc:
        logthrottle.warn(
            ("quiz-public", interaction.guild_id), f"回答の結果を投稿できません: guild={interaction.guild_id}: {exc!r}"
        )


class PublicButton(discord.ui.DynamicItem[discord.ui.Button], template=r"quiz:(?P<qid>\d+):(?P<pos>[0-3])"):
    """公開された問題の選択肢ボタン。再起動しても動くように、問題の番号と位置を custom_id に入れている。"""

    def __init__(self, qid: int, pos: int):
        super().__init__(
            discord.ui.Button(label=LETTERS[pos], style=discord.ButtonStyle.primary, custom_id=f"quiz:{qid}:{pos}")
        )
        self.qid, self.pos = qid, pos

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match):
        return cls(int(match["qid"]), int(match["pos"]))

    async def callback(self, interaction: discord.Interaction):
        await record_answer(interaction.client, interaction, self.qid, self.pos)


def public_view(qid: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for pos in range(4):
        view.add_item(PublicButton(qid, pos))
    return view


class AnswerView(ui.OwnerView):
    """4択のボタン。押した人が挑戦者本人のときだけ受け付け、答えたらボタンを外す。"""

    def __init__(self, cog: "QuizCog", user_id: int, quiz: dict, order: list):
        super().__init__(user_id, timeout=600)
        self.cog, self.user_id, self.quiz, self.order = cog, user_id, quiz, order
        for i in range(4):
            btn = discord.ui.Button(label=LETTERS[i], style=discord.ButtonStyle.primary)
            btn.callback = self._make_callback(i)
            self.add_item(btn)

    async def _reject_non_owner(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=ui.error_embed("挑戦者本人だけが答えられます"), ephemeral=True)

    def _make_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            eco = self.cog.bot.economy.for_guild(interaction.guild_id)
            ok, result = eco.quiz_answer(self.user_id, self.quiz["id"], self.order[index], time.time())
            if not ok:
                self.stop()
                return await interaction.response.edit_message(
                    embed=ui.error_embed("答えられません", format_failure(result)), view=None
                )
            _, more = eco.quiz_next(self.user_id, time.time())
            self.stop()
            await interaction.response.edit_message(embed=result_embed(result, more, self.quiz), view=None)

        return callback


@dataclass
class FormValues:
    """出題フォームの入力を、検証したあとの値。minutes が None なら無期限。"""

    choices: list
    reward: int
    minutes: Optional[int]
    msg_right: str
    msg_wrong: str


def verdicts_input() -> discord.ui.TextInput:
    return discord.ui.TextInput(
        label="正解・不正解の文言（2行。省略可）",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=45,
        placeholder="1行目: 正解のとき（例: お見事！）\n2行目: 不正解のとき（例: おしい！）",
    )


class QuizForm(discord.ui.Modal):
    """持ち主の登録フォームと、みんなの出題フォームに共通の入力欄と検証。"""

    terms: discord.ui.TextInput
    verdicts: discord.ui.TextInput

    question = discord.ui.TextInput(label="問題文", style=discord.TextStyle.paragraph, max_length=500)
    choices = discord.ui.TextInput(
        label="選択肢（4行。1行目が正解）", style=discord.TextStyle.paragraph, max_length=400
    )
    explanation = discord.ui.TextInput(
        label="解説（なくてもよい）", style=discord.TextStyle.paragraph, required=False, max_length=800
    )

    def read(
        self, max_reward: Optional[int], default_minutes: Optional[int], min_minutes: int, max_minutes: int
    ) -> FormValues:
        """入力を検証して返す。問題があれば ValueError（文面はそのまま利用者に見せる）。"""
        choices = parse_choices(self.choices.value)
        reward, duration = parse_terms(self.terms.value)
        if reward < 1 or (max_reward is not None and reward > max_reward):
            raise ValueError("報酬は 1 以上で入力してください。")
        msg_right, msg_wrong = parse_verdicts(self.verdicts.value)
        minutes = parse_duration(duration) if duration else default_minutes
        if minutes is not None and not min_minutes <= minutes <= max_minutes:
            raise ValueError(
                f"有効な期間は {format_minutes(min_minutes)}〜{format_minutes(max_minutes)} の範囲で入力してください。"
            )
        return FormValues(choices, reward, minutes, msg_right, msg_wrong)


class AddModal(QuizForm, title="クイズを追加"):
    terms = discord.ui.TextInput(
        label="報酬 / 期間（例: 100 / 3日。期間は省略可）",
        default=str(DEFAULT_REWARD),
        max_length=30,
        placeholder="報酬は 1 以上（上限なし）。期間は 90分・2時間・3日 など（省略で無期限）",
    )
    verdicts = verdicts_input()

    def __init__(self, cog: "QuizCog", announce: bool = True, guild=None, channel=None):
        super().__init__()
        self.cog, self.announce, self.guild, self.channel = cog, announce, guild, channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            form = self.read(None, None, 1, 365 * 1440)
        except ValueError as exc:
            await interaction.response.send_message(embed=ui.error_embed("入力エラー", str(exc)), ephemeral=True)
            return
        now = time.time()
        expires = now + form.minutes * 60 if form.minutes else None
        qid = self.cog.bot.economy.quiz_add(
            self.question.value.strip(),
            form.choices,
            self.explanation.value.strip(),
            form.reward,
            expires,
            now,
            interaction.user.display_name,
            form.msg_right,
            form.msg_wrong,
        )
        await interaction.response.defer(ephemeral=True)
        posted = await self.cog.broadcast(qid, self.guild, self.channel) if self.announce else None
        note = (
            "告知はしていません。`/クイズ 挑戦` で出ます。" if posted is None else f"{posted}サーバーに告知しました。"
        )
        period = f"期間: {format_minutes(form.minutes)}" if form.minutes else "期間: 無期限"
        await interaction.followup.send(
            embed=ui.ok_embed(
                f"クイズ #{qid} を登録しました",
                f"正解: **{form.choices[0]}**　報酬: {ui.pelica(form.reward)}　{period}\n{note}",
            ),
            ephemeral=True,
        )


class UserModal(QuizForm, title="クイズを出題"):
    terms = discord.ui.TextInput(
        label="報酬 / 期間（例: 100 / 2日。期間は省略可）",
        default="100",
        max_length=30,
        placeholder=f"報酬は正解1人あたり 1 以上（上限なし。あなたのペリカ）。期間は最長7日、省略で{format_minutes(quizpay.USER_DEFAULT_MINUTES)}",
    )
    verdicts = verdicts_input()

    def __init__(self, cog: "QuizCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        try:
            form = self.read(
                None,
                quizpay.USER_DEFAULT_MINUTES,
                quizpay.USER_MIN_MINUTES,
                quizpay.USER_MAX_MINUTES,
            )
        except ValueError as exc:
            await interaction.response.send_message(embed=ui.error_embed("入力エラー", str(exc)), ephemeral=True)
            return
        eco = self.cog.bot.economy.for_guild(interaction.guild_id)
        ok, result = eco.quiz_post(
            interaction.user.id,
            self.question.value.strip(),
            form.choices,
            self.explanation.value.strip(),
            form.reward,
            form.minutes,
            interaction.channel_id,
            time.time(),
            interaction.user.display_name,
            form.msg_right,
            form.msg_wrong,
        )
        if not ok:
            await interaction.response.send_message(
                embed=ui.error_embed("出題できません", format_failure(result)), ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=public_embed(eco.quiz_get(result)),
            view=public_view(result),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        eco.quiz_set_message(result, (await interaction.original_response()).id)
