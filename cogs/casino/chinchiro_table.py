"""チンチロの卓（`/カジノ チンチロ卓`）の Discord の投稿。卓の進め方は cogs/casino/chinchiro_engine.py。

投稿は、卓の状態が変わるたびに書き換える（卓の listeners に登録する）。ボタンは、押した人の操作を卓に渡すだけ。
"""

import logging

import discord

from cogs.casino import chinchiro_engine as engine
from cogs.casino.chinchiro_engine import ChinchiroTable
from cogs.casino.common import BeginFailure, CasinoDeps, signed, start_game
from cogs.casino.texts import CHINCHIRO_TITLE, dice_text
from cogs.shared import ui
from domain.games import chinchiro as rules

_log = logging.getLogger(__name__)

TITLE = f"{CHINCHIRO_TITLE}の卓"


def _hand_line(rolls: engine.Rolls) -> str:
    """振った目を1行で（目なしの振り直しも並べる）。"""
    return " → ".join(f"{dice_text(dice)} **{hand.name}**" for dice, hand in rolls)


def _pot_text(table: ChinchiroTable) -> str:
    return f"賭け金: **{ui.pelica(table.bet)}**（全員同じ）　場代: 負けた分の{int(rules.TABLE_RAKE * 100)}%"


# --------------------------- 表示 --------------------------- #
def lobby_embed(table: ChinchiroTable) -> discord.Embed:
    names = "\n".join(f"🪑 {table.name_of(uid)}" for uid in table.seats) or "（まだ誰もいません）"
    return ui.embed(
        TITLE,
        f"{_pot_text(table)}　席: **{len(table.seats)}/{engine.MAX_SEATS}**\n"
        f"締切: <t:{int(table.closes_at)}:R>（卓を立てた人は「▶️ 始める」で早く始められます）\n\n"
        f"{names}\n\n全員が振って、**いちばん強い役の人が総取り**します（並んだら山分け）。"
        f"{engine.MIN_SEATS}人から遊べます。",
        ui.DARK,
    )


def rolling_embed(table: ChinchiroTable) -> discord.Embed:
    lines = []
    for uid in table.seats:
        rolls = table.rolls.get(uid)
        lines.append(f"**{table.name_of(uid)}**　{_hand_line(rolls) if rolls else '…まだ振っていません'}")
    return ui.embed(
        TITLE,
        f"{_pot_text(table)}　{len(table.seats)}人\n\n"
        + "\n".join(lines)
        + f"\n\n座っている人は「🎲 振る」を押してください（{engine.ROLL_SECONDS}秒で、まだの人は自動で振ります）。",
        ui.DARK,
    )


def result_embed(table: ChinchiroTable) -> discord.Embed:
    lines = []
    for r in table.results:
        mark = "👑" if r.best else "　"
        extra = "".join(f"\n　{n}" for n in r.notices)
        lines.append(f"{mark} **{r.name}**　**{signed(r.net)}ペリカ**\n　{_hand_line(r.rolls)}{extra}")
    winners = [r.name for r in table.results if r.best]
    if len(winners) == len(table.results):
        head = "全員が同じ強さで、引き分け。賭け金は全額戻りました。"
    elif len(winners) == 1:
        head = f"👑 **{winners[0]}** の総取り！"
    else:
        head = f"👑 {'・'.join(f'**{w}**' for w in winners)} の山分け！"
    tail = "\n".join(_jackpot_lines(table.seats.values()))
    body = f"{_pot_text(table)}　{len(table.results)}人\n\n{head}\n\n" + "\n\n".join(lines)
    return ui.embed(f"{TITLE} — 決着", (body + "\n\n" + tail).rstrip(), ui.GOLD)


def table_embed(table: ChinchiroTable) -> discord.Embed:
    """いまの段階の投稿。"""
    if table.phase == engine.LOBBY:
        return lobby_embed(table)
    if table.phase == engine.ROLLING:
        return rolling_embed(table)
    if table.phase == engine.DONE:
        return result_embed(table)
    if table.phase == engine.FOLDED:
        text = f"{engine.MIN_SEATS}人そろわなかったので、卓をたたみました（賭け金は全額返しました）。"
        return ui.embed(TITLE, text, ui.INFO)
    return ui.error_embed("卓の精算に失敗しました", "精算前の賭け金は、全額返しました。")


