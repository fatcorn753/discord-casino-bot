"""競馬の厩舎画面（Stable）と調教・引退 View（GoldCarrotView / RetireView）。"""

import time

import discord

from cogs.horse.cards import race_title, refresh_entry_card
from cogs.shared import ui
from cogs.shared.horse_display import stat_lines
from cogs.shared.messages import format_failure
from domain import horse as horse_mod
from domain import shop


class GoldCarrotView(discord.ui.View):
    """金にんじんの振り分け。5点ずつ、2か所を選んでから「使う」を押す（押すまで、反映しない）。"""

    def __init__(self, stable: "Stable", user_id: int):
        super().__init__(timeout=300)
        self.stable, self.user_id = stable, user_id
        self.horse_id = stable.horse_id
        self.first = self.second = "speed"
        self._build()

    def embed(self) -> discord.Embed:
        horse = self.stable.eco_for().horse_get(self.horse_id)
        text = f"**{horse['name']}** に、金にんじん1本を使います。選んだ2か所に、{horse_mod.GOLD_CARROT_POINTS}点ずつ足します（同じところを2回選べば、+{2 * horse_mod.GOLD_CARROT_POINTS}）。\n"
        text += f"いまのステータス:\n{stat_lines(horse)}\n"
        text += f"**{horse_mod.STAT_NAMES[self.first]}** +{horse_mod.GOLD_CARROT_POINTS}　**{horse_mod.STAT_NAMES[self.second]}** +{horse_mod.GOLD_CARROT_POINTS}"
        return ui.embed("🥇 金にんじん", text, ui.GOLD)

    def _build(self) -> None:
        self.clear_items()
        for attr, row, label in (("first", 0, "1つ目の5点"), ("second", 1, "2つ目の5点")):
            select = discord.ui.Select(
                placeholder=f"{label}（いま: {horse_mod.STAT_NAMES[getattr(self, attr)]}）",
                row=row,
                options=[
                    discord.SelectOption(label=horse_mod.STAT_NAMES[k], value=k, default=k == getattr(self, attr))
                    for k in horse_mod.STAT_KEYS
                ],
            )
            select.callback = self._make_pick(attr)
            self.add_item(select)
        use = discord.ui.Button(label="🥇 使う", style=discord.ButtonStyle.success, row=2)
        use.callback = self._on_use
        self.add_item(use)

    def _make_pick(self, attr: str):
        async def callback(interaction: discord.Interaction):
            setattr(self, attr, ui.selected_values(interaction)[0])
            self._build()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        return callback

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def _on_use(self, interaction: discord.Interaction):
        eco = self.stable.eco(interaction)
        ok, result = eco.horse_gold_carrot(interaction.user.id, self.horse_id, self.first, self.second)
        if not ok:
            return await interaction.response.send_message(
                embed=ui.error_embed("使えません", format_failure(result)), ephemeral=True
            )
        self.stop()
        await interaction.response.edit_message(
            embed=ui.ok_embed("金にんじんを使いました", f"{stat_lines(result)}"), view=None
        )


