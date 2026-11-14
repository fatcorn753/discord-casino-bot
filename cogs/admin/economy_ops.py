"""運営向けの経済・イベント・還元率・統計・履歴操作（`/運営 付与/残高設定/イベント/還元率/統計/履歴`）。"""

import time
from typing import Optional

import discord

from cogs.admin.embeds import LEVEL_NAME, click_history_embed, pachinko_stats_embed, rtp_table_embed
from cogs.shared import ui
from cogs.shared.cog_ports import IEventCogPort, require_cog_port
from domain import labor as labor_mod


async def grant(cog, interaction: discord.Interaction, guild_id: str, user_id: str, amount: int, reason: str) -> None:
    picked = await cog._pick_member(interaction, guild_id, user_id, "付与")
    if picked is None:
        return
    guild, member = picked
    balance = cog.bot.economy.for_guild(guild.id).credit(member.id, amount, f"付与（管理）{reason}".strip())
    await interaction.response.send_message(
        embed=ui.ok_embed(
            "付与しました",
            f"{guild.name} の {member.mention}（{member.display_name}）に **{ui.pelica(amount)}** を付与しました（残高 {ui.pelica(balance)}）。"
            + (f"\n理由: {reason}" if reason else ""),
        ),
        ephemeral=True,
    )


async def set_balance(
    cog, interaction: discord.Interaction, guild_id: str, user_id: str, amount: int, reason: str
) -> None:
    picked = await cog._pick_member(interaction, guild_id, user_id, "設定")
    if picked is None:
        return
    guild, member = picked
    before, after, debt_before, debt_after = cog.bot.economy.for_guild(guild.id).set_balance(
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


async def event_start(
    cog,
    interaction: discord.Interaction,
    game: str,
    minutes: int,
    boost: int,
    guild_id: Optional[str],
    channel_id: Optional[str],
) -> None:
    picked = await cog._pick_guild(interaction, guild_id) if guild_id else None
    if guild_id and picked is None:
        return
    ok, channel = await cog._pick_channel(interaction, picked, channel_id)
    if not ok:
        return
    targets = cog._event_targets(guild_id)
    if not targets:
        await interaction.response.send_message(
            embed=ui.error_embed("開く先がありません", "許可されているサーバーがありません。"), ephemeral=True
        )
        return
    event_cog = await require_cog_port(cog.bot, interaction, "EventCog", IEventCogPort, "イベント機能")
    if event_cog is None:
        return
    await interaction.response.defer(ephemeral=True)
    lines = [(await event_cog.start_manual(g, game, boost, minutes, channel))[1] for g in targets]
    await interaction.followup.send(
        embed=ui.ok_embed(f"特別イベントを開きました（{len(targets)}サーバー）", "\n".join(lines)[:3900]),
        ephemeral=True,
    )


async def event_stop(cog, interaction: discord.Interaction, guild_id: Optional[str]) -> None:
    if guild_id and await cog._pick_guild(interaction, guild_id) is None:
        return
    event_cog = await require_cog_port(cog.bot, interaction, "EventCog", IEventCogPort, "イベント機能")
    if event_cog is None:
        return
    await interaction.response.defer(ephemeral=True)
    lines = [(await event_cog.stop_manual(g))[1] for g in cog._event_targets(guild_id)]
    await interaction.followup.send(
        embed=ui.embed("特別イベントを終わらせました", "\n".join(lines)[:3900] or "対象がありません。", ui.INFO),
        ephemeral=True,
    )


async def rtp_set(cog, interaction: discord.Interaction, guild_id: str, game: str, level: int) -> None:
    guild = await cog._pick_guild(interaction, guild_id)
    if guild is None:
        return
    eco = cog.bot.economy.for_guild(guild.id)
    changed = eco.rtp_level_set(None if game == "*" else game, level)
    await interaction.response.send_message(
        embeds=[
            ui.ok_embed(
                "還元率のレベルを変えました",
                f"{guild.name}: {'、'.join(changed)} → レベル {level}（{LEVEL_NAME[level]}）",
            ),
            rtp_table_embed(eco.rtp_levels(), f"🎚️ {guild.name} の還元率"),
        ],
        ephemeral=True,
    )


async def rtp_list(cog, interaction: discord.Interaction, guild_id: Optional[str]) -> None:
    guild = await cog._pick_guild(interaction, guild_id) if guild_id else None
    if guild_id and guild is None:
        return
    targets = [guild] if guild else cog._allowed_guilds()
    embeds = [
        rtp_table_embed(cog.bot.economy.for_guild(g.id).rtp_levels(), f"🎚️ {g.name} の還元率") for g in targets[:10]
    ]
    await interaction.response.send_message(embeds=embeds or [ui.error_embed("対象がありません")], ephemeral=True)


async def pachinko_stats(cog, interaction: discord.Interaction, guild_id: Optional[str], days: Optional[int]) -> None:
    guild = None
    if guild_id:
        guild = await cog._pick_guild(interaction, guild_id)
        if guild is None:
            return
    since = time.time() - days * 86400 if days else None
    data = cog.bot.economy.pachinko_stats(guild.id if guild else None, since)
    scope = (guild.name if guild else "全サーバー") + (f"・過去{days}日" if days else "・全期間")
    await interaction.response.send_message(embed=pachinko_stats_embed(data, scope), ephemeral=True)


async def click_history(cog, interaction: discord.Interaction, guild_id: str, user_id: str, hours: int) -> None:
    picked = await cog._pick_member(interaction, guild_id, user_id, "確認")
    if picked is None:
        return
    guild, member = picked
    eco = cog.bot.economy.for_guild(guild.id)
    report = labor_mod.click_report(eco.labor_click_hours(member.id, time.time() - hours * 3600))
    tz = float(cog.bot.config.get("utc_offset_hours", 9))
    await interaction.response.send_message(
        embed=click_history_embed(f"{guild.name} の {member.display_name}", report, tz, hours), ephemeral=True
    )
