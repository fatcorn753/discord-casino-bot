"""チンチロ（地下カジノ風）。親（ディーラー）が先に振り、そのあと子（あなた）がボタンで振る。

世界観は「賭博もの」の雰囲気（ざわ…ざわ…）。原作のセリフそのものは使わず、雰囲気の言い回しだけにしている。
"""

import asyncio
import random

import discord

from core import ui
from games import chinchiro as rules
from play.common import GameContext, OwnerView, ReplayView, game_title, result_lines, signed, start_game

RNG = random.SystemRandom()
TITLE = "🎲 地下チンチロ"
STEP = 0.9

WIN_LINE = {
    rules.PINZORO: "ピンゾロ…ッ！ 5倍…！ 場の空気が凍りつく…",
    rules.ARASHI: "嵐…ッ！ 3倍…！ ざわ…ざわ…",
    rules.SHIGORO: "シゴロ…！ 2倍…。流れが来ている…",
    rules.ME: "目で押し切った…！ 静かな勝利…",
    rules.MENASHI: "相手のヒフミに救われた…！",
}
LOSE_LINE = {
    rules.PINZORO: "親のピンゾロ…！ 5倍払い…。血の気が引いていく…",
    rules.ARASHI: "親の嵐…！ 3倍払い…。地下に悲鳴が響く…",
    rules.SHIGORO: "親のシゴロ…2倍払い…。",
    rules.ME: "親の目が上だった…。",
    rules.MENASHI: "目なし同士は親の勝ち…。それが地下のルール…",
    rules.HIFUMI: "ヒフミ…ッ！ 倍払い…！ 地獄だ…",
}


def _rolls_text(rolls: list, who: str) -> str:
    lines = []
    for i, (dice, hand) in enumerate(rolls, 1):
        again = "　→ 振り直し" if hand.kind == rules.MENASHI and i < rules.MAX_ROLLS else ""
        lines.append(f"{i}回目: {rules.dice_text(dice)} **{hand.name}**{again}")
    return f"**{who}**\n" + "\n".join(lines)


def _rolling_text() -> str:
    return rules.dice_text([RNG.randint(1, 6) for _ in range(3)]) + "　カラカラカラ…"


async def play_chinchiro(interaction: discord.Interaction, bot, bet: int):
    # 負けたときは最大5倍払い。賭け金のほかに払う分を確実に払えるよう、残高が賭け金の5倍以上ある人だけ遊べる
    ctx = await start_game(interaction, bot, bet, "チンチロ", balance_factor=rules.ROLE_MULT[rules.PINZORO],
                           factor_reason="チンチロは最大5倍払いになるため、")
    if ctx is None:
        return
    try:
        dealer_rolls = rules.play_hand(RNG)
        await interaction.response.send_message(embed=ui.embed(
            game_title(TITLE, ctx), f"賭け金: **{ui.pelica(bet)}**\n\nざわ… ざわ…\n親（ディーラー）が丼にサイコロを放る…", ui.DARK))
    except Exception:
        ctx.abort()
        raise
    try:
        shown = []
        for dice, hand in dealer_rolls:
            await asyncio.sleep(STEP)
            await interaction.edit_original_response(embed=ui.embed(
                game_title(TITLE, ctx), ((_rolls_text(shown, "親") + "\n\n") if shown else "") + _rolling_text(), ui.DARK))
            await asyncio.sleep(STEP)
            shown.append((dice, hand))
            await interaction.edit_original_response(embed=ui.embed(game_title(TITLE, ctx), _rolls_text(shown, "親"), ui.DARK))
        view = ChinchiroView(ctx, interaction, dealer_rolls)
        await asyncio.sleep(STEP)
        await interaction.edit_original_response(embed=view.embed(), view=view)
    except discord.NotFound:
        # 親の出目が見えたあとにメッセージを消して、賭けや倍払いを無かったことにするのを防ぐ（目なしで振ったものとして精算）
        payout, extra = rules.payout(bet, rules.compare(rules.Hand(rules.MENASHI), dealer_rolls[-1][1]))
        ctx.settle(payout, penalty=extra)
    except Exception:
        ctx.abort()
        raise


