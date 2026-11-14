"""画面の部品: 埋め込みの生成とペリカの表記。"""

import datetime
from typing import TYPE_CHECKING, Any, cast

import discord

if TYPE_CHECKING:
    from core.bot import CasinoBot

OK = 0x2ECC71
INFO = 0x3498DB
WARN = 0xF1C40F
ERROR = 0xE74C3C
GOLD = 0xF39C12
DARK = 0x2C2F33  # 地下カジノ用の暗い色

UNIT = "ペリカ"


def bot_of(interaction: discord.Interaction) -> "CasinoBot":
    """interaction.client を CasinoBot として型安全に取り出す。"""
    return cast("CasinoBot", interaction.client)


def selected_values(interaction: discord.Interaction) -> list[str]:
    """Select コンポーネントの選択値リストを型安全に取り出す。"""
    data = interaction.data
    values = data.get("values") if isinstance(data, dict) else None
    return list(values) if values else []


def guild_id_of(interaction: discord.Interaction) -> int:
    """サーバー内コマンドの guild_id を返す（サーバー外なら RuntimeError）。"""
    gid = getattr(interaction, "guild_id", None)
    if gid is None and getattr(interaction, "guild", None) is not None:
        gid = cast(discord.Guild, interaction.guild).id
    if gid is None:
        raise RuntimeError("この操作はサーバー内でのみ利用できます。")
    return gid


async def send_channel(channel: object, **kwargs: Any) -> Any:
    """送信可能なチャンネルであればメッセージを送る。"""
    send = getattr(channel, "send", None)
    if callable(send):
        return await cast(Any, send)(**kwargs)
    return None


def partial_message(channel: object, message_id: int) -> Any:
    """メッセージ編集・削除用の PartialMessage を取得する。"""
    getter = getattr(channel, "get_partial_message", None)
    return getter(message_id) if callable(getter) else None


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
