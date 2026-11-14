"""`/ペリカ 帝愛` のパネル: 帝愛銀行の金庫メーター・破産猶予の一覧・預金・種銭・CDS（cogs/economy_cog.py から使う）。

数字と考え方は core/teiai.py、処理は core/eco_bank.py。ここは画面の部品だけ（読み込まれる Cog ではない）。
"""

import time
from typing import TYPE_CHECKING, cast

import discord

from cogs.shared import ui
from cogs.teiai.escape import run_escape
from domain import teiai
from infra import catalogs

if TYPE_CHECKING:
    from cogs.shop.cog import ShopCog

_TEIAI_CFG = catalogs.teiai()
LEVEL_MARK = dict(_TEIAI_CFG.panel_level_mark)
LEVEL_TEXT = dict(_TEIAI_CFG.panel_level_text)


def meter(ratio, width: int = 10) -> str:
    """準備率のメーター（預金総額に対する、利用可能現金の割合。30% で満タン）。"""
    if ratio is None:
        return "─" * width
    filled = max(0, min(width, round(ratio / 0.30 * width)))
    return "█" * filled + "░" * (width - filled)


def bank_lines(bank: dict) -> str:
    mark = LEVEL_MARK[bank["level"]]
    if bank["ratio"] is None:
        ratio_text = "預金はまだありません"
    else:
        ratio_text = f"準備率 **{bank['ratio']:.0%}**　`{meter(bank['ratio'])}`"
    lines = [
        f"{mark} **{LEVEL_TEXT[bank['level']]}**　{ratio_text}",
        f"預金総額 {ui.pelica(bank['deposits'])}　手元の現金 {ui.pelica(int(bank['cash']))}　地下融資 {ui.pelica(int(bank['loaned']))}",
        f"引当金のロック {ui.pelica(int(bank['locks']))}　**引き出せる現金 {ui.pelica(max(0, int(bank['available'])))}**",
    ]
    return "\n".join(lines)


def render_teiai_panel_embed(
    *,
    bank: dict,
    house: dict,
    graced: list,
    claims: int,
    status: str,
    display_status: str,
    deposit: int,
    debt: int,
    grace_until: float | None,
    cds: dict,
    uid: int,
) -> discord.Embed:
    """帝愛銀行と自分の状況の画面を組み立てる（純粋な Base）。"""
    color = {"safe": ui.OK, "warn": ui.GOLD, "danger": ui.ERROR, "frozen": ui.ERROR}[bank["level"]]
    e = ui.embed("🏦 帝愛銀行", bank_lines(bank), color)
    e.add_field(
        name="🕴️ 帝愛の胴元の口座",
        value=f"**{ui.pelica(house['balance'])}**　（ピンハネの合計 {ui.pelica(house['skimmed'])}。デイリーと地下作業から入る）",
        inline=False,
    )
    if graced:
        e.add_field(
            name="⚠️ 破産猶予のカウントダウン中",
            value="\n".join(
                f"<@{u}>　借金 {ui.pelica(u_debt)}　<t:{int(until)}:R> に強制破産" for u, u_debt, until in graced[:10]
            ),
            inline=False,
        )
    if claims:
        e.add_field(name="💀 掛けられている CDS（配当見込みの合計）", value=ui.pelica(claims), inline=False)
    shown_status = display_status if status == teiai.KATAGI else status
    me = [f"身分: {teiai.STATUS_LABEL[shown_status]}　預金: **{ui.pelica(deposit)}**"]
    if debt:
        me.append(
            f"帝愛への借金: **{ui.pelica(debt)}**（利息: 1日 {teiai.DEBT_RATE_PER_DAY:.0%}・{teiai.DEBT_INTEREST_CEILING:,}ペリカまで）"
        )
    if grace_until is not None:
        me.append(
            f"⚠️ **破産猶予中**。<t:{int(grace_until)}:R> までに借金を {ui.pelica(teiai.GRACE_DEBT)} 未満へ戻せないと、強制破産です。"
        )
    if cds["bought"]:

        def who(c: dict) -> str:
            return "自分" if c["target_id"] == uid else f"<@{c['target_id']}>"

        me.append(
            "持っている CDS: "
            + "、".join(f"{who(c)}（{ui.pelica(c['stake'])}・{c['odds']}倍）" for c in cds["bought"][:5])
        )
    if cds["on_me"]:
        me.append(f"あなたに掛けられている CDS: {cds['on_me']}件")
    e.add_field(name="あなた", value="\n".join(me), inline=False)
    e.set_footer(
        text=f"預金はカタギだけ。利回りは、ピンハネ・CDS の掛け金・カジノの負け分から。CDS は、対象が時間切れで強制破産したときだけ配当（{teiai.CDS_TERM_DAYS}日で失効）"
    )
    return e


