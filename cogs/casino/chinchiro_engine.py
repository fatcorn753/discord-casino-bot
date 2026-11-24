"""チンチロの卓の進め方（座る・立つ・始める・振る・精算）。Discord の投稿にも、Activity の画面にも依存しない。

卓を立てた人が最初の席に座り、ほかの人が同じ賭け金を出して座る。締切（JOIN_SECONDS）か、卓を立てた人の
「始める」で勝負が始まり、座った人がそれぞれ振る（ROLL_SECONDS のうちに振らなかった人は、自動で振る）。
全員が振ったら、いちばん強い役の人が、負けた人の賭け金から場代を引いた分を受け取る（domain/games/chinchiro.py の
pot_payouts）。1人ずつ別の GameContext で預かり・精算するので、途中で Bot が落ちても、預かった賭け金は起動時に全額返る。

見せ方は、listeners に登録した関数が受け持つ。卓の状態が変わるたびに、変わったあとの卓を渡して呼ぶ
（Discord の投稿の書き換えは cogs/casino/chinchiro_table.py）。
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from cogs.casino.common import BeginFailure, CasinoDeps, GameContext, begin_context
from domain.games import chinchiro as rules

_log = logging.getLogger(__name__)
RNG = random.SystemRandom()

JOIN_SECONDS = 60  # 座れる時間
ROLL_SECONDS = 45  # 振れる時間（過ぎたら、まだの人は自動で振る）
MAX_SEATS = 6
MIN_SEATS = 2  # 総取りなので、1人では勝負にならない
GAME = "チンチロ"

# 卓の段階
LOBBY = "lobby"  # 座れる
ROLLING = "rolling"  # 振る番
FOLDED = "folded"  # 人数がそろわず、たたんだ（全額返した）
DONE = "done"  # 精算した
FAILED = "failed"  # 精算に失敗した（精算前の席は全額返した）

Rolls = list[tuple[tuple[int, ...], rules.Hand]]  # 振った目と役（目なしの振り直しも並ぶ）
Listener = Callable[["ChinchiroTable"], Awaitable[None]]
RegisterHook = Callable[["ChinchiroTable"], None]


@dataclass(frozen=True)
class SeatResult:
    """精算した1席の結果。"""

    user_id: int
    name: str
    rolls: Rolls
    net: int  # 損益（賭け金を引いたあと）
    best: bool  # いちばん強い役だったか
    notices: list[str] = field(default_factory=list)


class ChinchiroTable:
    """1つの卓。席（GameContext）を持ち、全員が振ったら一度に精算する。"""

    def __init__(self, deps: CasinoDeps, guild_id: int, bet: int, host_id: int, closes_at: float):
        self.deps = deps
        self.guild_id = guild_id
        self.bet = bet
        self.host_id = host_id
        self.closes_at = closes_at  # 締切の UNIX 秒（表示用）
        self.channel_id = 0  # 卓のあるチャンネル（register で決まる。Activity はこのチャンネルの卓を見せる）
        self.seats: dict[int, GameContext] = {}
        self.rolls: dict[int, Rolls] = {}
        self.phase = LOBBY
        self.results: list[SeatResult] = []  # 精算したら、強い順（同じ強さは座った順）
        self.listeners: list[Listener] = []
        self._lock = asyncio.Lock()
        # 締切・振る時間切れのタスク（参照を持って、途中で消されないようにする）
        self._task: asyncio.Task | None = None
        self._roll_task: asyncio.Task | None = None

    # --------------------------- 見る --------------------------- #
    @property
    def started(self) -> bool:
        """座るのを締め切った。"""
        return self.phase != LOBBY

    @property
    def finished(self) -> bool:
        """精算した（または卓をたたんだ）。"""
        return self.phase in (FOLDED, DONE, FAILED)

    @property
    def all_rolled(self) -> bool:
        return set(self.rolls) >= set(self.seats)

    def name_of(self, uid: int) -> str:
        return self.seats[uid].name or f"<@{uid}>"

    @property
    def winners(self) -> list[int]:
        return [r.user_id for r in self.results if r.best]

    async def _notify(self) -> None:
        """見せ方に、卓が変わったことを知らせる。見せ方の失敗で、勝負は止めない。"""
        for listener in list(self.listeners):
            try:
                await listener(self)
            except Exception:
                _log.exception("チンチロの卓の表示に失敗")

    # --------------------------- 席 --------------------------- #
    def seat_host(self, ctx: GameContext) -> None:
        """卓を立てた人を、最初の席に座らせる（賭け金は預かり済み）。"""
        self.seats[ctx.user_id] = ctx

    async def sit(self, user_id: int, name: str) -> str | BeginFailure | None:
        """座る。座れなかった理由を返す（残高不足などは BeginFailure）。座れたら None。"""
        async with self._lock:
            if self.started:
                return "もう始まっています。"
            if user_id in self.seats:
                return "もう座っています。"
            if len(self.seats) >= MAX_SEATS:
                return f"満席です（{MAX_SEATS}人）。"
            got = begin_context(self.deps, self.guild_id, user_id, name, self.bet, GAME)
            if isinstance(got, BeginFailure):
                return got
            self.seats[user_id] = got
        await self._notify()
        return None

    async def stand(self, uid: int) -> bool:
        """席を立つ（賭け金は全額戻る）。始まる前だけ。"""
        async with self._lock:
            ctx = self.seats.pop(uid, None) if not self.started else None
            if ctx is None:
                return False
            ctx.abort()
        await self._notify()
        return True

    # --------------------------- 勝負 --------------------------- #
    def schedule_start(self) -> None:
        """締切に、自動で始める。"""

        async def auto_start() -> None:
            await asyncio.sleep(JOIN_SECONDS)
            await self.play()

        self._task = asyncio.get_running_loop().create_task(auto_start())

    async def play(self) -> None:
        """締切か「始める」で呼ぶ。座るのを締め切り、振る番に移る。二度呼んでも一度だけ。"""
        async with self._lock:
            if self.started:
                return
            if len(self.seats) < MIN_SEATS:
                self.phase = FOLDED
                for ctx in self.seats.values():
                    ctx.abort()  # 1人きりなら、賭け金を全額返す
            else:
                self.phase = ROLLING
        if self._task and self._task is not asyncio.current_task():
            self._task.cancel()
        await self._notify()
        if self.phase != ROLLING:
            return

        async def roll_deadline() -> None:
            await asyncio.sleep(ROLL_SECONDS)
            await self.finish()

        self._roll_task = asyncio.get_running_loop().create_task(roll_deadline())

    async def roll(self, uid: int) -> str | None:
        """uid の人が振る。振れなかった理由を返す。振れたら None。全員が振ったら、そのまま精算する。"""
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
            everyone = self.all_rolled
        if everyone:
            await self.finish()
        else:
            await self._notify()
        return None

    async def finish(self) -> None:
        """まだ振っていない人を自動で振り、総取りで精算する。二度呼んでも一度だけ。"""
        async with self._lock:
            if self.phase != ROLLING:
                return
            for uid in self.seats:
                if uid not in self.rolls:
                    self.rolls[uid] = rules.play_hand(RNG)
            try:
                self._settle()
                self.phase = DONE
            except Exception:
                _log.exception("チンチロの卓の精算に失敗")
                for ctx in self.seats.values():
                    ctx.abort()  # 精算前の席は、賭け金を全額返す（精算済みの席には何もしない）
                self.phase = FAILED
        if self._roll_task and self._roll_task is not asyncio.current_task():
            self._roll_task.cancel()
        await self._notify()

    def _settle(self) -> None:
        hands = {uid: rolls[-1][1] for uid, rolls in self.rolls.items() if uid in self.seats}
        payouts = rules.pot_payouts(hands, self.bet)
        best = max(h.strength for h in hands.values())
        results = []
        # 強い順に並べる（同じ強さは座った順）
        for uid in sorted(self.seats, key=lambda u: -hands[u].strength):
            ctx = self.seats[uid]
            net = ctx.settle(payouts[uid], rtp_done=True)  # 還元率の補正はかけない（ペリカは参加者の間で動く）
            results.append(
                SeatResult(uid, self.name_of(uid), self.rolls[uid], net, hands[uid].strength == best, list(ctx.notices))
            )
        self.results = results

    def close(self) -> None:
        """締切・時間切れのタスクを止める（テストや、卓を捨てるとき用。預かり金は動かさない）。"""
        for t in (self._task, self._roll_task):
            if t:
                t.cancel()


# ----------------------- チャンネルごとの卓の台帳 ----------------------- #
# Discord の投稿から立てた卓も、Activity の画面から立てた卓も、ここに載せる。Activity は、開いたチャンネル
# （ボイスチャンネルなら、その VC のチャット）のいちばん新しい卓を見せる。Bot が再起動したら空になる
# （預かった賭け金は、起動時に全額返る）。
_TABLES: dict[tuple[int, int], ChinchiroTable] = {}
_HOOKS: list[RegisterHook] = []


def register(table: ChinchiroTable, channel_id: int) -> None:
    """卓をチャンネルの台帳に載せる（同じチャンネルの前の卓は、新しい卓に置き換わる）。"""
    table.channel_id = channel_id
    _TABLES[(table.guild_id, channel_id)] = table
    for hook in list(_HOOKS):
        hook(table)


def table_at(guild_id: int, channel_id: int) -> ChinchiroTable | None:
    """そのチャンネルのいちばん新しい卓（決着したあとも、次の卓が立つまで残す）。"""
    return _TABLES.get((guild_id, channel_id))


def add_register_hook(hook: RegisterHook) -> None:
    """卓が台帳に載るたびに呼ぶ関数を足す（Activity の画面が、卓の変化を受け取るため）。"""
    _HOOKS.append(hook)


def remove_register_hook(hook: RegisterHook) -> None:
    if hook in _HOOKS:
        _HOOKS.remove(hook)


def create(
    deps: CasinoDeps, guild_id: int, channel_id: int, host_id: int, name: str, bet: int, now: float
) -> ChinchiroTable | BeginFailure:
    """卓を立て、立てた人を最初の席に座らせ、台帳に載せて、締切のタスクを始める（Activity から立てるとき）。"""
    current = table_at(guild_id, channel_id)
    if current is not None and not current.finished:
        return BeginFailure("卓を立てられません", "このチャンネルには、まだ決着していない卓があります。")
    got = begin_context(deps, guild_id, host_id, name, bet, GAME)
    if isinstance(got, BeginFailure):
        return got
    table = ChinchiroTable(deps, guild_id, bet, host_id, now + JOIN_SECONDS)
    table.seat_host(got)
    register(table, channel_id)
    table.schedule_start()
    return table
