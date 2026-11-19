"""ランキング表示ビュー（RankingView）と自動更新ランキング掲示板の管理。"""

import datetime
import logging
from typing import Optional

import discord

from cogs.shared import logthrottle, ranking, ui
from domain.ports.repos import IGuildRepo
from services.ranking import RankingService


class RankingView(ui.OwnerView):
    """ランキングの切り替え（純資産 / 総資産）と、自分の資産の内訳。押せるのは、開いた本人だけ。"""

    def __init__(self, ranking_repos: RankingService, user_id: int, guild_id: int):
        super().__init__(
            user_id,
            timeout=300,
            owner_message="これは他の人のランキングです。`/ペリカ ランキング` で自分を開けます。",
        )
        self.repos = ranking_repos.for_guild(guild_id)
        self.user_id = user_id

    def embed(self, mode: str) -> discord.Embed:
        if mode == "final":
            return ranking.build_final_embed(
                self.repos,
                None,
                "🏆 このサーバーのランキング（総資産）",
                self.user_id,
                footer="総資産 = 純資産 + 馬・持ち物・発掘品・地下作業の工具・預かり中のお金・貸し借りなど、ペリカに換算できるもの全部",
            )
        return ranking.build_embed(
            self.repos,
            None,
            "🏆 このサーバーのランキング（純資産）",
            self.user_id,
            footer="純資産 = 現金 − 借金 + 信託・株・債券・オプションの評価額 ｜ ペリカはサーバーごとに別々です",
        )

    async def _switch(self, interaction: discord.Interaction, mode: str) -> None:
        await interaction.response.defer()
        self.normal.style = discord.ButtonStyle.primary if mode == "normal" else discord.ButtonStyle.secondary
        self.final.style = discord.ButtonStyle.primary if mode == "final" else discord.ButtonStyle.secondary
        embed = self.embed(mode)
        await interaction.edit_original_response(embed=embed, view=self)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        logging.getLogger(__name__).exception("ランキングの切り替えに失敗しました: %r", error)

    @discord.ui.button(label="純資産", emoji="🏆", style=discord.ButtonStyle.primary)
    async def normal(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._switch(interaction, "normal")

    @discord.ui.button(label="総資産", emoji="💎", style=discord.ButtonStyle.secondary)
    async def final(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._switch(interaction, "final")

    @discord.ui.button(label="自分の内訳", emoji="🧾", style=discord.ButtonStyle.secondary)
    async def mine(self, interaction: discord.Interaction, _button: discord.ui.Button):
        e = ranking.build_breakdown_embed(self.repos, self.user_id, interaction.user.display_name)
        await interaction.response.send_message(embed=e, ephemeral=True)


def board_embed(ranking_repos: RankingService, guild_id: int, interval_minutes: int) -> discord.Embed:
    ts = int(datetime.datetime.now().timestamp())
    return ranking.build_embed(
        ranking_repos.for_guild(guild_id),
        None,
        "🏆 ペリカランキング（純資産）",
        header=f"最終更新: <t:{ts}:R>\n\n",
        footer=f"{interval_minutes}分ごとに自動で更新されます",
    )


async def refresh_board(
    bot: discord.Client,
    ranking_repos: RankingService,
    guild_repo: IGuildRepo,
    board: dict,
    interval_minutes: int,
) -> None:
    """掲示板のメッセージを最新の順位に書き換える。消されていたら投稿し直す。"""
    guild = bot.get_guild(board["guild_id"])
    if guild is None:
        return
    channel = guild.get_channel(board["channel_id"])
    if channel is None:
        guild_repo.delete_leaderboard(guild.id)
        return
    embed = board_embed(ranking_repos, guild.id, interval_minutes)
    try:
        await ui.partial_message(channel, board["message_id"]).edit(embed=embed)
    except discord.NotFound:
        try:
            new = await ui.send_channel(channel, embed=embed, allowed_mentions=discord.AllowedMentions.none())
            if new is not None:
                guild_repo.set_leaderboard(guild.id, channel.id, new.id)
        except discord.HTTPException as exc:
            logthrottle.warn(
                ("board-repost", guild.id), f"ランキング掲示板を投稿し直せません: guild={guild.id}: {exc!r}"
            )
    except discord.HTTPException as exc:
        logthrottle.warn(("board", guild.id), f"ランキング掲示板を更新できません: guild={guild.id}: {exc!r}")


async def delete_board_message(guild: discord.Guild, board: Optional[dict]) -> None:
    if not board:
        return
    channel = guild.get_channel(board["channel_id"])
    if channel is not None:
        try:
            await ui.partial_message(channel, board["message_id"]).delete()
        except discord.HTTPException:
            pass


async def board_set(
    ranking_repos: RankingService,
    guild_repo: IGuildRepo,
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel],
    stop: bool,
    interval_minutes: int,
) -> None:
    if stop:
        await board_unset(guild_repo, interaction)
        return
    if channel is None:
        await interaction.response.send_message(
            embed=ui.error_embed(
                "チャンネルを指定してください",
                "掲示するチャンネルを指定します。止めるときは「止める」を True にしてください。",
            ),
            ephemeral=True,
        )
        return
    guild = interaction.guild
    if guild is None:
        return
    perms = channel.permissions_for(guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        await interaction.response.send_message(
            embed=ui.error_embed(
                "掲示できません",
                f"Bot が {channel.mention} に埋め込みを投稿できません。チャンネルの権限を確認してください。",
            ),
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    old = guild_repo.get_leaderboard(guild.id)
    message = await channel.send(
        embed=board_embed(ranking_repos, guild.id, interval_minutes),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    guild_repo.set_leaderboard(guild.id, channel.id, message.id)
    await delete_board_message(guild, old)
    await interaction.followup.send(
        embed=ui.ok_embed(
            "ランキングを掲示しました",
            f"{message.jump_url}\n{interval_minutes}分ごとに、この投稿を自動で書き換えます。止めるときは `/ペリカ ランキング掲示` の「止める」を True にしてください。",
        ),
        ephemeral=True,
    )


async def board_unset(guild_repo: IGuildRepo, interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        return
    board = guild_repo.get_leaderboard(guild.id)
    if board is None:
        await interaction.response.send_message(
            embed=ui.warn_embed("掲示していません", "このサーバーには、ランキングの掲示がありません。"),
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    await delete_board_message(guild, board)
    guild_repo.delete_leaderboard(guild.id)
    await interaction.followup.send(
        embed=ui.ok_embed("掲示を止めました", "自動の更新をやめて、ランキングの投稿を削除しました。"),
        ephemeral=True,
    )
