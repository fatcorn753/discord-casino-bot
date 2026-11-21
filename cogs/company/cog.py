"""/会社 — 自分だけの会社（帝愛の子会社）を作り、事業所で製品を作って帝愛に売る。

数字と決まりは domain/company.py（catalogs/company.yaml）、処理は services/company.py（保存は infra/db/company.py）、画面の部品は cogs/company/*.py。
"""

import logging
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.company.embeds import holdings_embed, kind_label
from cogs.company.views import CompanyPanel
from cogs.shared import ui
from cogs.shared.announce import announce_channel
from cogs.shared.messages import format_failure
from cogs.shared.tasks import run_for_allowed_guilds
from domain import company as company_mod
from domain.ports.repos import IGuildRepo
from services.company import CompanyService

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot

_log = logging.getLogger(__name__)
NONE = discord.AllowedMentions.none()


class CompanyCog(commands.Cog):
    def __init__(
        self, bot: commands.Bot, config: dict[str, Any], company: CompanyService, guild_repo: IGuildRepo
    ) -> None:
        self.bot = bot
        self._config = config
        self._company = company
        self._guild_repo = guild_repo
        self._busy: set[int] = set()

    async def cog_load(self) -> None:
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    group = app_commands.Group(name="会社", description="🏢 会社（帝愛の子会社を作って、製品を売る）", guild_only=True)

    @group.command(name="設立", description="会社を設立する（設立費と、事業所の建設費がかかります）")
    @app_commands.rename(name="社名", kind="事業所")
    @app_commands.describe(name=f"社名（{company_mod.NAME_MAX}文字まで）", kind="事業所の種類")
    @app_commands.choices(
        kind=[app_commands.Choice(name=kind.name, value=key) for key, kind in company_mod.KINDS.items()]
    )
    async def found(self, interaction: discord.Interaction, name: str, kind: app_commands.Choice[str]) -> None:
        gid = ui.guild_id_of(interaction)
        ok, result = self._company.found(gid, interaction.user.id, name, kind.value)
        if not ok:
            await ui.reply_error(interaction, format_failure(result), title="設立できません")
            return
        await interaction.response.send_message(
            embed=ui.ok_embed(
                "🏢 会社を設立しました",
                f"{interaction.user.mention} が、帝愛の子会社 **{result['name']}**（{kind_label(result['kind'])}）を設立しました。",
            ),
            allowed_mentions=NONE,
        )

    @group.command(name="状況", description="自分の会社を見る・出荷・出資・増築をする")
    async def status(self, interaction: discord.Interaction) -> None:
        panel = CompanyPanel(self._company, interaction.user.id, ui.guild_id_of(interaction))
        await interaction.response.send_message(embed=panel.embed(), view=panel, ephemeral=True)

    # ------------------------------ ほかの人の会社への出資（#71） ------------------------------ #
    @group.command(name="出資", description="ほかの人の会社に出資する（払い戻しなし。配当を受け取れる）")
    @app_commands.rename(owner="社長", amount="金額")
    @app_commands.describe(owner="出資する会社の社長", amount=f"出資する額（{company_mod.STAKE_MIN:,}ペリカから）")
    async def stake(self, interaction: discord.Interaction, owner: discord.Member, amount: int) -> None:
        gid = ui.guild_id_of(interaction)
        ok, result = self._company.stake_invest(gid, interaction.user.id, owner.id, amount)
        if not ok:
            await ui.reply_error(interaction, format_failure(result), title="出資できません")
            return
        await interaction.response.send_message(
            embed=ui.ok_embed(
                "🤝 出資しました",
                f"{interaction.user.mention} が、{owner.mention} の **{result['name']}** に"
                f" **{ui.pelica(result['amount'])}** を出資しました（この会社への出資の合計 {ui.pelica(result['total'])}）。\n"
                "出資は払い戻しできません。配当は、社長が決めた配当率で、出荷のたびに届きます。",
            ),
            allowed_mentions=NONE,
        )

    @group.command(name="配当率", description="自分の会社の配当率を決める（出荷の買取代金のうち、出資者に配る割合）")
    @app_commands.rename(percent="割合")
    @app_commands.describe(percent=f"0〜{company_mod.DIVIDEND_MAX:.0%} の整数（%）")
    async def dividend(self, interaction: discord.Interaction, percent: app_commands.Range[int, 0, 100]) -> None:
        gid = ui.guild_id_of(interaction)
        ok, result = self._company.set_dividend(gid, interaction.user.id, percent / 100)
        if not ok:
            await ui.reply_error(interaction, format_failure(result), title="配当率を変えられません")
            return
        await ui.reply_ok(
            interaction,
            f"これからの出荷では、買取代金の **{result:.0%}** を出資者に配ります。",
            title="配当率を決めました",
        )

    @group.command(name="持分", description="自分の出資先と、受け取った配当を見る")
    async def holdings(self, interaction: discord.Interaction) -> None:
        gid = ui.guild_id_of(interaction)
        rows = self._company.stake_holdings(gid, interaction.user.id)
        await interaction.response.send_message(embed=holdings_embed(rows), ephemeral=True, allowed_mentions=NONE)

    # ------------------------------ 定時の精算（維持費・倒産） ------------------------------ #
    @tasks.loop(minutes=10)
    async def tick(self) -> None:
        try:
            await self.run_once()
        except Exception as exc:
            _log.exception("会社の精算に失敗: %r", exc)

    @tick.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def run_once(self) -> None:
        async def _handle(guild: discord.Guild) -> None:
            for gone in self._company.settle_all(guild.id):
                channel = announce_channel(guild, self._guild_repo)
                if channel is None:
                    continue
                try:
                    await ui.send_channel(
                        channel,
                        embed=ui.embed(
                            "🏚️ 倒産",
                            f"<@{gone['user_id']}> の **{gone['name']}** は、維持費を払えない日が続いたため倒産しました。",
                            ui.ERROR,
                        ),
                        allowed_mentions=NONE,
                    )
                except discord.HTTPException as exc:
                    _log.warning("倒産の告知に失敗: guild=%s: %r", guild.id, exc)

        await run_for_allowed_guilds(
            self.bot,
            self._guild_repo,
            self._config,
            _handle,
            error_label="会社を精算できません",
            busy=self._busy,
            logger=_log,
        )


async def setup(bot: "CasinoBot") -> None:
    await bot.add_cog(CompanyCog(bot, bot.config, bot.services.company, bot.repos.guild))
