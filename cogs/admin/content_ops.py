"""運営向けのコンテンツ・メッセージ操作（臨時競馬・お知らせ・発言削除・抽選・クイズ）。"""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Optional

import discord

from cogs.admin.embeds import bulk_preview_lines
from cogs.admin.purge import can_read_history
from cogs.admin.targets import pick_channel, pick_guild
from cogs.admin.views import AnnounceModal, PurgeConfirm, QuizBulkConfirm
from cogs.shared import ui
from cogs.shared.bridges import (
    IArenaAdminBridge,
    IHorseAdminBridge,
    IQuizAdminBridge,
    IRaffleAdminBridge,
    require_bridge,
)
from cogs.shared.tasks import allowed_guilds
from domain import quizimport

if TYPE_CHECKING:
    from cogs.admin.cog import ApprovalCog

MAX_IMAGE_BYTES = ui.MAX_IMAGE_BYTES
MAX_IMPORT_BYTES = 200 * 1024


async def special_race(
    cog: "ApprovalCog",
    interaction: discord.Interaction,
    guild_id: str,
    entry_min: int,
    bet_min: int,
    channel_id: Optional[str],
) -> None:
    guild = await pick_guild(cog.bot, interaction, guild_id)
    if guild is None:
        return
    ok, channel = await pick_channel(interaction, guild, channel_id)
    if not ok:
        return
    horse_bridge = await require_bridge(interaction, cog.bot, IHorseAdminBridge, "競馬", cog._horse_bridge)
    if horse_bridge is None:
        return
    race_id = await horse_bridge.start_special(guild, entry_min, bet_min, channel)
    await ui.reply_ok(
        interaction,
        f"{guild.name} で、臨時レース（#{race_id}）の申し込みを始めました（掲示は{channel.mention if channel else '告知チャンネル'}）。申し込み {entry_min}分 → 馬券 {bet_min}分 → 発走（2分後）。",
        title="臨時レースを開催しました",
    )


async def host_arena(
    cog: "ApprovalCog",
    interaction: discord.Interaction,
    guild_id: str,
    advance: bool,
    fmt: Optional[str],
    channel_id: Optional[str],
) -> None:
    guild = await pick_guild(cog.bot, interaction, guild_id)
    if guild is None:
        return
    ok, channel = await pick_channel(interaction, guild, channel_id)
    if not ok:
        return
    arena_bridge = await require_bridge(interaction, cog.bot, IArenaAdminBridge, "闘技場")
    if arena_bridge is None:
        return
    status = await arena_bridge.host_now(guild, channel, advance, fmt)
    await ui.reply_ok(
        interaction,
        f"{guild.name} で、今日の大会を出しました（掲示は{channel.mention if channel else '告知チャンネル'}）。状態: {status}",
        title="闘技場を開催しました",
    )


async def announce(
    cog: "ApprovalCog",
    interaction: discord.Interaction,
    guild_id: Optional[str],
    image: Optional[discord.Attachment],
    title: Optional[str],
    channel_id: Optional[str],
) -> None:
    if image is not None and not ((image.content_type or "").startswith("image/") and image.size <= MAX_IMAGE_BYTES):
        await ui.reply_error(interaction, "画像ファイル（8MBまで）を添付してください。", title="画像を使えません")
        return
    if guild_id:
        guild = await pick_guild(cog.bot, interaction, guild_id)
        if guild is None:
            return
        guilds = [guild]
    else:
        guilds = allowed_guilds(cog.bot, cog._guild_repo, cog._config)
    ok, channel = await pick_channel(interaction, guilds[0] if guild_id else None, channel_id)
    if not ok:
        return
    if not guilds:
        await ui.reply_error(interaction, "許可されているサーバーがありません。", title="送り先がありません")
        return
    await interaction.response.send_modal(AnnounceModal(cog, guilds, (title or "").strip()[:200], image, channel))


async def purge_own_messages(
    cog: "ApprovalCog", interaction: discord.Interaction, guild_id: str, channel_id: str
) -> None:
    guild = await pick_guild(cog.bot, interaction, guild_id)
    if guild is None:
        return
    ok, channel = await pick_channel(interaction, guild, channel_id)
    if not ok:
        return
    if not can_read_history(channel, guild):
        await ui.reply_error(
            interaction,
            f"Bot は #{channel.name} の「メッセージ履歴を読む」権限がありません。招待URLの権限に足して（README を参照）、もう一度試してください。",
            title="履歴を読めません",
        )
        return
    warning = (
        f"**{guild.name}** の **#{channel.name}** にある、**Bot 自身の発言を、すべて消します。元に戻せません。**\n"
        "ランキング掲示板、チャートの自動更新、クイズ・貸付・抽選の投稿（ボタン付き）も消え、ボタンは使えなくなります。"
        "Bot 以外の人の発言には触れません。件数が多いと、数分かかります。"
    )
    await interaction.response.send_message(
        embeds=[ui.embed("🧹 発言の削除（確認）", warning, ui.WARN)],
        view=PurgeConfirm(cog, interaction.user.id, channel),
        ephemeral=True,
    )


async def _raffle_panel_op(
    cog: "ApprovalCog",
    interaction: discord.Interaction,
    guild_id: str,
    channel_id: Optional[str],
    op: Callable[[IRaffleAdminBridge, discord.Guild, Optional[discord.abc.Messageable]], Awaitable[tuple[bool, str]]],
    ok_title: str,
    ng_title: str,
) -> None:
    """抽選パネルの運営操作（host/repost/cancel）の共通手順: サーバーとチャンネルを選び、ブリッジを解決して defer し、結果を返す。"""
    guild = await pick_guild(cog.bot, interaction, guild_id)
    if guild is None:
        return
    ok, channel = await pick_channel(interaction, guild, channel_id)
    if not ok:
        return
    raffle_bridge = await require_bridge(interaction, cog.bot, IRaffleAdminBridge, "抽選機能", cog._raffle_bridge)
    if raffle_bridge is None:
        return
    await interaction.response.defer(ephemeral=True)
    ok, text = await op(raffle_bridge, guild, channel)
    await interaction.followup.send(
        embed=ui.ok_embed(ok_title, text) if ok else ui.error_embed(ng_title, text), ephemeral=True
    )


