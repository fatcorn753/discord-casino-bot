"""Activity（Discord の中で開く画面）のサーバーを、Bot と一緒に動かす cog。コマンドは持たない。

config.json に `activity` の項目があるときだけ、サーバーを立てる。

    "activity": {
        "client_secret": "（Developer Portal の OAuth2 の Client Secret）",
        "client_id": "（省略すると Bot のアプリケーション ID）",
        "host": "127.0.0.1",
        "port": 8787
    }

外からは、Tailscale Funnel などの HTTPS で `host:port` に届ける。Bot は 127.0.0.1 でだけ待ち受けるのが既定。
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
import discord
from aiohttp import web
from discord.ext import commands

from cogs.activity.auth import SessionSigner, discord_exchanger
from cogs.activity.server import Hub, build_app
from cogs.casino import chinchiro_engine as engine
from cogs.casino.common import CasinoDeps

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot

_log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parents[2] / "web" / "activity" / "dist"


class ActivityCog(commands.Cog):
    """Activity の画面のための HTTP サーバー（aiohttp）を、cog の読み込みと一緒に立て、外すときに止める。"""

    def __init__(self, bot: "CasinoBot"):
        self.bot = bot
        self.settings = bot.config.get("activity") or {}
        self.hub = Hub(
            CasinoDeps(
                casino=bot.services.casino,
                teiai=bot.services.teiai,
                guild_repo=bot.repos.guild,
                config=bot.config,
            )
        )
        self._http: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None

    async def member_check(self, guild_id: int, user_id: int, channel_id: int) -> str | None:
        """その人が、このサーバーのメンバーで、そのチャンネルを見られるなら表示名。だめなら None。"""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None
        channel = guild.get_channel_or_thread(channel_id)
        if channel is None:
            return None
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                return None
        if not channel.permissions_for(member).view_channel:
            return None
        return member.display_name

    async def cog_load(self) -> None:
        secret = self.settings.get("client_secret")
        if not secret:
            _log.info("config.json に activity.client_secret がないので、Activity のサーバーは立てません")
            return
        client_id = str(self.settings.get("client_id") or self.bot.application_id or "")
        if not client_id:
            _log.warning("Activity の client_id が分からないので、サーバーを立てません")
            return
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        app = build_app(
            self.hub,
            SessionSigner(),
            discord_exchanger(client_id, secret, self._http),
            self.member_check,
            STATIC_DIR,
            client_id,
        )
        self._runner = web.AppRunner(app, access_log=None)  # URL に何も載せないが、念のためアクセスの記録は残さない
        await self._runner.setup()
        host, port = self.settings.get("host", "127.0.0.1"), int(self.settings.get("port", 8787))
        await web.TCPSite(self._runner, host, port).start()
        engine.add_register_hook(self.hub.on_register)
        _log.info("Activity のサーバーを %s:%s で立てました", host, port)

    async def cog_unload(self) -> None:
        engine.remove_register_hook(self.hub.on_register)
        if self._runner is not None:
            await self._runner.cleanup()
        if self._http is not None:
            await self._http.close()


async def setup(bot: "CasinoBot") -> None:
    await bot.add_cog(ActivityCog(bot))
