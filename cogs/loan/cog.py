"""/貸付 — ユーザー同士の貸し借り（日割りの単利）。

- 貸す人が `/貸付 提案` で、相手・金額・日利・期間を決める。投稿の「承諾」を借りる本人が押すと成立し、お金が移る。
- 借りたい人が `/貸付 募集` で、金額・日利・期間を決めて募集する。最初に「貸す」を押した人が貸し手になり、その場でお金が移る。
- 返済は `/貸付 返済`。期限には、借りた人の残高から自動で回収する。足りない分は延滞になり、入ってくるお金から古い順に回収する。
- 期限の前日と、延滞になったときに、貸付を出したチャンネルに知らせる。
- 担保（CSトークン・称号・債券）は、次の段階で足す。
"""

import re
import time
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.shared import ui
from cogs.shared.amount import balance_choices
from cogs.shared.announce import announce_channel
from cogs.shared.confirm import confirm_action
from domain import approval, loans, teiai
from domain.duration import format_minutes
from infra import logthrottle


def who(user_id: int) -> str:
    return f"<@{user_id}>"


def terms_lines(loan: dict) -> list:
    p, r, t = loan["principal"], loan["rate_tenths"], loan["term_days"]
    lender = who(loan["lender_id"]) if loan["lender_id"] else "（募集中）"
    return [
        f"貸す人: {lender}　借りる人: {who(loan['borrower_id'])}",
        f"金額: **{ui.pelica(p)}**　日利: **{loans.rate_text(r)}**　期間: **{t}日**",
        f"期間いっぱい借りた場合の返済: **{ui.pelica(loans.full_term_total(p, r, t))}**（利息 {ui.pelica(loans.interest(p, r, t))}）",
        "早く返せば、利息は日割りで減ります（経過日数は切り上げで、最低1日）。",
    ]


def borrower_note(eco, user_id: int) -> str:
    """貸す人が、貸す相手のリスクを見られるように、借りる人の信用情報を出す（資産の額は見せない）。"""
    c = eco.loan_credit(user_id)
    lines = [
        f"**借りる人の信用情報**\n身分: {teiai.STATUS_LABEL[c['status']]}"
        + (f"　帝愛への借金: {ui.pelica(c['debt'])}" if c["debt"] else "")
    ]
    if c["grace_until"] is not None:
        lines.append(f"⚠️ 破産猶予中（<t:{int(c['grace_until'])}:R> に強制破産）")
    lines.append(
        f"返済の実績: 完済 **{c['repaid']}件**　延滞中 **{c['overdue']}件**　踏み倒し（破産で免責） **{c['discharged']}件**"
    )
    lines.append(f"いま借りている貸付: **{c['active']}件**（元本の残り {ui.pelica(c['owed'])}）")
    if c["bankrupt_at"]:
        lines.append(f"破産の履歴: 最後に <t:{int(c['bankrupt_at'])}:D>")
    return "\n".join(lines)


def request_embed(loan: dict, note: str = "") -> discord.Embed:
    lines = (
        terms_lines(loan)
        + ([note] if note else [])
        + [
            f"募集の期限: <t:{int(loan['offer_expires'])}:R>",
            "最初に「貸す」を押した人が、貸し手になります（その場で、貸す人の残高から、借りる人へお金が移ります）。",
        ]
    )
    return ui.embed(f"🙋 貸付の募集 #{loan['id']}", "\n".join(lines), ui.GOLD)


def offer_embed(loan: dict, note: str = "") -> discord.Embed:
    lines = (
        terms_lines(loan)
        + ([note] if note else [])
        + [
            f"承諾の期限: <t:{int(loan['offer_expires'])}:R>",
            "借りる人が「承諾」を押すと、お金が移ります。",
        ]
    )
    return ui.embed(f"🤝 貸付の提案 #{loan['id']}", "\n".join(lines), ui.GOLD)


def state_embed(loan: dict, title: str, note: str, color: int = ui.INFO) -> discord.Embed:
    return ui.embed(f"🤝 貸付 #{loan['id']} {title}", "\n".join(terms_lines(loan) + ([note] if note else [])), color)


class LoanButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"loan:(?P<act>accept|decline|cancel|lend):(?P<id>\d+)"
):
    """提案の投稿のボタン。再起動しても動くように、貸付の番号を custom_id に入れている。"""

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
        await handle_button(interaction.client, interaction, self.act, self.loan_id)


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


