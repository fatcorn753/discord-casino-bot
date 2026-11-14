"""運営向けのコンテンツ・メッセージ操作（臨時競馬・お知らせ・発言削除・抽選・クイズ）。"""

from typing import Optional

import discord

from cogs.admin.embeds import bulk_preview_lines
from cogs.admin.purge import can_read_history
from cogs.admin.views import AnnounceModal, PurgeConfirm, QuizBulkConfirm
from cogs.shared import ui
from cogs.shared.cog_ports import IHorseCogPort, IQuizCogPort, IRaffleCogPort, require_cog_port
from domain import quizimport

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMPORT_BYTES = 200 * 1024


async def special_race(
    cog,
    interaction: discord.Interaction,
    guild_id: str,
    entry_min: int,
    bet_min: int,
    channel_id: Optional[str],
) -> None:
    guild = await cog._pick_guild(interaction, guild_id)
    if guild is None:
        return
    ok, channel = await cog._pick_channel(interaction, guild, channel_id)
    if not ok:
        return
    horse_cog = await require_cog_port(cog.bot, interaction, "HorseCog", IHorseCogPort, "競馬")
    if horse_cog is None:
        return
    race_id = await horse_cog.start_special(guild, entry_min, bet_min, channel)
    await interaction.response.send_message(
        embed=ui.ok_embed(
            "臨時レースを開催しました",
            f"{guild.name} で、臨時レース（#{race_id}）の申し込みを始めました（掲示は{channel.mention if channel else '告知チャンネル'}）。申し込み {entry_min}分 → 馬券 {bet_min}分 → 発走（2分後）。",
        ),
        ephemeral=True,
    )


async def announce(
    cog,
    interaction: discord.Interaction,
    guild_id: Optional[str],
    image: Optional[discord.Attachment],
    title: Optional[str],
    channel_id: Optional[str],
) -> None:
    if image is not None and not ((image.content_type or "").startswith("image/") and image.size <= MAX_IMAGE_BYTES):
        await interaction.response.send_message(
            embed=ui.error_embed("画像を使えません", "画像ファイル（8MBまで）を添付してください。"), ephemeral=True
        )
        return
    if guild_id:
        guild = await cog._pick_guild(interaction, guild_id)
        if guild is None:
            return
        guilds = [guild]
    else:
        guilds = cog._allowed_guilds()
    ok, channel = await cog._pick_channel(interaction, guilds[0] if guild_id else None, channel_id)
    if not ok:
        return
    if not guilds:
        await interaction.response.send_message(
            embed=ui.error_embed("送り先がありません", "許可されているサーバーがありません。"), ephemeral=True
        )
        return
    await interaction.response.send_modal(AnnounceModal(cog, guilds, (title or "").strip()[:200], image, channel))


