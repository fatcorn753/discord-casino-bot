"""金額の入力欄に、自分の現在の残高を見せる。

金額の欄は、押すと候補の枠が開く。そこに残高を1行だけ出す（全額・半分などの候補は出さない）。
数字を打ち始めると、その数字に残高を添えた1行になり、そのまま選べる。
`_` で始まるファイルは部品で、Bot は読み込まない。コマンドの `amount` / `bet` などの欄に
`@app_commands.autocomplete(bet=...)` でつなぐ。
"""

import unicodedata
from collections.abc import Awaitable, Callable
from typing import Optional

import discord
from discord import app_commands

from cogs.shared import ui
from services.economy import EconomyService


def own_balance(economy: EconomyService, guild_id: Optional[int], user_id: int) -> Optional[int]:
    """いまの残高。口座を作らずに読む（初めての人は、最初に遊ぶときに初期支給が入る額を返す）。DM などサーバー外は None。"""
    if guild_id is None:
        return None
    return economy.peek_balance(guild_id, user_id)


def typed_amount(current: str) -> Optional[int]:
    """入力途中の文字列を整数にする（全角・カンマ可）。数字でなければ None。"""
    text = unicodedata.normalize("NFKC", current).replace(",", "").strip()
    return int(text) if text.isascii() and text.isdigit() else None


def optional_amount(raw: str) -> tuple[bool, Optional[int]]:
    """空欄にできる金額の欄を読む。空欄なら (True, None)、数字なら (True, 値)、それ以外は (False, None)。"""
    if not unicodedata.normalize("NFKC", raw).replace(",", "").strip():
        return True, None
    amount = typed_amount(raw)
    return amount is not None, amount


async def reject_bad_amount(interaction: discord.Interaction) -> None:
    """金額の欄に数字以外が入っていたときの返事（Modal 共通）。"""
    await ui.reply_error(interaction, "半角の数字で入れてください。", title="金額が正しくありません")


class AmountModal(discord.ui.Modal):
    """金額を1つ入れる Modal。空欄を許すとき（required=False）は、空欄を None として on_amount に渡す。"""

    def __init__(
        self,
        *,
        title: str,
        label: str,
        required: bool,
        on_amount: Callable[[discord.Interaction, Optional[int]], Awaitable[None]],
        max_length: int = 12,
    ) -> None:
        super().__init__(title=title)
        self._on_amount = on_amount
        self.amount: discord.ui.TextInput[AmountModal] = discord.ui.TextInput(
            label=label, required=required, max_length=max_length
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        ok, amount = optional_amount(self.amount.value)
        if not ok:
            await reject_bad_amount(interaction)
            return
        await self._on_amount(interaction, amount)


def balance_choices(
    economy: EconomyService,
    interaction: discord.Interaction,
    current: str,
    minimum: int,
    maximum: Optional[int] = None,
) -> list[app_commands.Choice[int]]:
    """残高を見せる、1行だけの候補。

    - 範囲（minimum 以上、maximum があればそれ以下）内の数字を入力中なら「500（残高 8,000ペリカ）」。選ぶと、その数字になる。
    - それ以外（空・範囲外）は「残高 8,000ペリカ」。選ぶと minimum になるので、見るだけなら選ばずに数字を打つ。
    DM では候補を出さない。
    """
    balance = own_balance(economy, interaction.guild_id, interaction.user.id)
    if balance is None:
        return []
    typed = typed_amount(current)
    if typed is not None and typed >= minimum and (maximum is None or typed <= maximum):
        return [app_commands.Choice(name=f"{typed:,}（残高 {balance:,}ペリカ）", value=typed)]
    return [app_commands.Choice(name=f"残高 {balance:,}ペリカ（選ぶと {minimum:,} になります）", value=minimum)]
