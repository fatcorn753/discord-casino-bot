"""`/運営 機能`: サーバーごとに、賭け事や仕組みを止める・再開する・一覧する。"""

from typing import TYPE_CHECKING

import discord

from cogs.admin.targets import pick_guild
from cogs.shared import ui
from cogs.shared.tasks import allowed_guilds
from domain import features

if TYPE_CHECKING:
    from cogs.admin.cog import ApprovalCog


def features_embed(guild_name: str, off: set[str]) -> discord.Embed:
    lines = [f"{'⏸️ 停止中' if key in off else '▶️ 稼働中'}　{name}" for key, name in features.FEATURES.items()]
    return ui.embed(f"🎛️ {guild_name} の機能", "\n".join(lines))


async def feature_set(
    cog: "ApprovalCog", interaction: discord.Interaction, guild_id: str, feature: str, on: bool
) -> None:
    guild = await pick_guild(cog.bot, interaction, guild_id)
    if guild is None:
        return
    keys = list(features.FEATURES) if feature == "*" else [feature]
    for key in keys:
        cog._guild_repo.set_feature(guild.id, key, on)
    names = "すべての機能" if feature == "*" else features.label(feature)
    text = (
        f"{guild.name}: **{names}** を再開しました。"
        if on
        else f"{guild.name}: **{names}** を止めました。新しく始めることだけを止めます。"
        "止める前に始めたもの（賭け金を預かった大会・レース・建玉など）は、いつもどおり精算します。"
    )
    await interaction.response.send_message(
        embeds=[
            ui.ok_embed("▶️ 再開しました" if on else "⏸️ 止めました", text),
            features_embed(guild.name, cog._guild_repo.features_off(guild.id)),
        ],
        ephemeral=True,
    )


async def feature_list(cog: "ApprovalCog", interaction: discord.Interaction, guild_id: str | None) -> None:
    guild = await pick_guild(cog.bot, interaction, guild_id) if guild_id else None
    if guild_id and guild is None:
        return
    targets = [guild] if guild else allowed_guilds(cog.bot, cog._guild_repo, cog._config)
    embeds = [features_embed(g.name, cog._guild_repo.features_off(g.id)) for g in targets[:10]]
    if embeds:
        await interaction.response.send_message(embeds=embeds, ephemeral=True)
    else:
        await ui.reply_error(interaction, "対象がありません")
