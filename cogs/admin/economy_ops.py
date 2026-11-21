"""運営向けの経済・イベント・還元率・統計・履歴操作（`/運営 付与/残高設定/イベント/還元率/統計/履歴`）。"""

from typing import TYPE_CHECKING, Optional

import discord

from cogs.admin.embeds import LEVEL_NAME, click_history_embed, pachinko_stats_embed, rtp_table_embed
from cogs.admin.targets import event_targets, pick_channel, pick_guild, pick_member
from cogs.shared import ui
from cogs.shared.bridges import IEventAdminBridge, require_bridge
from cogs.shared.tasks import allowed_guilds

if TYPE_CHECKING:
    from cogs.admin.cog import ApprovalCog


async def grant(
    cog: "ApprovalCog", interaction: discord.Interaction, guild_id: str, user_id: str, amount: int, reason: str
) -> None:
    picked = await pick_member(cog.bot, interaction, guild_id, user_id, "付与")
    if picked is None:
        return
    guild, member = picked
    balance = cog._admin.grant(guild.id, member.id, amount, f"付与（管理）{reason}".strip())
    await ui.reply_ok(
        interaction,
        f"{guild.name} の {member.mention}（{member.display_name}）に **{ui.pelica(amount)}** を付与しました（残高 {ui.pelica(balance)}）。"
        + (f"\n理由: {reason}" if reason else ""),
        title="付与しました",
    )


async def set_balance(
    cog: "ApprovalCog", interaction: discord.Interaction, guild_id: str, user_id: str, amount: int, reason: str
) -> None:
    picked = await pick_member(cog.bot, interaction, guild_id, user_id, "設定")
    if picked is None:
        return
    guild, member = picked
    before, after, debt_before, debt_after = cog._admin.set_balance(
        guild.id, member.id, amount, f"残高の設定（管理）{reason}".strip()
    )
    await ui.reply_ok(
        interaction,
        f"{guild.name} の {member.mention}（{member.display_name}）の残高を "
        f"**{ui.pelica(before)} → {ui.pelica(after)}** にしました（差額は台帳に残ります）。"
        + (f"\n帝愛の借金: **{ui.pelica(debt_before)} → {ui.pelica(debt_after)}**" if amount < 0 else "")
        + (f"\n理由: {reason}" if reason else ""),
        title="残高を設定しました",
    )


async def event_start(
    cog: "ApprovalCog",
    interaction: discord.Interaction,
    game: str,
    minutes: int,
    boost: int,
    guild_id: Optional[str],
    channel_id: Optional[str],
) -> None:
    picked = await pick_guild(cog.bot, interaction, guild_id) if guild_id else None
    if guild_id and picked is None:
        return
    ok, channel = await pick_channel(interaction, picked, channel_id)
    if not ok:
        return
    targets = event_targets(cog.bot, cog._guild_repo, cog._config, guild_id)
    if not targets:
        await ui.reply_error(interaction, "許可されているサーバーがありません。", title="開く先がありません")
        return
    event_bridge = await require_bridge(interaction, cog.bot, IEventAdminBridge, "イベント機能", cog._event_bridge)
    if event_bridge is None:
        return
    await interaction.response.defer(ephemeral=True)
    lines = [(await event_bridge.start_manual(g, game, boost, minutes, channel))[1] for g in targets]
    await interaction.followup.send(
        embed=ui.ok_embed(f"特別イベントを開きました（{len(targets)}サーバー）", "\n".join(lines)[:3900]),
        ephemeral=True,
    )


async def event_stop(cog: "ApprovalCog", interaction: discord.Interaction, guild_id: Optional[str]) -> None:
    if guild_id and await pick_guild(cog.bot, interaction, guild_id) is None:
        return
    event_bridge = await require_bridge(interaction, cog.bot, IEventAdminBridge, "イベント機能", cog._event_bridge)
    if event_bridge is None:
        return
    await interaction.response.defer(ephemeral=True)
    lines = [
        (await event_bridge.stop_manual(g))[1] for g in event_targets(cog.bot, cog._guild_repo, cog._config, guild_id)
    ]
    await interaction.followup.send(
        embed=ui.embed("特別イベントを終わらせました", "\n".join(lines)[:3900] or "対象がありません。", ui.INFO),
        ephemeral=True,
    )


async def rtp_set(cog: "ApprovalCog", interaction: discord.Interaction, guild_id: str, game: str, level: int) -> None:
    guild = await pick_guild(cog.bot, interaction, guild_id)
    if guild is None:
        return
    changed = cog._admin.rtp_set(guild.id, None if game == "*" else game, level)
    await interaction.response.send_message(
        embeds=[
            ui.ok_embed(
                "還元率のレベルを変えました",
                f"{guild.name}: {'、'.join(changed)} → レベル {level}（{LEVEL_NAME[level]}）",
            ),
            rtp_table_embed(cog._admin.rtp_levels(guild.id), f"🎚️ {guild.name} の還元率"),
        ],
        ephemeral=True,
    )


async def rtp_list(cog: "ApprovalCog", interaction: discord.Interaction, guild_id: Optional[str]) -> None:
    guild = await pick_guild(cog.bot, interaction, guild_id) if guild_id else None
    if guild_id and guild is None:
        return
    targets = [guild] if guild else allowed_guilds(cog.bot, cog._guild_repo, cog._config)
    embeds = [rtp_table_embed(cog._admin.rtp_levels(g.id), f"🎚️ {g.name} の還元率") for g in targets[:10]]
    await interaction.response.send_message(embeds=embeds or [ui.error_embed("対象がありません")], ephemeral=True)


async def pachinko_stats(
    cog: "ApprovalCog", interaction: discord.Interaction, guild_id: Optional[str], days: Optional[int]
) -> None:
    guild = None
    if guild_id:
        guild = await pick_guild(cog.bot, interaction, guild_id)
        if guild is None:
            return
    since = cog._clock.now_ts() - days * 86400 if days else None
    data = cog._admin.pachinko_stats(guild.id if guild else None, since)
    scope = (guild.name if guild else "全サーバー") + (f"・過去{days}日" if days else "・全期間")
    await interaction.response.send_message(embed=pachinko_stats_embed(data, scope), ephemeral=True)


async def click_history(
    cog: "ApprovalCog", interaction: discord.Interaction, guild_id: str, user_id: str, hours: int
) -> None:
    picked = await pick_member(cog.bot, interaction, guild_id, user_id, "確認")
    if picked is None:
        return
    guild, member = picked
    report = cog._labor_svc.click_report(guild.id, member.id, hours)
    tz = cog._labor_svc.clock.offset_hours()
    await interaction.response.send_message(
        embed=click_history_embed(f"{guild.name} の {member.display_name}", report, tz, hours), ephemeral=True
    )
