"""/ペリカ — 残高・デイリー・ランキング・送金・履歴。"""

import datetime
import logging
import math
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.economy import boards, embeds
from cogs.economy.boards import RankingView as RankingView
from cogs.shared import ranking, ui
from cogs.shared.amount import balance_choices
from cogs.shared.bridges import DeferredShopTitleBridge, IShopTitleBridge
from cogs.shared.confirm import confirm_action, confirmed_reply, user_session
from cogs.shared.messages import format_failure, format_memo
from cogs.teiai.panel import TeiaiPanel, panel_embed
from domain import approval, skins
from domain.errors import Failure
from domain.ports import get_catalogs
from domain.ports.clock import IClock
from domain.ports.repos import IGuildRepo
from domain.ports.session import ISessionGuard
from services.economy import EconomyService
from services.ranking import RankingService
from services.teiai import TeiaiService

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot

_ECON_CFG = get_catalogs().economy()
_TRANSFER_CFG = _ECON_CFG.transfer
TRANSFER_FEE_RATE = _TRANSFER_CFG.fee_rate  # 送る側が、送金額の10%を手数料として払う
TRANSFER_DAILY_LIMIT = _TRANSFER_CFG.daily_limit  # 1日に送れる合計（送る金額だけで数える。手数料は含まない）
TRANSFER_CONFIRM_FROM = _TRANSFER_CFG.confirm_from  # この額以上は確認ボタンを挟む
BOARD_INTERVAL_MINUTES = _TRANSFER_CFG.board_interval_minutes  # ランキング掲示板を更新する間隔
TRANSFER_MIN_GAMES = _TRANSFER_CFG.min_games  # 送金できるのは、ゲームで3回以上遊んだ人（複数アカウントで稼ぐのを防ぐ）


def tz_label(utc_offset: float) -> str:
    return "日本時間" if utc_offset == 9 else f"UTC{utc_offset:+g}"


def reset_label(utc_offset: float) -> str:
    """日付が切り替わる（デイリーやミッションがリセットされる）時刻の表示。"""
    return f"{tz_label(utc_offset)} 0:00" if utc_offset == 9 else f"{tz_label(utc_offset)} の 0:00"


DAILY_SLOT_HOURS = _ECON_CFG.wallet.daily_slot_hours  # デイリーボーナスを受け取れる間隔（0・6・12・18時に切り替わる）


