"""/help — はじめての人向けの短い案内と、ボタンで開くカテゴリ別の説明。"""

import discord
from discord import app_commands
from discord.ext import commands

from cogs.help.texts import BUTTONS, HOME_DESCRIPTION, HOME_TITLE, PAGES, home_footer
from cogs.shared import ui
from domain.games.rtplevel import MIN_BET

HOME = "home"


def page_embed(key: str) -> discord.Embed:
    if key == HOME:
        e = ui.embed(HOME_TITLE, HOME_DESCRIPTION, ui.GOLD)
        e.set_footer(text=home_footer(MIN_BET))
        return e
    title, lines = PAGES[key]
    return ui.embed(title, "\n".join(lines), ui.GOLD)


def _make_callback(key: str):
    async def callback(interaction: discord.Interaction):
        await interaction.response.edit_message(embed=page_embed(key), view=HelpView(key))

    return callback


class HelpView(discord.ui.View):
    def __init__(self, current: str = HOME, timeout: float = 180):
        super().__init__(timeout=timeout)
        for key, label in BUTTONS:
            button = discord.ui.Button(
                label=label, style=discord.ButtonStyle.primary if key == current else discord.ButtonStyle.secondary
            )
            button.callback = _make_callback(key)
            self.add_item(button)


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="📖 はじめての人向けの案内（ボタンでくわしい説明）")
    async def help(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=page_embed(HOME), view=HelpView(), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Help(bot))
