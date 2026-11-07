"""運営コマンド（`/運営`）の確認ボタンと入力フォーム（お知らせ・クイズの一括登録）。"""

import io
import time
from typing import TYPE_CHECKING, Optional

import discord

from cogs._admin_embeds import announce_embed
from cogs.weekly_cog import announce_channel
from core import ui

if TYPE_CHECKING:
    from cogs.approval_cog import ApprovalCog


class AnnounceConfirm(discord.ui.View):
    """送信前の確認。持ち主本人だけが押せる。"""

    def __init__(
        self,
        cog: "ApprovalCog",
        owner_id: int,
        guilds: list,
        title: str,
        body: str,
        filename: str,
        data: bytes,
        channel=None,
    ):
        super().__init__(timeout=300)
        self.cog, self.owner_id, self.guilds, self.channel = cog, owner_id, guilds, channel
        self.title, self.body, self.filename, self.data = title, body, filename, data

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="送信する", style=discord.ButtonStyle.success)
    async def send(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            embeds=[ui.embed("📢 送信中…", f"{len(self.guilds)}サーバーに送っています。", ui.INFO)],
            attachments=[],
            view=None,
        )
        done, failed = [], []
        for guild in self.guilds:
            channel = self.channel or announce_channel(guild, self.cog.bot.economy.for_guild(guild.id))
            if channel is None:
                failed.append(f"{guild.name}（投稿できるチャンネルがありません）")
                continue
            try:
                file = (
                    discord.File(io.BytesIO(self.data), filename=self.filename) if self.data else discord.utils.MISSING
                )
                await channel.send(
                    embed=announce_embed(self.title, self.body, self.filename if self.data else ""),
                    file=file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                done.append(guild.name)
            except discord.HTTPException as exc:
                failed.append(f"{guild.name}（{exc}）")
        lines = [f"✅ {len(done)}サーバーに送りました" + (f": {', '.join(done)}" if done else "")]
        if failed:
            lines.append(f"⚠️ {len(failed)}サーバーには送れませんでした: " + " / ".join(failed))
        await interaction.edit_original_response(embed=ui.ok_embed("お知らせを送りました", "\n".join(lines)[:3900]))

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            embeds=[ui.embed("📢 お知らせ", "送らずにやめました。", ui.INFO)], attachments=[], view=None
        )


class QuizBulkConfirm(discord.ui.View):
    """一括登録の確認。持ち主本人だけが押せる。"""

    def __init__(self, cog: "ApprovalCog", owner_id: int, quizzes: list, announce: bool, guild=None, channel=None):
        super().__init__(timeout=300)
        self.cog, self.owner_id, self.quizzes, self.announce, self.guild, self.channel = (
            cog,
            owner_id,
            quizzes,
            announce,
            guild,
            channel,
        )

    def target_text(self) -> str:
        if self.guild is None:
            return "全サーバーの告知チャンネル"
        return f"{self.guild.name} の " + (f"#{self.channel.name}" if self.channel is not None else "告知チャンネル")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="登録する", style=discord.ButtonStyle.success)
    async def register(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.defer()
        ids = self.cog.bot.economy.quiz_add_many(self.quizzes, time.time(), interaction.user.display_name)
        note = "告知はしていません。`/クイズ 挑戦` に出ます。"
        if self.announce:
            quiz_cog = self.cog.bot.get_cog("QuizCog")
            counts = [await quiz_cog.broadcast(qid, self.guild, self.channel) for qid in ids]
            note = f"{self.target_text()}に、{len(ids)}問を出しました（のべ{sum(counts)}件の投稿）。"
        await interaction.edit_original_response(
            embeds=[ui.ok_embed(f"クイズを{len(ids)}問登録しました", f"番号: #{ids[0]}〜#{ids[-1]}\n{note}")], view=None
        )

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            embeds=[ui.embed("📰 クイズの一括登録", "登録せずにやめました。", ui.INFO)], view=None
        )


class AnnounceModal(discord.ui.Modal, title="お知らせの本文"):
    body = discord.ui.TextInput(label="本文（改行できます）", style=discord.TextStyle.paragraph, max_length=2000)

    def __init__(
        self, cog: "ApprovalCog", guilds: list, heading: str, image: Optional[discord.Attachment], channel=None
    ):
        super().__init__()
        self.cog, self.guilds, self.heading, self.image, self.channel = cog, guilds, heading, image, channel

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        data, filename = b"", ""
        if self.image is not None:
            data, filename = await self.image.read(), self.image.filename
        view = AnnounceConfirm(
            self.cog, interaction.user.id, self.guilds, self.heading, self.body.value, filename, data, self.channel
        )
        names = ", ".join(g.name for g in self.guilds) or "なし"
        if self.channel is not None:
            names += f"（#{self.channel.name}）"
        preview = announce_embed(self.heading, self.body.value, filename)
        head = ui.embed(
            "📢 送信前の確認",
            f"送り先（{len(self.guilds)}サーバー）: {names}\n下がプレビューです。問題なければ「送信する」を押してください。",
            ui.WARN,
        )
        files = [discord.File(io.BytesIO(data), filename=filename)] if data else []
        await interaction.followup.send(embeds=[head, preview], files=files, view=view, ephemeral=True)
