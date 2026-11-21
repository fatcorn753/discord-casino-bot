"""競馬の厩舎画面（Stable）と調教・引退 View（GoldCarrotView / RetireView）。"""

import discord

from cogs.horse.cards import race_title, refresh_entry_card
from cogs.shared import ui
from cogs.shared.horse_display import stat_lines
from cogs.shared.messages import format_failure
from domain import horse as horse_mod
from domain import shop
from domain.errors import Failure
from domain.ports.contracts import HorseRow
from services.horse import HorseDesk, HorseService


class NameModal(discord.ui.Modal):
    """新しい馬の名前。そのあと、毛色と勝負服の色を選ぶ。"""

    def __init__(self, stable: "Stable"):
        super().__init__(title="競走馬の名前")
        self.stable = stable
        self.name = discord.ui.TextInput(label=f"名前（{horse_mod.NAME_MAX}文字まで）", max_length=horse_mod.NAME_MAX)
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction):
        ok, result = horse_mod.check_name(self.name.value)
        if not ok:
            await ui.reply_error(interaction, format_failure(result), title="名前を直してください")
            return
        assert not isinstance(result, Failure)
        view = AppearanceView(self.stable, result)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


class AppearanceView(discord.ui.View):
    """毛色と勝負服の色を選んで、馬を迎える（購入券を1枚使う）。"""

    def __init__(self, stable: "Stable", name: str):
        super().__init__(timeout=300)
        self.stable, self.name = stable, name
        self.coat, self.silks = horse_mod.COATS[0], horse_mod.SILKS[0]
        self._build()

    def embed(self) -> discord.Embed:
        return ui.embed(
            "🐴 見た目を決める",
            f"名前: **{self.name}**\n毛色: **{self.coat}**　勝負服: {self.silks}\n決まったら「迎える」を押します（購入券を1枚使います）。",
            ui.INFO,
        )

    def _build(self) -> None:
        self.clear_items()
        coat = discord.ui.Select(
            placeholder="毛色",
            row=0,
            options=[discord.SelectOption(label=c, default=c == self.coat) for c in horse_mod.COATS],
        )
        coat.callback = self._on_coat
        self.add_item(coat)
        silks = discord.ui.Select(
            placeholder="勝負服の色",
            row=1,
            options=[discord.SelectOption(label=s, value=s, emoji=s, default=s == self.silks) for s in horse_mod.SILKS],
        )
        silks.callback = self._on_silks
        self.add_item(silks)
        go = discord.ui.Button(label="迎える", style=discord.ButtonStyle.success, row=2)
        go.callback = self._on_go
        self.add_item(go)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.stable.user_id

    async def _on_coat(self, interaction: discord.Interaction):
        self.coat = ui.selected_values(interaction)[0]
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _on_silks(self, interaction: discord.Interaction):
        self.silks = ui.selected_values(interaction)[0]
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _on_go(self, interaction: discord.Interaction):
        ok, result = self.stable.desk(interaction).horse_create(interaction.user.id, self.name, self.coat, self.silks)
        if not ok:
            return await ui.reply_error(interaction, format_failure(result), title="迎えられません")
        assert not isinstance(result, Failure)
        self.stable.horse_id = result
        self.stop()
        await interaction.response.edit_message(
            embed=ui.ok_embed(
                "🐴 競走馬を迎えました",
                f"{self.silks} **{self.name}**（{self.coat}）\n`/競馬 厩舎` で、にんじんでステータスを配分し、レースに申し込めます。",
            ),
            view=None,
        )


class StyleView(ui.OwnerView):
    """脚質を選ぶ（選ばなければ、ステータスから自動で決まる）。"""

    def __init__(self, stable: "Stable", user_id: int, horse: HorseRow):
        super().__init__(user_id, timeout=300)
        self.stable, self.user_id, self.horse = stable, user_id, horse
        auto = horse_mod.style_of({k: horse[k] for k in horse_mod.STAT_KEYS})
        options = [
            discord.SelectOption(
                label=f"（自動: {horse_mod.STYLE_NAMES[auto]}）",
                value="auto",
                default=horse["style"] is None,
            )
        ] + [
            discord.SelectOption(label=name, value=key, emoji=horse_mod.STYLE_MARK[key], default=horse["style"] == key)
            for key, name in horse_mod.STYLE_NAMES.items()
        ]
        select = discord.ui.Select(placeholder="脚質を選ぶ", options=options)
        select.callback = self._on_pick
        self.add_item(select)

    def embed(self) -> discord.Embed:
        cur = self.horse["style"]
        auto = horse_mod.style_of({k: self.horse[k] for k in horse_mod.STAT_KEYS})
        label = (
            f"{horse_mod.STYLE_MARK[cur]} {horse_mod.STYLE_NAMES[cur]}"
            if cur
            else f"（自動: {horse_mod.STYLE_NAMES[auto]}）"
        )
        return ui.embed(f"🎭 {self.horse['name']} の脚質", f"いまの脚質: **{label}**", ui.INFO)

    async def _on_pick(self, interaction: discord.Interaction):
        value = ui.selected_values(interaction)[0]
        horses = self.stable.desk(interaction)
        ok, reason = horses.horse_set_style(self.user_id, self.horse["id"], None if value == "auto" else value)
        if not ok:
            return await ui.reply_error(interaction, format_failure(reason), title="選べません")
        updated = horses.horse_get(self.horse["id"])
        assert updated is not None
        self.horse = updated
        self.stop()
        await interaction.response.edit_message(embed=self.embed(), view=None)