class RetireView(discord.ui.View):
    """引退の確認。戦績に応じて、引退金をもらう（戦績が悪いと、引退費用を払う）。"""

    def __init__(self, stable: "Stable", user_id: int, horse: dict, value: int):
        super().__init__(timeout=300)
        self.stable, self.user_id, self.horse, self.value = stable, user_id, horse, value
        ok = discord.ui.Button(label="🎌 引退させる", style=discord.ButtonStyle.danger)
        ok.callback = self._on_ok
        self.add_item(ok)

    def embed(self) -> discord.Embed:
        h = self.horse
        if self.value >= 0:
            money = f"引退金 **+{ui.pelica(self.value)}** をもらえます。"
        else:
            money = f"引退費用 **{ui.pelica(-self.value)}** を払います。"
        return ui.embed(
            "🎌 引退",
            f"{h['silks']} **{h['name']}** を引退させます。\n戦績 {h['races']}戦{h['wins']}勝（3着以内 {h['places']}）\n{money}\n"
            "引退した馬は、もう戻りません。",
            ui.INFO,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def _on_ok(self, interaction: discord.Interaction):
        eco = self.stable.eco(interaction)
        ok, result = eco.horse_retire(interaction.user.id, self.horse["id"])
        if not ok:
            return await interaction.response.send_message(
                embed=ui.error_embed("引退させられません", format_failure(result)), ephemeral=True
            )
        self.stop()
        remaining = eco.horse_list(interaction.user.id)
        self.stable.horse_id = remaining[0]["id"] if remaining else None  # 厩舎の画面で選んでいた馬は、もういない
        text = f"引退金 +{ui.pelica(result)}" if result >= 0 else f"引退費用 {ui.pelica(-result)}"
        await interaction.response.edit_message(
            embed=ui.ok_embed("引退しました", f"**{self.horse['name']}** は引退しました。{text}"), view=None
        )


class Stable(ui.OwnerView):
    """`/競馬 厩舎` の画面。馬を選び、出走の申し込み・取り下げ・にんじん・新しい馬を、ボタンで行う。"""

    def __init__(self, bot, user_id: int, guild_id: int):
        super().__init__(user_id, timeout=600)
        self.bot, self.user_id, self.guild_id = bot, user_id, guild_id
        self.horse_id = None
        self.src, self.dst = "speed", "guts"
        eco = self.eco_for()
        eco.rental_ensure(user_id, time.time(), self._tz())
        horses = eco.horse_list(user_id)
        if horses:
            self.horse_id = horses[0]["id"]
        self._build()

    async def _reject_non_owner(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "これは他の人の厩舎です。`/競馬 厩舎` で自分の厩舎を開けます。", ephemeral=True
        )

    def _tz(self) -> float:
        return float(self.bot.config.get("utc_offset_hours", 9))

    def eco_for(self):
        return self.bot.economy.for_guild(self.guild_id)

    def eco(self, interaction: discord.Interaction):
        return self.bot.economy.for_guild(interaction.guild_id)

    def embed(self) -> discord.Embed:
        eco = self.eco_for()
        eco.rental_ensure(self.user_id, time.time(), self._tz())
        inv = eco.inventory(self.user_id)
        horses = eco.horse_list(self.user_id)
        e = ui.embed(
            "🐴 厩舎",
            f"購入券 **{inv.get(shop.HORSE_VOUCHER, 0)}枚**　🥕 にんじん **{inv.get(shop.CARROT, 0)}本**　"
            f"🥇 金にんじん **{inv.get(shop.GOLD_CARROT, 0)}本**　（ショップで買えます。持ち馬は {horse_mod.MAX_HORSES} 頭まで）\n"
            f"ステータスの合計は **{horse_mod.STAT_TOTAL}**。にんじん1本で、{horse_mod.CARROT_POINTS}点を別のステータスへ動かせます。"
            f"金にんじん1本で、合計が {2 * horse_mod.GOLD_CARROT_POINTS} 点増えます（5点ずつ好きなところへ。1頭に {horse_mod.GOLD_CARROT_MAX} 本まで）。\n"
            "🎫 帝愛から、毎日1頭ランダムな馬をレンタルできます（出走料はかかりますが、レンタル自体は無料。調整不可。"
            "購入券があれば、その日のうちに買って自分の馬にできます。これが馬を増やす唯一の方法です）。",
            ui.INFO,
        )
        open_races = {r["id"]: r for r in eco.race_open()}
        for h in horses:
            registered = [
                r for r in open_races.values() if any(x["horse_id"] == h["id"] for x in eco.race_entries(r["id"]))
            ]
            note = f"\n🏁 {race_title(eco, registered[0])} に出走予定" if registered else ""
            mark = "▶ " if h["id"] == self.horse_id else ""
            tag = "🎫 本日のレンタル\n" if h["rented_day"] else ""
            style = horse_mod.style_of({k: h[k] for k in horse_mod.STAT_KEYS})
            e.add_field(
                name=f"{mark}{h['silks']} {h['name']}（{h['coat']}）",
                value=f"{tag}{horse_mod.STYLE_MARK[style]} {horse_mod.STYLE_NAMES[style]}\n{stat_lines(h)}\n戦績 {h['races']}/{horse_mod.MAX_RACES}戦 {h['wins']}勝（3着以内 {h['places']}）　賞金 {ui.pelica(h['earnings'])}{note}",
                inline=False,
            )
        nxt = next((r for r in eco.race_open() if r["status"] == "entry"), None)
        if nxt:
            e.set_footer(
                text=f"次の申し込み: 第{eco.race_number(nxt['id'])}競走（締切 {time.strftime('%H:%M', time.localtime(nxt['entry_close']))}）"
            )
        return e

    def _build(self) -> None:
        self.clear_items()
        eco = self.eco_for()
        horses = eco.horse_list(self.user_id)
        picked = next((h for h in horses if h["id"] == self.horse_id), None)
        rented = bool(picked and picked["rented_day"])
        inv = eco.inventory(self.user_id)
        owned_count = sum(1 for h in horses if not h["rented_day"])
        can_buy = rented and inv.get(shop.HORSE_VOUCHER, 0) > 0 and owned_count < horse_mod.MAX_HORSES
        if horses:
            pick = discord.ui.Select(
                placeholder="馬を選ぶ",
                row=0,
                options=[
                    discord.SelectOption(
                        label=h["name"], value=str(h["id"]), emoji=h["silks"], default=h["id"] == self.horse_id
                    )
                    for h in horses
                ],
            )
            pick.callback = self._on_pick
            self.add_item(pick)
            names = [discord.SelectOption(label=horse_mod.STAT_NAMES[k], value=k) for k in horse_mod.STAT_KEYS]
            src = discord.ui.Select(
                placeholder=f"🥕 減らすステータス（いま: {horse_mod.STAT_NAMES[self.src]}）",
                row=1,
                options=[
                    discord.SelectOption(label=o.label, value=o.value, default=o.value == self.src) for o in names
                ],
            )
            src.callback = self._on_src
            self.add_item(src)
            dst = discord.ui.Select(
                placeholder=f"🥕 増やすステータス（いま: {horse_mod.STAT_NAMES[self.dst]}）",
                row=2,
                options=[
                    discord.SelectOption(label=o.label, value=o.value, default=o.value == self.dst) for o in names
                ],
            )
            dst.callback = self._on_dst
            self.add_item(dst)
        buttons = [
            ("🥕 にんじんで動かす", discord.ButtonStyle.primary, self._on_carrot, not horses or rented, 3),
            ("🏁 出走を申し込む", discord.ButtonStyle.success, self._on_enter, not horses, 3),
            ("↩️ 申し込みを取り下げる", discord.ButtonStyle.secondary, self._on_withdraw, not horses, 3),
            ("🐴 この馬を買う", discord.ButtonStyle.secondary, self._on_buy_rental, not can_buy, 4),
            ("🥇 金にんじんを使う", discord.ButtonStyle.primary, self._on_gold, not horses or rented, 4),
            ("🎌 引退させる", discord.ButtonStyle.danger, self._on_retire, not horses or rented, 4),
        ]
        for label, style, cb, disabled, row in buttons:
            b = discord.ui.Button(label=label, style=style, disabled=disabled, row=row)
            b.callback = cb
            self.add_item(b)

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _on_pick(self, interaction: discord.Interaction):
        self.horse_id = int(ui.selected_values(interaction)[0])
        await self._redraw(interaction)

    async def _on_src(self, interaction: discord.Interaction):
        self.src = ui.selected_values(interaction)[0]
        await self._redraw(interaction)

    async def _on_dst(self, interaction: discord.Interaction):
        self.dst = ui.selected_values(interaction)[0]
        await self._redraw(interaction)

    async def _on_carrot(self, interaction: discord.Interaction):
        ok, result = self.eco(interaction).horse_carrot(interaction.user.id, self.horse_id, self.src, self.dst)
        if not ok:
            return await interaction.response.send_message(
                embed=ui.error_embed("動かせません", format_failure(result)), ephemeral=True
            )
        await self._redraw(interaction)

    async def _on_gold(self, interaction: discord.Interaction):
        view = GoldCarrotView(self, interaction.user.id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    async def _on_retire(self, interaction: discord.Interaction):
        eco = self.eco(interaction)
        horse = eco.horse_get(self.horse_id)
        if horse is None or horse["owner_id"] != interaction.user.id:
            return await interaction.response.send_message(
                embed=ui.error_embed("引退させられません", "自分の馬を選んでください。"), ephemeral=True
            )
        value = horse_mod.retire_value(horse["races"], horse["wins"], horse["places"])
        view = RetireView(self, interaction.user.id, horse, value)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    def _next_entry_race(self, eco):
        return next((r for r in eco.race_open() if r["status"] == "entry"), None)

    async def _on_enter(self, interaction: discord.Interaction):
        eco = self.eco(interaction)
        race = self._next_entry_race(eco)
        if race is None:
            return await interaction.response.send_message(
                embed=ui.error_embed("申し込めません", "いま、申し込みを受け付けているレースはありません。"),
                ephemeral=True,
            )
        ok, reason = eco.race_register(interaction.user.id, self.horse_id, race["id"], time.time())
        if not ok:
            return await interaction.response.send_message(
                embed=ui.error_embed("申し込めません", format_failure(reason)), ephemeral=True
            )
        await self._redraw(interaction)
        await refresh_entry_card(interaction, eco, race["id"])

    async def _on_withdraw(self, interaction: discord.Interaction):
        eco = self.eco(interaction)
        race = self._next_entry_race(eco)
        if race is None:
            return await interaction.response.send_message(
                embed=ui.error_embed("取り下げられません", "申し込み中のレースはありません。"), ephemeral=True
            )
        ok, reason = eco.race_withdraw(interaction.user.id, self.horse_id, race["id"], time.time())
        if not ok:
            return await interaction.response.send_message(
                embed=ui.error_embed("取り下げられません", format_failure(reason)), ephemeral=True
            )
        await self._redraw(interaction)
        await refresh_entry_card(interaction, eco, race["id"])

    async def _on_buy_rental(self, interaction: discord.Interaction):
        eco = self.eco(interaction)
        ok, result = eco.horse_buy_rental(interaction.user.id, time.time(), self._tz())
        if not ok:
            return await interaction.response.send_message(
                embed=ui.error_embed("買えません", format_failure(result)), ephemeral=True
            )
        horse = eco.horse_get(result)
        await self._redraw(interaction)
        await interaction.followup.send(
            embed=ui.ok_embed(
                "🐴 競走馬を買いました",
                f"{horse['silks']} **{horse['name']}**（{horse['coat']}）\n"
                "`/競馬 厩舎` で、にんじんでステータスを配分し、レースに申し込めます。",
            ),
            ephemeral=True,
        )
