"""サーバーへの追加を、Bot の持ち主が承認する仕組み（DM のボタン）。

Bot が新しいサーバーに追加されると、持ち主（owner_ids）に DM が届き、【許可する】【断って退出させる】を選べる。
許可すると、そのサーバーIDが記録に保存され、再起動なしですぐ遊べるようになる。
ボタンは Bot を再起動しても動く（DynamicItem）。
"""

import re
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs._admin_embeds import LEVEL_NAME, bulk_preview_lines, pachinko_stats_embed, rtp_table_embed
from cogs._admin_purge import can_read_history
from cogs._admin_views import AnnounceModal, PurgeConfirm, QuizBulkConfirm
from core import approval, events, quizimport, ui
from games import rtplevel

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMPORT_BYTES = 200 * 1024


def valid_guild_id(text: str) -> bool:
    """サーバーIDとして正しい文字列（半角数字で、SQLite の整数に収まる正の値）。"""
    t = text.strip()
    return t.isascii() and t.isdigit() and 1 <= int(t) < 2**63


def postable(channel, guild: discord.Guild) -> bool:
    """Bot がそのチャンネルに、埋め込みを投稿できるか。"""
    perms = channel.permissions_for(guild.me)
    return bool(perms.view_channel and perms.send_messages and perms.embed_links)


def parse_user_id(text: str) -> Optional[int]:
    """`123`・`<@123>`・`<@!123>` のどれからでもユーザーIDを取り出す。取れなければ None。"""
    m = re.fullmatch(r"<@!?(\d+)>|(\d+)", text.strip())
    uid = int(m.group(1) or m.group(2)) if m else None
    return uid if uid is not None and 1 <= uid < 2**63 else None


RENOTIFY_AFTER = 3600  # 承認待ちのサーバーで誰かがコマンドを使ったとき、前の通知からこれ以上たっていれば催促する（秒）


class ApprovalButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"guild-approve:(?P<action>allow|deny):(?P<gid>[0-9]+)"
):
    def __init__(self, action: str, guild_id: int):
        allow = action == "allow"
        super().__init__(
            discord.ui.Button(
                label="許可する" if allow else "断って退出させる",
                style=discord.ButtonStyle.success if allow else discord.ButtonStyle.danger,
                custom_id=f"guild-approve:{action}:{guild_id}",
            )
        )
        self.action, self.guild_id = action, guild_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(match["action"], int(match["gid"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in approval.owner_ids(interaction.client.config):
            await interaction.response.send_message("これは Bot の持ち主だけが操作できます。", ephemeral=True)
            return False
        return True

    async def callback(self, interaction: discord.Interaction):
        bot, eco, gid = interaction.client, interaction.client.economy, self.guild_id
        guild = bot.get_guild(gid)
        row = eco.get_guild_status(gid) or {}
        name = guild.name if guild else row.get("name") or str(gid)
        if self.action == "allow":
            eco.set_guild_status(gid, approval.APPROVED, name, interaction.user.id)
            embed = ui.ok_embed("許可しました", f"**{name}**（`{gid}`）で遊べるようになりました。")
            if guild is not None:
                cog = bot.get_cog("ApprovalCog")
                if cog is not None:
                    await cog.announce_approved(guild)
        else:
            eco.set_guild_status(gid, approval.DENIED, name, interaction.user.id)
            left = ""
            if guild is not None:
                try:
                    await guild.leave()
                    left = "Bot はそのサーバーから退出しました。"
                except discord.HTTPException:
                    left = "退出できませんでした（コマンドは使えないままです）。"
            embed = ui.warn_embed("断りました", f"**{name}**（`{gid}`）の利用を断りました。{left}")
        await interaction.response.edit_message(embed=embed, view=None)


class ApprovalCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.purging = set()  # 発言を消している最中のチャンネル（同じチャンネルを二重に消さない）

    async def cog_load(self):
        self.bot.add_dynamic_items(ApprovalButton)

    @property
    def eco(self):
        return self.bot.economy

    # --------------------------- 依頼の送信 --------------------------- #
    async def request_approval(self, guild: discord.Guild) -> int:
        """持ち主に承認を依頼する DM を送る。届いた人数を返す（DM を受け取れない人には届かない）。"""
        self.eco.mark_guild_notified(
            guild.id
        )  # 送信（await）の前に記録する。同時に来た別のコマンドが、重ねて依頼しないように
        embed = ui.embed("🆕 新しいサーバーに追加されました", color=ui.GOLD)
        embed.add_field(name="サーバー", value=f"{guild.name}\n`{guild.id}`", inline=True)
        embed.add_field(name="持ち主", value=f"<@{guild.owner_id}>", inline=True)
        embed.add_field(name="メンバー数", value=f"{guild.member_count or '?'}人", inline=True)
        embed.set_footer(text="許可するまで、このサーバーではコマンドが使えません")
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        view = discord.ui.View(timeout=None)
        view.add_item(ApprovalButton("allow", guild.id))
        view.add_item(ApprovalButton("deny", guild.id))
        delivered = 0
        for uid in approval.owner_ids(self.bot.config):
            try:
                user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                await user.send(embed=embed, view=view)
                delivered += 1
            except discord.HTTPException as exc:
                print(f"承認の依頼を DM で送れません: owner={uid}: {exc!r}")
        return delivered

    async def ensure_requested(self, guild: discord.Guild, reset_denied: bool = False) -> None:
        """承認待ちとして記録し、まだ依頼していなければ依頼する。しばらく前の依頼なら催促する。"""
        if guild is None or guild.id in approval.preapproved(self.bot.config):
            return
        row = self.eco.get_guild_status(guild.id)
        if row is None or (row["status"] == approval.DENIED and reset_denied):
            self.eco.set_guild_status(guild.id, approval.PENDING, guild.name)
            await self.request_approval(guild)
        elif row["status"] == approval.PENDING:
            if row["notified_at"] is None or time.time() - row["notified_at"] > RENOTIFY_AFTER:
                await self.request_approval(guild)

    async def announce_approved(self, guild: discord.Guild) -> None:
        """承認されたことを、サーバー側にも知らせる（できなければ何もしない）。"""
        channel = guild.system_channel
        if channel is None or not channel.permissions_for(guild.me).send_messages:
            channel = next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
        if channel is None:
            return
        try:
            await channel.send(
                embed=ui.ok_embed(
                    "遊べるようになりました",
                    "Bot の持ち主に承認されました。`/help` で遊び方、`/ペリカ デイリー` からどうぞ。",
                )
            )
        except discord.HTTPException:
            pass

    # ----------------------------- イベント ----------------------------- #
    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        if not approval.requires_approval(self.bot.config):
            return
        row = self.eco.get_guild_status(guild.id)
        if row and row["status"] == approval.APPROVED:
            return  # 承認済みのサーバーに追加し直された
        await self.ensure_requested(guild, reset_denied=True)

    @commands.Cog.listener()
    async def on_ready(self):
        if not approval.requires_approval(self.bot.config):
            return
        for guild in self.bot.guilds:  # Bot が止まっている間に追加されたサーバーも拾う
            await self.ensure_requested(guild)

    # ------------------- 許可の管理（Bot の持ち主だけ・DMでだけ使える） ------------------- #
    group = app_commands.Group(
        name="運営",
        description="[管理者] Bot を使えるサーバーの管理。Bot とのDMで使えます",
        allowed_contexts=app_commands.AppCommandContext(guild=False, dm_channel=True, private_channel=True),
    )

    async def _owner_only(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in approval.owner_ids(self.bot.config):
            await interaction.response.send_message(
                embed=ui.error_embed("権限がありません", "このコマンドは Bot の持ち主だけが使えます。"), ephemeral=True
            )
            return False
        return True

    # ------------------- ペリカ・抽選・クイズの管理（これも `/運営` の下。サーバーは持ち主が指定する） ------------------- #
    raffle_group = app_commands.Group(name="抽選", description="[管理者] 抽選の開催・再掲示・中止", parent=group)
    quiz_group = app_commands.Group(name="クイズ", description="[管理者] クイズの登録・一覧・削除", parent=group)
    event_group = app_commands.Group(
        name="イベント", description="[管理者] 特別イベントを開く・終わらせる", parent=group
    )
    rtp_group = app_commands.Group(
        name="還元率", description="[管理者] ゲームごとの還元率のレベル（1〜6。3が標準）を変える", parent=group
    )

    def _guild_choices(self, current: str) -> list:
        cur = current.strip().lower()
        return [
            app_commands.Choice(name=g.name[:100], value=str(g.id))
            for g in self.bot.guilds
            if not cur or cur in g.name.lower() or cur in str(g.id)
        ][:25]

    async def guild_autocomplete(self, interaction: discord.Interaction, current: str):
        return self._guild_choices(current) if interaction.user.id in approval.owner_ids(self.bot.config) else []

    async def _pick_guild(self, interaction: discord.Interaction, guild_id: str) -> Optional[discord.Guild]:
        """入力されたサーバーを探す。見つからなければ理由を返して None。"""
        guild = self.bot.get_guild(int(guild_id)) if valid_guild_id(guild_id) else None
        if guild is None:
            await interaction.response.send_message(
                embed=ui.error_embed(
                    "サーバーが見つかりません", "候補から選ぶか、Bot が参加しているサーバーのIDを入れてください。"
                ),
                ephemeral=True,
            )
        return guild

    def _namespace_guild(self, interaction: discord.Interaction) -> Optional[discord.Guild]:
        """入力途中の `サーバー` で選ばれているサーバー（候補を絞るため）。"""
        ns = vars(interaction.namespace)
        raw = str(ns.get("guild_id") or ns.get("サーバー") or "")
        return self.bot.get_guild(int(raw)) if valid_guild_id(raw) else None

    async def channel_autocomplete(self, interaction: discord.Interaction, current: str):
        """選んだサーバーの、Bot が投稿できるチャンネルの候補。"""
        guild = self._namespace_guild(interaction)
        if interaction.user.id not in approval.owner_ids(self.bot.config) or guild is None:
            return []
        cur = current.strip().lower()
        return [
            app_commands.Choice(name=f"#{ch.name}"[:100], value=str(ch.id))
            for ch in guild.text_channels
            if postable(ch, guild) and (not cur or cur in ch.name.lower())
        ][:25]

    async def _pick_channel(
        self, interaction: discord.Interaction, guild: Optional[discord.Guild], channel_id: Optional[str]
    ) -> tuple:
        """指定されたチャンネルを探す。(続けてよいか, チャンネルか None) を返す。続けてはいけないときは、理由を返信済み。"""
        if not channel_id:
            return True, None
        problem = None
        channel = None
        if guild is None:
            problem = "チャンネルを指定するときは、`サーバー` も指定してください。"
        else:
            channel = guild.get_channel(int(channel_id)) if channel_id.strip().isdigit() else None
            if channel is None or not hasattr(channel, "send"):
                problem = f"{guild.name} にそのチャンネルが見つかりません。候補から選んでください。"
            elif not postable(channel, guild):
                problem = f"#{channel.name} には、Bot が投稿する権限（チャンネルを見る・メッセージを送る・埋め込みリンク）がありません。"
        if problem:
            await interaction.response.send_message(
                embed=ui.error_embed("チャンネルを使えません", problem), ephemeral=True
            )
            return False, None
        return True, channel

    async def user_autocomplete(self, interaction: discord.Interaction, current: str):
        if interaction.user.id not in approval.owner_ids(self.bot.config):
            return []
        guild = self._namespace_guild(interaction)
        cur = current.strip().lower()
        if guild is None or not cur:
            return []
        return [
            app_commands.Choice(name=f"{m.display_name} ({m.name})"[:100], value=str(m.id))
            for m in guild.members
            if not m.bot and (cur in m.display_name.lower() or cur in m.name.lower())
        ][:25]  # キャッシュにいる人だけ。出なければIDを貼る

    async def _pick_member(self, interaction: discord.Interaction, guild_id: str, user_id: str, verb: str):
        """管理コマンドで、サーバーと相手を確かめる。問題があれば、その場で返信して None（呼び出し側は何もせず戻る）。"""
        guild = await self._pick_guild(interaction, guild_id)
        if guild is None:
            return None
        uid = parse_user_id(user_id)
        if uid is None:
            return await interaction.response.send_message(
                embed=ui.error_embed(
                    "ユーザーを指定してください", "候補から選ぶか、ユーザーID（数字）を入れてください。"
                ),
                ephemeral=True,
            )
        try:
            member = await guild.fetch_member(uid)
        except discord.NotFound:
            return await interaction.response.send_message(
                embed=ui.error_embed("そのサーバーにいません", f"`{uid}` は {guild.name} のメンバーではありません。"),
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            return await interaction.response.send_message(
                embed=ui.error_embed("確認できません", f"メンバーを確認できませんでした: {exc}"), ephemeral=True
            )
        if member.bot:
            return await interaction.response.send_message(
                embed=ui.error_embed(f"Bot には{verb}できません"), ephemeral=True
            )
        return guild, member

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
        if not await self._owner_only(interaction):
            return
        picked = await self._pick_member(interaction, guild_id, user_id, "付与")
        if picked is None:
            return
        guild, member = picked
        uid = member.id
        balance = self.bot.economy.for_guild(guild.id).credit(uid, amount, f"付与（管理）{reason}".strip())
        await interaction.response.send_message(
            embed=ui.ok_embed(
                "付与しました",
                f"{guild.name} の {member.mention}（{member.display_name}）に **{ui.pelica(amount)}** を付与しました（残高 {ui.pelica(balance)}）。"
                + (f"\n理由: {reason}" if reason else ""),
            ),
            ephemeral=True,
        )

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
        if not await self._owner_only(interaction):
            return
        picked = await self._pick_member(interaction, guild_id, user_id, "設定")
        if picked is None:
            return
        guild, member = picked
        before, after, debt_before, debt_after = self.bot.economy.for_guild(guild.id).set_balance(
            member.id, amount, f"残高の設定（管理）{reason}".strip()
        )
        await interaction.response.send_message(
            embed=ui.ok_embed(
                "残高を設定しました",
                f"{guild.name} の {member.mention}（{member.display_name}）の残高を "
                f"**{ui.pelica(before)} → {ui.pelica(after)}** にしました（差額は台帳に残ります）。"
                + (f"\n帝愛の借金: **{ui.pelica(debt_before)} → {ui.pelica(debt_after)}**" if amount < 0 else "")
                + (f"\n理由: {reason}" if reason else ""),
            ),
            ephemeral=True,
        )

    set_balance.autocomplete("guild_id")(guild_autocomplete)
    set_balance.autocomplete("user_id")(user_autocomplete)

    # ---- 特別イベント ---- #
    def _event_targets(self, guild_id: Optional[str]) -> list:
        return (
            [self.bot.get_guild(int(guild_id))]
            if guild_id
            else [g for g in self.bot.guilds if approval.is_allowed(self.bot.economy, self.bot.config, g.id, 0)]
        )

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
        guild_id: str = None,
        channel_id: str = None,
    ):
        if not await self._owner_only(interaction):
            return
        picked = await self._pick_guild(interaction, guild_id) if guild_id else None
        if guild_id and picked is None:
            return
        ok, channel = await self._pick_channel(interaction, picked, channel_id)
        if not ok:
            return
        targets = self._event_targets(guild_id)
        if not targets:
            return await interaction.response.send_message(
                embed=ui.error_embed("開く先がありません", "許可されているサーバーがありません。"), ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        cog = self.bot.get_cog("EventCog")
        lines = [(await cog.start_manual(g, game.value, boost, minutes, channel))[1] for g in targets]
        await interaction.followup.send(
            embed=ui.ok_embed(f"特別イベントを開きました（{len(targets)}サーバー）", "\n".join(lines)[:3900]),
            ephemeral=True,
        )

    event_start.autocomplete("guild_id")(guild_autocomplete)
    event_start.autocomplete("channel_id")(channel_autocomplete)

    @event_group.command(name="終了", description="[管理者] 開催中の特別イベントを、すぐ終わらせる")
    @app_commands.rename(guild_id="サーバー")
    @app_commands.describe(guild_id="終わらせるサーバー（候補から選ぶ。空なら全サーバー）")
    async def event_stop(self, interaction: discord.Interaction, guild_id: str = None):
        if not await self._owner_only(interaction):
            return
        if guild_id and await self._pick_guild(interaction, guild_id) is None:
            return
        await interaction.response.defer(ephemeral=True)
        cog = self.bot.get_cog("EventCog")
        lines = [(await cog.stop_manual(g))[1] for g in self._event_targets(guild_id)]
        await interaction.followup.send(
            embed=ui.embed("特別イベントを終わらせました", "\n".join(lines)[:3900] or "対象がありません。", ui.INFO),
            ephemeral=True,
        )

    event_stop.autocomplete("guild_id")(guild_autocomplete)

    # ---- 還元率 ---- #
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
        if not await self._owner_only(interaction):
            return
        guild = await self._pick_guild(interaction, guild_id)
        if guild is None:
            return
        eco = self.bot.economy.for_guild(guild.id)
        changed = eco.rtp_level_set(None if game.value == "*" else game.value, level.value)
        await interaction.response.send_message(
            embeds=[
                ui.ok_embed(
                    "還元率のレベルを変えました",
                    f"{guild.name}: {'、'.join(changed)} → レベル {level.value}（{LEVEL_NAME[level.value]}）",
                ),
                rtp_table_embed(eco.rtp_levels(), f"🎚️ {guild.name} の還元率"),
            ],
            ephemeral=True,
        )

    rtp_set.autocomplete("guild_id")(guild_autocomplete)

    @rtp_group.command(name="一覧", description="[管理者] ゲームごとの還元率レベルと、還元率の目安を見る")
    @app_commands.rename(guild_id="サーバー")
    @app_commands.describe(guild_id="見るサーバー（候補から選ぶ。空なら全サーバー）")
    async def rtp_list(self, interaction: discord.Interaction, guild_id: str = None):
        if not await self._owner_only(interaction):
            return
        guild = await self._pick_guild(interaction, guild_id) if guild_id else None
        if guild_id and guild is None:
            return
        targets = (
            [guild]
            if guild
            else [g for g in self.bot.guilds if approval.is_allowed(self.bot.economy, self.bot.config, g.id, 0)]
        )
        embeds = [
            rtp_table_embed(self.bot.economy.for_guild(g.id).rtp_levels(), f"🎚️ {g.name} の還元率") for g in targets[:10]
        ]
        await interaction.response.send_message(embeds=embeds or [ui.error_embed("対象がありません")], ephemeral=True)

    rtp_list.autocomplete("guild_id")(guild_autocomplete)

    # ---- パチンコ統計 ---- #
    @group.command(name="パチンコ統計", description="[管理者] パチンコの当たり率を、設計した数字と実際の記録で比べる")
    @app_commands.rename(guild_id="サーバー", days="日数")
    @app_commands.describe(
        guild_id="見るサーバー（候補から選ぶ。空なら全サーバーの合計）", days="過去何日分か（空なら全期間）"
    )
    async def pachinko_stats(
        self, interaction: discord.Interaction, guild_id: str = None, days: app_commands.Range[int, 1, 365] = None
    ):
        if not await self._owner_only(interaction):
            return
        guild = None
        if guild_id:
            guild = await self._pick_guild(interaction, guild_id)
            if guild is None:
                return
        since = time.time() - days * 86400 if days else None
        data = self.bot.economy.pachinko_stats(guild.id if guild else None, since)
        scope = (guild.name if guild else "全サーバー") + (f"・過去{days}日" if days else "・全期間")
        await interaction.response.send_message(embed=pachinko_stats_embed(data, scope), ephemeral=True)

    pachinko_stats.autocomplete("guild_id")(guild_autocomplete)

    # ---- 競馬 ---- #
    @group.command(
        name="競馬開催", description="[管理者] 臨時の競馬レースを開催する（出走の申し込み → 馬券の発売 → 発走）"
    )
    @app_commands.rename(guild_id="サーバー", entry_min="申し込み分", bet_min="発売分")
    @app_commands.describe(
        guild_id="開催するサーバー（候補から選ぶ）",
        entry_min="出走の申し込みを受け付ける時間（分。1〜120。省略で5分）",
        bet_min="馬券を発売する時間（分。1〜120。省略で5分。そのあと2分で発走）",
    )
    async def special_race(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        entry_min: app_commands.Range[int, 1, 120] = 5,
        bet_min: app_commands.Range[int, 1, 120] = 5,
    ):
        if not await self._owner_only(interaction):
            return
        guild = await self._pick_guild(interaction, guild_id)
        if guild is None:
            return
        cog = self.bot.get_cog("HorseCog")
        if cog is None:
            return await interaction.response.send_message(
                embed=ui.error_embed("競馬が読み込まれていません"), ephemeral=True
            )
        race_id = await cog.start_special(guild, entry_min, bet_min)
        await interaction.response.send_message(
            embed=ui.ok_embed(
                "臨時レースを開催しました",
                f"{guild.name} で、臨時レース（#{race_id}）の申し込みを始めました。申し込み {entry_min}分 → 馬券 {bet_min}分 → 発走（2分後）。",
            ),
            ephemeral=True,
        )

    special_race.autocomplete("guild_id")(guild_autocomplete)

    # ---- お知らせ ---- #
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
        guild_id: str = None,
        image: discord.Attachment = None,
        title: str = None,
        channel_id: str = None,
    ):
        if not await self._owner_only(interaction):
            return
        if image is not None and not (
            (image.content_type or "").startswith("image/") and image.size <= MAX_IMAGE_BYTES
        ):
            return await interaction.response.send_message(
                embed=ui.error_embed("画像を使えません", "画像ファイル（8MBまで）を添付してください。"), ephemeral=True
            )
        if guild_id:
            guild = await self._pick_guild(interaction, guild_id)
            if guild is None:
                return
            guilds = [guild]
        else:
            guilds = [g for g in self.bot.guilds if approval.is_allowed(self.bot.economy, self.bot.config, g.id, 0)]
        ok, channel = await self._pick_channel(interaction, guilds[0] if guild_id else None, channel_id)
        if not ok:
            return
        if not guilds:
            return await interaction.response.send_message(
                embed=ui.error_embed("送り先がありません", "許可されているサーバーがありません。"), ephemeral=True
            )
        await interaction.response.send_modal(AnnounceModal(self, guilds, (title or "").strip()[:200], image, channel))

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
        if not await self._owner_only(interaction):
            return
        guild = await self._pick_guild(interaction, guild_id)
        if guild is None:
            return
        ok, channel = await self._pick_channel(interaction, guild, channel_id)
        if not ok:
            return
        if not can_read_history(channel, guild):
            return await interaction.response.send_message(
                embed=ui.error_embed(
                    "履歴を読めません",
                    f"Bot は #{channel.name} の「メッセージ履歴を読む」権限がありません。招待URLの権限に足して（README を参照）、もう一度試してください。",
                ),
                ephemeral=True,
            )
        warning = (
            f"**{guild.name}** の **#{channel.name}** にある、**Bot 自身の発言を、すべて消します。元に戻せません。**\n"
            "ランキング掲示板、チャートの自動更新、クイズ・貸付・抽選の投稿（ボタン付き）も消え、ボタンは使えなくなります。"
            "Bot 以外の人の発言には触れません。件数が多いと、数分かかります。"
        )
        await interaction.response.send_message(
            embeds=[ui.embed("🧹 発言の削除（確認）", warning, ui.WARN)],
            view=PurgeConfirm(self, interaction.user.id, channel),
            ephemeral=True,
        )

    purge_own_messages.autocomplete("guild_id")(guild_autocomplete)
    purge_own_messages.autocomplete("channel_id")(channel_autocomplete)

    # ---- 抽選 ---- #
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
        fee: app_commands.Range[int, 500] = None,
        minutes: app_commands.Range[int, 1, 1440] = 60,
        channel_id: str = None,
    ):
        if not await self._owner_only(interaction):
            return
        guild = await self._pick_guild(interaction, guild_id)
        if guild is None:
            return
        ok, channel = await self._pick_channel(interaction, guild, channel_id)
        if not ok:
            return
        await interaction.response.defer(ephemeral=True)
        ok, text = await self.bot.get_cog("RaffleCog").host(guild, interaction.user.id, fee, minutes, channel)
        await interaction.followup.send(
            embed=ui.ok_embed("抽選を始めました", text) if ok else ui.error_embed("始められません", text),
            ephemeral=True,
        )

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
    async def raffle_repost(self, interaction: discord.Interaction, guild_id: str, channel_id: str = None):
        if not await self._owner_only(interaction):
            return
        guild = await self._pick_guild(interaction, guild_id)
        if guild is None:
            return
        ok, channel = await self._pick_channel(interaction, guild, channel_id)
        if not ok:
            return
        await interaction.response.defer(ephemeral=True)
        ok, text = await self.bot.get_cog("RaffleCog").repost(guild, channel)
        await interaction.followup.send(
            embed=ui.ok_embed("出し直しました", text) if ok else ui.error_embed("出し直せません", text), ephemeral=True
        )

    raffle_repost.autocomplete("guild_id")(guild_autocomplete)
    raffle_repost.autocomplete("channel_id")(channel_autocomplete)

    @raffle_group.command(name="中止", description="[管理者] 募集中の抽選をやめて、参加費を全員に返す")
    @app_commands.rename(guild_id="サーバー")
    @app_commands.describe(guild_id="抽選を開いているサーバー（候補から選ぶ）")
    async def raffle_cancel(self, interaction: discord.Interaction, guild_id: str):
        if not await self._owner_only(interaction):
            return
        guild = await self._pick_guild(interaction, guild_id)
        if guild is None:
            return
        await interaction.response.defer(ephemeral=True)
        ok, text = await self.bot.get_cog("RaffleCog").cancel(guild)
        await interaction.followup.send(
            embed=ui.ok_embed("中止しました", text) if ok else ui.error_embed("中止できません", text), ephemeral=True
        )

    raffle_cancel.autocomplete("guild_id")(guild_autocomplete)

    # ---- クイズ ---- #
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
        announce: app_commands.Choice[int] = None,
        guild_id: str = None,
        channel_id: str = None,
    ):
        if not await self._owner_only(interaction):
            return
        guild = await self._pick_guild(interaction, guild_id) if guild_id else None
        if guild_id and guild is None:
            return
        ok, channel = await self._pick_channel(interaction, guild, channel_id)
        if not ok:
            return
        await interaction.response.send_modal(
            self.bot.get_cog("QuizCog").add_modal(announce is None or announce.value == 1, guild, channel)
        )

    quiz_add.autocomplete("guild_id")(guild_autocomplete)
    quiz_add.autocomplete("channel_id")(channel_autocomplete)

    @quiz_group.command(
        name="一括追加", description="[管理者] 問題を書いたテキストファイルから、クイズをまとめて登録する"
    )
    @app_commands.rename(file="ファイル", announce="告知", guild_id="サーバー", channel_id="チャンネル")
    @app_commands.describe(
        file="問題を書いた .txt ファイル（書き方は README の「クイズ」）",
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
        announce: app_commands.Choice[int] = None,
        guild_id: str = None,
        channel_id: str = None,
    ):
        if not await self._owner_only(interaction):
            return
        guild = await self._pick_guild(interaction, guild_id) if guild_id else None
        if guild_id and guild is None:
            return
        ok, channel = await self._pick_channel(interaction, guild, channel_id)
        if not ok:
            return
        if file.size > MAX_IMPORT_BYTES or not (
            file.filename.lower().endswith((".txt", ".md", ".text")) or (file.content_type or "").startswith("text/")
        ):
            return await interaction.response.send_message(
                embed=ui.error_embed(
                    "ファイルを使えません",
                    f"テキストファイル（.txt、{MAX_IMPORT_BYTES // 1024}KBまで）を添付してください。",
                ),
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)
        try:
            text = (await file.read()).decode("utf-8-sig")
        except UnicodeDecodeError:
            return await interaction.followup.send(
                embed=ui.error_embed("文字コードを読めません", "UTF-8 で保存したテキストファイルを添付してください。"),
                ephemeral=True,
            )
        quizzes, errors = quizimport.parse_bulk(text)
        if not errors:
            existing = self.bot.economy.quiz_existing_questions()
            errors = [
                f"{i}問目: すでに登録されている問題です（同じ問題文）。"
                for i, q in enumerate(quizzes, start=1)
                if q["question"] in existing
            ]
        if errors:
            more = f"\n…ほか{len(errors) - 15}件" if len(errors) > 15 else ""
            return await interaction.followup.send(
                embed=ui.error_embed(
                    f"登録できません（誤り{len(errors)}件・1問も登録していません）", "\n".join(errors[:15]) + more
                ),
                ephemeral=True,
            )
        view = QuizBulkConfirm(
            self, interaction.user.id, quizzes, announce is not None and announce.value == 1, guild, channel
        )
        head = ui.embed(
            f"📰 クイズを{len(quizzes)}問登録します",
            bulk_preview_lines(quizzes)
            + "\n\n問題なければ「登録する」を押してください。"
            + (f"\n（告知: {view.target_text()}に出します）" if view.announce else "\n（告知: しません）"),
            ui.WARN,
        )
        await interaction.followup.send(embeds=[head], view=view, ephemeral=True)

    quiz_bulk.autocomplete("guild_id")(guild_autocomplete)
    quiz_bulk.autocomplete("channel_id")(channel_autocomplete)

    @quiz_group.command(name="一覧", description="[管理者] 登録したクイズを見る")
    async def quiz_list(self, interaction: discord.Interaction):
        if not await self._owner_only(interaction):
            return
        await interaction.response.send_message(embed=self.bot.get_cog("QuizCog").listing_embed(), ephemeral=True)

    @quiz_group.command(name="削除", description="[管理者] クイズを削除する（回答の記録も消える）")
    @app_commands.rename(quiz_id="番号")
    @app_commands.describe(quiz_id="`/運営 クイズ 一覧` の # の番号")
    async def quiz_delete(self, interaction: discord.Interaction, quiz_id: int):
        if not await self._owner_only(interaction):
            return
        if not self.bot.get_cog("QuizCog").remove(quiz_id):
            return await interaction.response.send_message(
                embed=ui.error_embed("その番号の問題はありません"), ephemeral=True
            )
        await interaction.response.send_message(embed=ui.ok_embed(f"クイズ #{quiz_id} を削除しました"), ephemeral=True)

    @group.command(name="サーバー一覧", description="[管理者] 許可・承認待ち・断ったサーバーを表示する")
    async def server_list(self, interaction: discord.Interaction):
        if not await self._owner_only(interaction):
            return
        joined = {g.id: g for g in self.bot.guilds}
        rows = {r["guild_id"]: r for r in self.eco.list_guild_status()}
        pre = approval.preapproved(self.bot.config)
        groups = {"✅ 許可": [], "⏳ 承認待ち": [], "🚫 断った": []}
        for gid in pre:
            groups["✅ 許可"].append(
                (gid, joined[gid].name if gid in joined else "（設定ファイルで許可）", gid in joined)
            )
        for gid, r in rows.items():
            if gid in pre:
                continue
            key = {approval.APPROVED: "✅ 許可", approval.PENDING: "⏳ 承認待ち", approval.DENIED: "🚫 断った"}[
                r["status"]
            ]
            groups[key].append((gid, joined[gid].name if gid in joined else (r["name"] or "?"), gid in joined))
        e = ui.embed(
            "🌐 サーバーの許可状況",
            f"承認制: **{'ON' if approval.requires_approval(self.bot.config) else 'OFF'}**　参加中: {len(joined)}サーバー",
            ui.INFO,
        )
        for title, items in groups.items():
            e.add_field(
                name=f"{title}（{len(items)}）",
                value="\n".join(f"{'🟢' if on else '⚪'} {name}　`{gid}`" for gid, name, on in items)[:1024] or "なし",
                inline=False,
            )
        e.set_footer(text="🟢 = いま参加中 / ⚪ = 参加していない")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @group.command(name="サーバー許可", description="[管理者] サーバーIDを指定して、利用を許可する")
    @app_commands.rename(guild_id="サーバーid")
    @app_commands.describe(guild_id="許可するサーバーのID（承認待ちの一覧にあるもの）")
    async def server_allow(self, interaction: discord.Interaction, guild_id: str):
        if not await self._owner_only(interaction):
            return
        if not valid_guild_id(guild_id):
            return await interaction.response.send_message(
                embed=ui.error_embed("入力エラー", "サーバーidは数字だけで入力してください。"), ephemeral=True
            )
        gid = int(guild_id)
        guild = self.bot.get_guild(gid)
        self.eco.set_guild_status(gid, approval.APPROVED, guild.name if guild else None, interaction.user.id)
        if guild is not None:
            await self.announce_approved(guild)
        await interaction.response.send_message(
            embed=ui.ok_embed(
                "許可しました",
                f"`{gid}`"
                + (f"（{guild.name}）" if guild else "（まだ参加していません。追加されたらすぐ遊べます）")
                + " で遊べます。",
            ),
            ephemeral=True,
        )

    @group.command(name="サーバー取消", description="[管理者] 許可を取り消し、Bot をそのサーバーから退出させる")
    @app_commands.rename(guild_id="サーバーid")
    @app_commands.describe(guild_id="取り消すサーバーのID")
    async def server_revoke(self, interaction: discord.Interaction, guild_id: str):
        if not await self._owner_only(interaction):
            return
        if not valid_guild_id(guild_id):
            return await interaction.response.send_message(
                embed=ui.error_embed("入力エラー", "サーバーidは数字だけで入力してください。"), ephemeral=True
            )
        gid = int(guild_id)
        if gid in approval.preapproved(self.bot.config):
            return await interaction.response.send_message(
                embed=ui.warn_embed(
                    "取り消せません",
                    "設定ファイル（allowed_guild_ids）で許可されているサーバーです。設定ファイルから外してください。",
                ),
                ephemeral=True,
            )
        guild = self.bot.get_guild(gid)
        self.eco.set_guild_status(gid, approval.DENIED, guild.name if guild else None, interaction.user.id)
        left = ""
        if guild is not None:
            try:
                await guild.leave()
                left = "Bot はそのサーバーから退出しました。"
            except discord.HTTPException:
                left = "退出はできませんでしたが、コマンドは使えなくなりました。"
        await interaction.response.send_message(
            embed=ui.ok_embed("取り消しました", f"`{gid}` の許可を取り消しました。{left}"), ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(ApprovalCog(bot))