class GoldCarrotView(ui.OwnerView):
    """金にんじんの振り分け。5点ずつ、2か所を選んでから「使う」を押す（押すまで、反映しない）。"""

    def __init__(self, stable: "Stable", user_id: int):
        super().__init__(user_id, timeout=300)
        self.stable, self.user_id = stable, user_id
        assert stable.horse_id is not None
        self.horse_id: int = stable.horse_id
        self.first = self.second = "speed"
        self._build()

    def embed(self) -> discord.Embed:
        horse = self.stable.desk_here().horse_get(self.horse_id)
        assert horse is not None
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

    async def _on_use(self, interaction: discord.Interaction):
        ok, result = self.stable.desk(interaction).horse_gold_carrot(
            interaction.user.id, self.horse_id, self.first, self.second
        )
        if not ok:
            return await ui.reply_error(interaction, format_failure(result), title="使えません")
        assert not isinstance(result, Failure)
        self.stop()
        await interaction.response.edit_message(
            embed=ui.ok_embed("金にんじんを使いました", f"{stat_lines(result)}"), view=None
        )


class RetireView(ui.OwnerView):
    """引退の確認。戦績に応じて、引退金をもらう（戦績が悪いと、引退費用を払う）。"""

    def __init__(self, stable: "Stable", user_id: int, horse: HorseRow, value: int):
        super().__init__(user_id, timeout=300)
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

    async def _on_ok(self, interaction: discord.Interaction):
        horses = self.stable.desk(interaction)
        ok, result = horses.horse_retire(interaction.user.id, self.horse["id"])
        if not ok:
            return await ui.reply_error(interaction, format_failure(result), title="引退させられません")
        assert not isinstance(result, Failure)
        self.stop()
        remaining = horses.horse_list(interaction.user.id)
        self.stable.horse_id = remaining[0]["id"] if remaining else None  # 厩舎の画面で選んでいた馬は、もういない
        text = f"引退金 +{ui.pelica(result)}" if result >= 0 else f"引退費用 {ui.pelica(-result)}"
        await interaction.response.edit_message(
            embed=ui.ok_embed("引退しました", f"**{self.horse['name']}** は引退しました。{text}"), view=None
        )