def panel_embed(eco, uid: int) -> discord.Embed:
    """帝愛銀行と、自分の状況の画面（Wings: eco から値を集めて Base へ渡す）。"""
    now = time.time()
    status = eco.status(uid, now)
    return render_teiai_panel_embed(
        bank=eco.bank_status(now),
        house=eco.house_account(),
        graced=eco.grace_list(),
        claims=eco.cds_total_claims(),
        status=status,
        display_status=eco.display_status(uid) if status == teiai.KATAGI else status,
        deposit=eco.deposit_of(uid),
        debt=eco.debt(uid),
        grace_until=eco.grace_until(uid),
        cds=eco.cds_list(uid),
        uid=uid,
    )


class AmountModal(discord.ui.Modal):
    """預ける・引き出す額の入力。"""

    def __init__(self, panel, mode: str):
        super().__init__(title="帝愛に預ける" if mode == "deposit" else "帝愛から引き出す")
        self.panel, self.mode = panel, mode
        self.amount = discord.ui.TextInput(
            label="金額（ペリカ）" if mode == "deposit" else "金額（空欄なら全額）",
            required=mode == "deposit",
            max_length=12,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.amount.value.replace(",", "").strip()
        if raw and not raw.isdigit():
            await interaction.response.send_message(
                embed=ui.error_embed("金額が正しくありません", "半角の数字で入れてください。"), ephemeral=True
            )
            return
        eco, uid = self.panel.eco(interaction), interaction.user.id
        if self.mode == "deposit":
            ok, result = eco.bank_deposit(uid, int(raw))
            text = f"帝愛に預けました。いまの預金: **{ui.pelica(result)}**" if ok else result
        else:
            ok, result = eco.bank_withdraw(uid, int(raw) if raw else None)
            text = f"**{ui.pelica(result)}** を引き出しました。" if ok else result
        embed = ui.ok_embed("完了", text) if ok else ui.error_embed("できませんでした", text)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class StakeModal(discord.ui.Modal):
    """CDS の掛け金の入力。"""

    def __init__(self, panel, target_id: int, odds: float):
        super().__init__(title=f"CDS を買う（{odds}倍）")
        self.panel, self.target_id = panel, target_id
        self.stake = discord.ui.TextInput(label=f"掛け金（{teiai.CDS_MIN_STAKE:,}ペリカ以上）", max_length=12)
        self.add_item(self.stake)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.stake.value.replace(",", "").strip()
        if not raw.isdigit():
            await interaction.response.send_message(
                embed=ui.error_embed("金額が正しくありません", "半角の数字で入れてください。"), ephemeral=True
            )
            return
        eco, uid = self.panel.eco(interaction), interaction.user.id
        ok, result = eco.cds_buy(uid, self.target_id, int(raw))
        if not ok:
            await interaction.response.send_message(embed=ui.error_embed("買えませんでした", result), ephemeral=True)
            return
        mine = self.target_id == uid
        text = (
            f"**{result}倍**で買いました（掛け金 {ui.pelica(int(raw))}・配当の見込み {ui.pelica(int(int(raw) * result))}）。\n"
            + (
                "自分の資産を差し押さえても足りない分を、帝愛が肩代わりします（1件、使い切り。先に自分の資産は失います）。"
                if mine
                else f"<@{self.target_id}> が**破産猶予の時間切れで強制破産**したとき（猶予に入ってから自分で破産したときも）に配当が出ます。猶予に入る前に自分で破産したときは、出ません。"
            )
        )
        if mine:  # 自分への保険は、戦略が見えるので、本人だけ
            await interaction.response.send_message(embed=ui.ok_embed("CDS を買いました", text), ephemeral=True)
            return
        await interaction.response.send_message(  # 他人への CDS は公開（「あいつの破産に賭けた」）
            embed=ui.embed(
                f"💀 {interaction.user.display_name} が、<@{self.target_id}> の破産に賭けた",
                f"掛け金 **{ui.pelica(int(raw))}**・**{result}倍**（的中すれば約 {ui.pelica(int(int(raw) * result))}）。\n"
                f"<@{self.target_id}> が**破産猶予の時間切れで強制破産**したときだけ配当が出ます。",
                ui.ERROR,
            )
        )


class TargetSelect(discord.ui.UserSelect):
    def __init__(self, panel):
        super().__init__(placeholder="CDS の対象を選ぶ（自分も選べる）", min_values=1, max_values=1)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        target = self.values[0]
        if target.bot:
            return await interaction.response.send_message(
                embed=ui.error_embed("選べません", "Bot は選べません。"), ephemeral=True
            )
        quote = self.panel.eco(interaction).cds_quote(target.id)
        await interaction.response.send_modal(StakeModal(self.panel, target.id, quote["odds"]))


class TargetView(discord.ui.View):
    def __init__(self, panel):
        super().__init__(timeout=120)
        self.add_item(TargetSelect(panel))


class TeiaiPanel(ui.OwnerView):
    """パネルの下のボタン（押せるのはコマンドを使った本人だけ）。"""

    def __init__(self, bot, uid: int):
        super().__init__(
            uid,
            timeout=300,
            owner_message="この画面を使えるのは、コマンドを使った本人だけです。",
        )
        self.bot, self.uid = bot, uid

    def eco(self, interaction: discord.Interaction):
        return self.bot.economy.for_guild(ui.guild_id_of(interaction))

    @discord.ui.button(label="預ける", style=discord.ButtonStyle.success)
    async def deposit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AmountModal(self, "deposit"))

    @discord.ui.button(label="引き出す", style=discord.ButtonStyle.primary)
    async def withdraw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AmountModal(self, "withdraw"))

    @discord.ui.button(label="CDS を掛ける", style=discord.ButtonStyle.danger)
    async def cds(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=ui.embed(
                "💀 CDS（破綻保証）",
                "対象を選んでください。**自分**を選ぶと、資産を差し押さえても足りない借金を、帝愛が肩代わりする保険（配当の見込みの合計は 5万ペリカまで）。"
                "**他人**を選ぶと、その人が破産猶予の時間切れで強制破産したときに配当が出ます（オッズは、相手の危険度で決まります）。",
                ui.INFO,
            ),
            view=TargetView(self),
            ephemeral=True,
        )

    @discord.ui.button(label="種銭を借りる", style=discord.ButtonStyle.secondary)
    async def seed(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, result = self.eco(interaction).borrow_seed(interaction.user.id)
        self.eco(interaction).take_notices(interaction.user.id)
        if ok:  # 成功は公開（「💸 ○○ が帝愛から種銭を借りた」）。失敗は本人だけ
            return await interaction.response.send_message(
                embed=ui.embed(
                    f"💸 {interaction.user.display_name} が帝愛から種銭を借りました",
                    f"**{ui.pelica(result)}** を借りました（借金に加わります）。「これで取り返してこい…！」",
                    ui.GOLD,
                )
            )
        embed = ui.error_embed("借りられません", result)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🚢 海外逃亡チャンス！！", style=discord.ButtonStyle.danger, row=1)
    async def escape(self, interaction: discord.Interaction, button: discord.ui.Button):
        eco = self.eco(interaction)
        if eco.debt(interaction.user.id) < teiai.ESCAPE_MIN_DEBT:
            return await interaction.response.send_message(
                embed=ui.error_embed(
                    "逃亡できません", f"試せるのは、帝愛の借金が {teiai.ESCAPE_MIN_DEBT:,}ペリカ 以上のときだけです。"
                ),
                ephemeral=True,
            )

        async def after_success(inter: discord.Interaction):
            if inter.guild is None:
                return
            shop_cog = cast(
                "ShopCog | None", ui.bot_of(inter).get_cog("ShopCog")
            )  # 破産と同じく、色ロールとニックネームをすぐに反映する
            member = inter.guild.get_member(inter.user.id) or inter.user
            if shop_cog is not None and isinstance(member, discord.Member):
                await shop_cog.after_bankruptcy(inter.guild, member)

        await run_escape(interaction, eco, after_success)

    @discord.ui.button(label="更新", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=panel_embed(self.eco(interaction), interaction.user.id), view=self
        )
