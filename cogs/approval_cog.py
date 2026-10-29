"""サーバーへの追加を、Bot の持ち主が承認する仕組み（DM のボタン）。

Bot が新しいサーバーに追加されると、持ち主（owner_ids）に DM が届き、【許可する】【断って退出させる】を選べる。
許可すると、そのサーバーIDが記録に保存され、再起動なしですぐ遊べるようになる。
ボタンは Bot を再起動しても動く（DynamicItem）。
"""

import time

import discord
from discord.ext import commands

from core import approval, ui

RENOTIFY_AFTER = 3600       # 承認待ちのサーバーで誰かがコマンドを使ったとき、前の通知からこれ以上たっていれば催促する（秒）


class ApprovalButton(discord.ui.DynamicItem[discord.ui.Button], template=r"guild-approve:(?P<action>allow|deny):(?P<gid>[0-9]+)"):
    def __init__(self, action: str, guild_id: int):
        allow = action == "allow"
        super().__init__(discord.ui.Button(
            label="許可する" if allow else "断って退出させる",
            style=discord.ButtonStyle.success if allow else discord.ButtonStyle.danger,
            custom_id=f"guild-approve:{action}:{guild_id}"))
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

    async def cog_load(self):
        self.bot.add_dynamic_items(ApprovalButton)

    @property
    def eco(self):
        return self.bot.economy

    # --------------------------- 依頼の送信 --------------------------- #
    async def request_approval(self, guild: discord.Guild) -> int:
        """持ち主に承認を依頼する DM を送る。届いた人数を返す（DM を受け取れない人には届かない）。"""
        self.eco.mark_guild_notified(guild.id)        # 送信（await）の前に記録する。同時に来た別のコマンドが、重ねて依頼しないように
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
            await channel.send(embed=ui.ok_embed("遊べるようになりました", "Bot の持ち主に承認されました。`/ヘルプ` で遊び方、`/ペリカ デイリー` からどうぞ。"))
        except discord.HTTPException:
            pass

    # ----------------------------- イベント ----------------------------- #
    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        if not approval.requires_approval(self.bot.config):
            return
        row = self.eco.get_guild_status(guild.id)
        if row and row["status"] == approval.APPROVED:
            return                          # 承認済みのサーバーに追加し直された
        await self.ensure_requested(guild, reset_denied=True)

    @commands.Cog.listener()
    async def on_ready(self):
        if not approval.requires_approval(self.bot.config):
            return
        for guild in self.bot.guilds:        # Bot が止まっている間に追加されたサーバーも拾う
            await self.ensure_requested(guild)


async def setup(bot):
    await bot.add_cog(ApprovalCog(bot))
