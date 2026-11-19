"""帝愛共済（#34）— `/共済`。帝愛銀行の最後の損を負う代わりに、余剰から配当を受け取る持分。

- 口は、口座を作った人に 1 口ずつ配られる。破産・休眠で帝愛が回収し、値下がりしながら売りに出す（`/共済 買う`）
- 口は、値段をつけてほかの人へ譲れる（`譲渡` → 相手が `受諾`）。準備率が低いとき・破産猶予中の人がいるときは止まる
- 動議（`提案`）で、帝愛信託の上乗せ金利を変えられる。口数で数えた賛成が反対より多ければ可決
- 配当と動議の締めは、1分ごとの見回りで行う
"""

import logging
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.shared import logthrottle, ui
from cogs.shared.announce import announce_channel
from cogs.shared.messages import format_failure
from cogs.shared.tasks import run_for_allowed_guilds
from domain import kyosai
from domain.ports.repos import IGuildRepo
from services.kyosai import KyosaiService

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot


def status_embed(s: dict[str, Any], offers: list[dict[str, Any]], user_id: int) -> discord.Embed:
    share = s["units"] / s["total"] if s["total"] else 0.0
    lines = [
        f"あなたの口: **{s['units']}口**（全 {s['total']}口 の {share:.1%}）　持っている人 {s['holders']}人",
        f"次の配当の見込み: 全体で {ui.pelica(s['pool'])}"
        + (f"（<t:{int(s['next_dividend'])}:R>）" if s["next_dividend"] else "")
        + f"　これまでの配当 {ui.pelica(int(s['paid']))}　これまでの負担 {ui.pelica(int(s['charged']))}",
        f"帝愛の持ち口: {s['house']}口（1口 {ui.pelica(s['house_price'])}。`/共済 買う`）",
        "口の売買: " + ("🔒 **停止中**（準備率が低いか、破産猶予中の人がいます）" if s["locked"] else "🟢 できます"),
        f"帝愛信託の上乗せ金利: **1日 {s['premium']:.2%}**",
    ]
    if s["motion"]:
        m = s["motion"]
        yes, no = s["tally"]
        lines.append(
            f"🗳️ 動議 #{m['id']}: 上乗せ金利を **1日 {m['premium']:.2%}** にする　賛成 {yes}口 / 反対 {no}口"
            f"（締切 <t:{int(m['closes'])}:R>。`/共済 投票`）"
        )
    for o in offers:
        if o["seller"] == user_id:
            lines.append(f"📤 申し出 #{o['id']}: <@{o['buyer']}> へ {o['units']}口 を {ui.pelica(o['price'])} で")
        else:
            lines.append(
                f"📥 申し出 #{o['id']}: <@{o['seller']}> から {o['units']}口 を {ui.pelica(o['price'])} で（`/共済 受諾`）"
            )
    embed = ui.embed("🏛️ 帝愛共済", "\n".join(lines))
    embed.set_footer(
        text="共済は帝愛銀行の最後の砦です。平時は余剰から配当を受け、信託でも払えない損失が出たときは口数に応じて負担します（足りなければ借金）。"
    )
    return embed


