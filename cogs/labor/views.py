"""`/地下 作業` の作業場の画面（ボタン・タブ）。値の読み書きは services/labor.py の LaborService を通す。"""

import discord

from cogs.labor.embeds import hire_embed, ore_text, panel_embed, relic_text
from cogs.shared import ui
from cogs.shared.messages import format_failure
from domain import labor as lab
from domain import relics, shop
from domain.errors import Failure
from domain.ports.contracts import LaborState
from services.labor import LaborService

VIEW_TIMEOUT = 900  # 画面が反応する時間（秒）。過ぎたら、`/地下 作業` で開き直す


class LaborView(ui.OwnerView):
    def __init__(
        self,
        labor_svc: LaborService,
        user_id: int,
        guild_id: int,
    ) -> None:
        super().__init__(
            user_id,
            timeout=VIEW_TIMEOUT,
            owner_message="これは他の人の作業場です。`/地下 作業` で自分の作業場を開けます。",
        )
        self._svc = labor_svc
        self.user_id = user_id
        self.guild_id = guild_id
        self.state: LaborState
        self.tab = "work"
        self.refresh()

    # -------------------------------------------------------------- #
    def refresh(self) -> None:
        """いまの状態を読み直して、ボタンを組み直す。"""
        self.state = self._svc.state(self.guild_id, self.user_id, self._svc.clock.now_ts())
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

    async def _show(self, interaction: discord.Interaction, note: str = "") -> None:
        self.refresh()
        embed = hire_embed(self.state, note) if self.tab == "hire" else panel_embed(self.state, note)
        await interaction.response.edit_message(embed=embed, view=self)

    # -------------------------------------------------------------- #
    async def _work(self, interaction: discord.Interaction):
        r = self._svc.click(self.guild_id, self.user_id, self._svc.clock.now_ts())
        if r["ok"] is False:
            return await self._show(interaction, "" if r.get("quiet") else f"⚠ {r['reason']}")
        note = f"⛏ **+{r['pay']:,}**"
        if r["crit"]:
            note = f"💥 鉱脈ヒット！！ {note}"
            if r["ore"]:
                note += f"（さらに {lab.ORE_MARK[r['ore']]}{lab.ORE_NAME[r['ore']]} も！）"
        if r["combo"]:
            note += f"\n🔥 {lab.COMBO_SIZE}回コンボ！（+{lab.COMBO_BONUS:.0%}）"
        await self._show(interaction, note)

    async def _drink(self, interaction: discord.Interaction):
        ok, reason = self._svc.drink(self.guild_id, self.user_id, self._svc.clock.now_ts())
        await self._show(
            interaction,
            f"🥤 ぐいっと飲んだ。疲労が {lab.DRINK_RECOVER:.0f} 回復した！" if ok else f"⚠ {format_failure(reason)}",
        )

    async def _collect(self, interaction: discord.Interaction):
        got = self._svc.collect(self.guild_id, self.user_id, self._svc.clock.now_ts())
        amount, found, collected_relics = got["amount"], got["ores"], got["relics"]
        note = f"💰 **{amount:,}** ペリカを、まとめて手元に移しました。" if amount else ""
        if got["skimmed"]:
            note += f"\n🎗️ ピンハネされた **{got['skimmed']:,}** ペリカは、慈善団体に寄付されます"
        if found:
            note += f"\n💎 掘り出し: {ore_text(found)}"
        if collected_relics:
            note += f"\n🏺 発掘品が持ち物に入りました: {relic_text(collected_relics)}"
        await self._show(interaction, note)
        big_relics = {i: n for i, n in collected_relics.items() if i.removeprefix("relic_") in lab.RELIC_BIG}
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
            ok, reason = self._svc.hire(self.guild_id, self.user_id, kind, self._svc.clock.now_ts())
            await self._show(
                interaction,
                f"{lab.WORKER_MARK[kind]} **{lab.WORKER_NAME[kind]}** を雇った！"
                if ok
                else f"⚠ {format_failure(reason)}",
            )

        return callback

    async def _dismiss(self, interaction: discord.Interaction):
        kind = ui.selected_values(interaction)[0]
        ok, reason = self._svc.dismiss(self.guild_id, self.user_id, kind, self._svc.clock.now_ts())
        await self._show(
            interaction, f"**{lab.WORKER_NAME[kind]}** を解雇した。" if ok else f"⚠ {format_failure(reason)}"
        )

    async def _renew(self, interaction: discord.Interaction):
        want = not self.state["renew"]
        self._svc.set_renew(self.guild_id, self.user_id, want, self._svc.clock.now_ts())
        await self._show(
            interaction,
            "🔁 自動更新を **オン** にした。契約が切れたら、貯まった稼ぎから契約料を払います。"
            if want
            else "🔁 自動更新を **オフ** にした。契約が切れた作業員は、退職します。",
        )

    async def _dorm(self, interaction: discord.Interaction):
        ok, reason = self._svc.dorm(self.guild_id, self.user_id, self._svc.clock.now_ts())
        await self._show(
            interaction, "🏠 寮を強化した！　雇える人数が増えた。" if ok else f"⚠ {format_failure(reason)}"
        )

    async def _merge(self, interaction: discord.Interaction):
        ok, result = self._svc.merge_relic(self.guild_id, self.user_id, ui.selected_values(interaction)[0])
        if not ok:
            return await self._show(interaction, f"⚠ {format_failure(result)}")
        assert not isinstance(result, Failure)
        item = shop.BY_ID[result]
        await self._show(interaction, f"🧬 合体した！　**{item.mark}{item.name}** ができた。{relics.buff_text(result)}")

    def _upgrader(self, tool: str):
        async def callback(interaction: discord.Interaction):
            ok, reason = self._svc.upgrade(self.guild_id, self.user_id, tool, self._svc.clock.now_ts())
            await self._show(
                interaction,
                f"{lab.TOOL_MARK[tool]} **{lab.TOOL_NAME[tool]}** を強化した！（{lab.TOOL_EFFECT[tool]}）"
                if ok
                else f"⚠ {format_failure(reason)}",
            )

        return callback