async def purge_own_messages(cog, interaction: discord.Interaction, guild_id: str, channel_id: str) -> None:
    guild = await cog._pick_guild(interaction, guild_id)
    if guild is None:
        return
    ok, channel = await cog._pick_channel(interaction, guild, channel_id)
    if not ok:
        return
    if not can_read_history(channel, guild):
        await interaction.response.send_message(
            embed=ui.error_embed(
                "履歴を読めません",
                f"Bot は #{channel.name} の「メッセージ履歴を読む」権限がありません。招待URLの権限に足して（README を参照）、もう一度試してください。",
            ),
            ephemeral=True,
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


async def raffle_start(
    cog,
    interaction: discord.Interaction,
    guild_id: str,
    fee: Optional[int],
    minutes: int,
    channel_id: Optional[str],
) -> None:
    guild = await cog._pick_guild(interaction, guild_id)
    if guild is None:
        return
    ok, channel = await cog._pick_channel(interaction, guild, channel_id)
    if not ok:
        return
    raffle_cog = await require_cog_port(cog.bot, interaction, "RaffleCog", IRaffleCogPort, "抽選機能")
    if raffle_cog is None:
        return
    await interaction.response.defer(ephemeral=True)
    ok, text = await raffle_cog.host(guild, interaction.user.id, fee, minutes, channel)
    await interaction.followup.send(
        embed=ui.ok_embed("抽選を始めました", text) if ok else ui.error_embed("始められません", text),
        ephemeral=True,
    )


async def raffle_repost(cog, interaction: discord.Interaction, guild_id: str, channel_id: Optional[str]) -> None:
    guild = await cog._pick_guild(interaction, guild_id)
    if guild is None:
        return
    ok, channel = await cog._pick_channel(interaction, guild, channel_id)
    if not ok:
        return
    raffle_cog = await require_cog_port(cog.bot, interaction, "RaffleCog", IRaffleCogPort, "抽選機能")
    if raffle_cog is None:
        return
    await interaction.response.defer(ephemeral=True)
    ok, text = await raffle_cog.repost(guild, channel)
    await interaction.followup.send(
        embed=ui.ok_embed("出し直しました", text) if ok else ui.error_embed("出し直せません", text), ephemeral=True
    )


async def raffle_cancel(cog, interaction: discord.Interaction, guild_id: str) -> None:
    guild = await cog._pick_guild(interaction, guild_id)
    if guild is None:
        return
    raffle_cog = await require_cog_port(cog.bot, interaction, "RaffleCog", IRaffleCogPort, "抽選機能")
    if raffle_cog is None:
        return
    await interaction.response.defer(ephemeral=True)
    ok, text = await raffle_cog.cancel(guild)
    await interaction.followup.send(
        embed=ui.ok_embed("中止しました", text) if ok else ui.error_embed("中止できません", text), ephemeral=True
    )


async def quiz_add(
    cog,
    interaction: discord.Interaction,
    announce_flag: bool,
    guild_id: Optional[str],
    channel_id: Optional[str],
) -> None:
    guild = await cog._pick_guild(interaction, guild_id) if guild_id else None
    if guild_id and guild is None:
        return
    ok, channel = await cog._pick_channel(interaction, guild, channel_id)
    if not ok:
        return
    quiz_cog = await require_cog_port(cog.bot, interaction, "QuizCog", IQuizCogPort, "クイズ機能")
    if quiz_cog is None:
        return
    await interaction.response.send_modal(quiz_cog.add_modal(announce_flag, guild, channel))


async def quiz_bulk(
    cog,
    interaction: discord.Interaction,
    file: discord.Attachment,
    announce_flag: bool,
    guild_id: Optional[str],
    channel_id: Optional[str],
) -> None:
    guild = await cog._pick_guild(interaction, guild_id) if guild_id else None
    if guild_id and guild is None:
        return
    ok, channel = await cog._pick_channel(interaction, guild, channel_id)
    if not ok:
        return
    if file.size > MAX_IMPORT_BYTES or not (
        file.filename.lower().endswith((".txt", ".md", ".text")) or (file.content_type or "").startswith("text/")
    ):
        await interaction.response.send_message(
            embed=ui.error_embed(
                "ファイルを使えません",
                f"テキストファイル（.txt、{MAX_IMPORT_BYTES // 1024}KBまで）を添付してください。",
            ),
            ephemeral=True,
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
        existing = cog.bot.economy.quiz_existing_questions()
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


async def quiz_list(cog, interaction: discord.Interaction) -> None:
    quiz_cog = await require_cog_port(cog.bot, interaction, "QuizCog", IQuizCogPort, "クイズ機能")
    if quiz_cog is None:
        return
    await interaction.response.send_message(embed=quiz_cog.listing_embed(), ephemeral=True)


async def quiz_delete(cog, interaction: discord.Interaction, quiz_id: int) -> None:
    quiz_cog = await require_cog_port(cog.bot, interaction, "QuizCog", IQuizCogPort, "クイズ機能")
    if quiz_cog is None:
        return
    if not quiz_cog.remove(quiz_id):
        await interaction.response.send_message(embed=ui.error_embed("その番号の問題はありません"), ephemeral=True)
        return
    await interaction.response.send_message(embed=ui.ok_embed(f"クイズ #{quiz_id} を削除しました"), ephemeral=True)
