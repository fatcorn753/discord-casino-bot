"""サーバー承認・許可・取消の業務フロー（`/運営 サーバー*` と自動承認依頼）。"""

import time

import discord

from cogs.admin.embeds import approval_request_embed, approved_notice_embed, server_list_embed
from cogs.admin.targets import valid_guild_id
from cogs.admin.views import ApprovalButton
from cogs.shared import ui
from domain import approval


async def request_approval(cog, guild: discord.Guild, renotify_after: int) -> int:
    """持ち主に承認を依頼する DM を送る。届いた人数を返す（DM を受け取れない人には届かない）。"""
    cog.bot.economy.mark_guild_notified(
        guild.id
    )  # 送信（await）の前に記録する。同時に来た別のコマンドが、重ねて依頼しないように
    embed = approval_request_embed(guild)
    view = discord.ui.View(timeout=None)
    view.add_item(ApprovalButton("allow", guild.id))
    view.add_item(ApprovalButton("deny", guild.id))
    delivered = 0
    for uid in approval.owner_ids(cog.bot.config):
        try:
            user = cog.bot.get_user(uid) or await cog.bot.fetch_user(uid)
            await user.send(embed=embed, view=view)
            delivered += 1
        except discord.HTTPException as exc:
            print(f"承認の依頼を DM で送れません: owner={uid}: {exc!r}")
    return delivered


async def ensure_requested(cog, guild: discord.Guild, renotify_after: int, reset_denied: bool = False) -> None:
    """承認待ちとして記録し、まだ依頼していなければ依頼する。しばらく前の依頼なら催促する。"""
    if guild is None or guild.id in approval.preapproved(cog.bot.config):
        return
    row = cog.bot.economy.get_guild_status(guild.id)
    if row is None or (row["status"] == approval.DENIED and reset_denied):
        cog.bot.economy.set_guild_status(guild.id, approval.PENDING, guild.name)
        await cog.request_approval(guild)
    elif row["status"] == approval.PENDING:
        if row["notified_at"] is None or time.time() - row["notified_at"] > renotify_after:
            await cog.request_approval(guild)


async def announce_approved(guild: discord.Guild) -> None:
    """承認されたことを、サーバー側にも知らせる（できなければ何もしない）。"""
    channel = guild.system_channel
    if channel is None or not channel.permissions_for(guild.me).send_messages:
        channel = next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
    if channel is None:
        return
    try:
        await channel.send(embed=approved_notice_embed())
    except discord.HTTPException:
        pass


async def handle_guild_join(cog, guild: discord.Guild) -> None:
    if not approval.requires_approval(cog.bot.config):
        return
    row = cog.bot.economy.get_guild_status(guild.id)
    if row and row["status"] == approval.APPROVED:
        return  # 承認済みのサーバーに追加し直された
    await cog.ensure_requested(guild, reset_denied=True)


async def handle_ready(cog) -> None:
    if not approval.requires_approval(cog.bot.config):
        return
    for guild in cog.bot.guilds:  # Bot が止まっている間に追加されたサーバーも拾う
        await cog.ensure_requested(guild)


async def server_list(cog, interaction: discord.Interaction) -> None:
    e = server_list_embed(cog.bot.guilds, cog.bot.economy.list_guild_status(), cog.bot.config)
    await interaction.response.send_message(embed=e, ephemeral=True)


async def server_allow(cog, interaction: discord.Interaction, guild_id: str) -> None:
    if not valid_guild_id(guild_id):
        await interaction.response.send_message(
            embed=ui.error_embed("入力エラー", "サーバーidは数字だけで入力してください。"), ephemeral=True
        )
        return
    gid = int(guild_id)
    guild = cog.bot.get_guild(gid)
    cog.bot.economy.set_guild_status(gid, approval.APPROVED, guild.name if guild else None, interaction.user.id)
    if guild is not None:
        await cog.announce_approved(guild)
    await interaction.response.send_message(
        embed=ui.ok_embed(
            "許可しました",
            f"`{gid}`"
            + (f"（{guild.name}）" if guild else "（まだ参加していません。追加されたらすぐ遊べます）")
            + " で遊べます。",
        ),
        ephemeral=True,
    )


async def server_revoke(cog, interaction: discord.Interaction, guild_id: str) -> None:
    if not valid_guild_id(guild_id):
        await interaction.response.send_message(
            embed=ui.error_embed("入力エラー", "サーバーidは数字だけで入力してください。"), ephemeral=True
        )
        return
    gid = int(guild_id)
    if gid in approval.preapproved(cog.bot.config):
        await interaction.response.send_message(
            embed=ui.warn_embed(
                "取り消せません",
                "設定ファイル（allowed_guild_ids）で許可されているサーバーです。設定ファイルから外してください。",
            ),
            ephemeral=True,
        )
        return
    guild = cog.bot.get_guild(gid)
    cog.bot.economy.set_guild_status(gid, approval.DENIED, guild.name if guild else None, interaction.user.id)
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
