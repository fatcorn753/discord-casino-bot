"""/債券 — 元本を預けて、満期に利息つきで受け取る。通常債（元本保証）とジャンク債（高利回り・倒産あり）。

種類ごとの利率・期間・上限は core/bonds.py。満期は 1分ごとに確認して自動で入金し、
決着の結果は `/債券 保有` の「最近の決着」で見られる（通知は出さない）。
ミッション・連勝・週間ランキング・突発イベントには数えない。
"""

import random
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import approval, bonds, ui

RNG = random.SystemRandom()
KIND_CHOICES = [app_commands.Choice(name=f"{k.emoji} {k.name}（{k.hours // 24}日・利息+{k.rate}%" + (f"・倒産{k.default_pct}%）" if k.default_pct else "・元本保証）"), value=k.key)
                for k in bonds.KINDS.values()]


def due_text(due: float) -> str:
    return f"<t:{int(due)}:R>"


def describe_done(row: dict) -> str:
    kind = bonds.KINDS[row["kind"]]
    p = row["principal"]
    if row["status"] == "paid":
        return f"{kind.emoji} {kind.name} {ui.pelica(p)} → 満期 **{ui.pelica(row['paid'])}**（+{row['paid'] - p:,}）"
    if row["status"] == "defaulted":
        return f"{kind.emoji} {kind.name} {ui.pelica(p)} → 💥 **倒産**（元本は戻りません）"
    return f"{kind.emoji} {kind.name} {ui.pelica(p)} → 解約 {ui.pelica(row['paid'])}（違約金 {row['principal'] - row['paid']:,}）"


class BondCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.tick.start()

    def cog_unload(self):
        self.tick.cancel()

    def g(self, interaction: discord.Interaction):
        return self.bot.economy.for_guild(interaction.guild_id)

    group = app_commands.Group(name="債券", description="🏦 ペリカを預けて満期に受け取る（ゲーム内の通貨・現実のお金とは無関係）")

    @group.command(name="買う", description=f"債券を買う（{bonds.MIN_AMOUNT:,}ペリカ以上。通常債は元本保証、ジャンク債は倒産あり）")
    @app_commands.rename(kind="種類", amount="金額")
    @app_commands.describe(kind="通常債かジャンク債", amount=f"預ける元本（{bonds.MIN_AMOUNT:,}ペリカ以上）")
    @app_commands.choices(kind=KIND_CHOICES)
    async def buy(self, interaction: discord.Interaction, kind: app_commands.Choice[str], amount: app_commands.Range[int, bonds.MIN_AMOUNT, 1_000_000]):
        eco = self.g(interaction)
        eco.bond_settle(time.time(), RNG)            # 満期の分を先に入金して、買える額を正しくする
        ok, reason = eco.bond_buy(interaction.user.id, kind.value, amount, time.time())
        if not ok:
            return await interaction.response.send_message(embed=ui.error_embed("買えません", reason), ephemeral=True)
        k = bonds.KINDS[kind.value]
        lines = [f"元本: **{ui.pelica(amount)}**　満期: {k.hours // 24}日後（{due_text(time.time() + k.hours * 3600)}）",
                 f"満期に受け取る額: **{ui.pelica(bonds.maturity_payout(k, amount))}**（利息 +{k.rate}%）"]
        if k.default_pct:
            lines.append(f"⚠️ **{k.default_pct}%の確率で倒産**して、元本が戻りません。途中で解約もできません。")
        else:
            lines.append(f"元本保証です。途中解約は元本の{k.cancel_penalty}%が引かれます（`/債券 解約`）。")
        await interaction.response.send_message(embed=ui.ok_embed(f"{k.emoji} {k.name}を買いました", "\n".join(lines)))

    @group.command(name="保有", description="持っている債券と、最近の決着を見る（自分だけに見える）")
    async def holdings(self, interaction: discord.Interaction):
        eco = self.g(interaction)
        eco.bond_settle(time.time(), RNG)
        data = eco.bond_list(interaction.user.id)
        lines = []
        for r in data["active"]:
            k = bonds.KINDS[r["kind"]]
            lines.append(f"`#{r['id']}` {k.emoji} {k.name} 元本 {ui.pelica(r['principal'])} → 満期 {due_text(r['due'])}（受取 {ui.pelica(bonds.maturity_payout(k, r['principal']))}）")
        e = ui.embed("🏦 保有中の債券", "\n".join(lines) or "債券は持っていません。`/債券 買う` で買えます。", ui.INFO)
        if data["done"]:
            e.add_field(name="最近の決着", value="\n".join(describe_done(r) for r in data["done"]), inline=False)
        e.set_footer(text="ランキングの総資産には、預けている元本も含まれます")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @group.command(name="解約", description="通常債を途中で解約する（元本から違約金が引かれる。ジャンク債は解約できない）")
    @app_commands.rename(bond_id="番号")
    @app_commands.describe(bond_id="`/債券 保有` に出ている # の番号")
    async def cancel(self, interaction: discord.Interaction, bond_id: int):
        ok, result = self.g(interaction).bond_cancel(interaction.user.id, bond_id)
        if not ok:
            return await interaction.response.send_message(embed=ui.error_embed("解約できません", result), ephemeral=True)
        await interaction.response.send_message(embed=ui.ok_embed("解約しました", f"{ui.pelica(result)}を残高に戻しました。"), ephemeral=True)

    # ------------------------------ 満期の入金 ------------------------------ #
    @tasks.loop(minutes=1)
    async def tick(self):
        try:
            self.run_once()
        except Exception as exc:
            print(f"債券の満期確認に失敗: {exc!r}")

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    def run_once(self) -> None:
        now = time.time()
        for guild in list(self.bot.guilds):
            if not approval.is_allowed(self.bot.economy, self.bot.config, guild.id, 0):
                continue
            try:
                self.bot.economy.for_guild(guild.id).bond_settle(now, RNG)
            except Exception as exc:       # 1サーバーの失敗で、ほかを止めない
                print(f"債券を決着できません: guild={guild.id}: {exc!r}")


async def setup(bot):
    await bot.add_cog(BondCog(bot))
