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
from core import relics, shop, ui

VIEW_TIMEOUT = 900  # 画面が反応する時間（秒）。過ぎたら、`/地下 作業` で開き直す


def today(bot) -> str:
    tz = datetime.timezone(datetime.timedelta(hours=float(bot.config.get("utc_offset_hours", 9))))
    return datetime.datetime.now(tz).date().isoformat()


def fatigue_label(fatigue: float) -> str:
    return "元気" if fatigue < 25 else "ふつう" if fatigue < 50 else "疲れ気味" if fatigue < 75 else "ぐったり"


def ore_text(found: dict) -> str:
    """掘った鉱石の一覧（価値の高い順）。"""
    order = sorted(lab.ORE_VALUE, key=lambda k: -lab.ORE_VALUE[k])  # 高い順
    return "　".join(f"{lab.ORE_MARK[k]}{lab.ORE_NAME[k]}×{found[k]}" for k in order if found.get(k))


def relic_text(found: dict) -> str:
    """見つけた発掘品の一覧（目安の価値の高い順）。"""
    order = sorted(found, key=lambda i: -shop.RELIC_VALUE.get(i, 0))
    return "　".join(f"{shop.BY_ID[i].mark}{shop.BY_ID[i].name}×{found[i]}" for i in order if found.get(i))


def _find_label(f: dict) -> str:
    """掘り出し1つの表示（鉱石は売値、発掘品は目安の価値）。"""
    if f["ore"] in shop.RELIC_VALUE:
        item = shop.BY_ID[f["ore"]]
        return f"{item.mark}{item.name}（目安 {shop.RELIC_VALUE[f['ore']]:,}）"
    return f"{lab.ORE_MARK[f['ore']]}{lab.ORE_NAME[f['ore']]}（{lab.ORE_VALUE[f['ore']]:,}）"


def finds_text(finds: list) -> str:
    """最近の掘り出し（石炭は除く）。いつ・どの作業員が・何を掘ったか。"""
    return "\n".join(
        f"<t:{int(f['at'])}:R>　{lab.WORKER_MARK[f['kind']]}{lab.WORKER_NAME[f['kind']]} が {_find_label(f)}"
        for f in finds
    )


def pot_text(state: dict) -> str:
    """未受け取りの額。作業員がいるときは、そのうち作業員が稼いだ分（鉱石の売値を含む）も見せる。"""
    text = f"**{ui.pelica(state['pot_total'])}**"
    if state["workers"] or state["pot_workers_total"]:
        text += f"\n（うち作業員 {ui.pelica(state['pot_workers_total'])}）"
    return text


def panel_embed(state: dict, note: str = "") -> discord.Embed:
    """作業の画面。上に、直前の結果。つぎに、お金と疲労。"""
    fat = state["fatigue"]
    e = ui.embed("⛏ 地下作業", note or "「働く」を押すと、ペリカが貯まります。", ui.DARK)
    e.add_field(name="💴 手元", value=ui.pelica(state["balance"]), inline=True)
    e.add_field(name="🎒 未受け取り", value=pot_text(state), inline=True)
    capped = state["day_click"] >= lab.CLICK_DAY_CAP
    e.add_field(
        name="📅 今日の稼ぎ",
        value=f"{ui.pelica(state['day_earned'])}\n（クリック {state['day_click']:,}/{lab.CLICK_DAY_CAP:,}ペリカ）",
        inline=True,
    )
    e.add_field(
        name="⛏ 1回の稼ぎ",
        value=f"**{ui.pelica(lab.OVER_CAP_PAY if capped else lab.click_pay(state['pick'], fat))}**"
        + (f"\n（上限に達しました。{lab.OVER_CAP_PAY}ペリカずつ）" if capped else ""),
        inline=True,
    )
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
    if state.get("ores"):
        e.add_field(name="💎 作業員の掘り出し", value=ore_text(state["ores"]), inline=False)
    if state.get("relics"):
        e.add_field(name="🏺 発掘品（受け取ると持ち物に入る）", value=relic_text(state["relics"]), inline=False)
    if state.get("held_relics"):
        e.add_field(
            name="🎒 持っている発掘品",
            value=relic_text(state["held_relics"])
            + f"\n（同じ品を{relics.MERGE_COUNT}個集めると、★が1つ増えた1個に合体できます）",
            inline=False,
        )
    if state.get("finds"):
        e.add_field(name="📜 最近の掘り出し", value=finds_text(state["finds"]), inline=False)
    e.set_footer(
        text=f"疲労は時間で回復します。「受け取る」で、貯まった分をまとめて手元に移します。\n"
        f"総寄付金額：{state.get('donated_all', 0):,}ペリカ"
    )
    return e


