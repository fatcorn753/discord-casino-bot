"""実行者だけが押せる確認ボタンと、セッション保護つき確認応答の共通処理。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Optional

import discord

from cogs.shared import ui


class ConfirmView(ui.OwnerView):
    def __init__(self, author_id: int, label: str, style: discord.ButtonStyle = discord.ButtonStyle.danger):
        super().__init__(
            author_id,
            timeout=30,
            owner_message="この操作を実行できるのはコマンドを使った本人だけです。",
        )
        self.author_id = author_id
        self.value: Optional[bool] = None
        self.confirm.label = label
        self.confirm.style = style

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.defer()
        self.stop()


async def confirm_action(interaction: discord.Interaction, title: str, description: str, label: str) -> Optional[bool]:
    """確認ボタンを出し、True / False / None(時間切れ) を返す。呼んだあとの返信は interaction.followup で行う。"""
    view = ConfirmView(interaction.user.id, label)
    await interaction.response.send_message(embed=ui.warn_embed(title, description), view=view, ephemeral=True)
    await view.wait()
    outcome = {
        True: ui.embed("実行します", "処理中です…", ui.INFO),
        False: ui.embed("中止", "キャンセルしました。", ui.INFO),
        None: ui.embed("中止", "30秒以内に操作がなかったため、中止しました。", ui.INFO),
    }[view.value]
    try:
        await interaction.edit_original_response(embed=outcome, view=None)
    except discord.HTTPException:
        pass
    return view.value


@asynccontextmanager
async def user_session(
    bot,
    interaction: discord.Interaction,
    busy_title: str = "いま実行できません",
) -> AsyncIterator[bool]:
    """連打・二重操作を防ぐためユーザー単位のセッションを開始し、終了時に必ず解放する。"""
    uid = interaction.user.id
    reason = bot.sessions.try_start(uid)
    if reason:
        await interaction.response.send_message(embed=ui.error_embed(busy_title, reason), ephemeral=True)
        yield False
        return
    try:
        yield True
    finally:
        bot.sessions.end(uid)


async def confirmed_reply(
    interaction: discord.Interaction,
    needed: bool,
    title: str,
    description: str,
    label: str,
    confirm_fn=confirm_action,
):
    """確認が必要なら確認ボタンを挟み、続行する場合だけ返信関数を返す。中止時は None。"""
    if not needed:
        return interaction.response.send_message
    ok = await confirm_fn(interaction, title, description, label)
    return interaction.followup.send if ok is True else None
