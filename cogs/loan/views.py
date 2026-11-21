"""/貸付 のボタン View とボタン押下ハンドラ。"""

import re
from typing import TYPE_CHECKING, Any, cast

import discord

from cogs.loan.embeds import borrower_note, state_embed, terms_lines, who
from cogs.shared import approval_guard, ui
from cogs.shared.bridges import ILoanButtonBridge, NoOpLoanButtonBridge
from cogs.shared.confirm import confirm_action
from cogs.shared.messages import format_failure
from domain.ports.repos import IGuildRepo
from services.loan import LoanService

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot


class LoanButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"loan:(?P<act>accept|decline|cancel|lend):(?P<id>\d+)"
):
    """提案の投稿のボタン。再起動しても動くよう、貸付の番号を custom_id に入れている。"""

    LABEL = {
        "accept": ("承諾", discord.ButtonStyle.success),
        "decline": ("辞退", discord.ButtonStyle.secondary),
        "cancel": ("取り下げ", discord.ButtonStyle.danger),
        "lend": ("貸す", discord.ButtonStyle.success),
    }

    def __init__(self, act: str, loan_id: int):
        label, style = self.LABEL[act]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"loan:{act}:{loan_id}"))
        self.act, self.loan_id = act, loan_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match):
        return cls(match["act"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        bot = cast("CasinoBot", interaction.client)
        bridge = bot.bridges.resolve_or(ILoanButtonBridge, NoOpLoanButtonBridge())
        await bridge.handle_button(interaction, self.act, self.loan_id)


def offer_view(loan_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for act in ("accept", "decline", "cancel"):
        view.add_item(LoanButton(act, loan_id))
    return view


def request_view(loan_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for act in ("lend", "cancel"):
        view.add_item(LoanButton(act, loan_id))
    return view


def _accepted_embed(loan: dict[str, Any]) -> discord.Embed:
    return state_embed(
        loan,
        "成立",
        f"<t:{int(loan['due'])}:R>が返済の期限です。`/貸付 返済` で返せます。期限には自動で回収します。",
        ui.OK,
    )


async def handle_lend(loan_svc: LoanService, interaction: discord.Interaction, loan_id: int) -> None:
    """募集の「貸す」。お金が動くので、確認ボタンを挟んでから貸す。"""
    guild_id = ui.guild_id_of(interaction)
    loan = loan_svc.get(guild_id, loan_id)
    if loan is None or loan["status"] != "request":
        await interaction.response.send_message(
            embed=ui.error_embed("できません", "この募集はもう終わっています。"), ephemeral=True
        )
        return
    if interaction.user.id == loan["borrower_id"]:
        await interaction.response.send_message(
            embed=ui.error_embed("できません", "自分の募集には、貸せません。"), ephemeral=True
        )
        return
    credit = loan_svc.credit(guild_id, loan["borrower_id"])
    ok = await confirm_action(
        interaction,
        "この募集に貸しますか？",
        "\n".join(terms_lines(loan) + [borrower_note(credit)])
        + "\n\n貸すと、その場で残高から引かれます。借りる人が破産すると、**全額は戻らない**ことがあります。",
        "貸す",
    )
    if ok is not True:
        return
    ok, reason = loan_svc.lend(guild_id, loan_id, interaction.user.id)
    if not ok:
        return await interaction.followup.send(
            embed=ui.error_embed("貸せませんでした", format_failure(reason)), ephemeral=True
        )
    loan = loan_svc.get(guild_id, loan_id)
    assert loan is not None
    embed = _accepted_embed(loan)
    if interaction.message is not None:
        try:
            await interaction.message.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass
    await interaction.followup.send(
        f"🤝 {who(loan['lender_id'])} が {who(loan['borrower_id'])} の募集に応じて、{ui.pelica(loan['principal'])}を貸しました。",
        allowed_mentions=discord.AllowedMentions(
            users=[discord.Object(id=loan["lender_id"]), discord.Object(id=loan["borrower_id"])]
        ),
    )


async def handle_button(
    loan_svc: LoanService,
    guild_repo: IGuildRepo,
    config: dict[str, Any],
    interaction: discord.Interaction,
    act: str,
    loan_id: int,
) -> None:
    if not await approval_guard.allow_component(interaction, guild_repo, config):
        return
    # 新しい貸し借りになる操作だけを止める（断る・取り消すは、止めていてもできる）
    if act in ("lend", "accept") and not await approval_guard.feature_guard(interaction, guild_repo, "loan"):
        return
    guild_id = ui.guild_id_of(interaction)
    if act == "lend":
        return await handle_lend(loan_svc, interaction, loan_id)
    if act == "accept":
        ok, reason = loan_svc.accept(guild_id, loan_id, interaction.user.id)
    elif act == "decline":
        ok, reason = loan_svc.decline(guild_id, loan_id, interaction.user.id)
    else:
        ok, reason = loan_svc.cancel(guild_id, loan_id, interaction.user.id)
    if not ok:
        await interaction.response.send_message(
            embed=ui.error_embed("できません", format_failure(reason)), ephemeral=True
        )
        return
    loan = loan_svc.get(guild_id, loan_id)
    assert loan is not None
    if act == "accept":
        embed = _accepted_embed(loan)
    else:
        embed = state_embed(loan, "辞退されました" if act == "decline" else "取り下げられました", "", ui.INFO)
    await interaction.response.edit_message(embed=embed, view=None)