class Stable(ui.OwnerView):
    """`/競馬 厩舎` の画面。馬を選び、出走の申し込み・取り下げ・にんじん・新しい馬を、ボタンで行う。"""

    def __init__(
        self,
        horse_svc: HorseService,
        user_id: int,
        guild_id: int,
    ) -> None:
        super().__init__(user_id, timeout=600)
        self._svc = horse_svc
        self.user_id, self.guild_id = user_id, guild_id
        self.horse_id: int | None = None
        self.src, self.dst = "speed", "guts"
        owned = self.desk_here().horse_list(user_id)
        if owned:
            self.horse_id = owned[0]["id"]
        self._build()

    async def _reject_non_owner(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "これは他の人の厩舎です。`/競馬 厩舎` で自分の厩舎を開けます。", ephemeral=True
        )

    def desk_here(self) -> HorseDesk:
        """画面を開いたサーバーの競馬窓口。"""
        return self._svc.desk(self.guild_id)

    def desk(self, interaction: discord.Interaction) -> HorseDesk:
        """ボタンを押されたサーバーの競馬窓口。"""
        return self._svc.desk(ui.guild_id_of(interaction))

    def embed(self) -> discord.Embed:
        desk = self.desk_here()
        inv = desk.inventory(self.user_id)
        horse_rows = desk.horse_list(self.user_id)
        e = ui.embed(
            "🐴 厩舎",
            f"購入券 **{inv.get(shop.HORSE_VOUCHER, 0)}枚**　🥕 にんじん **{inv.get(shop.CARROT, 0)}本**　"
            f"🥇 金にんじん **{inv.get(shop.GOLD_CARROT, 0)}本**　（ショップで買えます。持ち馬は {horse_mod.MAX_HORSES} 頭まで）\n"
            f"ステータスの合計は **{horse_mod.STAT_TOTAL}**。にんじん1本で、{horse_mod.CARROT_POINTS}点を別のステータスへ動かせます。"
            f"金にんじん1本で、合計が {2 * horse_mod.GOLD_CARROT_POINTS} 点増えます（5点ずつ好きなところへ。1頭に {horse_mod.GOLD_CARROT_MAX} 本まで）。\n"
            "🐴 購入券を持っていれば、「新しい馬を迎える」で名前・毛色・勝負服の色を決めて迎えられます。",
            ui.INFO,
        )
        open_races = {r["id"]: r for r in desk.race_open()}
        for h in horse_rows:
            registered = [
                r for r in open_races.values() if any(x["horse_id"] == h["id"] for x in desk.race_entries(r["id"]))
            ]
            note = f"\n🏁 {race_title(desk, registered[0])} に出走予定" if registered else ""
            mark = "▶ " if h["id"] == self.horse_id else ""
            style = h["style"] or horse_mod.style_of({k: h[k] for k in horse_mod.STAT_KEYS})
            e.add_field(
                name=f"{mark}{h['silks']} {h['name']}（{h['coat']}）",
                value=f"{horse_mod.STYLE_MARK[style]} {horse_mod.STYLE_NAMES[style]}\n{stat_lines(h)}\n戦績 {h['races']}/{horse_mod.MAX_RACES}戦 {h['wins']}勝（3着以内 {h['places']}）　賞金 {ui.pelica(h['earnings'])}{note}",
                inline=False,
            )
        nxt = next((r for r in desk.race_open() if r["status"] == "entry"), None)
        if nxt:
            e.set_footer(
                text=f"次の申し込み: 第{desk.race_number(nxt['id'])}競走（締切 {desk.local_time(nxt['entry_close']):%H:%M}）"
            )
        return e

    def _build(self) -> None:
        self.clear_items()
        horses = self.desk_here().horse_list(self.user_id)
        inv = self.desk_here().inventory(self.user_id)
        can_buy = inv.get(shop.HORSE_VOUCHER, 0) > 0 and len(horses) < horse_mod.MAX_HORSES
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
            ("🥕 にんじんで動かす", discord.ButtonStyle.primary, self._on_carrot, not horses, 3),
            ("🏁 出走を申し込む", discord.ButtonStyle.success, self._on_enter, not horses, 3),
            ("↩️ 申し込みを取り下げる", discord.ButtonStyle.secondary, self._on_withdraw, not horses, 3),
            ("🐴 新しい馬を迎える", discord.ButtonStyle.secondary, self._on_new, not can_buy, 4),
            ("🥇 金にんじんを使う", discord.ButtonStyle.primary, self._on_gold, not horses, 4),
            ("🎭 脚質を選ぶ", discord.ButtonStyle.secondary, self._on_style, not horses, 4),
            ("🎌 引退させる", discord.ButtonStyle.danger, self._on_retire, not horses, 4),
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
        assert self.horse_id is not None
        ok, result = self.desk(interaction).horse_carrot(interaction.user.id, self.horse_id, self.src, self.dst)
        if not ok:
            return await ui.reply_error(interaction, format_failure(result), title="動かせません")
        await self._redraw(interaction)

    async def _on_gold(self, interaction: discord.Interaction):
        view = GoldCarrotView(self, interaction.user.id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    async def _on_style(self, interaction: discord.Interaction):
        assert self.horse_id is not None
        horse = self.desk(interaction).horse_get(self.horse_id)
        if horse is None or horse["owner_id"] != interaction.user.id:
            return await ui.reply_error(interaction, "自分の馬を選んでください。", title="選べません")
        view = StyleView(self, interaction.user.id, horse)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    async def _on_retire(self, interaction: discord.Interaction):
        assert self.horse_id is not None
        horse = self.desk(interaction).horse_get(self.horse_id)
        if horse is None or horse["owner_id"] != interaction.user.id:
            return await ui.reply_error(interaction, "自分の馬を選んでください。", title="引退させられません")
        value = horse_mod.retire_value(horse["races"], horse["wins"], horse["places"])
        view = RetireView(self, interaction.user.id, horse, value)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    def _next_entry_race(self, desk: HorseDesk):
        return next((r for r in desk.race_open() if r["status"] == "entry"), None)

    async def _on_enter(self, interaction: discord.Interaction):
        assert self.horse_id is not None
        desk = self.desk(interaction)
        race = self._next_entry_race(desk)
        if race is None:
            return await ui.reply_error(
                interaction, "いま、申し込みを受け付けているレースはありません。", title="申し込めません"
            )
        ok, reason = desk.race_register(interaction.user.id, self.horse_id, race["id"], desk.clock.now_ts())
        if not ok:
            return await ui.reply_error(interaction, format_failure(reason), title="申し込めません")
        await self._redraw(interaction)
        await refresh_entry_card(interaction, desk, race["id"])

    async def _on_withdraw(self, interaction: discord.Interaction):
        assert self.horse_id is not None
        desk = self.desk(interaction)
        race = self._next_entry_race(desk)
        if race is None:
            return await ui.reply_error(interaction, "申し込み中のレースはありません。", title="取り下げられません")
        ok, reason = desk.race_withdraw(interaction.user.id, self.horse_id, race["id"], desk.clock.now_ts())
        if not ok:
            return await ui.reply_error(interaction, format_failure(reason), title="取り下げられません")
        await self._redraw(interaction)
        await refresh_entry_card(interaction, desk, race["id"])

    async def _on_new(self, interaction: discord.Interaction):
        await interaction.response.send_modal(NameModal(self))
