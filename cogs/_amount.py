"""金額の入力欄に、自分の現在の残高を見せる。

金額の欄は、押すと候補の枠が開く。そこに残高を1行だけ出す（全額・半分などの候補は出さない）。
数字を打ち始めると、その数字に残高を添えた1行になり、そのまま選べる。
`_` で始まるファイルは部品で、Bot は読み込まない。コマンドの `amount` / `bet` などの欄に
`@app_commands.autocomplete(bet=...)` でつなぐ。
"""

import unicodedata
from typing import Optional

import discord
from discord import app_commands

from core.eco_types import INITIAL_BALANCE


def own_balance(bot, interaction: discord.Interaction) -> Optional[int]:
    """いまの残高。口座を作らずに読む（初めての人は、最初に遊ぶときに初期支給が入る額を返す）。DM などサーバー外は None。"""
    if interaction.guild_id is None:
        return None
    stats = bot.economy.for_guild(interaction.guild_id).peek_stats(interaction.user.id)
    return stats["balance"] if stats else INITIAL_BALANCE


def typed_amount(current: str) -> Optional[int]:
    """入力途中の文字列を整数にする（全角・カンマ可）。数字でなければ None。"""
    text = unicodedata.normalize("NFKC", current).replace(",", "").strip()
    return int(text) if text.isascii() and text.isdigit() else None


def balance_choices(
    bot, interaction: discord.Interaction, current: str, minimum: int, maximum: int
) -> list[app_commands.Choice[int]]:
    """残高を見せる、1行だけの候補。

    - 範囲（minimum〜maximum）内の数字を入力中なら「500（残高 8,000ペリカ）」。選ぶと、その数字になる。
    - それ以外（空・範囲外）は「残高 8,000ペリカ」。選ぶと minimum になるので、見るだけなら選ばずに数字を打つ。
    DM では候補を出さない。
    """
    balance = own_balance(bot, interaction)
    if balance is None:
        return []
    typed = typed_amount(current)
    if typed is not None and minimum <= typed <= maximum:
        return [app_commands.Choice(name=f"{typed:,}（残高 {balance:,}ペリカ）", value=typed)]
    return [app_commands.Choice(name=f"残高 {balance:,}ペリカ（選ぶと {minimum:,} になります）", value=minimum)]
