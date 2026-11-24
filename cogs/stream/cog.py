"""配信 — `/配信 開始`。誰かの配信に、YouTube のようにスパチャを送ったり、配信者の勝敗に賭けたりする。

- 配信者が `/配信 開始` で、チャンネルに配信パネルを出す（Discord の画面共有でも、YouTube などの外の配信でも使える）
- 💸 スパチャは、配信者への送金（手数料・1日の上限・遊んだ回数の条件は送金と同じ）。額に応じた色で、チャンネルに出す
- 🏆/💀 勝敗の賭けは山分け。配信者が「🎛️ 配信者メニュー」から勝負を始め、締め切り、勝敗を決める
- 配信を終えるか、時間切れで自動で閉じると、決まっていない勝負は無効になり、賭け金は全額返る
"""

import logging
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.shared import approval_guard, logthrottle, ui
from cogs.shared.bridges import BotBridges, IStreamButtonBridge
from cogs.shared.confirm import confirm_action, confirmed_reply, user_session
from cogs.shared.messages import format_failure
from cogs.shared.tasks import run_for_allowed_guilds
from cogs.stream.views import (
    SIDE_TEXT,
    BetModal,
    RoundModal,
    StreamButton,
    StreamerMenu,
    TipModal,
    panel_embed,
    panel_view,
    result_text,
    tip_embed,
)
from domain import stream
from domain.errors import Failure
from domain.ports import get_catalogs
from domain.ports.contracts import StreamSettleResult, StreamView
from domain.ports.repos import IGuildRepo
from domain.ports.session import ISessionGuard
from services.stream import StreamService

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot

_TRANSFER = get_catalogs().economy().transfer
NONE = discord.AllowedMentions.none()


