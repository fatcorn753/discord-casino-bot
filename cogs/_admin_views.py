"""運営コマンド（`/運営`）の確認ボタンと入力フォーム（お知らせ・クイズの一括登録・発言の削除）。"""

import io
import time
from typing import TYPE_CHECKING, Optional

import discord

from cogs._admin_embeds import announce_embed
from cogs._admin_purge import PurgeResult, delete_own_messages
from cogs._shared import announce_channel
from core import approval, ui

if TYPE_CHECKING:
    from cogs.approval_cog import ApprovalCog


class ApprovalButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"guild-approve:(?P<action>allow|deny):(?P<gid>[0-9]+)"
):
    def __init__(self, action: str, guild_id: int):
        allow = action == "allow"
        super().__init__(
            discord.ui.Button(
                label="許可する" if allow else "断って退出させる",
                style=discord.ButtonStyle.success if allow else discord.ButtonStyle.danger,
                custom_id=f"guild-approve:{action}:{guild_id}",
            )
        )
        self.action, self.guild_id = action, guild_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(match["action"], int(match["gid"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in approval.owner_ids(interaction.client.config):
            await interaction.response.send_message("これは Bot の持ち主だけが操作できます。", ephemeral=True)
            return False
        return True

    async def callback(self, interaction: discord.Interaction):
        bot, eco, gid = interaction.client, interaction.client.economy, self.guild_id
        guild = bot.get_guild(gid)
        row = eco.get_guild_status(gid) or {}
        name = guild.name if guild else row.get("name") or str(gid)
        if self.action == "allow":
            eco.set_guild_status(gid, approval.APPROVED, name, interaction.user.id)
            embed = ui.ok_embed("許可しました", f"**{name}**（`{gid}`）で遊べるようになりました。")
            if guild is not None:
                cog = bot.get_cog("ApprovalCog")
                if cog is not None:
                    await cog.announce_approved(guild)
        else:
            eco.set_guild_status(gid, approval.DENIED, name, interaction.user.id)
            left = ""
            if guild is not None:
                try:
                    await guild.leave()
                    left = "Bot はそのサーバーから退出しました。"
                except discord.HTTPException:
                    left = "退出できませんでした（コマンドは使えないままです）。"
            embed = ui.warn_embed("断りました", f"**{name}**（`{gid}`）の利用を断りました。{left}")
        await interaction.response.edit_message(embed=embed, view=None)


class AnnounceConfirm(ui.OwnerView):
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
        super().__init__(owner_id, timeout=300)
        self.cog, self.guilds, self.channel = cog, guilds, channel
        self.title, self.body, self.filename, self.data = title, body, filename, data

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


class QuizBulkConfirm(ui.OwnerView):
    """一括登録の確認。持ち主本人だけが押せる。"""

    def __init__(self, cog: "ApprovalCog", owner_id: int, quizzes: list, announce: bool, guild=None, channel=None):
        super().__init__(owner_id, timeout=300)
        self.cog, self.quizzes, self.announce, self.guild, self.channel = (
            cog,
            quizzes,
            announce,
            guild,
            channel,
        )

    def target_text(self) -> str:
        if self.guild is None:
            return "全サーバーの告知チャンネル"
        return f"{self.guild.name} の " + (f"#{self.channel.name}" if self.channel is not None else "告知チャンネル")

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


def purge_report_embed(result: PurgeResult, done: bool) -> discord.Embed:
    head = "🧹 発言を消しました" if done else "🧹 消しています…"
    lines = [f"消せた発言: **{result.deleted}件**（見た発言: {result.scanned}件）"]
    if result.failed:
        lines.append(f"⚠️ 消せなかった発言: {result.failed}件（権限や通信のエラー。ログに記録しています）")
    return ui.ok_embed(head, "\n".join(lines)) if done else ui.embed(head, "\n".join(lines), ui.INFO)


class PurgeConfirm(ui.OwnerView):
    """Bot の発言を消す前の確認。持ち主本人だけが押せる。元に戻せないので、押すまで何も消さない。"""

    def __init__(self, cog: "ApprovalCog", owner_id: int, channel):
        super().__init__(owner_id, timeout=300)
        self.cog, self.channel = cog, channel

    @discord.ui.button(label="すべて消す", style=discord.ButtonStyle.danger)
    async def purge(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        key = self.channel.id
        if key in self.cog.purging:
            return await interaction.response.edit_message(
                embeds=[ui.error_embed("実行中です", "このチャンネルは、いま消している最中です。")], view=None
            )
        self.cog.purging.add(key)
        await interaction.response.edit_message(embeds=[purge_report_embed(PurgeResult(), False)], view=None)

        async def progress(result: PurgeResult) -> None:
            try:
                await interaction.edit_original_response(embeds=[purge_report_embed(result, False)])
            except discord.HTTPException:
                pass  # 画面が更新できなくても、削除は続ける

        try:
            result = await delete_own_messages(self.channel, self.cog.bot.user.id, progress)
            final = purge_report_embed(result, True)
        except discord.Forbidden:
            final = ui.error_embed(
                "履歴を読めません", "Bot に「メッセージ履歴を読む」権限がありません。招待URLの権限を確認してください。"
            )
        finally:
            self.cog.purging.discard(key)
        try:
            await interaction.edit_original_response(embeds=[final])
        except discord.HTTPException:
            try:
                await interaction.user.send(embed=final)  # 時間がかかって、DM の応答の有効期限（15分）を過ぎたとき
            except discord.HTTPException:
                pass

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            embeds=[ui.embed("🧹 発言の削除", "消さずにやめました。", ui.INFO)], view=None
        )
