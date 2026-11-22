"""チンチロの卓（`/カジノ チンチロ卓`）。何人かが同じ額を出し、いちばん強い役の人が総取りする。

卓を立てた人が最初の席に座り、ほかの人は「🪑 座る」で同じ賭け金を出して座る。締切（JOIN_SECONDS）か、
卓を立てた人の「▶️ 始める」で勝負が始まり、座った人がそれぞれ「🎲 振る」を押して振る（ROLL_SECONDS の
うちに振らなかった人は、自動で振る）。全員が振ったら、いちばん強い役の人が、負けた人の賭け金から場代を
引いた分を受け取る（domain/games/chinchiro.py の pot_payouts）。1人ずつ別の GameContext で預かり・精算する
ので、途中で Bot が落ちても、預かった賭け金は起動時に全額返る。
"""

import asyncio
import logging
import random

import discord

from cogs.casino.common import CasinoDeps, GameContext, signed, start_game
from cogs.casino.texts import CHINCHIRO_TITLE, dice_text
from cogs.shared import ui
from domain.games import chinchiro as rules

_log = logging.getLogger(__name__)
RNG = random.SystemRandom()

TITLE = f"{CHINCHIRO_TITLE}の卓"
JOIN_SECONDS = 60  # 座れる時間
ROLL_SECONDS = 45  # 振れる時間（過ぎたら、まだの人は自動で振る）
MAX_SEATS = 6
MIN_SEATS = 2  # 総取りなので、1人では勝負にならない


def _hand_line(rolls: list) -> str:
    """振った目を1行で（目なしの振り直しも並べる）。"""
    return " → ".join(f"{dice_text(dice)} **{hand.name}**" for dice, hand in rolls)