class StreamCog(commands.Cog, IStreamButtonBridge):
    def __init__(
        self,
        bot: commands.Bot,
        config: dict[str, Any],
        stream_svc: StreamService,
        guild_repo: IGuildRepo,
        sessions: ISessionGuard,
    ):
        self.bot = bot
        self._config = config
        self._stream = stream_svc
        self._guild_repo = guild_repo
        self._sessions = sessions

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(StreamButton)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        bridges = getattr(self.bot, "bridges", None)
        if isinstance(bridges, BotBridges):
            bridges.unregister(IStreamButtonBridge, self)

    group = app_commands.Group(
        name="配信", description="📺 配信パネルを出して、スパチャや勝敗の賭けを受ける", guild_only=True
    )

    @group.command(name="開始", description="配信パネルを出す（スパチャと、あなたの勝敗への賭けを受けられる）")
    @app_commands.rename(title="タイトル", url="url")
    @app_commands.describe(title="配信のタイトル", url="配信の URL（YouTube など。Discord の画面共有なら省略）")
    async def start(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, stream.TITLE_MAX],
        url: str | None = None,
    ):
        gid = ui.guild_id_of(interaction)
        channel_id = interaction.channel_id or 0
        ok, result = self._stream.start(gid, interaction.user.id, title, url or "", channel_id)
        if not ok:
            return await ui.reply_error(interaction, format_failure(result), title="配信パネルを出せません")
        assert not isinstance(result, Failure)
        s = self._stream.get(gid, result)
        assert s is not None
        await interaction.response.send_message(embed=panel_embed(s), view=panel_view(result), allowed_mentions=NONE)
        try:
            message = await interaction.original_response()
            self._stream.set_message(gid, result, channel_id, message.id)
        except discord.HTTPException as exc:
            _log.warning(f"配信パネルの投稿を記録できません: guild={gid} stream={result}: {exc!r}")

    # ------------------------------ パネルのボタン ------------------------------ #
    async def handle_button(self, interaction: discord.Interaction, act: str, stream_id: int) -> None:
        # 止めるのは、新しいスパチャと賭けだけ。配信者メニュー（精算・終了）は、止めていても使える
        if act != "menu" and not await approval_guard.feature_guard(interaction, self._guild_repo, "stream"):
            return
        s = self._stream.get(ui.guild_id_of(interaction), stream_id)
        if s is None or s["status"] != "live":
            return await ui.reply_error(interaction, "この配信は終わりました。")
        if act == "tip":
            await interaction.response.send_modal(TipModal(self, stream_id))
            return
        if act in stream.SIDES:
            await interaction.response.send_modal(BetModal(self, stream_id, act))
            return
        if s["streamer"] != interaction.user.id:
            return await ui.reply_error(interaction, "配信者メニューは、配信者だけが使えます。")
        await interaction.response.send_message(
            "🎛️ 配信者メニュー", view=StreamerMenu(self, interaction.user.id, s), ephemeral=True
        )

    async def do_tip(self, interaction: discord.Interaction, stream_id: int, amount: int, message: str) -> None:
        """スパチャ（TipModal から）。送れたら、チャンネルに色つきで出す。"""
        async with user_session(self._sessions, interaction, "いまは送れません") as ok:
            if not ok:
                return
            gid = ui.guild_id_of(interaction)
            fee = stream.transfer_fee(amount, _TRANSFER.fee_rate)
            reply = await confirmed_reply(
                interaction,
                amount >= _TRANSFER.confirm_from,
                "スパチャを送りますか？",
                f"**{ui.pelica(amount)}** を配信者に送ります。\n手数料: {ui.pelica(fee)}（合計 {ui.pelica(amount + fee)} が引かれます）",
                "送る",
                confirm_fn=confirm_action,
            )
            if reply is None:
                return
            res, s = self._stream.tip(gid, interaction.user.id, stream_id, amount)
            if not res.ok:
                return await ui.reply_error(interaction, format_failure(res.reason), title="スパチャを送れません")
            await reply(embed=tip_embed(interaction.user, amount, message), allowed_mentions=NONE)
            await interaction.followup.send(
                embed=ui.ok_embed(
                    "スパチャを送りました",
                    f"手数料: {ui.pelica(res.fee)}　あなたの残高: **{ui.pelica(res.sender_balance)}**",
                ),
                ephemeral=True,
            )
            if s is not None:
                await self._refresh(interaction.guild, s)

    async def do_bet(self, interaction: discord.Interaction, stream_id: int, side: str, amount: int) -> None:
        """賭ける（BetModal から）。"""
        async with user_session(self._sessions, interaction, "いまは賭けられません") as ok:
            if not ok:
                return
            gid = ui.guild_id_of(interaction)
            done, result = self._stream.bet(gid, interaction.user.id, stream_id, side, amount)
            if not done:
                return await ui.reply_error(interaction, format_failure(result), title="賭けられません")
            assert not isinstance(result, Failure)
            await interaction.response.send_message(
                embed=ui.ok_embed(
                    "賭けました",
                    f"**{SIDE_TEXT[side]}** に **{ui.pelica(amount)}**。\n"
                    f"残高: **{ui.pelica(self._stream.balance_of(gid, interaction.user.id))}**",
                ),
                ephemeral=True,
            )
            await self._refresh(interaction.guild, result)

    # ------------------------------ 配信者メニュー ------------------------------ #
    async def do_menu(self, interaction: discord.Interaction, stream_id: int, act: str) -> None:
        gid = ui.guild_id_of(interaction)
        uid = interaction.user.id
        if act == "open":
            await interaction.response.send_modal(RoundModal(self, stream_id))
            return
        if act == "lock":
            done, result = self._stream.lock_round(gid, uid, stream_id)
            if not done:
                return await ui.reply_error(interaction, format_failure(result))
            assert not isinstance(result, Failure)
            await interaction.response.send_message("🔒 賭けを締め切りました。", ephemeral=True)
            return await self._refresh(interaction.guild, result)
        if act in ("win", "lose", "void"):
            winner = None if act == "void" else act
            what = "無効にして、全員に賭け金を返します" if winner is None else f"**{SIDE_TEXT[winner]}** で精算します"
            reply = await confirmed_reply(
                interaction,
                True,
                "勝敗を決めますか？",
                f"{what}。取り消せません。",
                "決める",
                confirm_fn=confirm_action,
            )
            if reply is None:
                return
            done, settled = self._stream.settle(gid, uid, stream_id, winner)
            if not done:
                await reply(embed=ui.error_embed("できません", format_failure(settled)), ephemeral=True)
                return
            assert not isinstance(settled, Failure)
            await reply(content="精算しました。", ephemeral=True)
            await self._announce_result(interaction.guild, settled)
            return await self._refresh(interaction.guild, settled["stream"])
        # end
        reply = await confirmed_reply(
            interaction,
            True,
            "配信を終えますか？",
            "パネルを閉じます。決まっていない勝負は無効になり、賭け金は全額返ります。",
            "終える",
            confirm_fn=confirm_action,
        )
        if reply is None:
            return
        done, voided = self._stream.end(gid, uid, stream_id)
        if not done:
            await reply(embed=ui.error_embed("できません", format_failure(voided)), ephemeral=True)
            return
        assert not isinstance(voided, Failure)
        await reply(content="配信を終えました。", ephemeral=True)
        await self._finish(interaction.guild, stream_id, voided)

    async def do_open_round(self, interaction: discord.Interaction, stream_id: int, label: str) -> None:
        """勝負を始める（RoundModal から）。"""
        gid = ui.guild_id_of(interaction)
        done, result = self._stream.open_round(gid, interaction.user.id, stream_id, label)
        if not done:
            return await ui.reply_error(interaction, format_failure(result))
        assert not isinstance(result, Failure)
        r = result["round"]
        assert r is not None
        await interaction.response.send_message(
            embed=ui.embed(
                f"🎲 勝負「{r['label']}」の賭けを受け付けます",
                f"<@{result['streamer']}> の勝ち・負けに賭けられます（配信パネルの 🏆 / 💀 から）。",
            ),
            allowed_mentions=NONE,
        )
        await self._refresh(interaction.guild, result)

    # ------------------------------ 投稿 ------------------------------ #
    async def _refresh(self, guild: discord.Guild | None, s: StreamView) -> None:
        """配信パネルを、いまの状態に書き換える。終わったらボタンを外す。"""
        channel = guild.get_channel(s["channel_id"]) if guild else None
        msg = ui.partial_message(channel, s["message_id"]) if channel is not None and s["message_id"] else None
        if msg is None:
            return
        try:
            if s["status"] == "live":
                await msg.edit(embed=panel_embed(s))
            else:
                await msg.edit(embed=panel_embed(s), view=None)
        except discord.HTTPException as exc:
            logthrottle.warn(("stream", s["id"]), f"配信パネルを直せません: stream={s['id']}: {exc!r}")

    async def _announce_result(self, guild: discord.Guild | None, res: StreamSettleResult) -> None:
        s = res["stream"]
        channel = guild.get_channel(s["channel_id"]) if guild else None
        if channel is None:
            return
        try:
            await ui.send_channel(
                channel, embed=ui.embed(f"🏁 {s['title']} の勝負の結果", result_text(res)), allowed_mentions=NONE
            )
        except discord.HTTPException as exc:
            logthrottle.warn(("stream", s["id"]), f"勝負の結果を投稿できません: stream={s['id']}: {exc!r}")

    async def _finish(self, guild: discord.Guild | None, stream_id: int, voided: StreamSettleResult | None) -> None:
        """配信を閉じたあと。無効にした勝負があれば知らせ、パネルを「配信終了」にする。"""
        if voided is not None:
            await self._announce_result(guild, voided)
        s = self._stream.get(guild.id, stream_id) if guild else None
        if s is not None:
            await self._refresh(guild, s)

    # ------------------------------ 時間切れの見回り（毎分） ------------------------------ #
    @tasks.loop(minutes=1)
    async def tick(self):
        await run_for_allowed_guilds(
            self.bot, self._guild_repo, self._config, self._tick_guild, error_label="配信の見回りに失敗"
        )

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _tick_guild(self, guild: discord.Guild) -> None:
        for s, voided in self._stream.close_due(guild.id):
            if voided is not None:
                await self._announce_result(guild, voided)
            await self._refresh(guild, s)


async def setup(bot: "CasinoBot") -> None:
    cog = StreamCog(bot, bot.config, bot.services.stream, bot.repos.guild, bot.sessions)
    await bot.add_cog(cog)
    bot.bridges.register(IStreamButtonBridge, cog)
