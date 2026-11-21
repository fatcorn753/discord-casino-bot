"""/貸付 — ユーザー同士の貸し借り（日割りの単利）。

- 貸す人が `/貸付 提案` で、相手・金額・日利・期間を決める。投稿の「承諾」を借りる本人が押すと成立し、お金が移る。
- 借りたい人が `/貸付 募集` で、金額・日利・期間を決めて募集する。最初に「貸す」を押した人が貸し手になり、その場でお金が移る。
- 返済は `/貸付 返済`。期限には、借りた人の残高から自動で回収する。足りない分は延滞になり、入ってくるお金から古い順に回収する。
- 期限の前日と、延滞になったときに、貸付を出したチャンネルに知らせる。
- 担保（CSトークン・称号・債券）は、次の段階で足す。
"""

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.loan.embeds import (
    borrower_note as borrower_note,
)
from cogs.loan.embeds import offer_embed as offer_embed
from cogs.loan.embeds import request_embed as request_embed
from cogs.loan.embeds import state_embed as state_embed
from cogs.loan.embeds import terms_lines as terms_lines
from cogs.loan.embeds import who as who
from cogs.loan.views import (
    LoanButton as LoanButton,
)
from cogs.loan.views import handle_button as handle_button
from cogs.loan.views import handle_lend as handle_lend
from cogs.loan.views import offer_view as offer_view
from cogs.loan.views import request_view as request_view
from cogs.shared import logthrottle, ui
from cogs.shared.amount import balance_choices
from cogs.shared.announce import announce_channel
from cogs.shared.bridges import BotBridges, ILoanButtonBridge
from cogs.shared.messages import format_failure
from cogs.shared.tasks import run_for_allowed_guilds
from domain import loans
from domain.duration import format_minutes
from domain.errors import Failure
from domain.ports.contracts import LoanRow
from domain.ports.repos import IGuildRepo
from services.economy import EconomyService
from services.loan import LoanService

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot


class LoanCog(commands.Cog, ILoanButtonBridge):
    def __init__(
        self,
        bot: commands.Bot,
        config: dict[str, Any],
        loan_svc: LoanService,
        economy_svc: EconomyService,
        guild_repo: IGuildRepo,
    ) -> None:
        self.bot = bot
        self._config = config
        self._loans = loan_svc
        self._economy_svc = economy_svc
        self._guild_repo = guild_repo

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(LoanButton)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        bridges = getattr(self.bot, "bridges", None)
        if isinstance(bridges, BotBridges):
            bridges.unregister(ILoanButtonBridge, self)

    group = app_commands.Group(name="貸付", description="🤝 ペリカの貸し借り（日割りの単利つき）", guild_only=True)

    async def handle_button(self, interaction: discord.Interaction, action: str, loan_id: int) -> None:
        await handle_button(self._loans, self._guild_repo, self._config, interaction, action, loan_id)

    # ------------------------------ 提案 ------------------------------ #
    async def _principal_autocomplete(self, interaction: discord.Interaction, current: str):
        """貸す額の欄に、残高を見せる。"""
        return balance_choices(self._economy_svc, interaction, current, loans.USER_MIN_PRINCIPAL)

    async def _save_message_id(self, guild_id: int, loan_id: int, interaction: discord.Interaction, kind: str) -> None:
        try:
            self._loans.set_message(guild_id, loan_id, (await interaction.original_response()).id)
        except discord.HTTPException as exc:  # 投稿は出ている。ボタンは番号で動くが、あとから書き換えられなくなる
            logthrottle.warn(
                ("loan-msg", interaction.guild_id),
                f"貸付の{kind}の投稿番号を保存できません: loan={loan_id}: {exc!r}",
            )

    @group.command(name="提案", description="相手にペリカを貸す提案を出す")
    @app_commands.rename(member="相手", principal="金額", rate="日利", days="期間")
    @app_commands.describe(
        member="貸す相手",
        principal=f"貸す額（{loans.USER_MIN_PRINCIPAL:,}以上。上限なし。貸す人の残高が足りること）",
        rate="1日の利息（％。0以上、0.1刻み。上限なし）",
        days=f"返済の期限（日。{loans.MIN_TERM_DAYS}〜{loans.MAX_TERM_DAYS}）",
    )
    @app_commands.autocomplete(principal=_principal_autocomplete)
    async def offer(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        principal: app_commands.Range[int, loans.USER_MIN_PRINCIPAL],
        rate: app_commands.Range[float, 0.0],
        days: app_commands.Range[int, loans.MIN_TERM_DAYS, loans.MAX_TERM_DAYS],
    ):
        if member.bot:
            return await ui.reply_error(interaction, "", title="Bot には貸せません")
        guild_id = ui.guild_id_of(interaction)
        assert interaction.channel_id is not None
        ok, result = self._loans.offer(
            guild_id, interaction.user.id, member.id, principal, round(rate * 10), days, interaction.channel_id
        )
        if not ok:
            return await ui.reply_error(interaction, format_failure(result), title="提案できません")
        assert not isinstance(result, Failure)
        loan = self._loans.get(guild_id, result)
        assert loan is not None
        credit = self._loans.credit(guild_id, member.id)
        await interaction.response.send_message(
            content=f"<@{member.id}>",
            embed=offer_embed(loan, borrower_note(credit)),
            view=offer_view(result),
            allowed_mentions=discord.AllowedMentions(users=[member]),
        )
        await self._save_message_id(guild_id, result, interaction, "提案")

    @group.command(name="募集", description="お金を借りたい人を募集する（最初に貸すと言った人が、貸し手になる）")
    @app_commands.rename(principal="金額", rate="日利", days="期間")
    @app_commands.describe(
        principal=f"借りたい額（{loans.USER_MIN_PRINCIPAL:,}以上）",
        rate="払う1日の利息（％。0以上、0.1刻み。高いほど貸す人が現れやすい）",
        days=f"返済の期限（日。{loans.MIN_TERM_DAYS}〜{loans.MAX_TERM_DAYS}）",
    )
    async def request(
        self,
        interaction: discord.Interaction,
        principal: app_commands.Range[int, loans.USER_MIN_PRINCIPAL],
        rate: app_commands.Range[float, 0.0],
        days: app_commands.Range[int, loans.MIN_TERM_DAYS, loans.MAX_TERM_DAYS],
    ):
        guild_id = ui.guild_id_of(interaction)
        assert interaction.channel_id is not None
        ok, result = self._loans.request(
            guild_id, interaction.user.id, principal, round(rate * 10), days, interaction.channel_id
        )
        if not ok:
            return await ui.reply_error(interaction, format_failure(result), title="募集できません")
        assert not isinstance(result, Failure)
        loan = self._loans.get(guild_id, result)
        assert loan is not None
        credit = self._loans.credit(guild_id, interaction.user.id)
        await interaction.response.send_message(
            embed=request_embed(loan, borrower_note(credit)),
            view=request_view(result),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self._save_message_id(guild_id, result, interaction, "募集")

    # ------------------------------ 返済・一覧 ------------------------------ #
    @group.command(name="返済", description="借りたペリカを返す（金額を省くと全額）")
    @app_commands.rename(loan_id="貸付", amount="金額")
    @app_commands.describe(loan_id="返す貸付（自分が借りているものだけ候補に出ます）", amount="返す額（省略で全額）")
    async def repay(
        self,
        interaction: discord.Interaction,
        loan_id: int,
        amount: app_commands.Range[int, 1, 10000000] | None = None,
    ):
        guild_id = ui.guild_id_of(interaction)
        ok, result = self._loans.repay(guild_id, loan_id, interaction.user.id, amount)
        if not ok:
            return await ui.reply_error(interaction, format_failure(result), title="返せません")
        assert not isinstance(result, Failure)
        loan = self._loans.get(guild_id, loan_id)
        assert loan is not None
        left = self._loans.remaining(guild_id, loan)
        note = (
            "返済が終わりました。"
            if loan["status"] == "repaid"
            else f"残り {ui.pelica(left)}（期限は <t:{int(loan['due'] or 0)}:R>）。"
        )
        await interaction.response.send_message(
            embed=ui.ok_embed(
                f"#{loan_id} を {ui.pelica(result)} 返しました",
                f"借りた人: <@{loan['borrower_id']}>　貸した人: {who(loan['lender_id'])}\n{note}",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @repay.autocomplete("loan_id")
    async def repay_autocomplete(self, interaction: discord.Interaction, current: str):
        if interaction.guild_id is None:
            return []
        guild_id = interaction.guild_id
        now = self._loans.now_ts()
        out = []
        for loan in self._loans.list_for(guild_id, interaction.user.id)["borrowed"]:
            if loan["status"] in ("offer", "request"):
                continue
            left = self._loans.remaining(guild_id, loan, now)
            label = f"#{loan['id']} 残り{left:,}（{'延滞中' if loan['status'] == 'overdue' else '期限 ' + format_minutes(max(1, int(((loan['due'] or 0) - now) // 60))) + '後'}）"
            if not current or current in label:
                out.append(app_commands.Choice(name=label[:100], value=loan["id"]))
        return out[:25]

    @group.command(name="一覧", description="借りている・貸している貸付を見る")
    async def listing(self, interaction: discord.Interaction):
        guild_id = ui.guild_id_of(interaction)
        now = self._loans.now_ts()
        data = self._loans.list_for(guild_id, interaction.user.id)

        def line(loan: LoanRow, mine_is_borrower: bool) -> str:
            other = (
                who(loan["lender_id"] if mine_is_borrower else loan["borrower_id"])
                if loan["lender_id"]
                else "（募集中）"
            )
            if loan["status"] == "request":
                return f"`#{loan['id']}` {ui.pelica(loan['principal'])}　日利{loans.rate_text(loan['rate_tenths'])}・{loan['term_days']}日　**貸し手を募集中**"
            if loan["status"] == "offer":
                return f"`#{loan['id']}` {other}　{ui.pelica(loan['principal'])}　日利{loans.rate_text(loan['rate_tenths'])}・{loan['term_days']}日　**承諾待ち**"
            left = self._loans.remaining(guild_id, loan, now)
            accepted_at = loan["accepted_at"] if loan["accepted_at"] is not None else 0.0
            late = loans.late_interest(loan["principal"], loan["rate_tenths"], loan["term_days"], accepted_at, now)
            due = loan["due"] if loan["due"] is not None else 0.0
            state = f"⚠️延滞（うち遅延損害金 {late:,}）" if loan["status"] == "overdue" else f"期限 <t:{int(due)}:R>"
            return f"`#{loan['id']}` {other}　元本{loan['principal']:,}　日利{loans.rate_text(loan['rate_tenths'])}　残り **{left:,}**　{state}"

        e = ui.embed(f"🤝 {interaction.user.display_name} の貸し借り", "", ui.INFO)
        e.add_field(name="借りている", value="\n".join(line(r, True) for r in data["borrowed"]) or "なし", inline=False)
        e.add_field(name="貸している", value="\n".join(line(r, False) for r in data["lent"]) or "なし", inline=False)
        await interaction.response.send_message(embed=e, allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------ 期限・通知 ------------------------------ #
    @tasks.loop(minutes=1)
    async def tick(self):
        try:
            await self.run_once()
        except Exception as exc:
            _log.warning(f"貸付の確認に失敗: {exc!r}")

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def run_once(self) -> None:
        async def _handle(guild: discord.Guild) -> None:
            outcome = self._loans.process_tick(guild.id)
            for loan in outcome.expired:
                await self._edit_offer(
                    guild,
                    loan,
                    state_embed(
                        loan,
                        "募集の期限が切れました" if loan["lender_id"] == 0 else "承諾の期限が切れました",
                        "",
                        ui.INFO,
                    ),
                )
            for loan in outcome.due_soon:
                await self._say(
                    guild,
                    loan,
                    f"⏰ 貸付 #{loan['id']} の返済の期限は <t:{int(loan['due'] or 0)}:R> です。<@{loan['borrower_id']}> さん、`/貸付 返済` で返せます（期限には、残高から自動で回収します）。",
                )
            for loan in outcome.collected:
                if loan["status"] == "repaid":
                    await self._say(
                        guild,
                        loan,
                        f"✅ 貸付 #{loan['id']} の期限が来て、{ui.pelica(loan['collected'])}を回収し、返済が終わりました。",
                    )
                else:
                    await self._say(
                        guild,
                        loan,
                        f"⚠️ 貸付 #{loan['id']} は延滞です。{ui.pelica(loan['collected'])}を回収しましたが、残り {ui.pelica(loan['left'])} があります。<@{loan['borrower_id']}> さんに入るお金から、自動で回収していきます。",
                    )

        await run_for_allowed_guilds(
            self.bot,
            self._guild_repo,
            self._config,
            _handle,
            error_label="貸付を処理できません",
        )

    async def _edit_offer(self, guild: discord.Guild, loan: Mapping[str, Any], embed: discord.Embed) -> None:
        channel = guild.get_channel(loan["channel_id"]) if loan["channel_id"] else None
        if channel is None or not loan["message_id"]:
            return
        msg = ui.partial_message(channel, loan["message_id"])
        if msg is None:
            return
        try:
            await msg.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass

    async def _say(self, guild: discord.Guild, loan: Mapping[str, Any], text: str) -> None:
        channel = (guild.get_channel(loan["channel_id"]) if loan["channel_id"] else None) or announce_channel(
            guild, self._guild_repo
        )
        if channel is None:
            return
        users = [discord.Object(id=u) for u in {loan["borrower_id"], loan["lender_id"]} if u]
        try:
            await ui.send_channel(
                channel,
                content=text,
                allowed_mentions=discord.AllowedMentions(users=users, roles=False, everyone=False),
            )
        except discord.HTTPException as exc:
            logthrottle.warn(("loan", guild.id), f"貸付の知らせを投稿できません: guild={guild.id}: {exc!r}")


async def setup(bot: "CasinoBot") -> None:
    cog = LoanCog(bot, bot.config, bot.services.loan, bot.services.economy, bot.repos.guild)
    bot.bridges.register(ILoanButtonBridge, cog)
    await bot.add_cog(cog)
