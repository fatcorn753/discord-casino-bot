"""Cog 拡張モジュールの一覧管理とロード（main.py とテストで単一正本を共有）。"""

import traceback

from discord.ext import commands

COG_MODULES: tuple[str, ...] = (
    "cogs.admin.cog",
    "cogs.casino.cog",
    "cogs.economy.cog",
    "cogs.event.cog",
    "cogs.help.cog",
    "cogs.horse.cog",
    "cogs.labor.cog",
    "cogs.loan.cog",
    "cogs.market.bond_cog",
    "cogs.market.cog",
    "cogs.p2p.cog",
    "cogs.quiz.cog",
    "cogs.raffle.cog",
    "cogs.shop.cog",
    "cogs.weekly.cog",
)


async def load_all_cogs(bot: commands.Bot) -> list[str]:
    """定義済みのすべての Cog を読み込み、失敗したモジュール名のリストを返す。"""
    failed: list[str] = []
    for module in COG_MODULES:
        try:
            await bot.load_extension(module)
            print(f"✓ ロード: {module}")
        except Exception:
            print(f"✗ 失敗: {module}")
            traceback.print_exc()
            failed.append(module)
    return failed
