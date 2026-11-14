"""競馬の画面の部品: 出馬表・厩舎・馬券の画面、ボタンとモーダル（cogs/horse_cog.py から使う。読み込まれる Cog ではない）。

数字と処理は core/horse.py と core/eco_horse.py。ここは、見せ方と、ボタンの受け口だけ。
"""

import re
import time

import discord

from core import horse as horse_mod
from core import shop, ui

MEDAL = {1: "🥇", 2: "🥈", 3: "🥉"}


def bar(value: int) -> str:
    n = max(0, min(8, value // 10))
    return "█" * n + "░" * (8 - n)


def stat_lines(stats: dict) -> str:
    return "\n".join(f"`{horse_mod.STAT_NAMES[k]:<4}` {stats[k]:>2} {bar(stats[k])}" for k in horse_mod.STAT_KEYS)


def record_text(e: dict) -> str:
    """戦績（オッズの元）。初出走は、そう書く。"""
    if not e.get("races"):
        return "初出走"
    return f"{e['races']}戦{e['wins']}勝（3着内{e['places']}）"


def stat_short(e: dict) -> str:
    return f"速{e['speed']} ス{e['stamina']} パ{e['power']} 根{e['guts']}"


def race_title(eco, race: dict) -> str:
    return f"🏇 第{eco.race_number(race['id'])}競走" + ("（臨時）" if race["kind"] == "special" else "")


async def refresh_entry_card(interaction: discord.Interaction, eco, race_id: int) -> None:
    """申し込み・取り下げのあと、チャンネルの出馬表（申し込み中）を、いまの申し込みに書き換える。失敗しても、申し込みには影響しない。"""
    race = eco.race_get(race_id)
    if race is None or race["status"] != "entry" or not race["message_id"] or not race["channel_id"]:
        return
    channel = interaction.guild.get_channel(race["channel_id"]) if interaction.guild else None
    if channel is None:
        return
    try:
        await channel.get_partial_message(race["message_id"]).edit(embed=card_embed(eco, race))
    except discord.HTTPException:  # 消されていても、進行には関係ない
        pass


def when(ts: float) -> str:
    """時刻と「◯分後」。過ぎた時刻は、「◯秒前」が増え続けて紛らわしいので、時刻だけにする。"""
    if time.time() >= ts:
        return f"<t:{int(ts)}:t>"
    return f"<t:{int(ts)}:t>（<t:{int(ts)}:R>）"


def card_embed(eco, race: dict, closed: bool = False) -> discord.Embed:
    """出馬表。申し込み中は申し込みの馬、発売中は枠・オッズ・調子、終わったら着順。

    closed=True は、締切後に古い投稿を書き換える用。「◯秒前」は時間がたつほど増えて紛らわしいので、締切の時刻だけにする。
    """
    title = race_title(eco, race)
    entries = eco.race_entries(race["id"])
    if race["status"] == "entry":
        reg = [e for e in entries if e["lane"] < 0]
        if closed:
            lines = [f"出走の申し込みは、<t:{int(race['entry_close'])}:t> に締め切りました。"]
        else:
            lines = [f"出走の申し込み: **{when(race['entry_close'])}** まで（出走料 {ui.pelica(horse_mod.ENTRY_FEE)}）"]
            lines.append(f"発走: {when(race['start_at'])}")
            lines.append("`/競馬 厩舎` から、馬を申し込めます。足りない分は、帝愛の馬が走ります。")
        lines += ["", f"**申し込み {len(reg)} / {horse_mod.FIELD_SIZE}頭**"] + [
            f"{e['silks']} {e['name']}（<@{e['owner_id']}>）" for e in reg
        ]
        return ui.embed(f"{title}　{'受付終了' if closed else '出走受付中'}", "\n".join(lines), ui.INFO)
    track = horse_mod.Track(race["distance"], race["going"])
    if race["status"] == "betting":
        if closed:
            head = [
                f"コース: **{track.label}**",
                f"馬券の発売は、<t:{int(race['bet_close'])}:t> に締め切りました。",
            ]
        else:
            head = [
                f"コース: **{track.label}**　発走: {when(race['start_at'])}",
                f"馬券の発売: **{when(race['bet_close'])}** まで（単勝=1着・複勝=3着以内。**オッズは戦績で決まり、賭けた時点で確定**）",
            ]
        rows = [
            f"`{e['lane']}` {e['silks']} **{e['name']}** {horse_mod.FORM_MARK[e['form']]}　"
            f"単勝 **{e['odds_win']:.1f}** 複勝 **{e['odds_place']:.1f}**\n　└ {record_text(e)}　{stat_short(e)}"
            + (f"　馬主 <@{e['owner_id']}>" if e["owner_id"] else "")
            for e in entries
        ]
        e = ui.embed(f"{title}　{'発売終了' if closed else '発売中'}", "\n".join(head + [""] + rows), ui.GOLD)
        e.set_footer(
            text=f"調子 ◎好調 ○普通 △不調　控除 {horse_mod.TAKEOUT:.0%}　スピードは短距離、スタミナは長距離、パワーは重い馬場に効く"
        )
        return e
    order = sorted(entries, key=lambda x: x["finish"])

    def place(x: dict) -> str:
        return MEDAL.get(x["finish"], str(x["finish"]) + "着")

    rows = [
        f"{place(x)} {x['silks']} {x['name']}　単勝 {x['odds_win']:.1f}"
        + (f"（<@{x['owner_id']}>）" if x["owner_id"] else "")
        for x in order
    ]
    return ui.embed(f"{title}　結果", f"コース: **{track.label}**\n" + "\n".join(rows), ui.OK)


# ---------------------------------------------------------------- #
# ボタン（再起動しても動くように、レースの番号を custom_id に入れる）
# ---------------------------------------------------------------- #
class RaceButton(discord.ui.DynamicItem[discord.ui.Button], template=r"horse:(?P<act>bet|card):(?P<id>\d+)"):
    LABEL = {
        "bet": ("🎫 馬券を買う", discord.ButtonStyle.success),
        "card": ("📋 出馬表", discord.ButtonStyle.secondary),
    }

    def __init__(self, act: str, race_id: int):
        label, style = self.LABEL[act]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"horse:{act}:{race_id}"))
        self.act, self.race_id = act, race_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match):
        return cls(match["act"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        eco = interaction.client.economy.for_guild(interaction.guild_id)
        race = eco.race_get(self.race_id)
        if race is None:
            return await interaction.response.send_message(
                embed=ui.error_embed("見つかりません", "そのレースは、もうありません。"), ephemeral=True
            )
        if self.act == "card":
            return await interaction.response.send_message(embed=card_embed(eco, race), ephemeral=True)
        if race["status"] != "betting" or time.time() >= race["bet_close"]:
            return await interaction.response.send_message(
                embed=ui.error_embed("買えません", "このレースの馬券は、まだ（またはもう）買えません。"), ephemeral=True
            )
        panel = BetPanel(interaction.client, interaction.user.id, race)
        await interaction.response.send_message(embed=panel.embed(eco), view=panel, ephemeral=True)


def card_view(race_id: int, betting: bool) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(RaceButton("bet" if betting else "card", race_id))
    return view


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
            setattr(self, attr, interaction.data["values"][0])
            self._build()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        return callback

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def _on_use(self, interaction: discord.Interaction):
        eco = self.stable.eco(interaction)
        ok, result = eco.horse_gold_carrot(interaction.user.id, self.horse_id, self.first, self.second)
        if not ok:
            return await interaction.response.send_message(embed=ui.error_embed("使えません", result), ephemeral=True)
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
                embed=ui.error_embed("引退させられません", result), ephemeral=True
            )
        self.stop()
        remaining = eco.horse_list(interaction.user.id)
        self.stable.horse_id = remaining[0]["id"] if remaining else None  # 厩舎の画面で選んでいた馬は、もういない
        text = f"引退金 +{ui.pelica(result)}" if result >= 0 else f"引退費用 {ui.pelica(-result)}"
        await interaction.response.edit_message(
            embed=ui.ok_embed("引退しました", f"**{self.horse['name']}** は引退しました。{text}"), view=None
        )


# ---------------------------------------------------------------- #
# 馬券を買う
# ---------------------------------------------------------------- #
class StakeModal(discord.ui.Modal):
    def __init__(self, panel: "BetPanel"):
        super().__init__(title="馬券の金額")
        self.panel = panel
        many = panel.kind in horse_mod.MULTI_KINDS and len(panel.picked()) > 1
        self.stake = discord.ui.TextInput(
            label=("1頭あたりの" if many else "") + f"賭け金（{horse_mod.MIN_STAKE:,}〜{horse_mod.MAX_STAKE:,}ペリカ）",
            max_length=8,
        )
        self.add_item(self.stake)

    async def _submit_many(self, interaction: discord.Interaction, eco, lanes: list, stake: int) -> None:
        """単勝・複勝: 選んだ馬ごとに、同じ金額ずつ買う。"""
        ok, result = eco.race_bet_many(
            interaction.user.id, self.panel.race["id"], self.panel.kind, lanes, stake, time.time()
        )
        if not ok:
            return await interaction.response.send_message(
                embed=ui.error_embed("買えませんでした", result), ephemeral=True
            )
        await self.panel.announce_bets(interaction, result)
        lines = [
            f"{r['lane']}枠 {r['name']}　オッズ **{r['odds']:.1f}**（的中すれば {ui.pelica(horse_mod.payout(stake, r['odds']))}）"
            for r in result
        ]
        await interaction.response.send_message(
            embed=ui.ok_embed(
                "馬券を買いました",
                f"**{horse_mod.BET_KINDS[self.panel.kind]}**　各 {ui.pelica(stake)}（合計 {ui.pelica(stake * len(result))}）\n"
                + "\n".join(lines),
            ),
            ephemeral=True,
        )

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.stake.value.replace(",", "").strip()
        if not raw.isdigit():
            return await interaction.response.send_message(
                embed=ui.error_embed("金額が正しくありません", "半角の数字で入れてください。"), ephemeral=True
            )
        eco = self.panel.bot.economy.for_guild(interaction.guild_id)
        lanes = self.panel.picked()
        if self.panel.kind in horse_mod.MULTI_KINDS:
            return await self._submit_many(interaction, eco, lanes, int(raw))
        ok, result = eco.race_bet(
            interaction.user.id,
            self.panel.race["id"],
            self.panel.kind,
            lanes if self.panel.kind in horse_mod.COMBO_SIZE else lanes[0],
            int(raw),
            time.time(),
        )
        if not ok:
            return await interaction.response.send_message(
                embed=ui.error_embed("買えませんでした", result), ephemeral=True
            )
        picks = (
            "→".join(map(str, result["lanes"]))
            if result["kind"] == "exacta"
            else "・".join(map(str, sorted(result["lanes"])))
        )
        await self.panel.announce_bet(interaction, picks, result)
        await interaction.response.send_message(
            embed=ui.ok_embed(
                "馬券を買いました",
                f"**{horse_mod.BET_KINDS[result['kind']]}**　{picks}枠（{result['name']}）\n"
                f"賭け金 {ui.pelica(result['stake'])}　オッズ **{result['odds']:.1f}**"
                f"（的中すれば {ui.pelica(horse_mod.payout(result['stake'], result['odds']))}）",
            ),
            ephemeral=True,
        )


class BetPanel(discord.ui.View):
    """馬券の画面（買えるのは、開いた本人だけ）。種類を選び、馬を選んで、金額を入れる。

    単勝・複勝は何頭でも（選んだ頭ごとに、同じ金額ずつ別々の馬券になる）、馬連・ワイドは2頭、三連複は3頭（まとめて選ぶ）。馬単は、1着と2着を別々に選ぶ。
    """

    def __init__(self, bot, user_id: int, race: dict):
        super().__init__(timeout=300)
        self.bot, self.user_id, self.race = bot, user_id, race
        self.kind = "win"
        self.lanes = [1, 2, 3]  # 複数選ぶ馬券の選択（先頭から、必要な頭数だけ使う）
        self.multi = [1]  # 単勝・複勝で選んでいる馬（何頭でも）
        self._build(bot.economy.for_guild(race["guild_id"]))

    async def announce_bet(self, interaction: discord.Interaction, picks: str, result: dict) -> None:
        """買った馬券を、レースのチャンネルに公開する（誰が何に賭けたか）。投稿できなくても、購入は済んでいる。"""
        cog = self.bot.get_cog("HorseCog")
        eco = self.bot.economy.for_guild(interaction.guild_id)
        if cog is None or interaction.guild is None:
            return
        channel = await cog._channel(interaction.guild, eco, self.race)
        if channel is None:
            return
        await cog._post(
            channel,
            content=(
                f"🎫 <@{interaction.user.id}> が **{horse_mod.BET_KINDS[result['kind']]}** {picks}枠 "
                f"（{result['name']}）に {ui.pelica(result['stake'])}！　オッズ **{result['odds']:.1f}**"
            ),
        )

    async def announce_bets(self, interaction: discord.Interaction, results: list) -> None:
        """単勝・複勝をまとめて買ったとき、1つの投稿で公開する。"""
        cog = self.bot.get_cog("HorseCog")
        eco = self.bot.economy.for_guild(interaction.guild_id)
        if cog is None or interaction.guild is None or not results:
            return
        channel = await cog._channel(interaction.guild, eco, self.race)
        if channel is None:
            return
        parts = "、".join(f"{r['lane']}枠 {r['name']}（オッズ {r['odds']:.1f}）" for r in results)
        await cog._post(
            channel,
            content=(
                f"🎫 <@{interaction.user.id}> が **{horse_mod.BET_KINDS[results[0]['kind']]}** {parts} に"
                f"、各 {ui.pelica(results[0]['stake'])}！"
            ),
        )

    def picked(self) -> list:
        if self.kind in horse_mod.MULTI_KINDS:
            return sorted(self.multi)
        return self.lanes[: horse_mod.COMBO_SIZE.get(self.kind, 1)]

    def _options(self, entries: list, chosen) -> list:
        return [
            discord.SelectOption(
                label=f"{e['lane']}枠 {e['name']}"[:100],
                value=str(e["lane"]),
                description=f"{horse_mod.FORM_MARK[e['form']]} 単勝{e['odds_win']:.1f} 複勝{e['odds_place']:.1f}　{stat_short(e)}"[
                    :100
                ],
                default=e["lane"] in chosen,
            )
            for e in entries
        ]

    def _build(self, eco) -> None:
        self.clear_items()
        kind = discord.ui.Select(
            placeholder="馬券の種類",
            row=0,
            options=[
                discord.SelectOption(label=name, value=key, default=key == self.kind)
                for key, name in horse_mod.BET_KINDS.items()
            ],
        )
        kind.callback = self._on_kind
        self.add_item(kind)
        entries = eco.race_entries(self.race["id"])
        need = horse_mod.COMBO_SIZE.get(self.kind, 1)
        if self.kind == "exacta":
            for row, (label, i) in enumerate((("1着", 0), ("2着", 1)), start=1):
                sel = discord.ui.Select(
                    placeholder=f"{label}に来る馬", row=row, options=self._options(entries, [self.lanes[i]])
                )
                sel.callback = self._make_exacta(i)
                self.add_item(sel)
        elif need >= 2:
            sel = discord.ui.Select(
                placeholder=f"馬を {need} 頭選ぶ（順番は問いません）",
                row=1,
                min_values=need,
                max_values=need,
                options=self._options(entries, self.picked()),
            )
            sel.callback = self._on_many
            self.add_item(sel)
        else:
            sel = discord.ui.Select(
                placeholder="買う馬（何頭でも選べます。同じ金額ずつ賭けます）",
                row=1,
                min_values=1,
                max_values=max(1, len(entries)),
                options=self._options(entries, self.multi),
            )
            sel.callback = self._on_many_singles
            self.add_item(sel)
        go = discord.ui.Button(label="金額を入れて買う", style=discord.ButtonStyle.success, row=3)
        go.callback = self._on_go
        self.add_item(go)

    def embed(self, eco) -> discord.Embed:
        mine = eco.race_bets(self.user_id, self.race["id"])
        lanes = self.picked()
        lines = [
            f"{race_title(eco, self.race)}　発売は {when(self.race['bet_close'])} まで",
            "種類と馬を選んで、「金額を入れて買う」を押します。**賭けた時点のオッズで確定**します。",
            "",
        ]
        if self.kind in horse_mod.MULTI_KINDS:
            names = {e["lane"]: e["name"] for e in eco.race_entries(self.race["id"])}
            lines.append(
                f"選択中: **{horse_mod.BET_KINDS[self.kind]}**　{len(lanes)}頭（金額は、1頭ごとに同じだけ賭けます）"
            )
            for lane in lanes:
                odds = eco.race_odds(self.race["id"], self.kind, [lane])
                lines.append(f"　{lane}枠 {names.get(lane, '')}" + (f"　オッズ **{odds:.1f}**" if odds else ""))
        else:
            odds = eco.race_odds(self.race["id"], self.kind, lanes) if len(set(lanes)) == len(lanes) else None
            sep = "→" if self.kind == "exacta" else "・"
            shown = sep.join(map(str, lanes if self.kind == "exacta" else sorted(lanes)))
            lines.append(
                f"選択中: **{horse_mod.BET_KINDS[self.kind]}**　{shown}枠"
                + (f"　オッズ **{odds:.1f}**" if odds else "　（同じ馬は選べません）")
            )
        if mine:
            lines += ["", "**あなたの馬券**"] + [
                f"{horse_mod.BET_KINDS[b['kind']]} {b['picks'] or b['lane']}枠　{ui.pelica(b['stake'])}　オッズ {b['odds']:.1f}"
                for b in mine
            ]
        e = ui.embed("🎫 馬券を買う", "\n".join(lines), ui.GOLD)
        e.set_footer(
            text="単勝=1着 複勝=3着以内 馬連=1・2着(順不同) ワイド=3着以内の2頭 馬単=1・2着(順番) 三連複=1〜3着(順不同)"
        )
        return e

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("この画面を使えるのは、開いた本人だけです。", ephemeral=True)
            return False
        return True

    async def _redraw(self, interaction: discord.Interaction) -> None:
        eco = self.bot.economy.for_guild(self.race["guild_id"])
        self._build(eco)
        await interaction.response.edit_message(embed=self.embed(eco), view=self)

    async def _on_kind(self, interaction: discord.Interaction):
        self.kind = interaction.data["values"][0]
        await self._redraw(interaction)

    async def _on_many_singles(self, interaction: discord.Interaction):
        self.multi = [int(v) for v in interaction.data["values"]]
        await self._redraw(interaction)

    async def _on_many(self, interaction: discord.Interaction):
        self.lanes = [int(v) for v in interaction.data["values"]] + [0] * 3
        await self._redraw(interaction)

    def _make_exacta(self, index: int):
        async def callback(interaction: discord.Interaction):
            self.lanes[index] = int(interaction.data["values"][0])
            await self._redraw(interaction)

        return callback

    async def _on_go(self, interaction: discord.Interaction):
        await interaction.response.send_modal(StakeModal(self))


# ---------------------------------------------------------------- #
# 厩舎
# ---------------------------------------------------------------- #


class Stable(discord.ui.View):
    """`/競馬 厩舎` の画面。馬を選び、出走の申し込み・取り下げ・にんじん・新しい馬を、ボタンで行う。"""

    def __init__(self, bot, user_id: int, guild_id: int):
        super().__init__(timeout=600)
        self.bot, self.user_id, self.guild_id = bot, user_id, guild_id
        self.horse_id = None
        self.src, self.dst = "speed", "guts"
        eco = self.eco_for()
        eco.rental_ensure(user_id, time.time(), self._tz())
        horses = eco.horse_list(user_id)
        if horses:
            self.horse_id = horses[0]["id"]
        self._build()

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
            e.add_field(
                name=f"{mark}{h['silks']} {h['name']}（{h['coat']}）",
                value=f"{tag}{stat_lines(h)}\n戦績 {h['races']}/{horse_mod.MAX_RACES}戦 {h['wins']}勝（3着以内 {h['places']}）　賞金 {ui.pelica(h['earnings'])}{note}",
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

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "これは他の人の厩舎です。`/競馬 厩舎` で自分の厩舎を開けます。", ephemeral=True
            )
            return False
        return True

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _on_pick(self, interaction: discord.Interaction):
        self.horse_id = int(interaction.data["values"][0])
        await self._redraw(interaction)

    async def _on_src(self, interaction: discord.Interaction):
        self.src = interaction.data["values"][0]
        await self._redraw(interaction)

    async def _on_dst(self, interaction: discord.Interaction):
        self.dst = interaction.data["values"][0]
        await self._redraw(interaction)

    async def _on_carrot(self, interaction: discord.Interaction):
        ok, result = self.eco(interaction).horse_carrot(interaction.user.id, self.horse_id, self.src, self.dst)
        if not ok:
            return await interaction.response.send_message(embed=ui.error_embed("動かせません", result), ephemeral=True)
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
                embed=ui.error_embed("申し込めません", reason), ephemeral=True
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
                embed=ui.error_embed("取り下げられません", reason), ephemeral=True
            )
        await self._redraw(interaction)
        await refresh_entry_card(interaction, eco, race["id"])

    async def _on_buy_rental(self, interaction: discord.Interaction):
        eco = self.eco(interaction)
        ok, result = eco.horse_buy_rental(interaction.user.id, time.time(), self._tz())
        if not ok:
            return await interaction.response.send_message(embed=ui.error_embed("買えません", result), ephemeral=True)
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
