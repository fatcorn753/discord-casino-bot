"""画面の部品: 埋め込みの生成とペリカの表記。"""

import datetime

import discord

OK = 0x2ECC71
INFO = 0x3498DB
WARN = 0xF1C40F
ERROR = 0xE74C3C
GOLD = 0xF39C12
DARK = 0x2C2F33  # 地下カジノ用の暗い色

UNIT = "ペリカ"


def pelica(amount: int) -> str:
    """金額を「12,345ペリカ」の形にする。"""
    return f"{int(amount):,}{UNIT}"


def embed(title: str, description: str = "", color: int = INFO) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    e.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return e


def ok_embed(title: str, description: str = "") -> discord.Embed:
    return embed(f"✅ {title}", description, OK)


def error_embed(title: str, description: str = "") -> discord.Embed:
    return embed(f"❌ {title}", description, ERROR)


def warn_embed(title: str, description: str = "") -> discord.Embed:
    return embed(f"⚠️ {title}", description, WARN)


class OwnerView(discord.ui.View):
    """開いた本人（owner_id）だけが操作できる View の基底クラス。"""

    def __init__(self, owner_id: int, timeout: float | None = 300, owner_message: str = ""):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self._owner_message = owner_message

    async def _reject_non_owner(self, interaction: discord.Interaction) -> None:
        if self._owner_message:
            await interaction.response.send_message(self._owner_message, ephemeral=True)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await self._reject_non_owner(interaction)
            return False
        return True
