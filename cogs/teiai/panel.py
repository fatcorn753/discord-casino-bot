"""`/ペリカ 帝愛` のパネル: 帝愛銀行の金庫メーター・破産猶予の一覧・帝愛信託・種銭・CDS（cogs/economy/cog.py から使う）。

数字と考え方は domain/teiai.py、処理は services/teiai.py（保存は infra/db/bank.py）。ここは画面の部品だけ（読み込まれる Cog ではない）。
"""

from typing import Optional

import discord

from cogs.shared import ui
from cogs.shared.amount import AmountModal, reject_bad_amount, typed_amount
from cogs.shared.bridges import IShopTitleBridge, NoOpShopTitleBridge
from cogs.shared.messages import format_failure
from cogs.teiai.escape import run_escape
from cogs.teiai.texts import LEVEL_MARK as LEVEL_MARK
from cogs.teiai.texts import LEVEL_TEXT as LEVEL_TEXT
from cogs.teiai.texts import THERMOSTAT_OBSERVING, THERMOSTAT_TITLE
from domain import teiai
from domain.errors import Failure
from domain.ports.contracts import BankStatus, BankThermostat, CdsHoldings, HouseAccount
from services.teiai import TeiaiService


def meter(ratio, width: int = 10) -> str:
    """準備率のメーター（信託総額に対する、利用可能現金の割合。30% で満タン）。"""
    if ratio is None:
        return "─" * width
    filled = max(0, min(width, round(ratio / 0.30 * width)))
    return "█" * filled + "░" * (width - filled)


def thermostat_lines(thermo: BankThermostat) -> str:
    """サーモスタットの状態（熱量・器・熱浴とのやりとりの累計）。"""
    lines = [
        f"熱量 {ui.pelica(int(thermo['load']))}　器 {ui.pelica(int(thermo['capacity']))}　（空き {ui.pelica(int(thermo['room']))}）",
        f"熱浴へ消えた額 {ui.pelica(int(thermo['burned']))}　熱浴の肩代わり {ui.pelica(int(thermo['bailed']))}",
    ]
    if not thermo["enforce"]:
        lines.append(THERMOSTAT_OBSERVING)
    return "\n".join(lines)


def bank_lines(bank: BankStatus) -> str:
    mark = LEVEL_MARK[bank["level"]]
    if bank["ratio"] is None:
        ratio_text = "帝愛信託はまだありません"
    else:
        ratio_text = f"準備率 **{bank['ratio']:.0%}**　`{meter(bank['ratio'])}`"
    lines = [
        f"{mark} **{LEVEL_TEXT[bank['level']]}**　{ratio_text}",
        f"信託総額 {ui.pelica(bank['deposits'])}　手元の現金 {ui.pelica(int(bank['cash']))}　地下融資 {ui.pelica(int(bank['loaned']))}",
        f"引当金のロック {ui.pelica(int(bank['locks']))}　**引き出せる現金 {ui.pelica(max(0, int(bank['available'])))}**",
        f"信託の利率 **1日 {bank['trust_rate']:.2%}**（無リスク金利 {bank['risk_free']:.2%} + 上乗せ。金利は準備率に連動し、いまの目標は {bank['rf_target']:.2%}）　これまでの利息 {ui.pelica(int(bank['trust_paid']))}",
    ]
    if bank["bonds"] > 0:
        lines.append(f"帝愛社債の元本残高 {ui.pelica(bank['bonds'])}（準備率は、信託と社債の合計に対する割合）")
    if bank["excess_unpaid"] >= 1:
        lines.append(f"信託でも払いきれなかった超過損失 {ui.pelica(int(bank['excess_unpaid']))}")
    return "\n".join(lines)


