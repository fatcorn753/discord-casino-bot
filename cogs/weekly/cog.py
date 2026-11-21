"""週間ランキングの自動発表。毎週月曜0時（設定の時差）に、先週の稼ぎ1位へ限定称号を贈り、結果を投稿する。

発表の場所は、ランキング掲示をしているチャンネル。無ければサーバーのシステムチャンネル。
Bot が止まっていて月曜0時を逃しても、次に起動したときに1回だけ発表する（二重には発表しない）。
"""

import logging
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands, tasks

from cogs.shared import logthrottle, ui
from cogs.shared.announce import announce_channel
from cogs.shared.bridges import DeferredShopTitleBridge, IShopTitleBridge
from cogs.shared.tasks import run_for_allowed_guilds
from cogs.weekly.embeds import weekly_embed
from domain import weekly
from domain.ports import get_catalogs
from domain.ports.clock import IClock
from domain.ports.repos import IGuildRepo
from services.economy import EconomyService

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot

CHECK_MINUTES = get_catalogs().economy().weekly.check_minutes


class WeeklyCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        config: dict[str, Any],
        economy_svc: EconomyService,
        guild_repo: IGuildRepo,
        shop_bridge: IShopTitleBridge | None = None,
        clock: IClock | None = None,
    ) -> None:
        self.bot = bot
        self._config = config
        self._economy_svc = economy_svc
        self._guild_repo = guild_repo
        self._shop_bridge: IShopTitleBridge = shop_bridge if shop_bridge is not None else DeferredShopTitleBridge(bot)
        self._clock: IClock = clock if clock is not None else economy_svc.clock

    async def cog_load(self) -> None:
        done = self._economy_svc.backfill_weekly()  # この機能の前に遊んだ分を、台帳から補う（初回だけ）
        if done:
            _log.info(f"週間ランキング: 過去の {done} ゲーム分を台帳から補いました")
        self.check_weeks.start()

    async def cog_unload(self) -> None:
        self.check_weeks.cancel()

    @tasks.loop(minutes=CHECK_MINUTES)
    async def check_weeks(self) -> None:
        try:
            await self.run_once()
        except Exception as exc:
            _log.warning(f"週間ランキングの確認に失敗: {exc!r}")

    @check_weeks.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def run_once(self) -> None:
        today = self._clock.today()
        prev_week = weekly.previous_week_id(today)

        async def _handle(guild: discord.Guild) -> None:
            last = self._economy_svc.weekly_last_announced(guild.id)
            if last is None:
                # 初めて動いたときは、先週までを発表済みとして、次の月曜から発表する
                self._economy_svc.set_weekly_announced(guild.id, prev_week)
            elif last < prev_week:
                await self.announce(guild, prev_week)

        await run_for_allowed_guilds(
            self.bot,
            self._guild_repo,
            self._config,
            _handle,
            error_label="週間ランキングの発表に失敗",
        )

    async def announce(self, guild: discord.Guild, week: str) -> None:
        result = self._economy_svc.weekly_award(guild.id, week)
        if result is None or not result["standings"]:
            return  # 発表済み、またはその週は誰も遊んでいない
        channel = announce_channel(guild, self._guild_repo)
        if channel is not None:
            champ = result["champion"]
            mentions = discord.AllowedMentions(
                users=[discord.Object(id=champ[0])] if champ else [], roles=False, everyone=False
            )
            try:
                await ui.send_channel(
                    channel,
                    content=f"🎉 <@{champ[0]}>" if champ else None,
                    embed=weekly_embed(week, result),
                    allowed_mentions=mentions,
                )
            except discord.HTTPException as exc:
                logthrottle.warn(("weekly", guild.id), f"週間ランキングを投稿できません: guild={guild.id}: {exc!r}")
        for uid in result["previous"]:  # 外れた称号をニックネームからも消す
            try:
                await self._shop_bridge.sync_title_nick(uid, guild)
            except Exception as exc:
                _log.warning(f"称号の取り消しをニックネームに反映できません: guild={guild.id} user={uid}: {exc!r}")


async def setup(bot: "CasinoBot") -> None:
    await bot.add_cog(
        WeeklyCog(
            bot,
            bot.config,
            bot.services.economy,
            bot.repos.guild,
            clock=bot.services.clock,
        )
    )
