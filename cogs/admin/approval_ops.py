"""サーバー承認・許可・取消の業務フロー（`/運営 サーバー*` と自動承認依頼）。"""

import logging
from typing import TYPE_CHECKING

import discord

from cogs.admin.embeds import approval_request_embed, approved_notice_embed, server_list_embed
from cogs.admin.targets import valid_guild_id
from cogs.admin.views import ApprovalButton
from cogs.shared import ui
from domain import approval

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from cogs.admin.cog import ApprovalCog


async def request_approval(cog: "ApprovalCog", guild: discord.Guild, renotify_after: int) -> int:
    """持ち主に承認を依頼する DM を送る。届いた人数を返す（DM を受け取れない人には届かない）。"""
    cog._guild_repo.mark_guild_notified(
        guild.id
    )  # 送信（await）の前に記録する。同時に来た別のコマンドが、重ねて依頼しないように
    embed = approval_request_embed(guild)
    view = discord.ui.View(timeout=None)
    view.add_item(ApprovalButton("allow", guild.id))
    view.add_item(ApprovalButton("deny", guild.id))
    delivered = 0
    for uid in approval.owner_ids(cog._config):
        try:
            user = cog.bot.get_user(uid) or await cog.bot.fetch_user(uid)
            await user.send(embed=embed, view=view)
            delivered += 1
        except discord.HTTPException as exc:
            _log.warning(f"承認の依頼を DM で送れません: owner={uid}: {exc!r}")
    return delivered


async def ensure_requested(
    cog: "ApprovalCog", guild: discord.Guild, renotify_after: int, reset_denied: bool = False
) -> None:
    """承認待ちとして記録し、まだ依頼していなければ依頼する。しばらく前の依頼なら催促する。"""
    if guild is None or guild.id in approval.preapproved(cog._config):
        return
    row = cog._guild_repo.get_guild_status(guild.id)
    if row is None or (row["status"] == approval.DENIED and reset_denied):
        cog._guild_repo.set_guild_status(guild.id, approval.PENDING, guild.name)
        await request_approval(cog, guild, renotify_after)
    elif row["status"] == approval.PENDING:
        if row["notified_at"] is None or cog._clock.now_ts() - row["notified_at"] > renotify_after:
            await request_approval(cog, guild, renotify_after)


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


async def handle_button(cog: "ApprovalCog", interaction: discord.Interaction, allow: bool, guild_id: int) -> None:
    """承認依頼の DM のボタン（許可する／断って退出させる）の処理。IApprovalBridge から委譲される。"""
    guild = cog.bot.get_guild(guild_id)
    row = cog._guild_repo.get_guild_status(guild_id) or {}
    name = guild.name if guild else row.get("name") or str(guild_id)
    if allow:
        cog._guild_repo.set_guild_status(guild_id, approval.APPROVED, name, interaction.user.id)
        embed = ui.ok_embed("許可しました", f"**{name}**（`{guild_id}`）で遊べるようになりました。")
        if guild is not None:
            await announce_approved(guild)
    else:
        cog._guild_repo.set_guild_status(guild_id, approval.DENIED, name, interaction.user.id)
        left = ""
        if guild is not None:
            try:
                await guild.leave()
                left = "Bot はそのサーバーから退出しました。"
            except discord.HTTPException:
                left = "退出はできませんでした（コマンドは使えないままです）。"
        embed = ui.warn_embed("断りました", f"**{name}**（`{guild_id}`）の利用を断りました。{left}")
    await interaction.response.edit_message(embed=embed, view=None)


async def owner_only(cog: "ApprovalCog", interaction: discord.Interaction) -> bool:
    """Bot の持ち主だけが使えるコマンドの権限チェック。権限がなければエラー応答して False を返す。"""
    if interaction.user.id not in approval.owner_ids(cog._config):
        await ui.reply_error(interaction, "このコマンドは Bot の持ち主だけが使えます。", title="権限がありません")
        return False
    return True


async def handle_guild_join(cog: "ApprovalCog", guild: discord.Guild) -> None:
    if not approval.requires_approval(cog._config):
        return
    row = cog._guild_repo.get_guild_status(guild.id)
    if row and row["status"] == approval.APPROVED:
        return  # 承認済みのサーバーに追加し直された
    await cog.ensure_requested(guild, reset_denied=True)


async def handle_ready(cog: "ApprovalCog") -> None:
    if not approval.requires_approval(cog._config):
        return
    for guild in cog.bot.guilds:  # Bot が止まっている間に追加されたサーバーも拾う
        await cog.ensure_requested(guild)


async def server_list(cog: "ApprovalCog", interaction: discord.Interaction) -> None:
    e = server_list_embed(cog.bot.guilds, cog._guild_repo.list_guild_status(), cog._config)
    await interaction.response.send_message(embed=e, ephemeral=True)


async def server_allow(cog: "ApprovalCog", interaction: discord.Interaction, guild_id: str) -> None:
    if not valid_guild_id(guild_id):
        await ui.reply_error(interaction, "サーバーidは数字だけで入力してください。", title="入力エラー")
        return
    gid = int(guild_id)
    guild = cog.bot.get_guild(gid)
    cog._guild_repo.set_guild_status(gid, approval.APPROVED, guild.name if guild else None, interaction.user.id)
    if guild is not None:
        await cog.announce_approved(guild)
    await ui.reply_ok(
        interaction,
        f"`{gid}`"
        + (f"（{guild.name}）" if guild else "（まだ参加していません。追加されたらすぐ遊べます）")
        + " で遊べます。",
        title="許可しました",
    )


async def server_revoke(cog: "ApprovalCog", interaction: discord.Interaction, guild_id: str) -> None:
    if not valid_guild_id(guild_id):
        await ui.reply_error(interaction, "サーバーidは数字だけで入力してください。", title="入力エラー")
        return
    gid = int(guild_id)
    if gid in approval.preapproved(cog._config):
        await ui.reply_warn(
            interaction,
            "設定ファイル（allowed_guild_ids）で許可されているサーバーです。設定ファイルから外してください。",
            title="取り消せません",
        )
        return
    guild = cog.bot.get_guild(gid)
    cog._guild_repo.set_guild_status(gid, approval.DENIED, guild.name if guild else None, interaction.user.id)
    left = ""
    if guild is not None:
        try:
            await guild.leave()
            left = "Bot はそのサーバーから退出しました。"
        except discord.HTTPException:
            left = "退出はできませんでしたが、コマンドは使えないままです。"
    await ui.reply_ok(interaction, f"`{gid}` の許可を取り消しました。{left}", title="取り消しました")
