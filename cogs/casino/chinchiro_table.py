"""チンチロの卓（`/カジノ チンチロ卓`）。何人かが同じ卓に座り、同じ親と勝負する。

卓を立てた人が最初の席に座り、ほかの人は「🪑 座る」で同じ賭け金を出して座る。締切（JOIN_SECONDS）か、
卓を立てた人の「▶️ 始める」で、親が1回だけ振り、座った全員がそれぞれ振る（自動）。勝ち負けと配当は、
1人で遊ぶチンチロと同じ（domain/games/chinchiro.py）。1人ずつ別の GameContext で預かり・精算するので、
途中で Bot が落ちても、預かった賭け金は起動時に全額返る。
"""

import asyncio
import logging
import random

import discord

from cogs.casino.common import CasinoDeps, GameContext, signed, start_game
from cogs.casino.texts import CHINCHIRO_STEP_SECONDS as STEP
from cogs.casino.texts import CHINCHIRO_TITLE, dice_text
from cogs.shared import ui
from domain.games import chinchiro as rules

_log = logging.getLogger(__name__)
RNG = random.SystemRandom()

TITLE = f"{CHINCHIRO_TITLE}の卓"
JOIN_SECONDS = 60  # 座れる時間
MAX_SEATS = 6


def _hand_line(rolls: list) -> str:
    """振った目を1行で（目なしの振り直しも並べる）。"""
    return " → ".join(f"{dice_text(dice)} **{hand.name}**" for dice, hand in rolls)


class ChinchiroTable:
    """1つの卓。席（GameContext）を持ち、始めたら全員を一度に精算する。"""

    def __init__(self, deps: CasinoDeps, bet: int, host_id: int, closes_at: float):
        self.deps = deps
        self.bet = bet
        self.host_id = host_id
        self.closes_at = closes_at  # 締切の UNIX 秒（表示用）
        self.seats: dict[int, GameContext] = {}
        self.started = False
        self.message: discord.Message | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None  # 締切で始めるタスク（参照を持って、途中で消されないようにする）

    # --------------------------- 表示 --------------------------- #
    def lobby_embed(self) -> discord.Embed:
        names = "\n".join(f"🪑 {ctx.name or f'<@{uid}>'}" for uid, ctx in self.seats.items()) or "（まだ誰もいません）"
        return ui.embed(
            TITLE,
            f"賭け金: **{ui.pelica(self.bet)}**（全員同じ）　席: **{len(self.seats)}/{MAX_SEATS}**\n"
            f"締切: <t:{int(self.closes_at)}:R>（卓を立てた人は「▶️ 始める」で早く始められます）\n\n"
            f"{names}\n\n親は1回だけ振り、座った全員が同じ親と勝負します。負けの倍払いは、1人で遊ぶときと同じです。",
            ui.DARK,
        )

    # --------------------------- 席 --------------------------- #
    async def sit(self, interaction: discord.Interaction) -> str | None:
        """座る。座れなかった理由を返す（start_game が返信したときは空文字）。座れたら None。"""
        async with self._lock:
            uid = interaction.user.id
            if self.started:
                return "もう始まっています。"
            if uid in self.seats:
                return "もう座っています。"
            if len(self.seats) >= MAX_SEATS:
                return f"満席です（{MAX_SEATS}人）。"
            ctx = await start_game(interaction, self.deps, self.bet, "チンチロ")
            if ctx is None:
                return ""  # 残高不足などは、start_game が返信している
            self.seats[uid] = ctx
            return None

    async def stand(self, uid: int) -> bool:
        """席を立つ（賭け金は全額戻る）。始まる前だけ。"""
        async with self._lock:
            ctx = self.seats.pop(uid, None) if not self.started else None
            if ctx is None:
                return False
            ctx.abort()
            return True

    # --------------------------- 勝負 --------------------------- #
    async def play(self) -> None:
        """締切か「始める」で呼ぶ。二度呼んでも、勝負は一度だけ。"""
        async with self._lock:
            if self.started:
                return
            self.started = True
        if not self.seats:
            await self._edit(ui.embed(TITLE, "誰も座らなかったので、卓をたたみました。", ui.INFO), None)
            return
        try:
            await self._play()
        except Exception:
            _log.exception("チンチロの卓の勝負に失敗")
            for ctx in self.seats.values():
                ctx.abort()  # 精算前の席は、賭け金を全額返す（精算済みの席には何もしない）
            await self._edit(ui.error_embed("卓の勝負に失敗しました", "精算前の賭け金は、全額返しました。"), None)

    async def _play(self) -> None:
        dealer_rolls = rules.play_hand(RNG)
        dealer = dealer_rolls[-1][1]
        head = f"賭け金: **{ui.pelica(self.bet)}**　{len(self.seats)}人\n\n**親**　{_hand_line(dealer_rolls)}"
        await self._edit(ui.embed(TITLE, head + "\n\nざわ…ざわ…　子が振る…", ui.DARK), None)
        await asyncio.sleep(STEP)
        lines: list[str] = []
        wins = 0
        for ctx in self.seats.values():
            rolls = rules.play_hand(RNG)
            outcome = rules.compare(rolls[-1][1], dealer)
            payout, extra = rules.payout(self.bet, outcome)
            net = ctx.settle(payout, penalty=extra)
            wins += outcome.result == "win"
            lines.append(self._result_line(ctx, rolls, outcome, net))
            await self._edit(ui.embed(TITLE, head + "\n\n" + "\n\n".join(lines), ui.DARK), None)
            await asyncio.sleep(STEP)
        color = ui.OK if wins * 2 >= len(lines) else ui.ERROR
        tail = "\n\n" + "\n".join(_jackpot_lines(self.seats.values()))
        await self._edit(ui.embed(f"{TITLE} — 決着", head + "\n\n" + "\n\n".join(lines) + tail.rstrip(), color), None)

    def _result_line(self, ctx: GameContext, rolls: list, outcome: rules.Outcome, net: int) -> str:
        who = ctx.name or f"<@{ctx.user_id}>"
        mult = f"（{outcome.multiplier}倍）" if outcome.result != "draw" and outcome.multiplier > 1 else ""
        verdict = {"win": "🟢 勝ち", "lose": "🔴 負け", "draw": "⚪ 引き分け"}[outcome.result]
        extra = "".join(f"\n　{n}" for n in ctx.notices)
        return f"**{who}**　{verdict}{mult}　**{signed(net)}ペリカ**\n　{_hand_line(rolls)}{extra}"

    async def _edit(self, embed: discord.Embed, view: discord.ui.View | None) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(embed=embed, view=view)
        except discord.HTTPException as exc:  # 投稿を消されていても、精算はそのまま進める
            _log.warning("チンチロの卓の表示に失敗: %r", exc)


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