def _jackpot_lines(seats) -> list[str]:
    lines = [
        f"# 💰 共有ジャックポット！！ 💰\n**{c.name}** が **+{ui.pelica(c.jackpot_won)}** を総取り！"
        for c in seats
        if c.jackpot_won
    ]
    pools = [c.jackpot_pool for c in seats if c.jackpot_pool]
    if pools and not lines:
        lines.append(f"💰 共有ジャックポット: いま **{ui.pelica(pools[-1])}**")
    return lines


async def _say(interaction: discord.Interaction, text: str) -> None:
    """本人だけに一言（応答済みなら followup で）。"""
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


class TablePost:
    """卓の Discord の投稿。卓が変わるたびに、いまの段階の投稿とボタンに書き換える。"""

    def __init__(self, table: ChinchiroTable):
        self.table = table
        self.message: discord.Message | None = None
        self.lobby = TableView(table)
        self.rolling: RollView | None = None

    def view(self) -> discord.ui.View | None:
        if self.table.phase == engine.LOBBY:
            return self.lobby
        self.lobby.stop()
        if self.table.phase == engine.ROLLING:
            if self.rolling is None:
                self.rolling = RollView(self.table)
            return self.rolling
        if self.rolling is not None:
            self.rolling.stop()
        return None

    async def __call__(self, table: ChinchiroTable) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(embed=table_embed(table), view=self.view())
        except discord.HTTPException as exc:  # 投稿を消されていても、精算はそのまま進める
            _log.warning("チンチロの卓の表示に失敗: %r", exc)


class TableView(discord.ui.View):
    """座る前の投稿のボタン。座る・立つ・始める（始めるは卓を立てた人だけ）。投稿の書き換えは TablePost。"""

    def __init__(self, table: ChinchiroTable):
        super().__init__(timeout=engine.JOIN_SECONDS + 30)
        self.table = table

    @discord.ui.button(label="🪑 座る", style=discord.ButtonStyle.success)
    async def sit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        name = getattr(interaction.user, "display_name", "") or ""
        reason = await self.table.sit(interaction.user.id, name)
        if isinstance(reason, BeginFailure):
            await ui.reply_error(interaction, reason.text, title=reason.title)
        elif reason:
            await _say(interaction, reason)

    @discord.ui.button(label="🚪 立つ", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if not await self.table.stand(interaction.user.id):
            await _say(interaction, "座っていないか、もう始まっています。")

    @discord.ui.button(label="▶️ 始める", style=discord.ButtonStyle.danger)
    async def begin(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.table.host_id:
            return await _say(interaction, "始められるのは、卓を立てた人だけです。")
        await interaction.response.defer()
        await self.table.play()


class RollView(discord.ui.View):
    """勝負中の投稿のボタン。座っている人が、それぞれ振る。"""

    def __init__(self, table: ChinchiroTable):
        super().__init__(timeout=engine.ROLL_SECONDS + 30)
        self.table = table

    @discord.ui.button(label="🎲 振る", style=discord.ButtonStyle.success)
    async def roll(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        reason = await self.table.roll(interaction.user.id)
        if reason:
            await _say(interaction, reason)


async def open_table(interaction: discord.Interaction, deps: CasinoDeps, bet: int) -> ChinchiroTable | None:
    """卓を立て、立てた人が最初の席に座る。締切で自動的に始める。"""
    closes_at = deps.casino.clock.now().timestamp() + engine.JOIN_SECONDS
    ctx = await start_game(interaction, deps, bet, engine.GAME)
    if ctx is None:
        return None
    table = ChinchiroTable(deps, ctx.guild_id, bet, interaction.user.id, closes_at)
    table.seat_host(ctx)
    post = TablePost(table)
    try:
        await interaction.response.send_message(embed=lobby_embed(table), view=post.lobby)
        post.message = await interaction.original_response()
    except Exception:
        ctx.abort()
        raise
    table.listeners.append(post)
    engine.register(table, interaction.channel_id or 0)
    table.schedule_start()
    return table
