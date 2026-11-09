"""/地下 作業 — ペリカが尽きた人の救済。ボタンを押すと稼げるクリッカー。

「働く」を押すとペリカが貯まる。疲れると稼ぎが減り、事故が起きることもある。稼いだペリカで、工具を強化していく。
数字と計算は core/labor.py、処理は core/eco_labor.py。画面は、開いた本人だけが押せる（再起動すると、画面は開き直す）。
"""

import datetime
import time

import discord
from discord import app_commands
from discord.ext import commands

from core import labor as lab
from core import ui

VIEW_TIMEOUT = 900  # 画面が反応する時間（秒）。過ぎたら、`/地下 作業` で開き直す


def today(bot) -> str:
    tz = datetime.timezone(datetime.timedelta(hours=float(bot.config.get("utc_offset_hours", 9))))
    return datetime.datetime.now(tz).date().isoformat()


def panel_embed(state: dict, note: str = "") -> discord.Embed:
    fat = state["fatigue"]
    rate = lab.pay_rate(fat)
    lines = [
        note,
        "",
        f"手元 **{ui.pelica(state['balance'])}**　未受け取りの稼ぎ **{ui.pelica(state['pot'])}**",
        f"疲労 {lab.bar(fat)} **{fat:.0f}%**（稼ぎ {rate:.0%}）",
        f"1回の稼ぎ **{ui.pelica(lab.click_pay(state['pick'], fat))}**　"
        f"事故 **{lab.accident_chance(state['helmet']):.1%}**",
        f"今日の残り枠 **{ui.pelica(state['left'])}** / {state['cap']:,}",
        "",
        "　".join(f"{lab.TOOL_MARK[t]} {lab.TOOL_NAME[t]} Lv{state[t]}" for t in lab.TOOLS),
    ]
    if state["balance"] > lab.BALANCE_LIMIT:
        lines += ["", f"手元が {lab.BALANCE_LIMIT:,} ペリカを超えているので、働けません。"]
    e = ui.embed("⛏ 地下作業", "\n".join(x for x in lines if x is not None).strip(), ui.DARK)
    e.set_footer(text="疲労は時間で回復します。工具を強化すると、稼ぎ・疲れにくさ・事故が良くなります。")
    return e


class LaborView(discord.ui.View):
    def __init__(self, bot, user_id: int, guild_id: int):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.bot, self.user_id, self.guild_id = bot, user_id, guild_id
        self.state: dict = {}
        self.refresh()

    # -------------------------------------------------------------- #
    def eco(self):
        return self.bot.economy.for_guild(self.guild_id)

    def refresh(self) -> None:
        """いまの状態を読み直して、ボタンを組み直す。"""
        self.state = self.eco().labor_state(self.user_id, time.time(), today(self.bot))
        s = self.state
        self.clear_items()
        can_work = s["balance"] <= lab.BALANCE_LIMIT and s["left"] > 0
        work = discord.ui.Button(
            label="働く", emoji="⛏", style=discord.ButtonStyle.primary, row=0, disabled=not can_work
        )
        work.callback = self._work
        rest = discord.ui.Button(label="休憩", emoji="☕", style=discord.ButtonStyle.secondary, row=0)
        rest.callback = self._rest
        collect = discord.ui.Button(
            label=f"受け取る（{s['pot']:,}）",
            emoji="💰",
            style=discord.ButtonStyle.success,
            row=0,
            disabled=s["pot"] <= 0,
        )
        collect.callback = self._collect
        for b in (work, rest, collect):
            self.add_item(b)
        for tool in lab.TOOLS:
            cost = lab.upgrade_cost(s[tool])
            b = discord.ui.Button(
                label=f"{lab.TOOL_NAME[tool]} 強化" + (f"（{cost:,}）" if cost else "（最大）"),
                emoji=lab.TOOL_MARK[tool],
                style=discord.ButtonStyle.secondary,
                row=1,
                disabled=cost is None or s["balance"] < cost,
            )
            b.callback = self._upgrader(tool)
            self.add_item(b)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "これは他の人の作業場です。`/地下 作業` で自分の作業場を開けます。", ephemeral=True
            )
            return False
        return True

    async def _show(self, interaction: discord.Interaction, note: str = "") -> None:
        self.refresh()
        await interaction.response.edit_message(embed=panel_embed(self.state, note), view=self)

    # -------------------------------------------------------------- #
    async def _work(self, interaction: discord.Interaction):
        r = self.eco().labor_click(self.user_id, time.time(), today(self.bot))
        if not r["ok"]:
            return await self._show(interaction, "" if r.get("quiet") else f"⚠ {r['reason']}")
        if r["accident"]:
            note = "💥 **事故！** この回の稼ぎは消えた……"
        else:
            note = f"⛏ **+{r['pay']:,}**"
        if r["collected"]:
            note += f"　💰 {r['collected']:,} が、手元に入りました"
        await self._show(interaction, note)

    async def _rest(self, interaction: discord.Interaction):
        ok, reason = self.eco().labor_rest(self.user_id, time.time(), today(self.bot))
        await self._show(
            interaction,
            f"☕ 一息ついた。疲労が {lab.REST_RECOVER:.0f} 抜けた（{lab.REST_SECONDS:.0f} 秒は押せません）。"
            if ok
            else f"⚠ {reason}",
        )

    async def _collect(self, interaction: discord.Interaction):
        amount = self.eco().labor_collect(self.user_id, time.time(), today(self.bot))
        await self._show(interaction, f"💰 {amount:,} ペリカを、手元に移しました。" if amount else "")

    def _upgrader(self, tool: str):
        async def callback(interaction: discord.Interaction):
            ok, reason = self.eco().labor_upgrade(self.user_id, tool, time.time(), today(self.bot))
            await self._show(
                interaction,
                f"{lab.TOOL_MARK[tool]} **{lab.TOOL_NAME[tool]}** を強化した！（{lab.TOOL_EFFECT[tool]}）"
                if ok
                else f"⚠ {reason}",
            )

        return callback


class LaborCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    group = app_commands.Group(name="地下", description="⛏ ペリカが尽きたときの、地下の仕事", guild_only=True)

    @group.command(name="作業", description="ボタンを押して稼ぐ（元手は0。疲労と事故に気をつけて、工具を強化する）")
    async def work(self, interaction: discord.Interaction):
        view = LaborView(self.bot, interaction.user.id, interaction.guild_id)
        await interaction.response.send_message(embed=panel_embed(view.state), view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(LaborCog(bot))