class TableView(discord.ui.View):
    """卓の投稿のボタン。座る・立つ・始める（始めるは卓を立てた人だけ）。"""

    def __init__(self, table: ChinchiroTable):
        super().__init__(timeout=JOIN_SECONDS + 30)
        self.table = table

    async def _show_lobby(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=self.table.lobby_embed(), view=self)

    @discord.ui.button(label="🪑 座る", style=discord.ButtonStyle.success)
    async def sit(self, interaction: discord.Interaction, button: discord.ui.Button):
        reason = await self.table.sit(interaction)
        if reason:
            return await interaction.response.send_message(reason, ephemeral=True)
        if reason is None:
            await self._show_lobby(interaction)

    @discord.ui.button(label="🚪 立つ", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.table.stand(interaction.user.id):
            return await interaction.response.send_message("座っていないか、もう始まっています。", ephemeral=True)
        await self._show_lobby(interaction)

    @discord.ui.button(label="▶️ 始める", style=discord.ButtonStyle.danger)
    async def begin(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.table.host_id:
            return await interaction.response.send_message("始められるのは、卓を立てた人だけです。", ephemeral=True)
        await interaction.response.defer()
        self.stop()
        await self.table.play()


async def open_table(interaction: discord.Interaction, deps: CasinoDeps, bet: int) -> ChinchiroTable | None:
    """卓を立て、立てた人が最初の席に座る。締切で自動的に始める。"""
    closes_at = deps.casino.clock.now().timestamp() + JOIN_SECONDS
    table = ChinchiroTable(deps, bet, interaction.user.id, closes_at)
    ctx = await start_game(interaction, deps, bet, "チンチロ")
    if ctx is None:
        return None
    table.seats[interaction.user.id] = ctx
    view = TableView(table)
    try:
        await interaction.response.send_message(embed=table.lobby_embed(), view=view)
        table.message = await interaction.original_response()
    except Exception:
        ctx.abort()
        raise

    async def auto_start() -> None:
        await asyncio.sleep(JOIN_SECONDS)
        view.stop()
        await table.play()

    table._task = asyncio.get_running_loop().create_task(auto_start())
    return table
