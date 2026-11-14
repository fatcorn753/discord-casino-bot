"""ブラックジャック。ヒット・スタンド・ダブルダウンをボタンで選ぶ。"""

import random

import discord

from core import catalogs, ui
from core.confirm import confirm_action
from games import blackjack as rules
from play.common import (
    GameContext,
    OwnerView,
    ReplayView,
    game_title,
    log_display_failure,
    result_lines,
    signed,
    start_game,
)

_BJ_CFG = catalogs.games().blackjack
_COLORS = {"ok": ui.OK, "info": ui.INFO, "warn": ui.WARN, "error": ui.ERROR, "gold": ui.GOLD, "dark": ui.DARK}

RNG = random.SystemRandom()
TITLE = _BJ_CFG.title

RESULT_TEXT = {k: (v.text, _COLORS[v.color]) for k, v in _BJ_CFG.result_text.items()}


def flair(res: str, g) -> str:
    """ブラックジャックの勝ち方に応じた一言（大勝ちの演出は倍率で決まるので、ここでは勝ち方を祝う）。"""
    if res == "blackjack":
        return _BJ_CFG.flair.blackjack
    if res == "win" and g.doubled:
        return _BJ_CFG.flair.doubled
    if res == "win" and rules.value(g.player) == 21 and len(g.player) >= 3:
        return _BJ_CFG.flair.twenty_one
    return ""


async def play_blackjack(interaction: discord.Interaction, bot, bet: int):
    ctx = await start_game(interaction, bot, bet, "ブラックジャック")
    if ctx is None:
        return
    try:
        game = rules.Game(RNG)
        view = BlackjackView(ctx, interaction, game)
        if game.finished:  # 最初の2枚で決着（ナチュラルブラックジャック）
            await interaction.response.send_message(embed=view.final_embed(), view=view.replay_view())
        else:
            await interaction.response.send_message(embed=view.embed(), view=view)
    except Exception:
        ctx.abort()
        raise


class BlackjackView(OwnerView):
    def __init__(self, ctx: GameContext, origin: discord.Interaction, game: rules.Game):
        super().__init__(ctx, timeout=90)
        self.origin = origin
        self.base_bet = ctx.bet
        self.game = game
        self._final = None  # 精算した結果の表示。精算は一度きりなので、2回目以降はこれを返す

    def embed(self) -> discord.Embed:
        g = self.game
        hole = f"{rules.hand_text(g.dealer[:1])} `？`"
        body = "\n".join(
            [
                f"賭け金: **{ui.pelica(self.ctx.bet)}**",
                f"\n**ディーラー**  {hole}",
                f"**あなた**  {rules.hand_text(g.player)}（**{rules.value(g.player)}**）",
            ]
        )
        return ui.embed(game_title(TITLE, self.ctx), body, ui.INFO)

    def replay_view(self) -> ReplayView:
        """結果の下に付ける「もう一度」ボタン（ダブルで増えた賭け金ではなく、最初の賭け金）。"""
        view = ReplayView(
            self.ctx.bot, self.ctx.user_id, self.base_bet, lambda it, b: play_blackjack(it, self.ctx.bot, b)
        )
        view.origin = self.origin
        return view

    def final_embed(self) -> discord.Embed:
        if self._final is not None:
            return self._final
        g = self.game
        res = g.result()
        bet = self.ctx.economy.escrow_amount(self.ctx.escrow_id) or self.ctx.bet
        self.ctx.bet = bet
        payout = rules.payout(res, bet)
        won = res in ("win", "blackjack")
        tags = set()
        if won and g.doubled:
            tags.add("double_win")
        if res == "blackjack":
            tags.add("natural")
        net = self.ctx.settle(payout, tags=tags)
        text, color = RESULT_TEXT[res]
        if res == "win" and rules.value(g.dealer) > 21:
            text = "ディーラーがバースト！ 勝ち🎉"
        body = "\n".join(
            [
                f"**ディーラー**  {rules.hand_text(g.dealer)}（**{rules.value(g.dealer)}**）",
                f"**あなた**  {rules.hand_text(g.player)}（**{rules.value(g.player)}**）"
                + ("　ダブルダウン" if g.doubled else ""),
                f"\n**{text}**",
                result_lines(self.ctx, payout, net, flair(res, g)),
            ]
        )
        if won and g.doubled:
            color = ui.GOLD
        self._final = ui.embed(f"{game_title(TITLE, self.ctx)} — {signed(net)}ペリカ", body, color)
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
            await interaction.response.edit_message(embed=self.final_embed(), view=self.replay_view())
        else:
            self.double_button.disabled = True  # ダブルは最初の判断のときだけ
            await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _after_origin(self) -> None:
        """_after の、確認ボタンを挟んだあと版（もとのメッセージを書き換える）。"""
        if self.game.finished:
            self.stop()
            await self.origin.edit_original_response(embed=self.final_embed(), view=self.replay_view())
        else:
            self.double_button.disabled = True
            await self.origin.edit_original_response(embed=self.embed(), view=self)

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
        if (
            len(self.game.player) != 2 or self.game.doubled
        ):  # 最初の2枚のときだけ。無効化の表示は古い画面には届かないので、ここでも断る
            return await interaction.response.send_message(
                embed=ui.error_embed("ダブルできません", "ダブルは、最初の2枚のときだけ選べます。"), ephemeral=True
            )
        eco = self.ctx.economy
        if not eco.add_to_escrow(self.ctx.escrow_id, self.ctx.bet):
            need = self.ctx.bet - eco.balance(interaction.user.id)
            can, why = eco.can_borrow_for_bet(interaction.user.id, need)
            if not can:
                return await interaction.response.send_message(
                    embed=ui.error_embed("ダブルできません", f"倍にするためのペリカが足りません。{why}"), ephemeral=True
                )
            agreed = await confirm_action(
                interaction,
                "帝愛の信用でダブルしますか？",
                f"手元が **{need:,}ペリカ** 足りません。足りない分を帝愛から借りて、倍賭けします。\n"
                "**借金になり、🟡地下労働者に落ちます**（デイリーのピンハネ・収入の天引きが始まります）。",
                "借りてダブル",
            )
            if (  # 確認を待つ間に、ゲームが進んだり終わったりしていたら、何もしない
                agreed is not True
                or self.ctx.done
                or self.game.finished
                or self.game.doubled
                or len(self.game.player) != 2
            ):
                return
            if not eco.add_to_escrow(self.ctx.escrow_id, self.ctx.bet, credit=True):
                return await interaction.followup.send(
                    embed=ui.error_embed("ダブルできません", "帝愛から借りられませんでした。"), ephemeral=True
                )
            self.ctx.bet = eco.escrow_amount(self.ctx.escrow_id)
            self.game.double()
            return await self._after_origin()
        self.ctx.bet = eco.escrow_amount(self.ctx.escrow_id)
        self.game.double()
        await self._after(interaction)

    async def on_timeout(self) -> None:
        """放置したらスタンド扱いで精算する。"""
        if self.ctx.done:
            return
        self.game.stand()
        try:
            await self.origin.edit_original_response(embed=self.final_embed(), view=self.replay_view())
        except discord.HTTPException as exc:
            log_display_failure("ブラックジャック", "時間切れの表示", exc)  # 表示に失敗しても、精算は済んでいる