class KyosaiCog(commands.Cog):
    def __init__(self, bot: commands.Bot, config: dict[str, Any], kyosai_svc: KyosaiService, guild_repo: IGuildRepo):
        self.bot = bot
        self._config = config
        self._kyosai = kyosai_svc
        self._guild_repo = guild_repo

    async def cog_load(self) -> None:
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    group = app_commands.Group(
        name="共済", description="🏛️ 帝愛共済（配当と、最後の損の負担・口の売買・動議）", guild_only=True
    )

    async def _fail(self, interaction: discord.Interaction, reason: Any) -> None:
        await interaction.response.send_message(
            embed=ui.error_embed("できません", format_failure(reason)), ephemeral=True
        )

    @group.command(name="状況", description="自分の口・配当・帝愛の持ち口・動議・申し出を見る")
    async def status(self, interaction: discord.Interaction):
        gid = ui.guild_id_of(interaction)
        uid = interaction.user.id
        s = self._kyosai.status(gid, uid)
        await interaction.response.send_message(
            embed=status_embed(s, self._kyosai.offers(gid, uid), uid), ephemeral=True
        )

    @group.command(name="買う", description="帝愛が持っている口を買う（値段は日ごとに下がる）")
    @app_commands.rename(units="口数")
    @app_commands.describe(units="買う口数（1以上）")
    async def buy(self, interaction: discord.Interaction, units: app_commands.Range[int, 1, 1000]):
        ok, result = self._kyosai.buy_house(ui.guild_id_of(interaction), interaction.user.id, units)
        if not ok:
            return await self._fail(interaction, result)
        await interaction.response.send_message(
            embed=ui.ok_embed("🏛️ 共済の口を買いました", f"{units}口 を {ui.pelica(result)} で買いました。"),
            ephemeral=True,
        )

    @group.command(name="譲渡", description="自分の口を、値段をつけて相手へ譲る申し出をする（相手が受けると成立）")
    @app_commands.rename(member="相手", units="口数", price="値段")
    @app_commands.describe(member="譲る相手", units="譲る口数（1以上）", price="全部でいくら（ペリカ。0なら無償）")
    async def offer(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        units: app_commands.Range[int, 1, 1000],
        price: app_commands.Range[int, 0, 1_000_000_000],
    ):
        if member.bot:
            return await interaction.response.send_message(
                embed=ui.error_embed("できません", "Bot には譲れません。"), ephemeral=True
            )
        ok, result = self._kyosai.offer(ui.guild_id_of(interaction), interaction.user.id, member.id, units, price)
        if not ok:
            return await self._fail(interaction, result)
        await interaction.response.send_message(
            content=member.mention,
            embed=ui.embed(
                "🏛️ 共済の口の譲渡の申し出",
                f"{interaction.user.mention} が {member.mention} に、共済の口 **{units}口** を **{ui.pelica(price)}** で譲ると申し出ました。\n"
                f"受けるなら `/共済 受諾 番号:{result}`（{kyosai.OFFER_HOURS}時間以内）。",
            ),
            allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False),
        )

    @group.command(name="受諾", description="自分あての口の譲渡の申し出を受ける（代金を払って口を受け取る）")
    @app_commands.rename(offer_id="番号")
    @app_commands.describe(offer_id="申し出の番号（`/共済 状況` に出ます）")
    async def accept(self, interaction: discord.Interaction, offer_id: int):
        ok, result = self._kyosai.accept(ui.guild_id_of(interaction), interaction.user.id, offer_id)
        if not ok:
            return await self._fail(interaction, result)
        await interaction.response.send_message(
            embed=ui.ok_embed(
                "🏛️ 譲渡が成立しました",
                f"<@{result['seller']}> から {result['units']}口 を {ui.pelica(result['price'])} で受け取りました。",
            )
        )

    @group.command(name="取消", description="自分が出した（または自分あての）申し出を取り消す")
    @app_commands.rename(offer_id="番号")
    @app_commands.describe(offer_id="申し出の番号（`/共済 状況` に出ます）")
    async def cancel(self, interaction: discord.Interaction, offer_id: int):
        if not self._kyosai.cancel(ui.guild_id_of(interaction), interaction.user.id, offer_id):
            return await interaction.response.send_message(
                embed=ui.error_embed("できません", "その申し出は見つかりません。"), ephemeral=True
            )
        await interaction.response.send_message(
            embed=ui.ok_embed("取り消しました", f"申し出 #{offer_id}"), ephemeral=True
        )

    @group.command(name="提案", description="帝愛信託の上乗せ金利を変える動議を出す（口を持つ人の投票で決まる）")
    @app_commands.rename(premium="上乗せ金利")
    @app_commands.describe(premium=f"1日あたりの上乗せ金利（％。0〜{kyosai.PREMIUM_MAX * 100:g}）")
    async def propose(
        self, interaction: discord.Interaction, premium: app_commands.Range[float, 0.0, kyosai.PREMIUM_MAX * 100]
    ):
        ok, result = self._kyosai.propose(ui.guild_id_of(interaction), interaction.user.id, premium / 100)
        if not ok:
            return await self._fail(interaction, result)
        await interaction.response.send_message(
            embed=ui.embed(
                "🗳️ 帝愛共済の動議",
                f"{interaction.user.mention} が、帝愛信託の上乗せ金利を **1日 {premium:.2f}%** にする動議 #{result} を出しました。\n"
                f"口を持つ人は `/共済 投票` で賛成か反対を選べます（{kyosai.MOTION_HOURS}時間後に、口数で数えます）。",
            )
        )

    @group.command(name="投票", description="投票中の動議に、賛成か反対を入れる（あとから変えられる。口数で数える）")
    @app_commands.rename(choice="賛否")
    @app_commands.describe(choice="賛成か反対")
    @app_commands.choices(
        choice=[app_commands.Choice(name="賛成", value="yes"), app_commands.Choice(name="反対", value="no")]
    )
    async def vote(self, interaction: discord.Interaction, choice: app_commands.Choice[str]):
        ok, result = self._kyosai.vote(ui.guild_id_of(interaction), interaction.user.id, choice.value == "yes")
        if not ok:
            return await self._fail(interaction, result)
        await interaction.response.send_message(
            embed=ui.ok_embed("投票しました", f"動議 #{result} に **{choice.name}**（締切のときの口数で数えます）"),
            ephemeral=True,
        )

    # ------------------------------ 見回り（毎分） ------------------------------ #
    @tasks.loop(minutes=1)
    async def tick(self):
        await run_for_allowed_guilds(
            self.bot, self._guild_repo, self._config, self._tick_guild, error_label="帝愛共済の見回りに失敗"
        )

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _tick_guild(self, guild: discord.Guild) -> None:
        dividend, motion = self._kyosai.tick(guild.id)
        if dividend:
            _log.info(f"共済の配当: guild={guild.id} {dividend}")
        if motion is None:
            return
        channel = announce_channel(guild, self._guild_repo)
        if channel is None:
            return
        verdict = "✅ 可決" if motion["status"] == "passed" else "❌ 否決"
        text = (
            f"動議 #{motion['id']}（帝愛信託の上乗せ金利を 1日 {motion['premium']:.2%} にする）は **{verdict}** されました。"
            f"\n賛成 {motion['yes']}口 / 反対 {motion['no']}口"
        )
        try:
            await ui.send_channel(channel, embed=ui.embed("🗳️ 帝愛共済の動議の結果", text))
        except discord.HTTPException as exc:
            logthrottle.warn(("kyosai", guild.id), f"共済の動議の結果を投稿できません: guild={guild.id}: {exc!r}")


async def setup(bot: "CasinoBot") -> None:
    await bot.add_cog(KyosaiCog(bot, bot.config, bot.services.kyosai, bot.repos.guild))