async def raffle_start(
    cog: "ApprovalCog",
    interaction: discord.Interaction,
    guild_id: str,
    fee: Optional[int],
    minutes: int,
    channel_id: Optional[str],
) -> None:
    await _raffle_panel_op(
        cog,
        interaction,
        guild_id,
        channel_id,
        lambda bridge, guild, channel: bridge.host(guild, interaction.user.id, fee, minutes, channel),
        "抽選を始めました",
        "始められません",
    )


async def raffle_repost(
    cog: "ApprovalCog", interaction: discord.Interaction, guild_id: str, channel_id: Optional[str]
) -> None:
    await _raffle_panel_op(
        cog,
        interaction,
        guild_id,
        channel_id,
        lambda bridge, guild, channel: bridge.repost(guild, channel),
        "出し直しました",
        "出し直せません",
    )


async def raffle_cancel(cog: "ApprovalCog", interaction: discord.Interaction, guild_id: str) -> None:
    await _raffle_panel_op(
        cog,
        interaction,
        guild_id,
        None,
        lambda bridge, guild, _channel: bridge.cancel(guild),
        "中止しました",
        "中止できません",
    )


async def quiz_add(
    cog: "ApprovalCog",
    interaction: discord.Interaction,
    announce_flag: bool,
    guild_id: Optional[str],
    channel_id: Optional[str],
) -> None:
    guild = await pick_guild(cog.bot, interaction, guild_id) if guild_id else None
    if guild_id and guild is None:
        return
    ok, channel = await pick_channel(interaction, guild, channel_id)
    if not ok:
        return
    quiz_bridge = await require_bridge(interaction, cog.bot, IQuizAdminBridge, "クイズ機能", cog._quiz_bridge)
    if quiz_bridge is None:
        return
    await interaction.response.send_modal(quiz_bridge.add_modal(announce_flag, guild, channel))


async def quiz_bulk(
    cog: "ApprovalCog",
    interaction: discord.Interaction,
    file: discord.Attachment,
    announce_flag: bool,
    guild_id: Optional[str],
    channel_id: Optional[str],
) -> None:
    guild = await pick_guild(cog.bot, interaction, guild_id) if guild_id else None
    if guild_id and guild is None:
        return
    ok, channel = await pick_channel(interaction, guild, channel_id)
    if not ok:
        return
    if file.size > MAX_IMPORT_BYTES or not (
        file.filename.lower().endswith((".txt", ".md", ".text")) or (file.content_type or "").startswith("text/")
    ):
        await ui.reply_error(
            interaction,
            f"テキストファイル（.txt、{MAX_IMPORT_BYTES // 1024}KBまで）を添付してください。",
            title="ファイルを使えません",
        )
        return
    await interaction.response.defer(ephemeral=True)
    try:
        text = (await file.read()).decode("utf-8-sig")
    except UnicodeDecodeError:
        await interaction.followup.send(
            embed=ui.error_embed("文字コードを読めません", "UTF-8 で保存したテキストファイルを添付してください。"),
            ephemeral=True,
        )
        return
    quizzes, errors = quizimport.parse_bulk(text)
    if not errors:
        existing = cog._quiz.existing_questions()
        errors = [
            f"{i}問目: すでに登録されている問題です（同じ問題文）。"
            for i, q in enumerate(quizzes, start=1)
            if q["question"] in existing
        ]
    if errors:
        more = f"\n…ほか{len(errors) - 15}件" if len(errors) > 15 else ""
        await interaction.followup.send(
            embed=ui.error_embed(
                f"登録できません（誤り{len(errors)}件・1問も登録していません）", "\n".join(errors[:15]) + more
            ),
            ephemeral=True,
        )
        return
    view = QuizBulkConfirm(cog, interaction.user.id, quizzes, announce_flag, guild, channel)
    head = ui.embed(
        f"📰 クイズを{len(quizzes)}問登録します",
        bulk_preview_lines(quizzes)
        + "\n\n問題なければ「登録する」を押してください。"
        + (f"\n（告知: {view.target_text()}に出します）" if view.announce else "\n（告知: しません）"),
        ui.WARN,
    )
    await interaction.followup.send(embeds=[head], view=view, ephemeral=True)


async def quiz_list(cog: "ApprovalCog", interaction: discord.Interaction) -> None:
    quiz_bridge = await require_bridge(interaction, cog.bot, IQuizAdminBridge, "クイズ機能", cog._quiz_bridge)
    if quiz_bridge is None:
        return
    await interaction.response.send_message(embed=quiz_bridge.listing_embed(), ephemeral=True)


async def quiz_delete(cog: "ApprovalCog", interaction: discord.Interaction, quiz_id: int) -> None:
    quiz_bridge = await require_bridge(interaction, cog.bot, IQuizAdminBridge, "クイズ機能", cog._quiz_bridge)
    if quiz_bridge is None:
        return
    if not quiz_bridge.remove(quiz_id):
        await ui.reply_error(interaction, "", title="その番号の問題はありません")
        return
    await ui.reply_ok(interaction, "", title=f"クイズ #{quiz_id} を削除しました")
