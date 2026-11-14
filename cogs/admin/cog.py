"""サーバーへの追加を、Bot の持ち主が承認する仕組み（DM のボタン）と `/運営` コマンド入口。

Bot が新しいサーバーに追加されると、持ち主（owner_ids）に DM が届き、【許可する】【断って退出させる】を選べる。
コマンドの業務ロジックは approval_ops / economy_ops / content_ops へ分離し、ここはコマンド定義と委譲のみを持つ。
"""

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs.admin import approval_ops, content_ops, economy_ops
from cogs.admin.embeds import LEVEL_NAME as LEVEL_NAME  # noqa: F401
from cogs.admin.embeds import bulk_preview_lines as bulk_preview_lines  # noqa: F401
from cogs.admin.embeds import pachinko_stats_embed as pachinko_stats_embed  # noqa: F401
from cogs.admin.embeds import rtp_table_embed as rtp_table_embed  # noqa: F401
from cogs.admin.targets import (
    channel_choices,
    guild_choices,
    namespace_guild,
    pick_guild,
    pick_member,
    user_choices,
)
from cogs.admin.targets import parse_user_id as parse_user_id  # noqa: F401
from cogs.admin.targets import pick_channel as _pick_channel_impl
from cogs.admin.targets import postable as postable  # noqa: F401
from cogs.admin.views import AnnounceModal as AnnounceModal  # noqa: F401
from cogs.admin.views import ApprovalButton
from cogs.shared import ui
from domain import approval, events
from domain.games import rtplevel

MAX_IMAGE_BYTES = content_ops.MAX_IMAGE_BYTES
MAX_IMPORT_BYTES = content_ops.MAX_IMPORT_BYTES
RENOTIFY_AFTER = 3600  # 承認待ちのサーバーで誰かがコマンドを使ったとき、前の通知からこれ以上たっていれば催促する（秒）


class ApprovalCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.purging = set()  # 発言を消している最中のチャンネル（同じチャンネルを二重に消さない）

    async def cog_load(self):
        self.bot.add_dynamic_items(ApprovalButton)

    # --------------------------- 依頼の送信・承認イベント --------------------------- #
    async def request_approval(self, guild: discord.Guild) -> int:
        return await approval_ops.request_approval(self, guild, RENOTIFY_AFTER)

    async def ensure_requested(self, guild: discord.Guild, reset_denied: bool = False) -> None:
        await approval_ops.ensure_requested(self, guild, RENOTIFY_AFTER, reset_denied=reset_denied)

    async def announce_approved(self, guild: discord.Guild) -> None:
        await approval_ops.announce_approved(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await approval_ops.handle_guild_join(self, guild)

    @commands.Cog.listener()
    async def on_ready(self):
        await approval_ops.handle_ready(self)

    # ------------------- 許可・運営コマンド定義（Bot の持ち主だけ・DMでだけ使える） ------------------- #
    group = app_commands.Group(
        name="運営",
        description="[管理者] Bot を使えるサーバーの管理。Bot とのDMで使えます",
        allowed_contexts=app_commands.AppCommandContext(guild=False, dm_channel=True, private_channel=True),
    )
    raffle_group = app_commands.Group(name="抽選", description="[管理者] 抽選の開催・再掲示・中止", parent=group)
    quiz_group = app_commands.Group(name="クイズ", description="[管理者] クイズの登録・一覧・削除", parent=group)
    event_group = app_commands.Group(
        name="イベント", description="[管理者] 特別イベントを開く・終わらせる", parent=group
    )
    rtp_group = app_commands.Group(
        name="還元率", description="[管理者] ゲームごとの還元率のレベル（1〜6。3が標準）を変える", parent=group
    )

    async def _owner_only(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in approval.owner_ids(self.bot.config):
            await interaction.response.send_message(
                embed=ui.error_embed("権限がありません", "このコマンドは Bot の持ち主だけが使えます。"), ephemeral=True
            )
            return False
        return True

    def _allowed_guilds(self) -> list:
        return [g for g in self.bot.guilds if approval.is_allowed(self.bot.economy, self.bot.config, g.id, 0)]

    def _event_targets(self, guild_id: Optional[str]) -> list:
        return [self.bot.get_guild(int(guild_id))] if guild_id else self._allowed_guilds()

    def _guild_choices(self, current: str) -> list:
        return guild_choices(self.bot, current)

    async def guild_autocomplete(self, interaction: discord.Interaction, current: str):
        return self._guild_choices(current) if interaction.user.id in approval.owner_ids(self.bot.config) else []

    async def _pick_guild(self, interaction: discord.Interaction, guild_id: str) -> Optional[discord.Guild]:
        return await pick_guild(self.bot, interaction, guild_id)

    def _namespace_guild(self, interaction: discord.Interaction) -> Optional[discord.Guild]:
        return namespace_guild(self.bot, interaction)

    async def channel_autocomplete(self, interaction: discord.Interaction, current: str):
        guild = self._namespace_guild(interaction)
        if interaction.user.id not in approval.owner_ids(self.bot.config) or guild is None:
            return []
        return channel_choices(guild, current)

    async def _pick_channel(
        self, interaction: discord.Interaction, guild: Optional[discord.Guild], channel_id: Optional[str]
    ) -> tuple:
        return await _pick_channel_impl(interaction, guild, channel_id)

    async def user_autocomplete(self, interaction: discord.Interaction, current: str):
        if interaction.user.id not in approval.owner_ids(self.bot.config):
            return []
        guild = self._namespace_guild(interaction)
        return [] if guild is None else user_choices(guild, current)

    async def _pick_member(self, interaction: discord.Interaction, guild_id: str, user_id: str, verb: str):
        return await pick_member(self.bot, interaction, guild_id, user_id, verb)

    # ---- 経済・イベント・還元率・統計・履歴 ---- #
    @group.command(name="付与", description="[管理者] 指定したサーバーの指定した人に、ペリカを付与する")
    @app_commands.rename(guild_id="サーバー", user_id="ユーザー", amount="金額", reason="理由")
    @app_commands.describe(
        guild_id="ペリカを渡すサーバー（候補から選ぶ）",
        user_id="渡す相手（名前の候補が出なければ、ユーザーIDを貼る）",
        amount="付与する額（1〜10,000,000）",
        reason="台帳に残す理由（なくてもよい）",
    )
    async def grant(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        user_id: str,
        amount: app_commands.Range[int, 1, 10_000_000],
        reason: str = "",
    ):
        if await self._owner_only(interaction):
            await economy_ops.grant(self, interaction, guild_id, user_id, amount, reason)

    grant.autocomplete("guild_id")(guild_autocomplete)
    grant.autocomplete("user_id")(user_autocomplete)

    @group.command(name="残高設定", description="[管理者] 指定したサーバーの指定した人の残高を、指定した額にする")
    @app_commands.rename(guild_id="サーバー", user_id="ユーザー", amount="金額", reason="理由")
    @app_commands.describe(
        guild_id="対象のサーバー（候補から選ぶ）",
        user_id="対象の人（名前の候補が出なければ、ユーザーIDを貼る）",
        amount="残高をこの額にする（0〜1,000,000,000）。マイナス（例 -5000）なら、手元を0にして、その額の借金にする",
        reason="台帳に残す理由（なくてもよい）",
    )
    async def set_balance(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        user_id: str,
        amount: app_commands.Range[int, -1_000_000_000, 1_000_000_000],
        reason: str = "",
    ):
        if await self._owner_only(interaction):
            await economy_ops.set_balance(self, interaction, guild_id, user_id, amount, reason)

    set_balance.autocomplete("guild_id")(guild_autocomplete)
    set_balance.autocomplete("user_id")(user_autocomplete)

    @event_group.command(
        name="開始", description="[管理者] 特別イベントを開く。指定したゲームで勝つと、勝ち分に上乗せされる"
    )
    @app_commands.rename(game="ゲーム", minutes="時間", boost="上乗せ率", guild_id="サーバー", channel_id="チャンネル")
    @app_commands.describe(
        game="対象のゲーム",
        minutes="開く時間（分。省略で30分）",
        boost="勝ち分に上乗せする割合（％。省略で50）",
        guild_id="開くサーバー（候補から選ぶ。空なら許可されている全サーバー）",
        channel_id="告知するチャンネル（サーバーを選んでから、候補から選ぶ。空なら告知チャンネル）",
    )
    @app_commands.choices(game=[app_commands.Choice(name=name, value=name) for name in events.GAMES])
    async def event_start(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str],
        minutes: app_commands.Range[int, 1, 180] = 30,
        boost: app_commands.Range[int, 1, 300] = 50,
        guild_id: Optional[str] = None,
        channel_id: Optional[str] = None,
    ):
        if await self._owner_only(interaction):
            await economy_ops.event_start(self, interaction, game.value, minutes, boost, guild_id, channel_id)

    event_start.autocomplete("guild_id")(guild_autocomplete)
    event_start.autocomplete("channel_id")(channel_autocomplete)

    @event_group.command(name="終了", description="[管理者] 開催中の特別イベントを、すぐ終わらせる")
    @app_commands.rename(guild_id="サーバー")
    @app_commands.describe(guild_id="終わらせるサーバー（候補から選ぶ。空なら全サーバー）")
    async def event_stop(self, interaction: discord.Interaction, guild_id: Optional[str] = None):
        if await self._owner_only(interaction):
            await economy_ops.event_stop(self, interaction, guild_id)

    event_stop.autocomplete("guild_id")(guild_autocomplete)

    @rtp_group.command(name="設定", description="[管理者] ゲームの還元率レベル（1〜6。3が標準）を変える")
    @app_commands.rename(guild_id="サーバー", game="ゲーム", level="レベル")
    @app_commands.describe(
        guild_id="変えるサーバー（候補から選ぶ）",
        game="対象のゲーム（「すべて」で全ゲーム）",
        level="1(渋い)〜6(甘い)。3が標準",
    )
    @app_commands.choices(
        game=[app_commands.Choice(name="すべて", value="*")]
        + [app_commands.Choice(name=g, value=g) for g in rtplevel.GAMES],
    )
    @app_commands.choices(
        level=[
            app_commands.Choice(name=f"{n}　{LEVEL_NAME[n]}" + ("（標準）" if n == 3 else ""), value=n)
            for n in rtplevel.LEVELS
        ],
    )
    async def rtp_set(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        game: app_commands.Choice[str],
        level: app_commands.Choice[int],
    ):
        if await self._owner_only(interaction):
            await economy_ops.rtp_set(self, interaction, guild_id, game.value, level.value)

    rtp_set.autocomplete("guild_id")(guild_autocomplete)

    @rtp_group.command(name="一覧", description="[管理者] ゲームごとの還元率レベルと、還元率の目安を見る")
    @app_commands.rename(guild_id="サーバー")
    @app_commands.describe(guild_id="見るサーバー（候補から選ぶ。空なら全サーバー）")
    async def rtp_list(self, interaction: discord.Interaction, guild_id: Optional[str] = None):
        if await self._owner_only(interaction):
            await economy_ops.rtp_list(self, interaction, guild_id)

    rtp_list.autocomplete("guild_id")(guild_autocomplete)

    @group.command(name="パチンコ統計", description="[管理者] パチンコの当たり率を、設計した数字と実際の記録で比べる")
    @app_commands.rename(guild_id="サーバー", days="日数")
    @app_commands.describe(
        guild_id="見るサーバー（候補から選ぶ。空なら全サーバーの合計）", days="過去何日分か（空なら全期間）"
    )
    async def pachinko_stats(
        self,
        interaction: discord.Interaction,
        guild_id: Optional[str] = None,
        days: Optional[app_commands.Range[int, 1, 365]] = None,
    ):
        if await self._owner_only(interaction):
            await economy_ops.pachinko_stats(self, interaction, guild_id, days)

    pachinko_stats.autocomplete("guild_id")(guild_autocomplete)

    @group.command(
        name="クリック履歴", description="[管理者] 「働く」を押した記録（1時間ごと）を見る。自動操作の疑いの確認用"
    )
    @app_commands.rename(guild_id="サーバー", user_id="ユーザー", hours="時間")
    @app_commands.describe(
        guild_id="見るサーバー（候補から選ぶ）",
        user_id="見る相手（名前の候補が出なければ、ユーザーIDを貼る）",
        hours="さかのぼる時間（1〜336。省略で24）",
    )
    async def click_history(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        user_id: str,
        hours: app_commands.Range[int, 1, 336] = 24,
    ):
        if await self._owner_only(interaction):
            await economy_ops.click_history(self, interaction, guild_id, user_id, hours)

    click_history.autocomplete("guild_id")(guild_autocomplete)
    click_history.autocomplete("user_id")(user_autocomplete)

    # ---- コンテンツ・メッセージ操作 ---- #
    @group.command(
        name="競馬開催", description="[管理者] 臨時の競馬レースを開催する（出走の申し込み → 馬券の発売 → 発走）"
    )
    @app_commands.rename(guild_id="サーバー", entry_min="申し込み分", bet_min="発売分", channel_id="チャンネル")
    @app_commands.describe(
        guild_id="開催するサーバー（候補から選ぶ）",
        channel_id="開催の掲示・馬券・レースを出すチャンネル（候補から選ぶ。空なら告知チャンネル）",
        entry_min="出走の申し込みを受け付ける時間（分。1〜120。省略で5分）",
        bet_min="馬券を発売する時間（分。1〜120。省略で5分。そのあと2分で発走）",
    )
    async def special_race(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        entry_min: app_commands.Range[int, 1, 120] = 5,
        bet_min: app_commands.Range[int, 1, 120] = 5,
        channel_id: Optional[str] = None,
    ):
        if await self._owner_only(interaction):
            await content_ops.special_race(self, interaction, guild_id, entry_min, bet_min, channel_id)

    special_race.autocomplete("guild_id")(guild_autocomplete)
    special_race.autocomplete("channel_id")(channel_autocomplete)

    @group.command(name="お知らせ", description="[管理者] Bot から、文字や画像をお知らせとして告知チャンネルに出す")
    @app_commands.rename(guild_id="サーバー", image="画像", title="見出し", channel_id="チャンネル")
    @app_commands.describe(
        guild_id="送り先のサーバー（候補から選ぶ。空なら許可されている全サーバー）",
        image="添付する画像（なくてもよい。8MBまで）",
        title="見出し（なくてもよい。空なら「📢 お知らせ」）",
        channel_id="送り先のチャンネル（サーバーを選んでから、候補から選ぶ。空なら告知チャンネル）",
    )
    async def announce(
        self,
        interaction: discord.Interaction,
        guild_id: Optional[str] = None,
        image: Optional[discord.Attachment] = None,
        title: Optional[str] = None,
        channel_id: Optional[str] = None,
    ):
        if await self._owner_only(interaction):
            await content_ops.announce(self, interaction, guild_id, image, title, channel_id)

    announce.autocomplete("guild_id")(guild_autocomplete)
    announce.autocomplete("channel_id")(channel_autocomplete)

    @group.command(
        name="発言削除", description="[管理者] 指定したチャンネルの、Bot 自身の発言をすべて消す（元に戻せない）"
    )
    @app_commands.rename(guild_id="サーバー", channel_id="チャンネル")
    @app_commands.describe(
        guild_id="消すサーバー（候補から選ぶ）", channel_id="消すチャンネル（サーバーを選んでから、候補から選ぶ）"
    )
    async def purge_own_messages(self, interaction: discord.Interaction, guild_id: str, channel_id: str):
        if await self._owner_only(interaction):
            await content_ops.purge_own_messages(self, interaction, guild_id, channel_id)

    purge_own_messages.autocomplete("guild_id")(guild_autocomplete)
    purge_own_messages.autocomplete("channel_id")(channel_autocomplete)

    @raffle_group.command(
        name="開催", description="[管理者] 抽選を始める。参加費は500ペリカ以上（上限なし）。省略するとランダム"
    )
    @app_commands.rename(guild_id="サーバー", fee="掛金", minutes="募集時間", channel_id="チャンネル")
    @app_commands.describe(
        guild_id="抽選を開くサーバー（候補から選ぶ）",
        fee="参加費（500以上。省略すると500〜3,000のランダム）",
        minutes="募集する時間（分。省略で60分）",
        channel_id="募集を出すチャンネル（候補から選ぶ。空なら告知チャンネル）",
    )
    async def raffle_start(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        fee: Optional[app_commands.Range[int, 500]] = None,
        minutes: app_commands.Range[int, 1, 1440] = 60,
        channel_id: Optional[str] = None,
    ):
        if await self._owner_only(interaction):
            await content_ops.raffle_start(self, interaction, guild_id, fee, minutes, channel_id)

    raffle_start.autocomplete("guild_id")(guild_autocomplete)
    raffle_start.autocomplete("channel_id")(channel_autocomplete)

    @raffle_group.command(
        name="再掲示", description="[管理者] 募集中の抽選の投稿を、告知チャンネルに出し直す（古い投稿は消える）"
    )
    @app_commands.rename(guild_id="サーバー", channel_id="チャンネル")
    @app_commands.describe(
        guild_id="抽選を開いているサーバー（候補から選ぶ）",
        channel_id="出し直すチャンネル（候補から選ぶ。空なら告知チャンネル）",
    )
    async def raffle_repost(self, interaction: discord.Interaction, guild_id: str, channel_id: Optional[str] = None):
        if await self._owner_only(interaction):
            await content_ops.raffle_repost(self, interaction, guild_id, channel_id)

    raffle_repost.autocomplete("guild_id")(guild_autocomplete)
    raffle_repost.autocomplete("channel_id")(channel_autocomplete)

    @raffle_group.command(name="中止", description="[管理者] 募集中の抽選をやめて、参加費を全員に返す")
    @app_commands.rename(guild_id="サーバー")
    @app_commands.describe(guild_id="抽選を開いているサーバー（候補から選ぶ）")
    async def raffle_cancel(self, interaction: discord.Interaction, guild_id: str):
        if await self._owner_only(interaction):
            await content_ops.raffle_cancel(self, interaction, guild_id)

    raffle_cancel.autocomplete("guild_id")(guild_autocomplete)

    @quiz_group.command(
        name="追加", description="[管理者] クイズを登録する。入力フォームが開き、全サーバーに告知もできる"
    )
    @app_commands.rename(announce="告知", guild_id="サーバー", channel_id="チャンネル")
    @app_commands.describe(
        announce="問題を告知する（既定: する）",
        guild_id="告知するサーバー（候補から選ぶ。空なら許可されている全サーバー）",
        channel_id="告知するチャンネル（サーバーを選んでから、候補から選ぶ。空なら告知チャンネル）",
    )
    @app_commands.choices(
        announce=[app_commands.Choice(name="する", value=1), app_commands.Choice(name="しない", value=0)]
    )
    async def quiz_add(
        self,
        interaction: discord.Interaction,
        announce: Optional[app_commands.Choice[int]] = None,
        guild_id: Optional[str] = None,
        channel_id: Optional[str] = None,
    ):
        if await self._owner_only(interaction):
            await content_ops.quiz_add(self, interaction, announce is None or announce.value == 1, guild_id, channel_id)

    quiz_add.autocomplete("guild_id")(guild_autocomplete)
    quiz_add.autocomplete("channel_id")(channel_autocomplete)

    @quiz_group.command(
        name="一括追加", description="[管理者] 問題を書いたテキストファイルから、クイズをまとめて登録する"
    )
    @app_commands.rename(file="ファイル", announce="告知", guild_id="サーバー", channel_id="チャンネル")
    @app_commands.describe(
        file="問題を書いた .txt ファイル（書き方は docs/casino.md の「クイズ」）",
        announce="登録した問題を告知する（既定: しない）",
        guild_id="告知するサーバー（候補から選ぶ。空なら許可されている全サーバー）",
        channel_id="告知するチャンネル（サーバーを選んでから、候補から選ぶ。空なら告知チャンネル）",
    )
    @app_commands.choices(
        announce=[app_commands.Choice(name="しない", value=0), app_commands.Choice(name="する", value=1)]
    )
    async def quiz_bulk(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
        announce: Optional[app_commands.Choice[int]] = None,
        guild_id: Optional[str] = None,
        channel_id: Optional[str] = None,
    ):
        if await self._owner_only(interaction):
            await content_ops.quiz_bulk(
                self, interaction, file, announce is not None and announce.value == 1, guild_id, channel_id
            )

    quiz_bulk.autocomplete("guild_id")(guild_autocomplete)
    quiz_bulk.autocomplete("channel_id")(channel_autocomplete)

    @quiz_group.command(name="一覧", description="[管理者] 登録したクイズを見る")
    async def quiz_list(self, interaction: discord.Interaction):
        if await self._owner_only(interaction):
            await content_ops.quiz_list(self, interaction)

    @quiz_group.command(name="削除", description="[管理者] クイズを削除する（回答の記録も消える）")
    @app_commands.rename(quiz_id="番号")
    @app_commands.describe(quiz_id="`/運営 クイズ 一覧` の # の番号")
    async def quiz_delete(self, interaction: discord.Interaction, quiz_id: int):
        if await self._owner_only(interaction):
            await content_ops.quiz_delete(self, interaction, quiz_id)

    # ---- サーバー管理 ---- #
    @group.command(name="サーバー一覧", description="[管理者] 許可・承認待ち・断ったサーバーを表示する")
    async def server_list(self, interaction: discord.Interaction):
        if await self._owner_only(interaction):
            await approval_ops.server_list(self, interaction)

    @group.command(name="サーバー許可", description="[管理者] サーバーIDを指定して、利用を許可する")
    @app_commands.rename(guild_id="サーバーid")
    @app_commands.describe(guild_id="許可するサーバーのID（承認待ちの一覧にあるもの）")
    async def server_allow(self, interaction: discord.Interaction, guild_id: str):
        if await self._owner_only(interaction):
            await approval_ops.server_allow(self, interaction, guild_id)

    @group.command(name="サーバー取消", description="[管理者] 許可を取り消し、Bot をそのサーバーから退出させる")
    @app_commands.rename(guild_id="サーバーid")
    @app_commands.describe(guild_id="取り消すサーバーのID")
    async def server_revoke(self, interaction: discord.Interaction, guild_id: str):
        if await self._owner_only(interaction):
            await approval_ops.server_revoke(self, interaction, guild_id)


async def setup(bot):
    await bot.add_cog(ApprovalCog(bot))