def render_teiai_panel_embed(
    *,
    bank: BankStatus,
    house: HouseAccount,
    graced: list,
    claims: int,
    status: str,
    display_status: str,
    deposit: int,
    debt: int,
    grace_until: float | None,
    cds: CdsHoldings,
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
    thermo = bank.get("thermostat")
    if thermo:
        e.add_field(name=THERMOSTAT_TITLE, value=thermostat_lines(thermo), inline=False)
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
    me = [f"身分: {teiai.STATUS_LABEL[shown_status]}　帝愛信託: **{ui.pelica(deposit)}**"]
    if debt:
        me.append(
            f"帝愛への借金: **{ui.pelica(debt)}**（利息: 1日 {teiai.DEBT_RATE_PER_DAY:.0%}・{teiai.DEBT_INTEREST_CEILING:,}ペリカまで）"
        )
    if grace_until is not None:
        me.append(
            f"⚠️ **破産猶予中**。<t:{int(grace_until)}:R> までに借金を {ui.pelica(teiai.GRACE_DEBT)} 未満へ戻さないと、強制破産です。"
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
        text="帝愛信託はカタギだけ。いつでも出し入れでき、決まった利息が毎時つく。ただし銀行が払いきれない損失を最初に負う（元本割れあり・借金にはならない）。"
        f"元本保証がほしいなら /証券 の債券。CDS は、対象が時間切れで強制破産したときだけ配当（{teiai.CDS_TERM_DAYS}日で失効）"
    )
    return e


def panel_embed(teiai_svc: TeiaiService, guild_id: int, uid: int) -> discord.Embed:
    """帝愛銀行と、自分の状況の画面（値は TeiaiService から集め、Base へ渡す）。"""
    snap = teiai_svc.panel(guild_id, uid)
    return render_teiai_panel_embed(
        bank=snap.bank,
        house=snap.house,
        graced=snap.graced,
        claims=snap.claims,
        status=snap.status,
        display_status=snap.display_status,
        deposit=snap.deposit,
        debt=snap.debt,
        grace_until=snap.grace_until,
        cds=snap.cds,
        uid=snap.uid,
    )


class StakeModal(discord.ui.Modal):
    """CDS の掛け金の入力。"""

    def __init__(self, panel: "TeiaiPanel", target_id: int, odds: float):
        super().__init__(title=f"CDS を買う（{odds}倍）")
        self.panel, self.target_id = panel, target_id
        self.stake = discord.ui.TextInput(label=f"掛け金（{teiai.CDS_MIN_STAKE:,}ペリカ以上）", max_length=12)
        self.add_item(self.stake)

    async def on_submit(self, interaction: discord.Interaction):
        stake = typed_amount(self.stake.value)
        if stake is None:
            await reject_bad_amount(interaction)
            return
        uid = interaction.user.id
        ok, result = self.panel.teiai.cds_buy(ui.guild_id_of(interaction), uid, self.target_id, stake)
        if not ok:
            await ui.reply_error(interaction, format_failure(result), title="買えませんでした")
            return
        assert not isinstance(result, Failure)
        mine = self.target_id == uid
        text = (
            f"**{result}倍**で買いました（掛け金 {ui.pelica(stake)}・配当の見込み {ui.pelica(int(stake * result))}）。\n"
            + (
                "自分の資産を差し押さえても足りない分を、帝愛が肩代わりする保険（1件、使い切り。先に自分の資産は失います）。"
                if mine
                else f"<@{self.target_id}> が**破産猶予の時間切れで強制破産**したとき（猶予に入ってから自分で破産したときも）に配当が出ます。猶予に入る前に自分で破産したときは、出ません。"
            )
        )
        if mine:  # 自分への保険は、戦略が見えるので、本人だけ
            await ui.reply_ok(interaction, text, title="CDS を買いました")
            return
        await interaction.response.send_message(  # 他人への CDS は公開（「あいつの破産に賭けた」）
            embed=ui.embed(
                f"💀 {interaction.user.display_name} が、<@{self.target_id}> の破産に賭けた",
                f"掛け金 **{ui.pelica(stake)}**・**{result}倍**（的中すれば約 {ui.pelica(int(stake * result))}）。\n"
                f"<@{self.target_id}> が**破産猶予の時間切れで強制破産**したときだけ配当が出ます。",
                ui.ERROR,
            )
        )


class TargetSelect(discord.ui.UserSelect):
    def __init__(self, panel: "TeiaiPanel"):
        super().__init__(placeholder="CDS の対象を選ぶ（自分も選べる）", min_values=1, max_values=1)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        target = self.values[0]
        if target.bot:
            return await ui.reply_error(interaction, "Bot は選べません。", title="選べません")
        quote = self.panel.teiai.cds_quote(ui.guild_id_of(interaction), target.id)
        await interaction.response.send_modal(StakeModal(self.panel, target.id, quote["odds"]))


class TargetView(discord.ui.View):
    def __init__(self, panel: "TeiaiPanel"):
        super().__init__(timeout=120)
        self.add_item(TargetSelect(panel))


class TeiaiPanel(ui.OwnerView):
    """パネルの下のボタン（押せるのはコマンドを使った本人だけ）。"""

    def __init__(
        self,
        teiai_svc: TeiaiService,
        uid: int,
        *,
        shop_bridge: Optional[IShopTitleBridge] = None,
    ):
        super().__init__(
            uid,
            timeout=300,
            owner_message="この画面を使えるのは、コマンドを使った本人だけです。",
        )
        self.teiai = teiai_svc
        self._shop_bridge: IShopTitleBridge = shop_bridge if shop_bridge is not None else NoOpShopTitleBridge()
        self.uid = uid

    @discord.ui.button(label="信託に入れる", style=discord.ButtonStyle.success)
    async def deposit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            AmountModal(title="帝愛信託に入れる", label="金額（ペリカ）", required=True, on_amount=self._deposit)
        )

    @discord.ui.button(label="信託から引き出す", style=discord.ButtonStyle.primary)
    async def withdraw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            AmountModal(
                title="帝愛信託から引き出す", label="金額（空欄なら全額）", required=False, on_amount=self._withdraw
            )
        )

    async def _deposit(self, interaction: discord.Interaction, amount: Optional[int]) -> None:
        ok, result = self.teiai.deposit(ui.guild_id_of(interaction), interaction.user.id, amount or 0)
        if ok:
            assert not isinstance(result, Failure)
            text = f"帝愛信託に入れました。いまの信託: **{ui.pelica(result)}**"
        else:
            text = format_failure(result)
        await self._reply_bank(interaction, ok, text)

    async def _withdraw(self, interaction: discord.Interaction, amount: Optional[int]) -> None:
        ok, result = self.teiai.withdraw(ui.guild_id_of(interaction), interaction.user.id, amount)  # None は全額
        if ok:
            assert not isinstance(result, Failure)
            text = f"**{ui.pelica(result)}** を引き出しました。"
        else:
            text = format_failure(result)
        await self._reply_bank(interaction, ok, text)

    async def _reply_bank(self, interaction: discord.Interaction, ok: bool, text: str) -> None:
        if ok:
            await ui.reply_ok(interaction, text, title="完了")
        else:
            await ui.reply_error(interaction, text, title="できませんでした")

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
        ok, result = self.teiai.borrow_seed(ui.guild_id_of(interaction), interaction.user.id)
        if ok:  # 成功は公開（「💸 ○○ が帝愛から種銭を借りた」）。失敗は本人だけ
            assert not isinstance(result, Failure)
            return await interaction.response.send_message(
                embed=ui.embed(
                    f"💸 {interaction.user.display_name} が帝愛から種銭を借りました",
                    f"**{ui.pelica(result)}** を借りました（借金に加わります）。「これで取り返してこい…！」",
                    ui.GOLD,
                )
            )
        await ui.reply_error(interaction, format_failure(result), title="借りられません")

    @discord.ui.button(label="🚢 海外逃亡チャンス！！", style=discord.ButtonStyle.danger, row=1)
    async def escape(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.teiai.debt_of(ui.guild_id_of(interaction), interaction.user.id) < teiai.ESCAPE_MIN_DEBT:
            return await ui.reply_error(
                interaction,
                f"試せるのは、帝愛の借金が {teiai.ESCAPE_MIN_DEBT:,}ペリカ 以上のときだけです。",
                title="逃亡できません",
            )

        async def after_success(inter: discord.Interaction):
            if inter.guild is None:
                return
            member = inter.guild.get_member(inter.user.id) or inter.user
            if isinstance(member, discord.Member):
                await self._shop_bridge.after_bankruptcy(
                    inter.guild, member
                )  # 破産と同じく、色ロールとニックネームをすぐに反映

        await run_escape(interaction, self.teiai, after_success)

    @discord.ui.button(label="更新", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=panel_embed(self.teiai, ui.guild_id_of(interaction), interaction.user.id),
            view=self,
        )
