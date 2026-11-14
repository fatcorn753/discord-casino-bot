"""/クイズ — 持ち主・プレイヤーが出題する 4 択クイズの Cog と定期精算タスク。"""

import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.quiz.embeds import (
    DEFAULT_REWARD as DEFAULT_REWARD,  # noqa: F401
)
from cogs.quiz.embeds import (
    DEFAULT_RIGHT as DEFAULT_RIGHT,  # noqa: F401
)
from cogs.quiz.embeds import (
    DEFAULT_WRONG as DEFAULT_WRONG,  # noqa: F401
)
from cogs.quiz.embeds import (
    LETTERS as LETTERS,  # noqa: F401
)
from cogs.quiz.embeds import (
    MAX_CHOICE_LEN as MAX_CHOICE_LEN,  # noqa: F401
)
from cogs.quiz.embeds import (
    MAX_VERDICT_LEN as MAX_VERDICT_LEN,  # noqa: F401
)
from cogs.quiz.embeds import (
    creator_label as creator_label,  # noqa: F401
)
from cogs.quiz.embeds import (
    names_field as names_field,  # noqa: F401
)
from cogs.quiz.embeds import (
    parse_choices as parse_choices,  # noqa: F401
)
from cogs.quiz.embeds import (
    parse_int as parse_int,  # noqa: F401
)
from cogs.quiz.embeds import (
    parse_terms as parse_terms,  # noqa: F401
)
from cogs.quiz.embeds import (
    parse_verdicts as parse_verdicts,  # noqa: F401
)
from cogs.quiz.embeds import (
    public_embed,
    question_embed,
    result_with_names,
    settle_text,
)
from cogs.quiz.embeds import (
    result_embed as result_embed,  # noqa: F401
)
from cogs.quiz.embeds import (
    verdict as verdict,  # noqa: F401
)
from cogs.quiz.views import (
    AddModal,
    AnswerView,
    PublicButton,
    UserModal,
    public_view,
)
from cogs.quiz.views import (
    FormValues as FormValues,  # noqa: F401
)
from cogs.quiz.views import (
    QuizForm as QuizForm,  # noqa: F401
)
from cogs.quiz.views import (
    record_answer as record_answer,  # noqa: F401
)
from cogs.quiz.views import (
    verdicts_input as verdicts_input,  # noqa: F401
)
from cogs.shared import ui
from cogs.shared.announce import announce_channel
from domain import approval
from domain.duration import format_minutes
from infra import logthrottle


class QuizCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_dynamic_items(PublicButton)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    # ------------------------------ 出題者への報酬（期限が来たら） ------------------------------ #
    @tasks.loop(minutes=1)
    async def tick(self):
        try:
            await self.run_once()
        except Exception as exc:
            print(f"クイズの精算に失敗: {exc!r}")

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def run_once(self) -> None:
        now = time.time()
        for guild in list(self.bot.guilds):
            if not approval.is_allowed(self.bot.economy, self.bot.config, guild.id, 0):
                continue
            try:
                for q in self.bot.economy.for_guild(guild.id).quiz_settle(now):
                    await self.announce_settle(guild, q)
            except Exception as exc:  # 1サーバーの失敗で、ほかを止めない
                print(f"クイズを精算できません: guild={guild.id}: {exc!r}")

    async def announce_settle(self, guild: discord.Guild, q: dict) -> None:
        """出題の投稿からボタンを外し、結果と出題報酬を同じチャンネルに出す（出題者にはメンションする）。"""
        channel = guild.get_channel(q["channel_id"]) if q["channel_id"] else None
        if channel is None:
            return
        closed = public_embed(q)
        closed.title = (closed.title or "").replace("📰 ", "📰 【終了】")
        result = result_with_names(q)
        try:
            if q["message_id"]:
                msg = ui.partial_message(channel, q["message_id"])
                if msg is not None:
                    await msg.edit(embed=closed, view=None)
            await ui.send_channel(
                channel,
                content=f"<@{q['author_id']}>",
                embed=result,
                allowed_mentions=discord.AllowedMentions(
                    users=[discord.Object(id=q["author_id"])], roles=False, everyone=False
                ),
            )
        except discord.HTTPException as exc:
            logthrottle.warn(("quiz-settle", guild.id), f"クイズの結果を投稿できません: guild={guild.id}: {exc!r}")

    async def broadcast(self, quiz_id: int, only_guild=None, only_channel=None) -> int:
        """問題を、許可されている全サーバーの告知チャンネルに出す。出せた投稿の数を返す。"""
        quiz = self.bot.economy.quiz_get(quiz_id)
        count = 0
        for guild in [only_guild] if only_guild is not None else list(self.bot.guilds):
            if only_guild is None and not approval.is_allowed(self.bot.economy, self.bot.config, guild.id, 0):
                continue
            channel = only_channel or announce_channel(guild, self.bot.economy.for_guild(guild.id))
            if channel is None:
                continue
            try:
                await ui.send_channel(
                    channel,
                    embed=public_embed(quiz),
                    view=public_view(quiz_id),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                count += 1
            except discord.HTTPException as exc:
                logthrottle.warn(("quiz", guild.id), f"クイズを投稿できません: guild={guild.id}: {exc!r}")
        return count

    group = app_commands.Group(name="クイズ", description="📰 時事ネタ・豆知識の4択に答えて、正解でペリカをもらう")

    @group.command(name="挑戦", description="クイズに挑戦する（正解でペリカがもらえる）")
    async def play(self, interaction: discord.Interaction):
        eco = self.bot.economy.for_guild(interaction.guild_id)
        quiz, remaining = eco.quiz_next(interaction.user.id, time.time())
        if quiz is None:
            return await interaction.response.send_message(
                embed=ui.embed("📰 クイズ", "いま挑戦できる問題はありません。新しい問題を待ってください。", ui.INFO),
                ephemeral=True,
            )
        await interaction.response.send_message(
            embed=question_embed(quiz, quiz["order"], remaining),
            view=AnswerView(self, interaction.user.id, quiz, quiz["order"]),
            ephemeral=True,
        )

    @group.command(name="出題", description="自分のクイズを出す（報酬は自分のペリカから払う）")
    async def post(self, interaction: discord.Interaction):
        await interaction.response.send_modal(UserModal(self))

    @group.command(name="取り下げ", description="自分が出したクイズを終わらせる")
    @app_commands.rename(quiz_id="番号")
    @app_commands.describe(quiz_id="取り下げる問題（自分の問題だけ候補に出ます）")
    async def withdraw(self, interaction: discord.Interaction, quiz_id: int):
        done = self.bot.economy.for_guild(interaction.guild_id).quiz_withdraw(interaction.user.id, quiz_id, time.time())
        if done is None:
            return await interaction.response.send_message(
                embed=ui.error_embed(
                    "取り下げられません", "あなたがこのサーバーで出した、終了前の問題の番号を指定してください。"
                ),
                ephemeral=True,
            )
        await interaction.response.send_message(
            embed=ui.ok_embed(f"クイズ #{quiz_id} を取り下げました", settle_text(done)), ephemeral=True
        )
        if interaction.guild is not None:
            await self.announce_settle(interaction.guild, done)

    @withdraw.autocomplete("quiz_id")
    async def withdraw_autocomplete(self, interaction: discord.Interaction, current: str):
        """候補は、自分がこのサーバーで出した、終了前の問題だけ。"""
        if interaction.guild_id is None:
            return []
        now = time.time()
        cur = current.strip().lower()
        out = []
        for q in self.bot.economy.for_guild(interaction.guild_id).quiz_own_open(interaction.user.id, now):
            text = q["question"].replace("\n", " ")
            label = f"#{q['id']} {text[:50]}（あと{format_minutes(max(1, int((q['expires'] - now) // 60)))}）"
            if not cur or cur in label.lower():
                out.append(app_commands.Choice(name=label[:100], value=q["id"]))
        return out[:25]

    # ------------------------------ 運営の操作（`/運営 クイズ …` から呼ぶ） ------------------------------ #
    def add_modal(self, announce: bool = True, guild=None, channel=None) -> AddModal:
        return AddModal(self, announce, guild, channel)

    def listing_embed(self) -> discord.Embed:
        now, lines = time.time(), []
        for q in self.bot.economy.quiz_list()[:15]:
            state = "⌛" if q["expires"] is not None and now >= q["expires"] else "🟢"
            until = "" if q["expires"] is None else f" 〜<t:{int(q['expires'])}:d>"
            lines.append(
                f"{state}{'👤' if q['author_id'] else ''} `#{q['id']}` {q['question'][:40].replace(chr(10), ' ')}　正解「{q['choices'][0][:20]}」　{q['reward']:,}ペリカ{until}　{q['right_count']}/{q['answered']}人正解"
            )
        return ui.embed(
            "📰 クイズ一覧（新しい順・15件まで）",
            "\n".join(lines) or "まだありません。`/運営 クイズ 追加` で登録できます。",
            ui.INFO,
        )

    def remove(self, quiz_id: int) -> bool:
        return self.bot.economy.quiz_delete(quiz_id)


async def setup(bot):
    await bot.add_cog(QuizCog(bot))
