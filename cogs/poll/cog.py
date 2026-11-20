"""投票 — `/投票 作成`。誰でも作れる賛成・反対の二択。期限は自由（1分〜30日）。

- 投票はボタン。1人1票で、締切まではあとから変えられる
- 💰 投票を作った人だけは、代金を払うと、表示する票数を好きな数に書き換えられる。書き換えはチャンネルで全員に告知する
  （代金は帝愛銀行の収入。書き換えたあとに入る本物の票は、書き換えた数に足される）
- 締切は1分ごとの見回りで締め、結果を同じチャンネルへ出す。再起動してもボタンは動く
"""

import logging
from typing import TYPE_CHECKING, Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.poll.views import PollButton, RigModal, poll_embed, poll_view, verdict
from cogs.shared import logthrottle, ui
from cogs.shared.bridges import BotBridges, IPollButtonBridge
from cogs.shared.messages import format_failure
from cogs.shared.tasks import run_for_allowed_guilds
from domain import poll
from domain.duration import format_minutes, parse_duration
from domain.ports.repos import IGuildRepo
from services.poll import PollService

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot


async def _error(interaction: discord.Interaction, text: str) -> None:
    await interaction.response.send_message(embed=ui.error_embed("できません", text), ephemeral=True)


class PollCog(commands.Cog, IPollButtonBridge):
    def __init__(self, bot: commands.Bot, config: dict[str, Any], poll_svc: PollService, guild_repo: IGuildRepo):
        self.bot = bot
        self._config = config
        self._poll = poll_svc
        self._guild_repo = guild_repo

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(PollButton)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        bridges = getattr(self.bot, "bridges", None)
        if isinstance(bridges, BotBridges):
            bridges.unregister(IPollButtonBridge, self)

    group = app_commands.Group(name="投票", description="🗳️ 誰でも作れる、賛成・反対の投票", guild_only=True)

    @group.command(name="作成", description="賛成・反対の投票を作る（期限は自由。票はボタンで入れる）")
    @app_commands.rename(title="議題", duration="期限")
    @app_commands.describe(
        title="何について投票するか",
        duration=f"締切までの時間（例: 30分・2時間・3日。省略で{format_minutes(poll.DEFAULT_MINUTES)}、最長30日）",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, poll.TITLE_MAX],
        duration: Optional[str] = None,
    ):
        try:
            minutes = parse_duration(duration) if duration else poll.DEFAULT_MINUTES
        except ValueError as exc:
            return await interaction.response.send_message(embed=ui.error_embed("できません", str(exc)), ephemeral=True)
        gid = ui.guild_id_of(interaction)
        channel_id = interaction.channel_id or 0
        ok, result = self._poll.create(gid, interaction.user.id, title, minutes, channel_id)
        if not ok:
            return await interaction.response.send_message(
                embed=ui.error_embed("できません", format_failure(result)), ephemeral=True
            )
        p = self._poll.get(gid, result)
        assert p is not None
        await interaction.response.send_message(
            embed=poll_embed(p), view=poll_view(result), allowed_mentions=discord.AllowedMentions.none()
        )
        try:
            message = await interaction.original_response()
            self._poll.set_message(gid, result, channel_id, message.id)
        except discord.HTTPException as exc:
            _log.warning(f"投票の投稿を記録できません: guild={gid} poll={result}: {exc!r}")

    # ------------------------------ ボタン ------------------------------ #
    async def handle_button(self, interaction: discord.Interaction, act: str, poll_id: int) -> None:
        gid = ui.guild_id_of(interaction)
        if act == "rig":
            p = self._poll.get(gid, poll_id)
            if p is None or p["status"] != "open":
                await _error(interaction, "この投票はもう締め切られています。")
            elif p["creator"] != interaction.user.id:
                await _error(interaction, "票数を書き換えられるのは、この投票を作った人だけです。")
            else:
                await interaction.response.send_modal(RigModal(self, poll_id, p["yes"], p["no"]))
            return
        ok, result = self._poll.vote(gid, interaction.user.id, poll_id, act == "yes")
        if not ok:
            await _error(interaction, format_failure(result))
            return
        await interaction.response.edit_message(embed=poll_embed(result))
        await interaction.followup.send(f"**{'賛成' if act == 'yes' else '反対'}** に入れました。", ephemeral=True)

    async def rig(self, interaction: discord.Interaction, poll_id: int, yes: int, no: int) -> None:
        """票数の書き換え（RigModal から）。代金を払い、チャンネルで全員に告知し、投稿の票数を直す。"""
        gid = ui.guild_id_of(interaction)
        ok, result = self._poll.rig(gid, interaction.user.id, poll_id, yes, no)
        if not ok:
            await _error(interaction, format_failure(result))
            return
        p, (old_yes, old_no) = result["poll"], result["before"]
        await interaction.response.send_message(
            embed=ui.warn_embed(
                "💰 投票の票数が書き換えられました",
                f"{interaction.user.mention} が **{poll.RIG_COST:,}ペリカ** 払って、投票 #{poll_id}「{p['title']}」の票数を書き換えました。\n"
                f"👍 賛成 {old_yes:,} → **{p['yes']:,}**　👎 反対 {old_no:,} → **{p['no']:,}**",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self._refresh(interaction.guild, p)

    async def _refresh(self, guild: Optional[discord.Guild], p: dict[str, Any], *, closed: bool = False) -> None:
        """投票の投稿の票数を直す。締め切ったときは、ボタンを外す。"""
        channel = guild.get_channel(p["channel_id"]) if guild else None
        msg = ui.partial_message(channel, p["message_id"]) if channel is not None and p["message_id"] else None
        if msg is None:
            return
        try:
            if closed:
                await msg.edit(embed=poll_embed(p), view=None)
            else:
                await msg.edit(embed=poll_embed(p))
        except discord.HTTPException as exc:
            logthrottle.warn(("poll", p["id"]), f"投票の投稿を直せません: poll={p['id']}: {exc!r}")

    # ------------------------------ 締切の見回り（毎分） ------------------------------ #
    @tasks.loop(minutes=1)
    async def tick(self):
        await run_for_allowed_guilds(
            self.bot, self._guild_repo, self._config, self._tick_guild, error_label="投票の締切の見回りに失敗"
        )

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _tick_guild(self, guild: discord.Guild) -> None:
        for p in self._poll.close_due(guild.id):
            await self._refresh(guild, p, closed=True)
            channel = guild.get_channel(p["channel_id"])
            if channel is None:
                continue
            text = f"**{p['title']}**\n👍 賛成 **{p['yes']:,}票**　👎 反対 **{p['no']:,}票** → **{verdict(p)}**"
            if p["rigs"]:
                text += f"\n⚠️ 票数は {p['rigs']}回 書き換えられています"
            try:
                await ui.send_channel(
                    channel,
                    embed=ui.embed(f"🗳️ 投票 #{p['id']} の結果", text),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as exc:
                logthrottle.warn(("poll", p["id"]), f"投票の結果を投稿できません: poll={p['id']}: {exc!r}")


async def setup(bot: "CasinoBot") -> None:
    cog = PollCog(bot, bot.config, bot.services.poll, bot.repos.guild)
    await bot.add_cog(cog)
    bot.bridges.register(IPollButtonBridge, cog)