def slot_of(now: datetime.datetime) -> int:
    """いまが、何番目の「受け取りの時間帯」か（6時間ごとに1つ進む）。"""
    return now.date().toordinal() * (24 // DAILY_SLOT_HOURS) + now.hour // DAILY_SLOT_HOURS


def next_slot(clock: IClock) -> datetime.datetime:
    """次にデイリーボーナスを受け取れる時刻。"""
    now = clock.now()
    start = datetime.datetime.combine(now.date(), datetime.time(), tzinfo=now.tzinfo)
    return start + datetime.timedelta(hours=(now.hour // DAILY_SLOT_HOURS + 1) * DAILY_SLOT_HOURS)


def next_slot_label(clock: IClock, utc_offset: float) -> str:
    t = next_slot(clock)
    return f"{t.hour}:00（{tz_label(utc_offset)}）"


def manage_guild_or_owner():
    """サーバー管理権限を持つ人、または Bot の持ち主（owner_ids）だけが使える。"""

    def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id in approval.owner_ids(ui.bot_of(interaction).config):
            return True
        perms = interaction.permissions
        if perms.manage_guild:
            return True
        raise app_commands.MissingPermissions(["manage_guild"])

    return app_commands.check(predicate)


class EconomyCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        config: dict[str, Any],
        economy_svc: EconomyService,
        teiai_svc: TeiaiService,
        ranking_svc: RankingService,
        guild_repo: IGuildRepo,
        sessions: ISessionGuard,
        clock: IClock,
        *,
        shop_bridge: IShopTitleBridge | None = None,
    ):
        self.bot = bot
        self._config = config
        self._economy_svc = economy_svc
        self._teiai = teiai_svc
        self._ranking = ranking_svc
        self._guild_repo = guild_repo
        self._sessions = sessions
        self._clock = clock
        self._shop_bridge: IShopTitleBridge = shop_bridge if shop_bridge is not None else DeferredShopTitleBridge(bot)

    group = app_commands.Group(name="ペリカ", description="💴 残高・デイリー・ランキング・送金")

    @group.command(name="残高", description="ペリカの残高と成績を表示")
    @app_commands.rename(member="対象")
    @app_commands.describe(member="見たい人（省略時: 自分）")
    async def balance(self, interaction: discord.Interaction, member: discord.Member | None = None):
        target = member or interaction.user
        card = self._economy_svc.balance_card(ui.guild_id_of(interaction), interaction.user.id, target.id)
        if card is None:
            return await interaction.response.send_message(embed=embeds.no_account_embed(target.display_name))
        await interaction.response.send_message(
            embed=embeds.balance_embed(card, target.display_name, target.display_avatar.url)
        )

    @group.command(name="デイリー", description="6時間ごとにもらえる無料ボーナス")
    async def daily(self, interaction: discord.Interaction):
        now = self._clock.now()
        outcome = self._economy_svc.claim_daily(ui.guild_id_of(interaction), interaction.user.id, now, slot_of(now))
        result = outcome.result
        next_label = next_slot_label(self._clock, self._clock.offset_hours())
        next_ts = int(next_slot(self._clock).timestamp())
        if not result.ok:
            return await interaction.response.send_message(
                embed=embeds.daily_wait_embed(result.streak, next_label, next_ts), ephemeral=True
            )
        await interaction.response.send_message(
            embed=embeds.daily_embed(result, next_label, next_ts, skins.active_season(now))
        )
        if outcome.memo:  # 本人にだけ見える
            await interaction.followup.send(
                embed=ui.embed("🕴️ 黒服の噂話", format_memo(outcome.memo), ui.DARK), ephemeral=True
            )

    @group.command(name="返済", description="帝愛への借金を、手元のペリカから返す（金額を省くと、返せる限り全額）")
    @app_commands.rename(amount="金額")
    @app_commands.describe(amount="返す額（省略で全額）")
    async def repay(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1] | None = None):
        gid, uid = ui.guild_id_of(interaction), interaction.user.id
        ok, result = self._teiai.repay(gid, uid, amount)
        if not ok:
            return await ui.reply_error(interaction, format_failure(result), title="返せません")
        assert not isinstance(result, Failure)
        await interaction.response.send_message(
            embed=embeds.repaid_embed(result, self._teiai.debt_of(gid, uid)), ephemeral=True
        )

    @group.command(name="帝愛", description="帝愛銀行（帝愛信託・準備率）・破産猶予・CDS（破綻保証）・種銭の画面")
    async def teiai_panel(self, interaction: discord.Interaction):
        gid, uid = ui.guild_id_of(interaction), interaction.user.id
        await interaction.response.send_message(
            embed=panel_embed(self._teiai, gid, uid),
            view=TeiaiPanel(self._teiai, uid, shop_bridge=self._shop_bridge),
            ephemeral=True,
        )

    @group.command(
        name="破産",
        description="自己破産する（全財産とショップの全品を差し押さえ、貸付は免責・借金は圧縮。元に戻せない）",
    )
    async def bankrupt(self, interaction: discord.Interaction):
        gid, uid = ui.guild_id_of(interaction), interaction.user.id
        refusal = self._teiai.can_go_bankrupt(gid, uid)
        if refusal is not None:
            title, text = embeds.BANKRUPT_REFUSALS[refusal]
            return await ui.reply_error(interaction, text, title=title)
        ok = await confirm_action(interaction, "自己破産しますか？", embeds.bankrupt_warning(), "破産する")
        if ok is not True:
            return
        ok, result = self._teiai.bankrupt(gid, uid)
        if not ok:
            return await ui.reply_error(interaction, format_failure(result), title="破産できません")
        assert not isinstance(result, Failure)
        await interaction.followup.send(  # 公開（見せ場）。誰が破産したかも出す
            embed=embeds.bankrupt_result_embed(result, interaction.user.display_name)
        )
        self._teiai.take_notices(gid, uid)
        guild = interaction.guild
        if guild is not None:
            member = guild.get_member(uid) or interaction.user
            if isinstance(member, discord.Member):
                await self._shop_bridge.after_bankruptcy(guild, member)  # 色ロールとニックネームを、すぐに反映する

    @group.command(name="ミッション", description="今日のミッションと進み具合を見る")
    async def missions(self, interaction: discord.Interaction):
        board = self._economy_svc.mission_board(ui.guild_id_of(interaction), interaction.user.id)
        await interaction.response.send_message(
            embed=embeds.missions_embed(board, reset_label(self._clock.offset_hours())), ephemeral=True
        )

    @group.command(
        name="ランキング",
        description="このサーバーで、ペリカの多い順に並べる（流動資産・資産の内訳も見られる）",
    )
    @app_commands.rename(member="内訳")
    @app_commands.describe(member="この人の資産の内訳を見る（省略でランキング）")
    async def ranking(self, interaction: discord.Interaction, member: discord.Member | None = None):
        gid = ui.guild_id_of(interaction)
        if member is not None:
            e = ranking.build_breakdown_embed(self._ranking.for_guild(gid), member.id, member.display_name)
            return await interaction.response.send_message(
                embed=e, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )
        view = RankingView(self._ranking, interaction.user.id, gid)
        await interaction.response.send_message(
            embed=view.embed("net"), view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    # ------------------- 自動で更新されるランキング掲示板（boards.py へ委譲） ------------------- #
    async def cog_load(self):
        self.refresh_boards.start()

    async def cog_unload(self) -> None:
        self.refresh_boards.cancel()

    @tasks.loop(minutes=BOARD_INTERVAL_MINUTES)
    async def refresh_boards(self):
        for board in self._guild_repo.list_leaderboards():
            try:
                await boards.refresh_board(self.bot, self._ranking, self._guild_repo, board, BOARD_INTERVAL_MINUTES)
            except Exception as exc:  # 1つのサーバーの失敗で、他のサーバーの更新を止めない
                _log.warning(f"ランキング掲示板の更新に失敗: {board}: {exc!r}")

    @refresh_boards.before_loop
    async def _before_refresh(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        self._guild_repo.delete_leaderboard(guild.id)

    @group.command(
        name="ランキング掲示", description="[管理者] ランキングを投稿して自動で更新する／止める（サーバー管理）"
    )
    @app_commands.rename(channel="チャンネル", stop="止める")
    @app_commands.describe(
        channel=f"ランキングを置くチャンネル（{BOARD_INTERVAL_MINUTES}分ごとに自動で更新。1サーバーに1つ）",
        stop="掲示を止めて、投稿を削除する（このときチャンネルは不要）",
    )
    @manage_guild_or_owner()
    async def board_set(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None, stop: bool = False
    ):
        await boards.board_set(self._ranking, self._guild_repo, interaction, channel, stop, BOARD_INTERVAL_MINUTES)

    async def _transfer_autocomplete(self, interaction: discord.Interaction, current: str):
        """送る金額の欄に、残高を見せる。"""
        return balance_choices(self._economy_svc, interaction, current, 1, 1_000_000)

    @group.command(name="送金", description="ほかの人にペリカを送る（手数料10%）")
    @app_commands.rename(member="相手", amount="金額")
    @app_commands.describe(member="送る相手", amount="送る金額")
    @app_commands.autocomplete(amount=_transfer_autocomplete)
    async def transfer(
        self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 1_000_000]
    ):
        if member.bot:
            return await ui.reply_error(interaction, "Bot には送金できません。", title="送れません")
        # 送金の間は「プレイ中」にする。ゲーム中に残高を逃がして負けや倍払いを避ける／確認待ちの間にゲームを始める、を防ぐ
        async with user_session(self._sessions, interaction, "いまは送れません") as ok:
            if not ok:
                return
            gid = ui.guild_id_of(interaction)
            uid = interaction.user.id
            fee = math.ceil(round(amount * TRANSFER_FEE_RATE, 6))
            # 確認ボタンを出す前に、残高を先に調べる（足りないのに確認だけさせない）
            if self._economy_svc.balance_of(gid, uid) < amount + fee:
                return await ui.reply_error(
                    interaction,
                    f"残高が足りません（送金額 {amount:,} + 手数料 {fee:,} が必要です）。",
                    title="送金できません",
                )
            reply = await confirmed_reply(
                interaction,
                amount >= TRANSFER_CONFIRM_FROM,
                "送金しますか？",
                f"{member.mention} に **{ui.pelica(amount)}** を送ります。\n手数料: {ui.pelica(fee)}（合計 {ui.pelica(amount + fee)} が引かれます）",
                "送金する",
                confirm_fn=confirm_action,
            )
            if reply is None:
                return
            now = self._clock.now()
            res = self._economy_svc.transfer(
                gid,
                uid,
                member.id,
                amount,
                TRANSFER_FEE_RATE,
                TRANSFER_DAILY_LIMIT,
                TRANSFER_MIN_GAMES,
                now=now,
            )
            if not res.ok:
                return await ui.reply_error(interaction, format_failure(res.reason), title="送金できません")
            await reply(
                embed=ui.ok_embed(
                    "送金しました",
                    f"{member.mention} に **{ui.pelica(res.received)}** を送りました。\n"
                    f"手数料: {ui.pelica(res.fee)}　あなたの残高: **{ui.pelica(res.sender_balance)}**",
                ),
                allowed_mentions=discord.AllowedMentions(users=[member]),
            )

    @group.command(name="履歴", description="最近の増減を表示（自分だけに見えます）")
    async def history(self, interaction: discord.Interaction):
        # 1ゲームは複数の行なので、多めに読んでまとめる
        rows = self._economy_svc.history(ui.guild_id_of(interaction), interaction.user.id, 60)
        await interaction.response.send_message(
            embed=embeds.history_embed(rows), ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot: "CasinoBot"):
    await bot.add_cog(
        EconomyCog(
            bot,
            config=bot.config,
            economy_svc=bot.services.economy,
            teiai_svc=bot.services.teiai,
            ranking_svc=bot.services.ranking,
            guild_repo=bot.repos.guild,
            sessions=bot.sessions,
            clock=bot.services.clock,
        )
    )
