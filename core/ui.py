"""画面の部品: 埋め込みの生成とペリカの表記。"""

import datetime

import discord

OK = 0x2ECC71
INFO = 0x3498DB
WARN = 0xF1C40F
ERROR = 0xE74C3C
GOLD = 0xF39C12
DARK = 0x2C2F33   # 地下カジノ用の暗い色

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