class ChinchiroView(OwnerView):
    def __init__(self, ctx: GameContext, origin: discord.Interaction, dealer_rolls: list):
        super().__init__(ctx, timeout=60)
        self.origin = origin
        self.base_bet = ctx.bet
        self.dealer_rolls = dealer_rolls
        self.player_rolls = []
        self._busy = False
        self._final = None      # 精算した結果の表示。精算は一度きりなので、2回目以降はこれを返す

    # --------------------------- 表示 --------------------------- #
    def embed(self, rolling: bool = False) -> discord.Embed:
        parts = [f"賭け金: **{ui.pelica(self.ctx.bet)}**", _rolls_text(self.dealer_rolls, "親")]
        if self.player_rolls:
            parts.append(_rolls_text(self.player_rolls, "あなた"))
        if rolling:
            parts.append(_rolling_text())
        else:
            left = rules.MAX_ROLLS - len(self.player_rolls)
            hint = "あなたの番。ボタンで振る（目が出なければ振り直せる）" if not self.player_rolls else f"目なし…！ あと **{left}回** 振れる"
            parts.append(f"ざわ…ざわ…\n{hint}")
        return ui.embed(game_title(TITLE, self.ctx), "\n\n".join(parts), ui.DARK)

    def replay_view(self) -> ReplayView:
        """結果の下に付ける「もう一度」ボタン。"""
        view = ReplayView(self.ctx.bot, self.ctx.user_id, self.base_bet, lambda it, b: play_chinchiro(it, self.ctx.bot, b))
        view.origin = self.origin
        return view

    def final_embed(self) -> discord.Embed:
        """精算して結果を作る。タイムアウトとボタン操作が重なっても、精算は一度だけ。"""
        if self._final is not None:
            return self._final
        player = self.player_rolls[-1][1] if self.player_rolls else rules.Hand(rules.MENASHI)
        dealer = self.dealer_rolls[-1][1]
        outcome = rules.compare(player, dealer)
        payout, extra = rules.payout(self.ctx.bet, outcome)
        # 負けの倍払い（預かり金で足りない分）は、精算と同じ1回の処理で、残高の範囲まで引く
        net = self.ctx.settle(payout, penalty=extra)
        taken = self.ctx.penalty_taken
        if outcome.result == "win":
            line, color = WIN_LINE.get(player.kind, WIN_LINE[rules.ME]), ui.OK
            if dealer.kind == rules.HIFUMI:
                line = "親がヒフミ…！ 倍もらう…！"
        elif outcome.result == "lose":
            key = rules.HIFUMI if player.kind == rules.HIFUMI else dealer.kind
            line, color = LOSE_LINE.get(key, LOSE_LINE[rules.ME]), ui.ERROR
            if extra and taken < extra:
                line += "\n全財産を持っていかれた…"
        else:
            line, color = "引き分け…。ざわ……何も動かない…", ui.INFO
        mult = f"（{outcome.multiplier}倍）" if outcome.result != "draw" and outcome.multiplier > 1 else ""
        body = "\n\n".join([
            _rolls_text(self.dealer_rolls, "親"),
            _rolls_text(self.player_rolls, "あなた") if self.player_rolls else "**あなた**\n時間切れ…（目なし扱い）",
            f"**{line}**{mult}",
            result_lines(self.ctx, payout, net),
        ])
        self._final = ui.embed(f"{game_title(TITLE, self.ctx)} — {signed(net)}ペリカ", body, color)
        return self._final

    # --------------------------- 操作 --------------------------- #
    @discord.ui.button(label="🎲 サイコロを振る", style=discord.ButtonStyle.danger)
    async def roll(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._busy or self.ctx.done:
            return await interaction.response.send_message("処理中です。少し待ってください。", ephemeral=True)
        self._busy = True
        try:
            await self._roll(interaction)
        except discord.NotFound:
            # 振っている途中でメッセージを消されても、いまの出目で精算する（消して返金を受ける抜けを防ぐ）
            self.stop()
            self.final_embed()
        finally:
            self._busy = False

    async def _roll(self, interaction: discord.Interaction):
        dice = rules.roll_dice(RNG)
        hand = rules.classify(dice)
        await interaction.response.edit_message(embed=self.embed(rolling=True), view=None)
        await asyncio.sleep(STEP)
        if self.ctx.done:        # 振っている間に時間切れで精算された
            return await interaction.edit_original_response(embed=self.final_embed(), view=self.replay_view())
        self.player_rolls.append((dice, hand))
        if hand.kind == rules.MENASHI and len(self.player_rolls) < rules.MAX_ROLLS:
            await interaction.edit_original_response(embed=self.embed(), view=self)
        else:
            self.stop()
            await interaction.edit_original_response(embed=self.final_embed(), view=self.replay_view())

    async def on_timeout(self) -> None:
        """振らずに放置したら、目なし扱いで精算する（賭け金を預かったままにしない）。"""
        if self.ctx.done:
            return
        try:
            await self.origin.edit_original_response(embed=self.final_embed(), view=self.replay_view())
        except discord.HTTPException:
            pass   # 表示の更新に失敗しても、精算は final_embed の中で済んでいる
