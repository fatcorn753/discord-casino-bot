"""ゲーム共通の流れ: 賭け金の検証と預かり、精算、ボタンの持ち主確認、結果の表示。"""

import discord

from core import ui

MIN_BET = 10
MAX_BET = 100_000


class GameContext:
    """1回のゲーム。賭け金は預かり済みで、settle() か abort() で必ず精算する。"""

    def __init__(self, bot, user_id: int, escrow_id: int, game: str, bet: int):
        self.bot = bot
        self.user_id = user_id
        self.escrow_id = escrow_id
        self.game = game
        self.bet = bet
        self.done = False
        self.penalty_taken = 0       # チンチロの倍払いなどで、預かり金のほかに引いた額
        self.insured_refund = 0      # 保険券で戻った額

    @property
    def economy(self):
        return self.bot.economy

    def settle(self, payout: int, penalty: int = 0, keep_session: bool = False) -> int:
        """払い戻し総額で精算し、損益を返す。二重に呼んでも一度しか払わない。

        penalty は倍払いなどの追加の支払い（残高の範囲まで）。精算と同じ1回の処理で行う。
        keep_session=True なら、演出が終わるまで「プレイ中」のままにする（release() で解除）。
        """
        if self.done:
            return 0
        try:
            result = self.economy.settle_with_penalty(self.escrow_id, payout, self.game, penalty)
        except Exception:
            self.bot.sessions.end(self.user_id)       # 精算に失敗してもロックしたままにしない（返金は起動時に行われる）
            raise
        self.done = True
        if not keep_session:
            self.bot.sessions.end(self.user_id)
        if result is None:
            print(f"警告: 精算済みの預かり金を再度精算しようとしました（{self.game}, escrow={self.escrow_id}）")
            return 0
        net, self.penalty_taken, self.insured_refund = result
        return net

    def release(self) -> None:
        """keep_session=True で精算したあと、演出が終わったら呼ぶ。"""
        self.bot.sessions.end(self.user_id)

    def abort(self) -> None:
        """エラー時: まだ精算していなければ、預かっている賭け金（追加分も含む）を全額返す。"""
        if self.done:
            return
        self.done = True
        try:
            self.economy.refund(self.escrow_id)
        finally:
            self.bot.sessions.end(self.user_id)


async def start_game(interaction: discord.Interaction, bot, bet: int, game: str, balance_factor: int = 1,
                     factor_reason: str = ""):
    """賭け金を検証して預かる。成功すれば GameContext、失敗すれば理由を返信して None。

    balance_factor: 開始時に「賭け金の何倍の残高」が必要か（チンチロの倍払いに備える）。
    """
    uid = interaction.user.id
    reason = bot.sessions.try_start(uid)
    if reason:
        await interaction.response.send_message(embed=ui.error_embed("まだ始められません", reason), ephemeral=True)
        return None
    escrow = None
    try:
        if not MIN_BET <= bet <= MAX_BET:
            await interaction.response.send_message(
                embed=ui.error_embed("賭け金が範囲外です", f"{ui.pelica(MIN_BET)} 〜 {ui.pelica(MAX_BET)} の間で指定してください。"), ephemeral=True)
            return None
        balance = bot.economy.balance(uid)
        if balance < bet * balance_factor:
            if balance_factor > 1 and balance >= bet:
                msg = f"{factor_reason}賭け金の **{balance_factor}倍**（{ui.pelica(bet * balance_factor)}）以上の残高が必要です（いまの残高: {ui.pelica(balance)}）。賭け金を減らしてください。"
            else:
                msg = f"賭け金 {ui.pelica(bet)} に対して、残高は {ui.pelica(balance)} です。\n`/ペリカ デイリー` で毎日もらえます。"
            await interaction.response.send_message(embed=ui.error_embed("ペリカが足りません", msg), ephemeral=True)
            return None
        escrow = bot.economy.place_bet(uid, bet, game)
        if escrow is None:
            await interaction.response.send_message(
                embed=ui.error_embed("ペリカが足りません", f"賭け金 {ui.pelica(bet)} に対して、残高は {ui.pelica(bot.economy.balance(uid))} です。"), ephemeral=True)
            return None
        return GameContext(bot, uid, escrow, game, bet)
    finally:
        if escrow is None:
            bot.sessions.end(uid)       # 始められなかった場合は、プレイ中のままにしない


def signed(n: int) -> str:
    return f"+{n:,}" if n > 0 else f"{n:,}"


def result_lines(ctx: GameContext, payout: int, net: int, extra: str = "") -> str:
    """結果の共通表示（賭け金・払い戻し・損益・残高）。"""
    balance = ctx.economy.balance(ctx.user_id)
    lines = [f"賭け金: **{ui.pelica(ctx.bet)}**", f"払い戻し: **{ui.pelica(payout + ctx.insured_refund)}**",
             f"損益: **{signed(net)}ペリカ**", f"残高: **{ui.pelica(balance)}**"]
    if ctx.insured_refund:
        lines.insert(0, f"🎟️ 保険券を使った！ 賭け金の半分（{ui.pelica(ctx.insured_refund)}）が戻った")
    if extra:
        lines.insert(0, extra)
    return "\n".join(lines)


class OwnerView(discord.ui.View):
    """賭けた本人だけが押せるボタン。エラーや時間切れでも、賭け金を失わないようにする。"""

    def __init__(self, ctx: GameContext, timeout: float = 90):
        super().__init__(timeout=timeout)
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.user_id:
            await interaction.response.send_message("これは他の人のゲームです。`/カジノ` で自分も遊べます。", ephemeral=True)
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        print(f"ゲーム中のエラー: {self.ctx.game}: {error!r}")
        self.ctx.abort()   # 賭け金を返す
        try:
            await interaction.channel.send(embed=ui.error_embed("エラーが発生しました", "ゲームを中止しました。精算前なら賭け金はお返ししています（`/ペリカ 履歴` で確認できます）。"))
        except discord.HTTPException:
            pass
        self.stop()