def hire_embed(state: dict, note: str = "") -> discord.Embed:
    """雇用の画面。作業員の一覧（人数・稼ぎ・次の値段）、寮。"""
    full = state["workers"] >= state["slots"]
    head = [x for x in (note, "⚠ 人数がいっぱいです。寮を強化するか、誰かを解雇しよう。" if full else "") if x]
    e = ui.embed(
        "👷 作業員の雇用",
        "\n".join(head)
        if head
        else "雇った作業員は、放置で働き、1時間ごとに鉱石を掘ります。レア鉱石を引けば、大きく黒字です。",
        ui.DARK,
    )
    e.add_field(name="💴 手元", value=ui.pelica(state["balance"]), inline=True)
    e.add_field(name="🎒 未受け取り", value=pot_text(state), inline=True)
    e.add_field(name="📈 作業員の稼ぎ", value=f"**{state['rate']:,.1f}/時**", inline=True)
    e.add_field(
        name="🏠 人数",
        value=f"**{state['workers']} / {state['slots']}人**（寮 Lv{state['dorm']}/{lab.DORM_MAX}）",
        inline=True,
    )
    e.add_field(
        name="🔁 自動更新",
        value="**オン**\n切れたら、貯まった稼ぎから契約料を払う" if state["renew"] else "**オフ**\n切れたら、退職する",
        inline=True,
    )
    for k in lab.WORKERS:
        if k == "foreman":
            gain = f"レア鉱石・発掘品が出やすい +{lab.FOREMAN_LUCK:.0%}"
        else:
            gain = f"稼ぎ {lab.WORKER_RATE[k]:g}/時 ＋ 鉱石 ＋ まれに発掘品"
        until = f"\n契約が切れる: <t:{int(state['expires'][k])}:R>" if state["expires"].get(k) else ""
        e.add_field(
            name=f"{lab.WORKER_MARK[k]} {lab.WORKER_NAME[k]} ×{state[k]}",
            value=f"{gain}\n契約料 **{lab.contract_fee(k):,}**（{lab.CONTRACT_DAYS}日）{until}",
            inline=True,
        )
    e.set_footer(
        text=f"契約は{lab.CONTRACT_DAYS}日ごと。作業員の稼ぎだけでは赤字で、平均でも契約料に届きません。当たり次第です。貯まるのは最大{lab.STOCK_HOURS:.0f}時間分。"
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
            disabled=s["pot_total"] <= 0 and not s["relics"],  # 発掘品だけでも、受け取れる
        )
        collect.callback = self._collect
        if self.tab == "hire":
            back = discord.ui.Button(label="作業にもどる", emoji="⛏", style=discord.ButtonStyle.secondary, row=0)
            back.callback = self._goto("work")
            self.add_item(back)
            self.add_item(collect)
            renew = discord.ui.Button(
                label="自動更新: " + ("オン" if s["renew"] else "オフ"),
                emoji="🔁",
                style=discord.ButtonStyle.success if s["renew"] else discord.ButtonStyle.secondary,
                row=0,
            )
            renew.callback = self._renew
            self.add_item(renew)
            full = s["workers"] >= s["slots"]
            for k in lab.WORKERS:
                cost = lab.contract_fee(k)
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
        drink = discord.ui.Button(
            label=f"栄養ドリンコ ×{s['drinks']}" if s["drinks"] else "栄養ドリンコ（ショップで買える）",
            emoji="🥤",
            style=discord.ButtonStyle.secondary,
            row=0,
            disabled=s["drinks"] <= 0 or s["fatigue"] < 1,
        )
        drink.callback = self._drink
        for b in (work, collect, hire, drink):
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
        mergeable = [
            i
            for i, n in sorted(s["held_relics"].items(), key=lambda kv: -shop.RELIC_VALUE.get(kv[0], 0))
            if n >= relics.MERGE_COUNT and relics.split_id(i)[1] < relics.STAR_MAX
        ]
        if mergeable:
            merge = discord.ui.Select(
                placeholder=f"🧬 発掘品を合体（{relics.MERGE_COUNT}個 → ★が1つ増えた1個）",
                row=2,
                options=[
                    discord.SelectOption(
                        label=f"{shop.BY_ID[i].name} {relics.MERGE_COUNT}個 → {shop.BY_ID[relics.star_id(relics.split_id(i)[0], relics.split_id(i)[1] + 1)].name}（いま {s['held_relics'][i]}個）",
                        value=i,
                        emoji=shop.BY_ID[i].mark,
                    )
                    for i in mergeable[:25]
                ],
            )
            merge.callback = self._merge
            self.add_item(merge)

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

    async def _drink(self, interaction: discord.Interaction):
        ok, reason = self.eco().labor_drink(self.user_id, time.time(), today(self.bot))
        await self._show(
            interaction, f"🥤 ぐいっと飲んだ。疲労が {lab.DRINK_RECOVER:.0f} 回復した！" if ok else f"⚠ {reason}"
        )

    async def _collect(self, interaction: discord.Interaction):
        got = self.eco().labor_collect(self.user_id, time.time(), today(self.bot), detail=True)
        amount, found, relics = got["amount"], got["ores"], got["relics"]
        note = f"💰 **{amount:,}** ペリカを、まとめて手元に移しました。" if amount else ""
        if got["skimmed"]:
            note += f"\n🎗️ ピンハネされた **{got['skimmed']:,}** ペリカは、慈善団体に寄付されます"
        if found:
            note += f"\n💎 掘り出し: {ore_text(found)}"
        if relics:
            note += f"\n🏺 発掘品が持ち物に入りました: {relic_text(relics)}"
        await self._show(interaction, note)
        big_relics = {i: n for i, n in relics.items() if i.removeprefix("relic_") in lab.RELIC_BIG}
        if big_relics:  # 大当たりの発掘品も、みんなに知らせる
            await interaction.followup.send(
                f"🎉 {interaction.user.mention} の作業員が、**{relic_text(big_relics)}** を掘り当てた！"
            )
        big = {k: n for k, n in found.items() if k in lab.ORE_BIG}
        if big:  # 大当たりは、みんなに知らせる
            await interaction.followup.send(
                f"🎉 {interaction.user.mention} の作業員が、**{ore_text(big)}** を掘り当てた！"
            )

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

    async def _renew(self, interaction: discord.Interaction):
        want = not self.state["renew"]
        self.eco().labor_set_renew(self.user_id, want, time.time(), today(self.bot))
        await self._show(
            interaction,
            "🔁 自動更新を **オン** にした。契約が切れたら、貯まった稼ぎから契約料を払います。"
            if want
            else "🔁 自動更新を **オフ** にした。契約が切れた作業員は、退職します。",
        )

    async def _dorm(self, interaction: discord.Interaction):
        ok, reason = self.eco().labor_dorm(self.user_id, time.time(), today(self.bot))
        await self._show(interaction, "🏠 寮を強化した！　雇える人数が増えた。" if ok else f"⚠ {reason}")

    async def _merge(self, interaction: discord.Interaction):
        ok, result = self.eco().relic_merge(self.user_id, interaction.data["values"][0])
        if not ok:
            return await self._show(interaction, f"⚠ {result}")
        item = shop.BY_ID[result]
        await self._show(interaction, f"🧬 合体した！　**{item.mark}{item.name}** ができた。{relics.buff_text(result)}")

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
        # (サーバー, 人) → 最後に開いた作業場の (チャンネルID, メッセージID)。新しく開いたら、前のを消す（チャンネルが作業場で埋まらないように）
        self._panels: dict = {}

    group = app_commands.Group(name="地下", description="⛏ ペリカが尽きたときの、地下の仕事", guild_only=True)

    @group.command(name="作業", description="ボタンを押して稼ぐ（元手は0）。稼いだら、作業員を雇って放置でも稼ぐ")
    async def work(self, interaction: discord.Interaction):
        view = LaborView(self.bot, interaction.user.id, interaction.guild_id)
        key = (interaction.guild_id, interaction.user.id)
        old = self._panels.pop(key, None)
        await interaction.response.send_message(embed=panel_embed(view.state), view=view)
        if old:
            try:
                await self.bot.get_partial_messageable(old[0]).get_partial_message(old[1]).delete()
            except discord.HTTPException:
                pass  # すでに消えている・消せない: そのまま
        try:
            msg = await interaction.original_response()
            self._panels[key] = (msg.channel.id, msg.id)
        except discord.HTTPException:
            pass

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