class ChinchiroTable:
    """1つの卓。席（GameContext）を持ち、全員が振ったら一度に精算する。"""

    def __init__(self, deps: CasinoDeps, bet: int, host_id: int, closes_at: float):
        self.deps = deps
        self.bet = bet
        self.host_id = host_id
        self.closes_at = closes_at  # 締切の UNIX 秒（表示用）
        self.seats: dict[int, GameContext] = {}
        self.rolls: dict[int, list] = {}  # 振った人の出目（[(出目, 役), ...]）
        self.started = False  # 座るのを締め切った
        self.finished = False  # 精算した（または卓をたたんだ）
        self.message: discord.Message | None = None
        self._lock = asyncio.Lock()
        # 締切・振る時間切れのタスク（参照を持って、途中で消されないようにする）
        self._task: asyncio.Task | None = None
        self._roll_task: asyncio.Task | None = None

    def _name(self, uid: int) -> str:
        ctx = self.seats[uid]
        return ctx.name or f"<@{uid}>"

    def _pot_text(self) -> str:
        return f"賭け金: **{ui.pelica(self.bet)}**（全員同じ）　場代: 負けた分の{int(rules.TABLE_RAKE * 100)}%"

    # --------------------------- 表示 --------------------------- #
    def lobby_embed(self) -> discord.Embed:
        names = "\n".join(f"🪑 {self._name(uid)}" for uid in self.seats) or "（まだ誰もいません）"
        return ui.embed(
            TITLE,
            f"{self._pot_text()}　席: **{len(self.seats)}/{MAX_SEATS}**\n"
            f"締切: <t:{int(self.closes_at)}:R>（卓を立てた人は「▶️ 始める」で早く始められます）\n\n"
            f"{names}\n\n全員が振って、**いちばん強い役の人が総取り**します（並んだら山分け）。"
            f"{MIN_SEATS}人から遊べます。",
            ui.DARK,
        )

    def rolling_embed(self) -> discord.Embed:
        lines = []
        for uid in self.seats:
            rolls = self.rolls.get(uid)
            lines.append(f"**{self._name(uid)}**　{_hand_line(rolls) if rolls else '…まだ振っていません'}")
        return ui.embed(
            TITLE,
            f"{self._pot_text()}　{len(self.seats)}人\n\n"
            + "\n".join(lines)
            + f"\n\n座っている人は「🎲 振る」を押してください（{ROLL_SECONDS}秒で、まだの人は自動で振ります）。",
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
        """締切か「始める」で呼ぶ。座るのを締め切り、振る番に移る。二度呼んでも一度だけ。"""
        async with self._lock:
            if self.started:
                return
            self.started = True
            if len(self.seats) < MIN_SEATS:
                self.finished = True
                for ctx in self.seats.values():
                    ctx.abort()  # 1人きりなら、賭け金を全額返す
        if self.finished:
            text = f"{MIN_SEATS}人そろわなかったので、卓をたたみました（賭け金は全額返しました）。"
            await self._edit(ui.embed(TITLE, text, ui.INFO), None)
            return
        await self._edit(self.rolling_embed(), RollView(self))

        async def roll_deadline() -> None:
            await asyncio.sleep(ROLL_SECONDS)
            await self.finish()

        self._roll_task = asyncio.get_running_loop().create_task(roll_deadline())

    async def roll(self, uid: int) -> str | None:
        """uid の人が振る。振れなかった理由を返す。振れたら None。"""
        async with self._lock:
            if uid not in self.seats:
                return "この卓に座っていません。"
            if self.finished:
                return "もう決着しています。"
            if not self.started:
                return "まだ始まっていません。"
            if uid in self.rolls:
                return "もう振りました。"
            self.rolls[uid] = rules.play_hand(RNG)
            return None

    @property
    def all_rolled(self) -> bool:
        return set(self.rolls) >= set(self.seats)

    async def finish(self) -> None:
        """まだ振っていない人を自動で振り、総取りで精算する。二度呼んでも一度だけ。"""
        async with self._lock:
            if self.finished or not self.started:
                return
            self.finished = True
            for uid in self.seats:
                if uid not in self.rolls:
                    self.rolls[uid] = rules.play_hand(RNG)
        if self._roll_task and self._roll_task is not asyncio.current_task():
            self._roll_task.cancel()
        try:
            await self._settle()
        except Exception:
            _log.exception("チンチロの卓の精算に失敗")
            for ctx in self.seats.values():
                ctx.abort()  # 精算前の席は、賭け金を全額返す（精算済みの席には何もしない）
            await self._edit(ui.error_embed("卓の精算に失敗しました", "精算前の賭け金は、全額返しました。"), None)

    async def _settle(self) -> None:
        hands = {uid: rolls[-1][1] for uid, rolls in self.rolls.items() if uid in self.seats}
        payouts = rules.pot_payouts(hands, self.bet)
        best = max(h.strength for h in hands.values())
        lines = []
        # 強い順に並べる（同じ強さは座った順）
        for uid in sorted(self.seats, key=lambda u: -hands[u].strength):
            ctx = self.seats[uid]
            net = ctx.settle(payouts[uid], rtp_done=True)  # 還元率の補正はかけない（ペリカは参加者の間で動く）
            mark = "👑" if hands[uid].strength == best else "　"
            extra = "".join(f"\n　{n}" for n in ctx.notices)
            lines.append(
                f"{mark} **{self._name(uid)}**　**{signed(net)}ペリカ**\n　{_hand_line(self.rolls[uid])}{extra}"
            )
        winners = [self._name(u) for u in self.seats if hands[u].strength == best]
        if len(winners) == len(self.seats):
            head = "全員が同じ強さで、引き分け。賭け金は全額戻りました。"
        elif len(winners) == 1:
            head = f"👑 **{winners[0]}** の総取り！"
        else:
            head = f"👑 {'・'.join(f'**{w}**' for w in winners)} の山分け！"
        tail = "\n".join(_jackpot_lines(self.seats.values()))
        body = f"{self._pot_text()}　{len(self.seats)}人\n\n{head}\n\n" + "\n\n".join(lines)
        await self._edit(ui.embed(f"{TITLE} — 決着", (body + "\n\n" + tail).rstrip(), ui.GOLD), None)

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
    """座る前の投稿のボタン。座る・立つ・始める（始めるは卓を立てた人だけ）。"""

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


class RollView(discord.ui.View):
    """勝負中の投稿のボタン。座っている人が、それぞれ振る。"""

    def __init__(self, table: ChinchiroTable):
        super().__init__(timeout=ROLL_SECONDS + 30)
        self.table = table

    @discord.ui.button(label="🎲 振る", style=discord.ButtonStyle.success)
    async def roll(self, interaction: discord.Interaction, button: discord.ui.Button):
        reason = await self.table.roll(interaction.user.id)
        if reason:
            return await interaction.response.send_message(reason, ephemeral=True)
        if self.table.all_rolled:
            await interaction.response.defer()
            self.stop()
            await self.table.finish()
            return
        await interaction.response.edit_message(embed=self.table.rolling_embed(), view=self)


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
