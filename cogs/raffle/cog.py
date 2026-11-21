"""抽選 — Bot の持ち主が（`/運営 抽選 …` から）開催する。参加費を先に預かり、締切に参加者から1人へ全額渡す。参加はボタン。

- 参加費は全員同じ額（掛金）。集めた参加費の全額（手数料なし）が当選者へ渡る。
- 参加者が2人に満たないとき、中止したときは、全員に全額を返す。
- 開催状況と参加者は DB に保存する。Bot が再起動しても、ボタンは動き、締切を過ぎていれば起動後に締める。
- ミッション・連勝・週間ランキング・突発イベントには数えない。
"""

import logging
import random
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands, tasks

from cogs.raffle.embeds import cancelled_embed as cancelled_embed  # （result_embed はテストも参照）
from cogs.raffle.embeds import raffle_embed as raffle_embed
from cogs.raffle.embeds import result_embed as result_embed
from cogs.raffle.views import JoinView
from cogs.shared import approval_guard, logthrottle, ui
from cogs.shared.announce import announce_channel
from cogs.shared.bridges import BotBridges, IRaffleAdminBridge
from cogs.shared.messages import format_failure
from cogs.shared.tasks import run_for_allowed_guilds
from domain.ports import get_catalogs
from domain.ports.contracts import RaffleResult
from domain.ports.repos import IGuildRepo
from services.raffle import RaffleService

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot

_RAFFLE_CFG = get_catalogs().economy().raffle
MIN_FEE = _RAFFLE_CFG.min_fee  # 参加費の最小。上限はない
RANDOM_FEE_RANGE = _RAFFLE_CFG.random_fee_range  # 参加費を省略したときの、ランダムの範囲
DEFAULT_MINUTES = _RAFFLE_CFG.default_minutes
MAX_MINUTES = _RAFFLE_CFG.max_minutes
RNG = random.SystemRandom()  # 参加費を省略したときの乱数。テストは rng を注入して決定性を担保する


