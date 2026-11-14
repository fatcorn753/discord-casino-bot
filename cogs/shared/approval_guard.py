"""サーバー承認・持ち主 DM コマンドの Discord 入口ガード（cogs 共通基盤）。"""

from typing import TYPE_CHECKING, Any, cast

import discord

from cogs.shared import ui
from domain.approval import DENIED, DM_GROUP, is_allowed, owner_ids, requires_approval

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot


def is_owner_dm_command(interaction: discord.Interaction) -> bool:
    """Bot の持ち主が、DM で「運営」のコマンド（サーバーの許可の管理）を使うとき。サーバーの外で使える、ただ1つの場合。"""
    bot = cast("CasinoBot", interaction.client)
    cmd = interaction.command
    root = (getattr(cmd, "root_parent", None) or cmd) if cmd is not None else None
    return (
        interaction.guild is None
        and getattr(root, "name", None) == DM_GROUP
        and interaction.user.id in owner_ids(bot.config)
    )


async def guard(interaction: discord.Interaction) -> bool:
    """コマンドの入口の検査。使えないサーバーには理由を返して False を返す。"""
    guild_id = interaction.guild_id
    if guild_id is None:
        return False
    bot = cast("CasinoBot", interaction.client)
    if is_allowed(bot.economy, bot.config, guild_id, interaction.user.id):
        return True
    if not requires_approval(bot.config):
        await interaction.response.send_message(
            embed=ui.error_embed("このサーバーでは使えません", "この Bot は、許可されたサーバーでだけ遊べます。"),
            ephemeral=True,
        )
        return False
    row = bot.economy.get_guild_status(guild_id)
    cog: Any = bot.get_cog("ApprovalCog")
    if cog is not None and interaction.guild is not None:
        await cog.ensure_requested(interaction.guild)  # 未登録なら承認を依頼する／しばらくたっていれば催促する
    if row and row["status"] == DENIED:
        msg = "このサーバーでの利用は、Bot の持ち主に許可されていません。"
    else:
        msg = "このサーバーは、まだ Bot の持ち主に承認されていません。承認を依頼しました。承認されるまで、少しお待ちください。"
    await interaction.response.send_message(embed=ui.warn_embed("承認待ちです", msg), ephemeral=True)
    return False
