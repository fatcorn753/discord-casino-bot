"""競馬の馬券購入パネル（BetPanel）と賭け金モーダル（StakeModal）。"""

from collections.abc import Awaitable, Callable
from typing import Optional

import discord

from cogs.horse.cards import race_title, stat_short, when
from cogs.shared import ui
from cogs.shared.amount import reject_bad_amount, typed_amount
from cogs.shared.messages import format_failure
from domain import horse as horse_mod
from domain.errors import Failure
from domain.ports.contracts import RaceBetResult, RaceRow
from services.horse import HorseDesk, HorseService

PostBetFn = Callable[[discord.Guild, RaceRow, str], Awaitable[None]]


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

    async def _submit_many(self, interaction: discord.Interaction, desk: HorseDesk, lanes: list, stake: int) -> None:
        """単勝・複勝: 選んだ馬ごとに、同じ金額ずつ買う。"""
        ok, result = desk.race_bet_many(
            interaction.user.id, self.panel.race["id"], self.panel.kind, lanes, stake, desk.clock.now_ts()
        )
        if not ok:
            await ui.reply_error(interaction, format_failure(result), title="買えませんでした")
            return
        assert not isinstance(result, Failure)
        await self.panel.announce_bets(interaction, result)
        lines = [
            f"{r['lane']}枠 {r['name']}　オッズ **{r['odds']:.1f}**（的中すれば {ui.pelica(horse_mod.payout(stake, r['odds']))}）"
            for r in result
        ]
        await ui.reply_ok(
            interaction,
            f"**{horse_mod.BET_KINDS[self.panel.kind]}**　各 {ui.pelica(stake)}（合計 {ui.pelica(stake * len(result))}）\n"
            + "\n".join(lines),
            title="馬券を買いました",
        )

    async def on_submit(self, interaction: discord.Interaction):
        stake = typed_amount(self.stake.value)
        if stake is None:
            await reject_bad_amount(interaction)
            return
        desk = self.panel.desk_for(ui.guild_id_of(interaction))
        lanes = self.panel.picked()
        if self.panel.kind in horse_mod.MULTI_KINDS:
            await self._submit_many(interaction, desk, lanes, stake)
            return
        ok, result = desk.race_bet(
            interaction.user.id,
            self.panel.race["id"],
            self.panel.kind,
            lanes if self.panel.kind in horse_mod.COMBO_SIZE else lanes[0],
            stake,
            desk.clock.now_ts(),
        )
        if not ok:
            await ui.reply_error(interaction, format_failure(result), title="買えませんでした")
            return
        assert not isinstance(result, Failure)
        picks = (
            "→".join(map(str, result["lanes"]))
            if result["kind"] == "exacta"
            else "・".join(map(str, sorted(result["lanes"])))
        )
        await self.panel.announce_bet(interaction, picks, result)
        await ui.reply_ok(
            interaction,
            f"**{horse_mod.BET_KINDS[result['kind']]}**　{picks}枠（{result['name']}）\n"
            f"賭け金 {ui.pelica(result['stake'])}　オッズ **{result['odds']:.1f}**"
            f"（的中すれば {ui.pelica(horse_mod.payout(result['stake'], result['odds']))}）",
            title="馬券を買いました",
        )


class BetPanel(ui.OwnerView):
    """馬券の画面（買えるのは、開いた本人だけ）。種類を選び、馬を選んで、金額を入れる。"""

    def __init__(
        self,
        horse_svc: HorseService,
        user_id: int,
        race: RaceRow,
        post_bet_fn: Optional[PostBetFn] = None,
    ) -> None:
        super().__init__(user_id, timeout=300)
        self._horse_svc = horse_svc
        self._post_bet = post_bet_fn
        self.user_id, self.race = user_id, race
        self.kind = "win"
        self.lanes = [1, 2, 3]  # 複数選ぶ馬券の選択（先頭から、必要な頭数だけ使う）
        self.multi = [1]  # 単勝・複勝で選んでいる馬（何頭でも）
        self._build(self.desk_for(race["guild_id"]))

    def desk_for(self, guild_id: int) -> HorseDesk:
        return self._horse_svc.desk(guild_id)

    async def _reject_non_owner(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("この画面を使えるのは、開いた本人だけです。", ephemeral=True)

    async def announce_bet(self, interaction: discord.Interaction, picks: str, result: RaceBetResult) -> None:
        """買った馬券を、レースのチャンネルに公開する（誰が何に賭けたか）。投稿できなくても、購入は済んでいる。"""
        if self._post_bet is None or interaction.guild is None:
            return
        await self._post_bet(
            interaction.guild,
            self.race,
            (
                f"🎫 <@{interaction.user.id}> が **{horse_mod.BET_KINDS[result['kind']]}** {picks}枠 "
                f"（{result['name']}）に {ui.pelica(result['stake'])}！　オッズ **{result['odds']:.1f}**"
            ),
        )

    async def announce_bets(self, interaction: discord.Interaction, results: list[RaceBetResult]) -> None:
        """単勝・複勝をまとめて買ったとき、1つの投稿で公開する。"""
        if self._post_bet is None or interaction.guild is None or not results:
            return
        parts = "、".join(f"{r['lane']}枠 {r['name']}（オッズ {r['odds']:.1f}）" for r in results)
        await self._post_bet(
            interaction.guild,
            self.race,
            (
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

    def _build(self, desk: HorseDesk) -> None:
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
        entries = desk.race_entries(self.race["id"])
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

    def embed(self, desk: HorseDesk) -> discord.Embed:
        mine = desk.race_bets(self.user_id, self.race["id"])
        lanes = self.picked()
        lines = [
            f"{race_title(desk, self.race)}　発売は {when(self.race['bet_close'], desk.clock.now_ts())} まで",
            "種類と馬を選んで、「金額を入れて買う」を押します。**賭けた時点のオッズで確定**します。",
            "",
        ]
        if self.kind in horse_mod.MULTI_KINDS:
            names = {e["lane"]: e["name"] for e in desk.race_entries(self.race["id"])}
            lines.append(
                f"選択中: **{horse_mod.BET_KINDS[self.kind]}**　{len(lanes)}頭（金額は、1頭ごとに同じだけ賭けます）"
            )
            for lane in lanes:
                odds = desk.race_odds(self.race["id"], self.kind, [lane])
                lines.append(f"　{lane}枠 {names.get(lane, '')}" + (f"　オッズ **{odds:.1f}**" if odds else ""))
        else:
            odds = desk.race_odds(self.race["id"], self.kind, lanes) if len(set(lanes)) == len(lanes) else None
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

    async def _redraw(self, interaction: discord.Interaction) -> None:
        desk = self.desk_for(self.race["guild_id"])
        self._build(desk)
        await interaction.response.edit_message(embed=self.embed(desk), view=self)

    async def _on_kind(self, interaction: discord.Interaction):
        self.kind = ui.selected_values(interaction)[0]
        await self._redraw(interaction)

    async def _on_many_singles(self, interaction: discord.Interaction):
        self.multi = [int(v) for v in ui.selected_values(interaction)]
        await self._redraw(interaction)

    async def _on_many(self, interaction: discord.Interaction):
        self.lanes = [int(v) for v in ui.selected_values(interaction)] + [0] * 3
        await self._redraw(interaction)

    def _make_exacta(self, index: int):
        async def callback(interaction: discord.Interaction):
            self.lanes[index] = int(ui.selected_values(interaction)[0])
            await self._redraw(interaction)

        return callback

    async def _on_go(self, interaction: discord.Interaction):
        await interaction.response.send_modal(StakeModal(self))
