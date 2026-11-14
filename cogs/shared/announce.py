"""複数の Cog で共有する Discord 依存の小さな部品（読み込まれる Cog ではない）。"""

import discord


def announce_channel(guild: discord.Guild, eco):
    """自動の告知を投稿するチャンネル。ランキング掲示のチャンネル、無ければシステムチャンネル。投稿できなければ None。"""
    board = eco.get_leaderboard(guild.id)
    candidates = [guild.get_channel(board["channel_id"])] if board else []
    candidates.append(guild.system_channel)
    for ch in candidates:
        if ch is not None:
            perms = ch.permissions_for(guild.me)
            if perms.view_channel and perms.send_messages and perms.embed_links:
                return ch
    return None
