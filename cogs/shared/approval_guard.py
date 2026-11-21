"""サーバー承認・持ち主 DM コマンド・機能のオン・オフの Discord 入口ガード（cogs 共通基盤）。"""

from typing import TYPE_CHECKING, Any, Optional, cast

import discord

from cogs.shared import ui
from cogs.shared.bridges import IApprovalBridge, NoOpApprovalBridge
from domain import approval, features
from domain.approval import DENIED, DM_GROUP, is_allowed, owner_ids, requires_approval
from domain.ports.repos import IGuildRepo

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
    guild_repo = bot.repos.guild
    if is_allowed(guild_repo, bot.config, guild_id, interaction.user.id):
        return True
    if not requires_approval(bot.config):
        await ui.reply_error(
            interaction, "この Bot は、許可されたサーバーだけで遊べます。", title="このサーバーでは使えません"
        )
        return False
    row = guild_repo.get_guild_status(guild_id)
    bridge = bot.bridges.resolve_or(IApprovalBridge, NoOpApprovalBridge())
    if interaction.guild is not None:
        await bridge.ensure_requested(interaction.guild)  # 未登録なら承認を依頼する／しばらくたっていれば催促する
    if row and row["status"] == DENIED:
        msg = "このサーバーでの利用は、Bot の持ち主に許可されていません。"
    else:
        msg = "このサーバーは、まだ Bot の持ち主に承認されていません。承認を依頼しました。承認されるまで、少しお待ちください。"
    await ui.reply_warn(interaction, msg, title="承認待ちです")
    return False


async def allow_component(
    interaction: discord.Interaction,
    guild_repo: IGuildRepo,
    config: dict[str, Any],
    *,
    title: str = "利用できません",
    text: Optional[str] = None,
    feature: Optional[str] = None,
) -> bool:
    """ボタン・Select の操作の入口の検査（スラッシュコマンドの guard とは別に、押された時点の許可を見る）。

    許可されていないサーバー（や DM）なら、本人だけに断りを返して False を返す。
    feature を渡すと、その機能が止められているときも断る（新しく始める操作のボタンだけに渡す）。
    """
    guild_id = interaction.guild_id
    if guild_id is not None and approval.is_allowed(guild_repo, config, guild_id, interaction.user.id):
        return feature is None or await feature_guard(interaction, guild_repo, feature)
    await ui.reply_error(interaction, text if text is not None else "", title=title)
    return False


def feature_off(guild_repo: IGuildRepo, guild_id: Optional[int], feature: str) -> bool:
    """その機能が、このサーバーで止められているか。"""
    return guild_id is not None and feature in guild_repo.features_off(guild_id)


async def feature_guard(interaction: discord.Interaction, guild_repo: IGuildRepo, feature: str) -> bool:
    """機能が止められていれば、本人だけに断りを返して False を返す（止まっていなければ True）。"""
    if not feature_off(guild_repo, interaction.guild_id, feature):
        return True
    await ui.reply_error(
        interaction,
        f"**{features.label(feature)}** は、いま管理者が止めています。再開まで、お待ちください。\n"
        "（止める前に始めたものは、いつもどおり精算されます）",
        title="⏸️ 停止中",
    )
    return False


async def client_feature_guard(interaction: discord.Interaction, feature: str) -> bool:
    """feature_guard を、Bot のサーバー設定で行う（リポジトリを持っていないボタン・Modal 用）。"""
    repos = getattr(getattr(interaction, "client", None), "repos", None)
    if repos is None:  # Bot の外（テストの偽の interaction など）では、止めようがない
        return True
    return await feature_guard(interaction, cast("CasinoBot", interaction.client).repos.guild, feature)


async def command_feature_guard(interaction: discord.Interaction) -> bool:
    """スラッシュコマンドの入口で、そのコマンドの機能が止められていないかを見る。"""
    cmd = interaction.command
    key = features.feature_of_command(cmd.qualified_name) if cmd is not None else None
    return key is None or await client_feature_guard(interaction, key)
