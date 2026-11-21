"""/地下 作業 — ペリカが尽きた人の救済。ボタンを押すと稼げるクリッカーと、作業員を雇って放置でも稼ぐ雇用。

「働く」を押すとペリカが貯まる。疲れると稼ぎが減る。稼いだペリカは、まとめて受け取る。稼いだペリカで、工具を強化していく。
数字と計算は domain/labor.py、処理は services/labor.py（保存は infra/db/labor.py）。画面の組み立ては embeds.py・views.py。画面は、チャンネルに公開される（みんなに見えるが、押せるのは開いた本人だけ。再起動すると、画面は開き直す）。
"""

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from cogs.labor.embeds import (  # テストと他の Cog が cog から参照する
    MEDALS as MEDALS,
)
from cogs.labor.embeds import fatigue_label as fatigue_label
from cogs.labor.embeds import finds_text as finds_text
from cogs.labor.embeds import hire_embed as hire_embed
from cogs.labor.embeds import ore_text as ore_text
from cogs.labor.embeds import panel_embed as panel_embed
from cogs.labor.embeds import pot_text as pot_text
from cogs.labor.embeds import ranking_embed as ranking_embed
from cogs.labor.embeds import relic_text as relic_text
from cogs.labor.views import VIEW_TIMEOUT as VIEW_TIMEOUT
from cogs.labor.views import LaborView as LaborView
from cogs.shared import ui
from services.labor import LaborService

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot


class LaborCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        labor_svc: LaborService,
    ) -> None:
        self.bot = bot
        self._svc = labor_svc
        # (サーバー, 人) → 最後に開いた作業場の (チャンネルID, メッセージID)。新しく開いたら、前のを消す（チャンネルが作業場で埋まらないように）
        self._panels: dict = {}

    group = app_commands.Group(name="地下", description="⛏ ペリカが尽きたときの、地下の仕事", guild_only=True)

    @group.command(name="作業", description="ボタンを押して稼ぐ（元手は0）。稼いだら、作業員を雇って放置でも稼ぐ")
    async def work(self, interaction: discord.Interaction):
        gid = ui.guild_id_of(interaction)
        view = LaborView(self._svc, interaction.user.id, gid)
        key = (gid, interaction.user.id)
        old = self._panels.pop(key, None)
        await interaction.response.send_message(embed=panel_embed(view.state), view=view)
        if old:
            try:
                await self.bot.get_partial_messageable(old[0]).get_partial_message(old[1]).delete()
            except discord.HTTPException:
                pass  # すでに消えている・消せない: そのまま
        try:
            msg = await interaction.original_response()
            self._panels[key] = (msg.channel.id, msg.id)
        except discord.HTTPException:
            pass

    @group.command(name="ランキング", description="⛏ 採掘ランキング（稼いだペリカの多い順）")
    @app_commands.rename(period="期間")
    @app_commands.describe(period="今日の稼ぎか、通算の稼ぎか（省略で通算）")
    @app_commands.choices(
        period=[app_commands.Choice(name="通算", value="total"), app_commands.Choice(name="今日", value="today")]
    )
    async def ranking(self, interaction: discord.Interaction, period: str = "total"):
        daily = period == "today"
        data = self._svc.top(ui.guild_id_of(interaction), daily, interaction.user.id)
        await interaction.response.send_message(
            embed=ranking_embed(data, daily), allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot: "CasinoBot") -> None:
    await bot.add_cog(LaborCog(bot, bot.services.labor))
