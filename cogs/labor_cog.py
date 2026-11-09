"""/地下 作業 — ペリカが尽きた人の救済。ボタンを押すと稼げるクリッカーと、作業員を雇って放置でも稼ぐ雇用。

「働く」を押すとペリカが貯まる。疲れると稼ぎが減る。稼いだペリカは、まとめて受け取る。稼いだペリカで、工具を強化していく。
数字と計算は core/labor.py、処理は core/eco_labor.py。画面は、チャンネルに公開される（みんなに見えるが、押せるのは開いた本人だけ。再起動すると、画面は開き直す）。
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


def fatigue_label(fatigue: float) -> str:
    return "元気" if fatigue < 25 else "ふつう" if fatigue < 50 else "疲れ気味" if fatigue < 75 else "ぐったり"


def next_goal(state: dict) -> str:
    """初めての人にも、次に何をすればよいかが分かる、ひとこと。"""
    bal, first = state["balance"], lab.worker_cost("apprentice", state["apprentice"])
    if state["workers"] == 0:
        if bal >= first:
            return f"💡 **{first:,}ペリカ** あるので、「👷 作業員」から見習いを雇えます。雇うと、放置でも稼ぎます。"
        return f"💡 あと **{first - bal:,}ペリカ** で、最初の作業員（見習い）を雇えます。まずは「働く」を押して稼ごう。"
    cost = lab.upgrade_cost(state["pick"])
    if cost is not None and bal >= cost:
        return f"💡 **{cost:,}ペリカ** あるので、ツルハシを強化できます（1回の稼ぎが増えます）。"
    if state["workers"] < state["slots"] and bal >= lab.worker_cost("apprentice", state["apprentice"]):
        return "💡 まだ、作業員を雇う枠があります。「👷 作業員」を見てみよう。"
    return ""


def panel_embed(state: dict, note: str = "") -> discord.Embed:
    """作業の画面。上に、直前の結果。つぎに、お金と疲労。最後に、次の目標。"""
    fat = state["fatigue"]
    head = [x for x in (note, next_goal(state)) if x]
    e = ui.embed("⛏ 地下作業", "\n".join(head) if head else "「働く」を押すと、ペリカが貯まります。", ui.DARK)
    e.add_field(name="💴 手元", value=ui.pelica(state["balance"]), inline=True)
    e.add_field(name="🎒 未受け取り", value=f"**{ui.pelica(state['pot_total'])}**", inline=True)
    e.add_field(name="📅 今日の稼ぎ", value=ui.pelica(state["day_earned"]), inline=True)
    e.add_field(name="⛏ 1回の稼ぎ", value=f"**{ui.pelica(lab.click_pay(state['pick'], fat))}**", inline=True)
    e.add_field(
        name=f"😮\u200d💨 疲労（{fatigue_label(fat)}）",
        value=f"{lab.bar(fat)}\n{fat:.0f}%　稼ぎ {lab.pay_rate(fat):.0%}",
        inline=True,
    )
    e.add_field(
        name="👷 作業員",
        value=f"{state['workers']}/{state['slots']}人　{state['rate']:,.0f}/時" if state["workers"] else "いません",
        inline=True,
    )
    e.add_field(
        name="🧰 工具",
        value="　".join(f"{lab.TOOL_MARK[t]} {lab.TOOL_NAME[t]} Lv{state[t]}/{lab.MAX_LEVEL}" for t in lab.TOOLS),
        inline=False,
    )
    e.set_footer(text="疲労は時間で回復します。「受け取る」で、貯まった分をまとめて手元に移します。")
    return e


def hire_embed(state: dict, note: str = "") -> discord.Embed:
    """雇用の画面。作業員の一覧（人数・稼ぎ・次の値段）、寮、賃金。"""
    full = state["workers"] >= state["slots"]
    head = [x for x in (note, "⚠ 人数がいっぱいです。寮を強化するか、誰かを解雇しよう。" if full else "") if x]
    e = ui.embed("👷 作業員の雇用", "\n".join(head) if head else "雇った作業員は、放置でも稼いでくれます。", ui.DARK)
    e.add_field(name="💴 手元", value=ui.pelica(state["balance"]), inline=True)
    e.add_field(name="🎒 未受け取り", value=f"**{ui.pelica(state['pot_total'])}**", inline=True)
    e.add_field(name="📈 作業員の稼ぎ", value=f"**{state['rate']:,.0f}/時**", inline=True)
    e.add_field(
        name="🏠 人数",
        value=f"**{state['workers']} / {state['slots']}人**（寮 Lv{state['dorm']}/{lab.DORM_MAX}）",
        inline=True,
    )
    wage = state["wage"]
    e.add_field(
        name="💼 賃金",
        value=f"**{lab.WAGE_NAME[wage]}**"
        + (f"\n稼ぎ ×{lab.WAGE_MULT['low']}・1日に {lab.QUIT_PER_DAY:.0%} で1人ずつ辞める" if wage == "low" else ""),
        inline=True,
    )
    e.add_field(name="\u200b", value="\u200b", inline=True)
    for k in lab.WORKERS:
        nxt = lab.worker_cost(k, state[k])
        gain = f"{lab.WORKER_RATE[k]}/時" if k != "foreman" else f"全員の稼ぎ +{lab.FOREMAN_BONUS:.0%}"
        e.add_field(
            name=f"{lab.WORKER_MARK[k]} {lab.WORKER_NAME[k]} ×{state[k]}",
            value=f"{gain}\n次の1人 **{nxt:,}**",
            inline=True,
        )
    e.set_footer(
        text=f"貯まるのは最大{lab.STOCK_HOURS:.0f}時間分。解雇してもお金は戻りません。同じ種類は、雇うほど値段が上がります。"
    )
    return e


MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def ranking_embed(data: dict, daily: bool) -> discord.Embed:
    """採掘ランキング。稼いだ額（ペリカ）の多い順。"""
    title = "⛏ 採掘ランキング（今日）" if daily else "⛏ 採掘ランキング（通算）"
    if not data["rows"]:
        return ui.embed(title, "まだ、だれも稼いでいません。`/地下 作業` で、1番乗りを狙おう。", ui.DARK)
    lines = [
        f"{MEDALS.get(rank, f'`{rank}`')} <@{uid}>　**{ui.pelica(earned)}**　（{clicks:,}回）"
        for rank, uid, earned, clicks in data["rows"]
    ]
    if data["me"] and data["me"][0] > len(data["rows"]):
        rank, earned, clicks, total = data["me"]
        lines += ["…", f"`{rank}` あなた　**{ui.pelica(earned)}**　（{clicks:,}回）"]
    e = ui.embed(title, "\n".join(lines), ui.DARK)
    e.set_footer(text="稼いだ額 = 働いて貯めたペリカ（作業員の稼ぎも含む。通算は、今日までの合計）")
    return e


class LaborView(discord.ui.View):
    def __init__(self, bot, user_id: int, guild_id: int):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.bot, self.user_id, self.guild_id = bot, user_id, guild_id
        self.state: dict = {}
        self.tab = "work"
        self.refresh()

    # -------------------------------------------------------------- #
    def eco(self):
        return self.bot.economy.for_guild(self.guild_id)

    def refresh(self) -> None:
        """いまの状態を読み直して、ボタンを組み直す。"""
        self.state = self.eco().labor_state(self.user_id, time.time(), today(self.bot))
        s = self.state
        self.clear_items()
        collect = discord.ui.Button(
            label=f"受け取る {s['pot_total']:,}",
            emoji="💰",
            style=discord.ButtonStyle.success,
            row=0,
            disabled=s["pot_total"] <= 0,
        )
        collect.callback = self._collect
        if self.tab == "hire":
            back = discord.ui.Button(label="作業にもどる", emoji="⛏", style=discord.ButtonStyle.secondary, row=0)
            back.callback = self._goto("work")
            self.add_item(back)
            self.add_item(collect)
            wage = discord.ui.Button(
                label="賃金を" + lab.WAGE_NAME["normal" if s["wage"] == "low" else "low"] + "にする",
                emoji="💼",
                style=discord.ButtonStyle.secondary,
                row=0,
            )
            wage.callback = self._wage
            self.add_item(wage)
            full = s["workers"] >= s["slots"]
            for k in lab.WORKERS:
                cost = lab.worker_cost(k, s[k])
                b = discord.ui.Button(
                    label=f"{lab.WORKER_NAME[k]}を雇う {cost:,}",
                    emoji=lab.WORKER_MARK[k],
                    style=discord.ButtonStyle.primary,
                    row=1,
                    disabled=full or s["balance"] < cost,
                )
                b.callback = self._hirer(k)
                self.add_item(b)
            dcost = lab.dorm_cost(s["dorm"])
            dorm = discord.ui.Button(
                label="寮を強化 " + (f"{dcost:,}" if dcost else "（最大）"),
                emoji="🏠",
                style=discord.ButtonStyle.secondary,
                row=2,
                disabled=dcost is None or s["balance"] < dcost,
            )
            dorm.callback = self._dorm
            self.add_item(dorm)
            owned = [k for k in lab.WORKERS if s[k] > 0]
            if owned:
                pick = discord.ui.Select(
                    placeholder="解雇する（お金は戻りません）",
                    row=3,
                    options=[
                        discord.SelectOption(
                            label=f"{lab.WORKER_NAME[k]}を解雇（いま {s[k]}人）", value=k, emoji=lab.WORKER_MARK[k]
                        )
                        for k in owned
                    ],
                )
                pick.callback = self._dismiss
                self._dismiss_select = pick
                self.add_item(pick)
            return
        work = discord.ui.Button(label="働く", emoji="⛏", style=discord.ButtonStyle.primary, row=0)
        work.callback = self._work
        hire = discord.ui.Button(label="作業員", emoji="👷", style=discord.ButtonStyle.secondary, row=0)
        hire.callback = self._goto("hire")
        for b in (work, collect, hire):
            self.add_item(b)
        for tool in lab.TOOLS:
            cost = lab.upgrade_cost(s[tool])
            b = discord.ui.Button(
                label=f"{lab.TOOL_NAME[tool]}を強化 " + (f"{cost:,}" if cost else "（最大）"),
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
        embed = hire_embed(self.state, note) if self.tab == "hire" else panel_embed(self.state, note)
        await interaction.response.edit_message(embed=embed, view=self)

    # -------------------------------------------------------------- #
    async def _work(self, interaction: discord.Interaction):
        r = self.eco().labor_click(self.user_id, time.time(), today(self.bot))
        if not r["ok"]:
            return await self._show(interaction, "" if r.get("quiet") else f"⚠ {r['reason']}")
        await self._show(interaction, f"⛏ **+{r['pay']:,}**")

    async def _collect(self, interaction: discord.Interaction):
        amount = self.eco().labor_collect(self.user_id, time.time(), today(self.bot))
        await self._show(interaction, f"💰 **{amount:,}** ペリカを、まとめて手元に移しました。" if amount else "")

    def _goto(self, tab: str):
        async def callback(interaction: discord.Interaction):
            self.tab = tab
            await self._show(interaction)

        return callback

    def _hirer(self, kind: str):
        async def callback(interaction: discord.Interaction):
            ok, reason = self.eco().labor_hire(self.user_id, kind, time.time(), today(self.bot))
            await self._show(
                interaction,
                f"{lab.WORKER_MARK[kind]} **{lab.WORKER_NAME[kind]}** を雇った！" if ok else f"⚠ {reason}",
            )

        return callback

    async def _dismiss(self, interaction: discord.Interaction):
        kind = interaction.data["values"][0]
        ok, reason = self.eco().labor_dismiss(self.user_id, kind, time.time(), today(self.bot))
        await self._show(interaction, f"**{lab.WORKER_NAME[kind]}** を解雇した。" if ok else f"⚠ {reason}")

    async def _dorm(self, interaction: discord.Interaction):
        ok, reason = self.eco().labor_dorm(self.user_id, time.time(), today(self.bot))
        await self._show(interaction, "🏠 寮を強化した！　雇える人数が増えた。" if ok else f"⚠ {reason}")

    async def _wage(self, interaction: discord.Interaction):
        want = "normal" if self.state["wage"] == "low" else "low"
        ok, reason = self.eco().labor_set_wage(self.user_id, want, time.time(), today(self.bot))
        await self._show(interaction, f"💴 賃金を **{lab.WAGE_NAME[want]}** にした。" if ok else f"⚠ {reason}")

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

    @group.command(name="作業", description="ボタンを押して稼ぐ（元手は0）。稼いだら、作業員を雇って放置でも稼ぐ")
    async def work(self, interaction: discord.Interaction):
        view = LaborView(self.bot, interaction.user.id, interaction.guild_id)
        await interaction.response.send_message(embed=panel_embed(view.state), view=view)

    @group.command(name="ランキング", description="⛏ 採掘ランキング（稼いだペリカの多い順）")
    @app_commands.rename(period="期間")
    @app_commands.describe(period="今日の稼ぎか、通算の稼ぎか（省略で通算）")
    @app_commands.choices(
        period=[app_commands.Choice(name="通算", value="total"), app_commands.Choice(name="今日", value="today")]
    )
    async def ranking(self, interaction: discord.Interaction, period: str = "total"):
        eco = self.bot.economy.for_guild(interaction.guild_id)
        daily = period == "today"
        data = eco.labor_top(10, today(self.bot) if daily else None, interaction.user.id)
        await interaction.response.send_message(
            embed=ranking_embed(data, daily), allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot):
    await bot.add_cog(LaborCog(bot))
