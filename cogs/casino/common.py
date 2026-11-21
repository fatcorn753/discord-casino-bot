"""ゲーム共通の流れ: 賭け金の検証と預かり、精算、ボタンの持ち主確認、結果の表示。"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import discord

from cogs.casino.texts import JACKPOT_ART, big_win_line, streak_line
from cogs.shared import approval_guard, ui
from cogs.shared.messages import format_notices
from domain import features, missions, teiai
from domain.games.rtplevel import MIN_BET as MIN_BET
from domain.ports.repos import IGuildRepo
from services.casino import CasinoService
from services.teiai import TeiaiService

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CasinoDeps:
    """カジノ UI / ゲーム実行に必要な明示依存コンテナ。"""

    casino: CasinoService
    teiai: TeiaiService
    guild_repo: IGuildRepo
    config: dict[str, Any]


def log_display_failure(game: str, step: str, exc: Exception) -> None:
    """演出の表示（メッセージの書き換え）に失敗したことを記録する。精算は済んでいるので結果には影響しないが、
    画面が途中で止まって見える原因（通信エラーなど）を、あとから調べられるようにする。"""
    status = getattr(exc, "status", "?")
    code = getattr(exc, "code", "?")
    _log.warning(f"演出の表示に失敗: {game}/{step}: {type(exc).__name__} status={status} code={code}: {exc}")


class GameContext:
    """1回のゲーム。賭け金は預かり済みで、settle() か abort() で必ず精算する。"""

    def __init__(
        self,
        deps: CasinoDeps,
        user_id: int,
        escrow_id: int,
        game: str,
        bet: int,
        guild_id: int,
        name: str = "",
    ):
        self.deps = deps
        self.casino = deps.casino
        self.name = name  # 遊んでいる人の表示名（タイトルに付けて、誰のゲームか分かるようにする）
        self.guild_id = guild_id
        self.user_id = user_id
        self.escrow_id = escrow_id
        self.game = game
        self.bet = bet
        self.done = False
        self.penalty_taken = 0  # チンチロの倍払いなどで、預かり金のほかに引いた額
        self.insured_refund = 0  # 保険券で戻った額
        self.event_bonus = 0  # 突発イベントで上乗せされた額
        self.rtp_shift = 0  # 還元率レベルで増減した払い戻し（表示の払い戻しに足す。レベルそのものは見せない）
        self.events: dict[str, Any] = {}  # 連勝・ミッションの結果（演出の表示に使う）
        self.skin: Any = None  # 装備中の、演出の変更品（domain/skins.py）。見た目だけが変わる
        self.notices: list[str] = []  # 精算で起きた、強制執行・種銭などの一言（infra/db/notices.py）

    def settle(
        self,
        payout: int,
        penalty: int = 0,
        keep_session: bool = False,
        tags: Iterable[str] = (),
        rtp_kind: str | None = None,
        rtp_done: bool = False,
    ) -> int:
        """払い戻し総額で精算し、損益を返す。二重に呼んでも一度しか払わない。"""
        if self.done:
            return 0
        res = self.casino.settle_game(
            self.guild_id,
            self.user_id,
            self.escrow_id,
            self.game,
            self.bet,
            payout,
            penalty=penalty,
            keep_session=keep_session,
            tags=tags,
            rtp_kind=rtp_kind,
            rtp_done=rtp_done,
            now=self.casino.clock.now(),
        )
        self.done = True
        if not rtp_done:
            self.rtp_shift = res.rtp_shift
        self.event_bonus = res.event_bonus
        if res.duplicate:
            return 0
        self.penalty_taken = res.penalty_taken
        self.insured_refund = res.insured_refund
        self.notices = format_notices(list(res.notices))
        if res.events is not None:
            self.events = res.events
        return res.net

    def release(self) -> None:
        """keep_session=True で精算したあと、演出が終わったら呼ぶ。"""
        self.casino.sessions.end(self.user_id)

    def abort(self) -> None:
        """エラー時: まだ精算していなければ、預かっている賭け金（追加分も含む）を全額返す。"""
        if self.done:
            return
        self.done = True
        self.casino.abort_game(self.guild_id, self.user_id, self.escrow_id)


async def start_game(
    interaction: discord.Interaction,
    deps: CasinoDeps,
    bet: int,
    game: str,
    balance_factor: int = 1,
    factor_reason: str = "",
) -> GameContext | None:
    """賭け金を検証して預かる。成功すれば GameContext、失敗すれば理由を返信して None。"""
    key = features.CASINO_GAMES.get(game)
    if key is not None and not await approval_guard.feature_guard(interaction, deps.guild_repo, key):
        return None  # 管理者が止めたゲーム（「もう一度」のボタンからも始めさせない）

    uid = interaction.user.id
    guild_id = ui.guild_id_of(interaction)
    res = deps.casino.begin_game(guild_id, uid, bet, game, balance_factor=balance_factor)

    if not res.ok:
        if res.reason_kind == "session":
            await interaction.response.send_message(
                embed=ui.error_embed("まだ始められません", res.reason), ephemeral=True
            )
        elif res.reason_kind == "min_bet":
            await interaction.response.send_message(
                embed=ui.error_embed("賭け金が少なすぎます", f"{ui.pelica(MIN_BET)} 以上で指定してください。"),
                ephemeral=True,
            )
        elif res.reason_kind == "balance_factor":
            msg = (
                f"{factor_reason}賭け金の **{balance_factor}倍**（{ui.pelica(bet * balance_factor)}）以上の残高が必要です（いまの残高: {ui.pelica(res.balance)}）。\n"
                f"いまの残高なら、**{ui.pelica(res.balance // balance_factor)}** まで賭けられます。"
            )
            await interaction.response.send_message(embed=ui.error_embed("ペリカが足りません", msg), ephemeral=True)
        elif res.reason_kind == "balance":
            msg = f"賭け金 {ui.pelica(bet)} に対して、残高は {ui.pelica(res.balance)} です。\n`/ペリカ デイリー` で6時間ごとにもらえます。"
            await interaction.response.send_message(embed=ui.error_embed("ペリカが足りません", msg), ephemeral=True)
        else:
            await interaction.response.send_message(
                embed=ui.error_embed(
                    "ペリカが足りません",
                    f"賭け金 {ui.pelica(bet)} に対して、残高は {ui.pelica(res.balance)} です。",
                ),
                ephemeral=True,
            )
        return None
    ctx = GameContext(
        deps, uid, res.escrow_id, game, bet, guild_id, getattr(interaction.user, "display_name", "") or ""
    )
    ctx.skin = res.skin
    return ctx


def game_title(base: str, ctx: GameContext) -> str:
    """ゲームの見出しに、遊んでいる人の名前を付ける（例: 🃏 ブラックジャック（たろう））。変更品を装備していれば、飾りが先頭に付く。"""
    prefix = ctx.skin.title_prefix if ctx.skin else ""
    return prefix + (f"{base}（{ctx.name}）" if ctx.name else base)


def signed(n: int) -> str:
    return f"+{n:,}" if n > 0 else f"{n:,}"


def result_lines(ctx: GameContext, payout: int, net: int, extra: str = "") -> str:
    """結果の共通表示。出目や役（extra）が先頭、そのあとに大勝ち・連勝の演出、賭け金・払い戻し・損益・残高、最後にミッション達成。"""
    balance = ctx.casino.balance_of(ctx.guild_id, ctx.user_id)
    top = [extra] if extra else []
    if ctx.skin and net != 0:  # 演出の変更品の一言（勝ったとき・負けたとき）
        top.append(ctx.skin.win_line if net > 0 else ctx.skin.lose_line)
    if ctx.insured_refund:
        top.append(f"🎟️ 保険券を使った！ 賭け金の半分（{ui.pelica(ctx.insured_refund)}）が戻った")
    event = [f"🔥 **突発イベントのボーナス +{ui.pelica(ctx.event_bonus)}**"] if ctx.event_bonus else []
    stats = event + [
        f"賭け金: **{ui.pelica(ctx.bet)}**",
        f"払い戻し: **{ui.pelica(payout + ctx.rtp_shift + ctx.insured_refund)}**",
        f"損益: **{signed(net)}ペリカ**",
        f"残高: **{ui.pelica(balance)}**",
    ]
    debt = ctx.deps.teiai.debt_of(ctx.guild_id, ctx.user_id)
    if debt:
        stats.append(f"借金: **{ui.pelica(debt)}**（収入の{int(teiai.REPAY_RATE * 100)}%が天引きで返済されます）")
    stats += ctx.notices  # 強制執行・種銭の一言
    head, tail = _celebration(ctx, net)
    if top and head:
        top.append("")
    return "\n".join(top + head + stats + tail)


def result_color(bet: int, net: int, base: int) -> int:
    """特大勝ち（賭け金の10倍以上の利益）は金色にして目立たせる。"""
    return ui.GOLD if bet > 0 and net >= bet * 10 else base


def _celebration(ctx: GameContext, net: int) -> tuple[list[str], list[str]]:
    """(結果の数字の前に出す、大勝ち・連勝の演出, 最後に出す、ミッション達成の通知)。"""
    ev, head, tail = ctx.events, [], []
    if missions.is_jackpot(ctx.bet, net):
        head.append(JACKPOT_ART)
    if line := big_win_line(ctx.bet, net):
        head.append(line)
    if ev and (line := streak_line(ev["streak"], ev["new_best"])):
        head.append(line)
    if head:
        head.append("")
    for m, reward in ev.get("completed", []) if ev else []:
        tail.append(f"📜 ミッション達成！ 「{m.name}」 **+{ui.pelica(reward)}**")
    if ev and ev.get("bonus"):
        tail.append(f"🎁 **今日のミッションを全部達成！** ボーナス **+{ui.pelica(ev['bonus'])}**")
    if tail:
        tail.insert(0, "")
    return head, tail


class ReplayView(ui.OwnerView):
    """結果の下に出す「もう一度」ボタン。遊んだ本人だけが押せて、押すと新しいゲームが始まる。"""

    def __init__(self, deps: CasinoDeps, user_id: int, bet: int, replay: Any, timeout: float = 90):
        super().__init__(
            user_id,
            timeout=timeout,
            owner_message="これは他の人のゲームです。`/カジノ` で自分も遊べます。",
        )
        self.deps, self.user_id, self.bet, self.replay = deps, user_id, bet, replay
        self.origin: discord.Interaction | None = (
            None  # この結果を出したメッセージの元になった interaction（時間切れでボタンを消すのに使う）
        )
        self.again.label = f"🔁 同じ額でもう一度（{bet:,}）"
        self.double.label = f"⏫ 倍にして（{bet * 2:,}）"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False
        return await approval_guard.allow_component(
            interaction,
            self.deps.guild_repo,
            self.deps.config,
            title="使えません",
            text="このサーバーでは、いまは遊べません。",
        )

    @discord.ui.button(style=discord.ButtonStyle.primary)
    async def again(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.replay(interaction, self.bet)

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def double(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.replay(interaction, self.bet * 2)

    async def on_timeout(self) -> None:
        if self.origin is not None:
            try:
                await self.origin.edit_original_response(view=None)
            except discord.HTTPException as exc:
                log_display_failure("ボタンの片付け", "時間切れ", exc)


class GameOwnerView(ui.OwnerView):
    """賭けた本人だけが押せるボタン。エラーや時間切れでも、賭け金を失わないようにする。"""

    def __init__(self, ctx: GameContext, timeout: float = 90):
        super().__init__(
            ctx.user_id,
            timeout=timeout,
            owner_message="これは他の人のゲームです。`/カジノ` で自分も遊べます。",
        )
        self.ctx = ctx

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item[Any]) -> None:
        _log.warning(f"ゲーム中のエラー: {self.ctx.game}: {error!r}")
        self.ctx.abort()  # 賭け金を返す
        try:
            await ui.send_channel(
                interaction.channel,
                embed=ui.error_embed(
                    "エラーが発生しました",
                    "ゲームを中止しました。精算前なら賭け金はお返ししています（`/ペリカ 履歴` で確認できます）。",
                ),
            )
        except discord.HTTPException:
            pass
        self.stop()
