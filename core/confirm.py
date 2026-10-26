"""実行者だけが押せる確認ボタン。"""

from typing import Optional

import discord

from core import ui


class ConfirmView(discord.ui.View):
    def __init__(self, author_id: int, label: str, style: discord.ButtonStyle = discord.ButtonStyle.danger):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.value: Optional[bool] = None
        self.confirm.label = label
        self.confirm.style = style

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("この操作を実行できるのはコマンドを使った本人だけです。", ephemeral=True)
            return False
        return True

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
    outcome = {True: ui.embed("実行します", "処理中です…", ui.INFO),
               False: ui.embed("中止", "キャンセルしました。", ui.INFO),
               None: ui.embed("中止", "30秒以内に操作がなかったため、中止しました。", ui.INFO)}[view.value]
    try:
        await interaction.edit_original_response(embed=outcome, view=None)
    except discord.HTTPException:
        pass
    return view.value