async def handle_lend(bot, interaction: discord.Interaction, loan_id: int) -> None:
    """募集の「貸す」。お金が動くので、確認ボタンを挟んでから貸す。"""
    eco = bot.economy.for_guild(interaction.guild_id)
    loan = eco.loan_get(loan_id)
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
    ok = await confirm_action(
        interaction,
        "この募集に貸しますか？",
        "\n".join(terms_lines(loan) + [borrower_note(eco, loan["borrower_id"])])
        + "\n\n貸すと、その場で残高から引かれます。借りる人が破産すると、**全額は戻らない**ことがあります。",
        "貸す",
    )
    if ok is not True:
        return
    ok, reason = eco.loan_lend(loan_id, interaction.user.id, time.time())
    if not ok:
        return await interaction.followup.send(embed=ui.error_embed("貸せませんでした", reason), ephemeral=True)
    loan = eco.loan_get(loan_id)
    embed = state_embed(
        loan,
        "成立",
        f"<t:{int(loan['due'])}:R>が返済の期限です。`/貸付 返済` で返せます。期限には自動で回収します。",
        ui.OK,
    )
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


async def handle_button(bot, interaction: discord.Interaction, act: str, loan_id: int) -> None:
    guild_id = interaction.guild_id
    if guild_id is None or not approval.is_allowed(bot.economy, bot.config, guild_id, interaction.user.id):
        await interaction.response.send_message(embed=ui.error_embed("利用できません"), ephemeral=True)
        return
    eco = bot.economy.for_guild(guild_id)
    now = time.time()
    if act == "lend":
        return await handle_lend(bot, interaction, loan_id)
    if act == "accept":
        ok, reason = eco.loan_accept(loan_id, interaction.user.id, now)
    elif act == "decline":
        ok, reason = eco.loan_decline(loan_id, interaction.user.id)
    else:
        ok, reason = eco.loan_cancel(loan_id, interaction.user.id)
    if not ok:
        await interaction.response.send_message(embed=ui.error_embed("できません", reason), ephemeral=True)
        return
    loan = eco.loan_get(loan_id)
    if act == "accept":
        embed = state_embed(
            loan,
            "成立",
            f"<t:{int(loan['due'])}:R>が返済の期限です。`/貸付 返済` で返せます。期限には自動で回収します。",
            ui.OK,
        )
    else:
        embed = state_embed(loan, "辞退されました" if act == "decline" else "取り下げられました", "", ui.INFO)
    await interaction.response.edit_message(embed=embed, view=None)


class LoanCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_dynamic_items(LoanButton)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    group = app_commands.Group(name="貸付", description="🤝 ペリカの貸し借り（日割りの利息つき）", guild_only=True)

    def g(self, interaction: discord.Interaction):
        return self.bot.economy.for_guild(interaction.guild_id)

    # ------------------------------ 提案 ------------------------------ #
    async def _principal_autocomplete(self, interaction: discord.Interaction, current: str):
        """貸す額の欄に、残高を見せる。"""
        return balance_choices(self.bot, interaction, current, loans.USER_MIN_PRINCIPAL)

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
            return await interaction.response.send_message(embed=ui.error_embed("Bot には貸せません"), ephemeral=True)
        eco = self.g(interaction)
        ok, result = eco.loan_offer(
            interaction.user.id, member.id, principal, round(rate * 10), days, interaction.channel_id, time.time()
        )
        if not ok:
            return await interaction.response.send_message(
                embed=ui.error_embed("提案できません", result), ephemeral=True
            )
        loan = eco.loan_get(result)
        await interaction.response.send_message(
            content=f"<@{member.id}>",
            embed=offer_embed(loan, borrower_note(eco, member.id)),
            view=offer_view(result),
            allowed_mentions=discord.AllowedMentions(users=[member]),
        )
        try:
            eco.loan_set_message(result, (await interaction.original_response()).id)
        except discord.HTTPException as exc:  # 投稿は出ている。ボタンは番号で動くが、あとから書き換えられなくなる
            logthrottle.warn(
                ("loan-msg", interaction.guild_id), f"貸付の提案の投稿番号を保存できません: loan={result}: {exc!r}"
            )

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
        eco = self.g(interaction)
        ok, result = eco.loan_request(
            interaction.user.id, principal, round(rate * 10), days, interaction.channel_id, time.time()
        )
        if not ok:
            return await interaction.response.send_message(
                embed=ui.error_embed("募集できません", result), ephemeral=True
            )
        loan = eco.loan_get(result)
        await interaction.response.send_message(
            embed=request_embed(loan, borrower_note(eco, interaction.user.id)),
            view=request_view(result),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            eco.loan_set_message(result, (await interaction.original_response()).id)
        except discord.HTTPException as exc:
            logthrottle.warn(
                ("loan-msg", interaction.guild_id), f"貸付の募集の投稿番号を保存できません: loan={result}: {exc!r}"
            )

    # ------------------------------ 返済・一覧 ------------------------------ #
    @group.command(name="返済", description="借りたペリカを返す（金額を省くと全額）")
    @app_commands.rename(loan_id="貸付", amount="金額")
    @app_commands.describe(loan_id="返す貸付（自分が借りているものだけ候補に出ます）", amount="返す額（省略で全額）")
    async def repay(
        self,
        interaction: discord.Interaction,
        loan_id: int,
        amount: Optional[app_commands.Range[int, 1, 10_000_000]] = None,
    ):
        ok, result = self.g(interaction).loan_repay(loan_id, interaction.user.id, amount, time.time())
        if not ok:
            return await interaction.response.send_message(embed=ui.error_embed("返せません", result), ephemeral=True)
        loan = self.g(interaction).loan_get(loan_id)
        left = self.g(interaction).loan_remaining(loan, time.time())
        note = (
            "返済が終わりました。"
            if loan["status"] == "repaid"
            else f"残り {ui.pelica(left)}（期限は <t:{int(loan['due'])}:R>）。"
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
        eco, now = self.g(interaction), time.time()
        out = []
        for loan in eco.loan_list(interaction.user.id)["borrowed"]:
            if loan["status"] in ("offer", "request"):
                continue
            left = eco.loan_remaining(loan, now)
            label = f"#{loan['id']} 残り{left:,}（{'延滞中' if loan['status'] == 'overdue' else '期限 ' + format_minutes(max(1, int((loan['due'] - now) // 60))) + '後'}）"
            if not current or current in label:
                out.append(app_commands.Choice(name=label[:100], value=loan["id"]))
        return out[:25]

    @group.command(name="一覧", description="借りている・貸している貸付を見る")
    async def listing(self, interaction: discord.Interaction):
        eco, now = self.g(interaction), time.time()
        data = eco.loan_list(interaction.user.id)

        def line(loan: dict, mine_is_borrower: bool) -> str:
            other = (
                who(loan["lender_id"] if mine_is_borrower else loan["borrower_id"])
                if loan["lender_id"]
                else "（募集中）"
            )
            if loan["status"] == "request":
                return f"`#{loan['id']}` {ui.pelica(loan['principal'])}　日利{loans.rate_text(loan['rate_tenths'])}・{loan['term_days']}日　**貸し手を募集中**"
            if loan["status"] == "offer":
                return f"`#{loan['id']}` {other}　{ui.pelica(loan['principal'])}　日利{loans.rate_text(loan['rate_tenths'])}・{loan['term_days']}日　**承諾待ち**"
            left = eco.loan_remaining(loan, now)
            late = loans.late_interest(
                loan["principal"], loan["rate_tenths"], loan["term_days"], loan["accepted_at"], now
            )
            state = (
                f"⚠️延滞（うち遅延損害金 {late:,}）" if loan["status"] == "overdue" else f"期限 <t:{int(loan['due'])}:R>"
            )
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
            print(f"貸付の確認に失敗: {exc!r}")

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def run_once(self) -> None:
        now = time.time()
        for guild in list(self.bot.guilds):
            if not approval.is_allowed(self.bot.economy, self.bot.config, guild.id, 0):
                continue
            try:
                eco = self.bot.economy.for_guild(guild.id)
                for loan in eco.loan_expire_offers(now):
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
                for loan in eco.loan_due_soon(now):
                    await self._say(
                        guild,
                        eco,
                        loan,
                        f"⏰ 貸付 #{loan['id']} の返済の期限は <t:{int(loan['due'])}:R> です。<@{loan['borrower_id']}> さん、`/貸付 返済` で返せます（期限には、残高から自動で回収します）。",
                    )
                for loan in eco.loan_collect_due(now):
                    if loan["status"] == "repaid":
                        await self._say(
                            guild,
                            eco,
                            loan,
                            f"✅ 貸付 #{loan['id']} の期限が来て、{ui.pelica(loan['collected'])}を回収し、返済が終わりました。",
                        )
                    else:
                        await self._say(
                            guild,
                            eco,
                            loan,
                            f"⚠️ 貸付 #{loan['id']} は延滞です。{ui.pelica(loan['collected'])}を回収しましたが、残り {ui.pelica(loan['left'])} があります。<@{loan['borrower_id']}> さんに入るお金から、自動で回収していきます。",
                        )
            except Exception as exc:  # 1サーバーの失敗で、ほかを止めない
                print(f"貸付を処理できません: guild={guild.id}: {exc!r}")

    async def _edit_offer(self, guild: discord.Guild, loan: dict, embed: discord.Embed) -> None:
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

    async def _say(self, guild: discord.Guild, eco, loan: dict, text: str) -> None:
        channel = (guild.get_channel(loan["channel_id"]) if loan["channel_id"] else None) or announce_channel(
            guild, eco
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


async def setup(bot):
    await bot.add_cog(LoanCog(bot))
