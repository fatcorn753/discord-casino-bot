"""/help — はじめての人向けの短い案内と、ボタンで開くカテゴリ別の説明。"""

import discord
from discord import app_commands
from discord.ext import commands

from cogs.shared import ui
from domain.games.rtplevel import MIN_BET
from infra import catalogs

HOME = "home"
_CFG = catalogs.help_pages()

# ボタンで開くページ。1ページは短く（6〜8行まで）
PAGES = {k: (v.title, list(v.lines)) for k, v in _CFG.pages.items()}
BUTTONS = [(b.key, b.label) for b in _CFG.buttons]


def page_embed(key: str) -> discord.Embed:
    if key == HOME:
        e = ui.embed(
            _CFG.home.title,
            _CFG.home.description,
            ui.GOLD,
        )
        e.set_footer(text=_CFG.home.footer_template.format(min_bet=MIN_BET))
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
