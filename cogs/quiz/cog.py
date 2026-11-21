"""/クイズ — 持ち主・プレイヤーが出題する 4 択クイズの Cog と定期精算タスク。"""

import logging
from typing import TYPE_CHECKING, Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.quiz.embeds import (
    DEFAULT_REWARD as DEFAULT_REWARD,  #
)
from cogs.quiz.embeds import (
    DEFAULT_RIGHT as DEFAULT_RIGHT,  #
)
from cogs.quiz.embeds import (
    DEFAULT_WRONG as DEFAULT_WRONG,  #
)
from cogs.quiz.embeds import (
    LETTERS as LETTERS,  #
)
from cogs.quiz.embeds import (
    MAX_CHOICE_LEN as MAX_CHOICE_LEN,  #
)
from cogs.quiz.embeds import (
    MAX_VERDICT_LEN as MAX_VERDICT_LEN,  #
)
from cogs.quiz.embeds import (
    creator_label as creator_label,  #
)
from cogs.quiz.embeds import (
    names_field as names_field,  #
)
from cogs.quiz.embeds import (
    parse_choices as parse_choices,  #
)
from cogs.quiz.embeds import (
    parse_int as parse_int,  #
)
from cogs.quiz.embeds import (
    parse_terms as parse_terms,  #
)
from cogs.quiz.embeds import (
    parse_verdicts as parse_verdicts,  #
)
from cogs.quiz.embeds import (
    public_embed,
    question_embed,
    result_with_names,
    settle_text,
)
from cogs.quiz.embeds import (
    result_embed as result_embed,  #
)
from cogs.quiz.embeds import (
    verdict as verdict,  #
)
from cogs.quiz.views import (
    AddModal,
    AnswerView,
    PublicButton,
    UserModal,
    public_view,
)
from cogs.quiz.views import (
    FormValues as FormValues,  #
)
from cogs.quiz.views import (
    QuizForm as QuizForm,  #
)
from cogs.quiz.views import (
    record_answer as record_answer,  #
)
from cogs.quiz.views import (
    verdicts_input as verdicts_input,  #
)
from cogs.shared import logthrottle, ui
from cogs.shared.announce import announce_channel
from cogs.shared.bridges import BotBridges, IQuizAdminBridge, IQuizButtonBridge
from cogs.shared.tasks import allowed_guilds, run_for_allowed_guilds
from domain.duration import format_minutes
from domain.ports.repos import IGuildRepo
from services.quiz import QuizService

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot


class QuizCog(commands.Cog, IQuizAdminBridge, IQuizButtonBridge):
    def __init__(
        self,
        bot: commands.Bot,
        config: dict[str, Any],
        quiz_svc: QuizService,
        guild_repo: IGuildRepo,
    ) -> None:
        self.bot = bot
        self._config = config
        self._quiz = quiz_svc
        self._guild_repo = guild_repo

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(PublicButton)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        bridges = getattr(self.bot, "bridges", None)
        if isinstance(bridges, BotBridges):
            bridges.unregister(IQuizAdminBridge, self)
            bridges.unregister(IQuizButtonBridge, self)

    async def handle_button(self, interaction: discord.Interaction, quiz_id: int, pos: int) -> None:
        await record_answer(self._quiz, self._guild_repo, self._config, interaction, quiz_id, pos)

    # ------------------------------ 出題者への報酬（期限が来たら） ------------------------------ #
    @tasks.loop(minutes=1)
    async def tick(self):
        try:
            await self.run_once()
        except Exception as exc:
            _log.warning(f"クイズの精算に失敗: {exc!r}")

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def run_once(self) -> None:
        async def _handle(guild: discord.Guild) -> None:
            for q in self._quiz.settle(guild.id):
                await self.announce_settle(guild, q)

        await run_for_allowed_guilds(
            self.bot,
            self._guild_repo,
            self._config,
            _handle,
            error_label="クイズを精算できません",
        )

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

    async def broadcast(
        self,
        quiz_id: int,
        guild: Optional[discord.Guild] = None,
        channel: Optional[discord.abc.Messageable] = None,
    ) -> int:
        """問題を、許可されている全サーバーの告知チャンネルに出す。出せた投稿の数を返す。"""
        quiz = self._quiz.get(quiz_id)
        if quiz is None:
            return 0
        count = 0
        guilds = [guild] if guild is not None else allowed_guilds(self.bot, self._guild_repo, self._config)
        for g in guilds:
            target = channel or announce_channel(g, self._guild_repo)
            if target is None:
                continue
            try:
                await ui.send_channel(
                    target,
                    embed=public_embed(quiz),
                    view=public_view(quiz_id),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                count += 1
            except discord.HTTPException as exc:
                logthrottle.warn(("quiz", g.id), f"クイズを投稿できません: guild={g.id}: {exc!r}")
        return count

    group = app_commands.Group(name="クイズ", description="📰 時事ネタ・豆知識の4択に答えて、正解でペリカをもらう")

    @group.command(name="挑戦", description="クイズに挑戦する（正解でペリカがもらえる）")
    async def play(self, interaction: discord.Interaction):
        quiz, remaining = self._quiz.next_for(ui.guild_id_of(interaction), interaction.user.id)
        if quiz is None:
            return await interaction.response.send_message(
                embed=ui.embed("📰 クイズ", "いま挑戦できる問題はありません。新しい問題を待ってください。", ui.INFO),
                ephemeral=True,
            )
        await interaction.response.send_message(
            embed=question_embed(quiz, quiz["order"], remaining),
            view=AnswerView(self._quiz, interaction.user.id, quiz, quiz["order"]),
            ephemeral=True,
        )

    @group.command(name="出題", description="自分のクイズを出す（報酬は自分のペリカから払う）")
    async def post(self, interaction: discord.Interaction):
        await interaction.response.send_modal(UserModal(self._quiz))

    @group.command(name="取り下げ", description="自分が出したクイズを終わらせる")
    @app_commands.rename(quiz_id="番号")
    @app_commands.describe(quiz_id="取り下げる問題（自分の問題だけ候補に出ます）")
    async def withdraw(self, interaction: discord.Interaction, quiz_id: int):
        done = self._quiz.withdraw(ui.guild_id_of(interaction), interaction.user.id, quiz_id)
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
        now = self._quiz.now_ts()
        cur = current.strip().lower()
        out = []
        for q in self._quiz.own_open(interaction.guild_id, interaction.user.id):
            text = q["question"].replace("\n", " ")
            label = f"#{q['id']} {text[:50]}（あと{format_minutes(max(1, int((q['expires'] - now) // 60)))}）"
            if not cur or cur in label.lower():
                out.append(app_commands.Choice(name=label[:100], value=q["id"]))
        return out[:25]

    # ------------------------------ 運営の操作（`/運営 クイズ …` から呼ぶ） ------------------------------ #
    def add_modal(
        self,
        announce: bool = True,
        guild: Optional[discord.Guild] = None,
        channel: Optional[discord.abc.Messageable] = None,
    ) -> AddModal:
        return AddModal(self._quiz, self, announce, guild, channel)

    def listing_embed(self) -> discord.Embed:
        now, lines = self._quiz.now_ts(), []
        for q in self._quiz.list_all()[:15]:
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
        return self._quiz.delete(quiz_id)


async def setup(bot: "CasinoBot") -> None:
    cog = QuizCog(bot, bot.config, bot.services.quiz, bot.repos.guild)
    bot.bridges.register(IQuizAdminBridge, cog)
    bot.bridges.register(IQuizButtonBridge, cog)
    await bot.add_cog(cog)
