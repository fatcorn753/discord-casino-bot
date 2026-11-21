"""証券市場の定期告知・ライブチャート更新・古い掲示板の片付け（Discord 投稿層）。"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import discord

from cogs.market.embeds import LIVE_MINUTES, earnings_announce_embed, earnings_result_embed
from cogs.shared import logthrottle, ui
from cogs.shared.announce import announce_channel
from cogs.shared.tasks import allowed_guilds
from domain.ports.repos import IGuildRepo
from services.market import MarketService

if TYPE_CHECKING:
    from cogs.market.cog import MarketCog


async def earnings_posts(
    bot: discord.Client,
    config: dict[str, Any],
    market_svc: MarketService,
    guild_repo: IGuildRepo,
    earnings_results: list,
    *,
    announce_fn=announce_channel,
) -> None:
    """決算の予告（月曜9:00）と発表の結果を、各サーバーの告知チャンネルに投稿する。"""
    now = market_svc.clock.now_ts()
    embeds = [earnings_announce_embed(r) for r in market_svc.earnings_to_announce(now)]
    results, earnings_results[:] = list(earnings_results), []
    for r in results:
        embeds.append(earnings_result_embed(r, market_svc.market_pin(r["symbol"])))
    if not embeds:
        return
    for guild in allowed_guilds(bot, guild_repo, config):
        channel = announce_fn(guild, guild_repo.for_guild(guild.id))
        if channel is None:
            continue
        for embed in embeds:
            try:
                await ui.send_channel(channel, embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc:
                logthrottle.warn(("earnings-post", guild.id), f"決算の投稿に失敗: guild={guild.id}: {exc!r}")


async def refresh_post(
    bot: discord.Client, market_svc: MarketService, cog: "MarketCog", post: Mapping[str, Any], now: float, cache: dict
) -> None:
    """出したチャートを、最新に書き換える。時間が来たら、最後に1回だけ書き換えて、更新を終える。"""
    ends = post["ts"] + LIVE_MINUTES * 60
    expired = now >= ends or post["guild_id"] <= 0
    channel = bot.get_channel(post["channel_id"])
    if channel is None:
        if expired or bot.get_guild(post["guild_id"]) is None:
            market_svc.end_chart_post(post["channel_id"])
        return
    if post["guild_id"] > 0:
        key = (post["guild_id"], post["period"], post["symbol"], expired, int(ends // 60))
        if key not in cache:
            cache[key] = cog.chart_embed(
                post["period"],
                guild_id=post["guild_id"],
                live_until=None if expired else ends,
                ended=expired,
                symbol=post["symbol"],
            )
        try:
            await ui.partial_message(channel, post["message_id"]).edit(embed=cache[key])
        except discord.NotFound:
            market_svc.delete_chart_post(post["channel_id"])
            return
        except discord.HTTPException as exc:
            logthrottle.warn(
                ("chart", post["channel_id"]), f"チャートを更新できません: channel={post['channel_id']}: {exc!r}"
            )
    if expired:
        market_svc.end_chart_post(post["channel_id"])


async def retire_old_boards(bot: discord.Client, market_svc: MarketService) -> None:
    """廃止した「チャート掲示」が残っていれば、メッセージを消して、設定も捨てる（1回だけ）。"""
    for board in market_svc.list_chart_boards():
        channel = bot.get_channel(board["channel_id"])
        if channel is not None:
            try:
                await ui.partial_message(channel, board["message_id"]).delete()
            except discord.HTTPException as exc:
                logthrottle.warn(
                    ("chart-board", board["guild_id"]),
                    f"廃止したチャート掲示を消せません: guild={board['guild_id']}: {exc!r}",
                )
        market_svc.delete_chart_board(board["guild_id"])