class RaffleCog(commands.Cog, IRaffleAdminBridge):
    def __init__(
        self,
        bot: commands.Bot,
        config: dict[str, Any],
        raffle_svc: RaffleService,
        guild_repo: IGuildRepo,
    ) -> None:
        self.bot = bot
        self._config = config
        self._raffle = raffle_svc
        self._guild_repo = guild_repo

    async def cog_load(self) -> None:
        self.bot.add_view(JoinView(self))
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        bridges = getattr(self.bot, "bridges", None)
        if isinstance(bridges, BotBridges):
            bridges.unregister(IRaffleAdminBridge, self)

    # ------------------------------ 運営の操作（`/運営 抽選 …` から呼ぶ。サーバーは持ち主が指定する） ------------------------------ #
    async def host(
        self,
        guild: discord.Guild,
        host_id: int,
        fee: int | None = None,
        minutes: int = DEFAULT_MINUTES,
        channel: discord.abc.Messageable | None = None,
        rng: random.Random | None = None,
    ) -> tuple[bool, str]:
        """そのサーバーの告知チャンネル（channel を渡せば、そのチャンネル）に、抽選の募集を出す。(成功か, 説明) を返す。"""
        target = channel or announce_channel(guild, self._guild_repo)
        if target is None:
            return (
                False,
                "投稿できるチャンネルがありません（ランキング掲示のチャンネル、またはシステムチャンネルに、投稿の権限が必要です）。",
            )
        r = rng if rng is not None else RNG
        fee = fee or r.randint(RANDOM_FEE_RANGE[0] // 100, RANDOM_FEE_RANGE[1] // 100) * 100
        target_id = getattr(target, "id", 0) or 0
        ok, reason = self._raffle.open(guild.id, host_id, fee, minutes, target_id)
        if not ok:
            return False, format_failure(reason)
        raffle = self._raffle.current(guild.id)
        assert raffle is not None
        try:
            message = await ui.send_channel(
                target,
                embed=raffle_embed(raffle),
                view=JoinView(self),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            self._raffle.cancel(guild.id)  # 投稿できなかったので、募集はなかったことにする
            return False, f"投稿できませんでした: {exc}"
        if message is not None:
            self._raffle.set_message(guild.id, raffle["id"], message.id)
        mention = getattr(target, "mention", str(target))
        return True, f"{guild.name} の {mention} に投稿しました（参加費 {ui.pelica(fee)}・{minutes}分）。"

    async def repost(self, guild: discord.Guild, channel: discord.abc.Messageable | None = None) -> tuple[bool, str]:
        """募集中の抽選の投稿を、告知チャンネル（channel を渡せば、そのチャンネル）に出し直す（古い投稿は消す）。"""
        raffle = self._raffle.current(guild.id)
        if raffle is None:
            return False, "募集中の抽選がありません。"
        target = channel or announce_channel(guild, self._guild_repo)
        if target is None:
            return False, "投稿できるチャンネルがありません。"
        old_channel, old_id = guild.get_channel(raffle["channel_id"]), raffle["message_id"]
        try:
            message = await ui.send_channel(
                target,
                embed=raffle_embed(raffle),
                view=JoinView(self),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            return False, f"投稿できませんでした: {exc}"
        target_id = getattr(target, "id", 0) or 0
        if message is not None:
            self._raffle.set_message(
                guild.id, raffle["id"], message.id, target_id
            )  # 古い投稿のボタンは、これで無効になる
        if old_channel is not None and old_id:
            try:
                old_msg = ui.partial_message(old_channel, old_id)
                if old_msg is not None:
                    await old_msg.delete()
            except discord.HTTPException:
                pass  # すでに消えている・消せない場合は、そのままにする
        mention = getattr(target, "mention", str(target))
        return True, f"{guild.name} の {mention} に出し直しました。"

    async def cancel(self, guild: discord.Guild) -> tuple[bool, str]:
        """募集中の抽選をやめて、参加費を全員に返す。"""
        raffle = self._raffle.cancel(guild.id)
        if raffle is None:
            return False, "募集中の抽選がありません。"
        await self._finish_message(
            guild,
            raffle,
            cancelled_embed(raffle),
        )
        return True, f"{guild.name} の抽選を中止し、{len(raffle['entries'])}人に返金しました。"

    # ------------------------------ 参加ボタン ------------------------------ #
    async def on_join(self, interaction: discord.Interaction) -> None:
        if not await approval_guard.allow_component(interaction, self._guild_repo, self._config, feature="raffle"):
            return
        gid = ui.guild_id_of(interaction)
        raffle = self._raffle.current(gid)

        if raffle is None or interaction.message is None or raffle["message_id"] != interaction.message.id:
            await ui.reply_error(interaction, "", title="募集は終わりました")
            return
        ok, reason = self._raffle.join(gid, interaction.user.id)
        if not ok:
            await ui.reply_error(interaction, format_failure(reason), title="参加できません")
            return
        raffle = self._raffle.current(gid)
        assert raffle is not None
        await interaction.response.edit_message(embed=raffle_embed(raffle))
        await interaction.followup.send(
            embed=ui.ok_embed(
                "参加しました",
                f"参加費 {ui.pelica(raffle['fee'])} を預かりました。結果は締切にこのメッセージの下へ出ます。",
            ),
            ephemeral=True,
        )

    # ------------------------------ 締切 ------------------------------ #
    @tasks.loop(seconds=20)
    async def tick(self) -> None:
        try:
            await self.run_once()
        except Exception as exc:
            _log.warning(f"抽選の確認に失敗: {exc!r}")

    @tick.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def run_once(self) -> None:
        async def _handle(guild: discord.Guild) -> None:
            done = self._raffle.close_if_due(guild.id)
            if done is not None:
                await self._finish_message(guild, done, result_embed(done))

        await run_for_allowed_guilds(
            self.bot,
            self._guild_repo,
            self._config,
            _handle,
            error_label="抽選を締められません",
        )

    async def _finish_message(self, guild: discord.Guild, raffle: RaffleResult, embed: discord.Embed) -> None:
        """募集の投稿からボタンを外し、結果を同じチャンネルへ出す（当選者にはメンションする）。"""
        channel = guild.get_channel(raffle["channel_id"]) if guild else None
        if channel is None:
            return
        winner = raffle.get("winner")
        mentions = discord.AllowedMentions(
            users=[discord.Object(id=winner)] if winner else [], roles=False, everyone=False
        )
        try:
            if raffle["message_id"]:
                closed = raffle_embed(raffle)
                closed.title = "🎟️ 抽選（募集終了）"
                msg = ui.partial_message(channel, raffle["message_id"])
                if msg is not None:
                    await msg.edit(embed=closed, view=None)
            await ui.send_channel(
                channel,
                content=f"🎉 <@{winner}>" if winner else None,
                embed=embed,
                allowed_mentions=mentions,
            )
        except discord.HTTPException as exc:
            logthrottle.warn(("raffle", guild.id), f"抽選の結果を投稿できません: guild={guild.id}: {exc!r}")


async def setup(bot: "CasinoBot") -> None:
    cog = RaffleCog(bot, bot.config, bot.services.raffle, bot.repos.guild)
    bot.bridges.register(IRaffleAdminBridge, cog)
    await bot.add_cog(cog)
