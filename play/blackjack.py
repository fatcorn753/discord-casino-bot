"""ブラックジャック。ヒット・スタンド・ダブルダウンをボタンで選ぶ。"""

import random

import discord

from core import ui
from games import blackjack as rules
from play.common import GameContext, OwnerView, result_lines, signed, start_game

RNG = random.SystemRandom()
TITLE = "🃏 ブラックジャック"

RESULT_TEXT = {
    "blackjack": ("ブラックジャック！ 3:2 で払い戻し🎉", ui.GOLD),
    "win": ("勝ち🎉", ui.OK),
    "push": ("引き分け（賭け金が戻る）", ui.INFO),
    "lose": ("負け…", ui.ERROR),
    "bust": ("バースト…（21を超えた）", ui.ERROR),
}


async def play_blackjack(interaction: discord.Interaction, bot, bet: int):
    ctx = await start_game(interaction, bot, bet, "ブラックジャック")
    if ctx is None:
        return
    try:
        game = rules.Game(RNG)
        view = BlackjackView(ctx, interaction, game)
        if game.finished:        # 最初の2枚で決着（ナチュラルブラックジャック）
            await interaction.response.send_message(embed=view.final_embed())
        else:
            await interaction.response.send_message(embed=view.embed(), view=view)
    except Exception:
        ctx.abort()
        raise


class BlackjackView(OwnerView):
    def __init__(self, ctx: GameContext, origin: discord.Interaction, game: rules.Game):
        super().__init__(ctx, timeout=90)
        self.origin = origin
        self.game = game
        self._final = None      # 精算した結果の表示。精算は一度きりなので、2回目以降はこれを返す

    def embed(self) -> discord.Embed:
        g = self.game
        hole = f"{rules.hand_text(g.dealer[:1])} `？`"
        body = "\n".join([
            f"賭け金: **{ui.pelica(self.ctx.bet)}**",
            f"\n**ディーラー**  {hole}",
            f"**あなた**  {rules.hand_text(g.player)}（**{rules.value(g.player)}**）",
        ])
        return ui.embed(TITLE, body, ui.INFO)

    def final_embed(self) -> discord.Embed:
        if self._final is not None:
            return self._final
        g = self.game
        res = g.result()
        bet = self.ctx.economy.escrow_amount(self.ctx.escrow_id) or self.ctx.bet
        self.ctx.bet = bet
        payout = rules.payout(res, bet)
        net = self.ctx.settle(payout)
        text, color = RESULT_TEXT[res]
        if res == "win" and rules.value(g.dealer) > 21:
            text = "ディーラーがバースト！ 勝ち🎉"
        body = "\n".join([
            f"**ディーラー**  {rules.hand_text(g.dealer)}（**{rules.value(g.dealer)}**）",
            f"**あなた**  {rules.hand_text(g.player)}（**{rules.value(g.player)}**）" + ("　ダブルダウン" if g.doubled else ""),
            f"\n**{text}**",
            result_lines(self.ctx, payout, net),
        ])
        self._final = ui.embed(f"{TITLE} — {signed(net)}ペリカ", body, color)
        return self._final

    async def _guard(self, interaction: discord.Interaction) -> bool:
        """ほぼ同時に2回押されたり、精算済みのゲームに押されたときは、何もしない。"""
        if self.ctx.done or self.game.finished:
            await interaction.response.defer()
            return True
        return False

    async def _after(self, interaction: discord.Interaction) -> None:
        if self.game.finished:
            self.stop()
            await interaction.response.edit_message(embed=self.final_embed(), view=None)
        else:
            self.double_button.disabled = True      # ダブルは最初の判断のときだけ
            await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="ヒット（もう1枚）", style=discord.ButtonStyle.primary)
    async def hit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._guard(interaction):
            return
        self.game.hit()
        await self._after(interaction)

    @discord.ui.button(label="スタンド（勝負）", style=discord.ButtonStyle.success)
    async def stand_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._guard(interaction):
            return
        self.game.stand()
        await self._after(interaction)

    @discord.ui.button(label="ダブル（倍賭けして1枚）", style=discord.ButtonStyle.danger)
    async def double_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._guard(interaction):
            return
        if len(self.game.player) != 2 or self.game.doubled:      # 最初の2枚のときだけ。無効化の表示は古い画面には届かないので、ここでも断る
            return await interaction.response.send_message(
                embed=ui.error_embed("ダブルできません", "ダブルは、最初の2枚のときだけ選べます。"), ephemeral=True)
        if not self.ctx.economy.add_to_escrow(self.ctx.escrow_id, self.ctx.bet):
            return await interaction.response.send_message(
                embed=ui.error_embed("ダブルできません", "倍にするためのペリカが足りません。"), ephemeral=True)
        self.ctx.bet = self.ctx.economy.escrow_amount(self.ctx.escrow_id)
        self.game.double()
        await self._after(interaction)

    async def on_timeout(self) -> None:
        """放置したらスタンド扱いで精算する。"""
        if self.ctx.done:
            return
        self.game.stand()
        try:
            await self.origin.edit_original_response(embed=self.final_embed(), view=None)
        except discord.HTTPException:
            pass
