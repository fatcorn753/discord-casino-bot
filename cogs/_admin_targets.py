"""運営コマンド（`/運営`）で使うサーバー・チャンネル・ユーザーの指定と候補。"""

import re
from typing import Optional

import discord
from discord import app_commands

from core import ui


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


def guild_choices(bot, current: str) -> list:
    cur = current.strip().lower()
    return [
        app_commands.Choice(name=g.name[:100], value=str(g.id))
        for g in bot.guilds
        if not cur or cur in g.name.lower() or cur in str(g.id)
    ][:25]


def namespace_guild(bot, interaction: discord.Interaction) -> Optional[discord.Guild]:
    """入力途中の `サーバー` で選ばれているサーバー（候補を絞るため）。"""
    ns = vars(interaction.namespace)
    raw = str(ns.get("guild_id") or ns.get("サーバー") or "")
    return bot.get_guild(int(raw)) if valid_guild_id(raw) else None


def channel_choices(guild: discord.Guild, current: str) -> list:
    cur = current.strip().lower()
    return [
        app_commands.Choice(name=f"#{ch.name}"[:100], value=str(ch.id))
        for ch in guild.text_channels
        if postable(ch, guild) and (not cur or cur in ch.name.lower())
    ][:25]


def user_choices(guild: discord.Guild, current: str) -> list:
    cur = current.strip().lower()
    if not cur:
        return []
    return [
        app_commands.Choice(name=f"{m.display_name} ({m.name})"[:100], value=str(m.id))
        for m in guild.members
        if not m.bot and (cur in m.display_name.lower() or cur in m.name.lower())
    ][:25]


async def pick_guild(bot, interaction: discord.Interaction, guild_id: str) -> Optional[discord.Guild]:
    """入力されたサーバーを探す。見つからなければ理由を返して None。"""
    guild = bot.get_guild(int(guild_id)) if valid_guild_id(guild_id) else None
    if guild is None:
        await interaction.response.send_message(
            embed=ui.error_embed(
                "サーバーが見つかりません", "候補から選ぶか、Bot が参加しているサーバーのIDを入れてください。"
            ),
            ephemeral=True,
        )
    return guild


async def pick_channel(
    interaction: discord.Interaction, guild: Optional[discord.Guild], channel_id: Optional[str]
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
        await interaction.response.send_message(embed=ui.error_embed("チャンネルを使えません", problem), ephemeral=True)
        return False, None
    return True, channel


async def pick_member(
    bot, interaction: discord.Interaction, guild_id: str, user_id: str, verb: str
) -> Optional[tuple[discord.Guild, discord.Member]]:
    """管理コマンドで、サーバーと相手を確かめる。問題があれば、その場で返信して None（呼び出し側は何もせず戻る）。"""
    guild = await pick_guild(bot, interaction, guild_id)
    if guild is None:
        return None
    uid = parse_user_id(user_id)
    if uid is None:
        await interaction.response.send_message(
            embed=ui.error_embed("ユーザーを指定してください", "候補から選ぶか、ユーザーID（数字）を入れてください。"),
            ephemeral=True,
        )
        return None
    try:
        member = await guild.fetch_member(uid)
    except discord.NotFound:
        await interaction.response.send_message(
            embed=ui.error_embed("そのサーバーにいません", f"`{uid}` は {guild.name} のメンバーではありません。"),
            ephemeral=True,
        )
        return None
    except discord.HTTPException as exc:
        await interaction.response.send_message(
            embed=ui.error_embed("確認できません", f"メンバーを確認できませんでした: {exc}"), ephemeral=True
        )
        return None
    if member.bot:
        await interaction.response.send_message(embed=ui.error_embed(f"Bot には{verb}できません"), ephemeral=True)
        return None
    return guild, member
