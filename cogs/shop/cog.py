"""/ショップ — ペリカで称号・カード・消耗品・名前の色を交換する。景品はすべてゲーム内・サーバー内で完結する。

`/ショップ` ひとつで、操作パネル（選択肢とボタン）を開く。購入・装備・解除の実処理は actions.py へ分離。
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.shared import logthrottle, ui
from cogs.shared.bridges import BotBridges, IP2PNavigationBridge, IShopTitleBridge
from cogs.shared.tasks import allowed_guilds
from cogs.shop.discord_ops import ShopError, apply_title_nick, set_color_role
from cogs.shop.embeds import (
    EFFECT_OF_ITEM as EFFECT_OF_ITEM,
)
from cogs.shop.embeds import MAX_QTY as MAX_QTY
from cogs.shop.embeds import P2P_FILTERS as P2P_FILTERS
from cogs.shop.embeds import SECTION_NAME as SECTION_NAME
from cogs.shop.embeds import SHOWN as SHOWN
from cogs.shop.embeds import SLOT_OF_KIND as SLOT_OF_KIND
from cogs.shop.embeds import TAB_NOTE as TAB_NOTE
from cogs.shop.embeds import TABS as TABS
from cogs.shop.embeds import TIERS as TIERS
from cogs.shop.embeds import UNEQUIP_NAMES as UNEQUIP_NAMES
from cogs.shop.embeds import auction_embed as auction_embed
from cogs.shop.embeds import catalog_embed as catalog_embed
from cogs.shop.embeds import inventory_embed as inventory_embed
from cogs.shop.embeds import item_line as item_line
from cogs.shop.embeds import kind_name as kind_name
from cogs.shop.embeds import shop_slots as shop_slots
from cogs.shop.embeds import stock_text as stock_text
from cogs.shop.embeds import tab_items as tab_items
from cogs.shop.views import ShopView
from domain import teiai
from domain.ports import get_catalogs
from domain.ports.repos import IGuildRepo
from domain.ports.session import ISessionGuard
from services.shop import ShopService

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot

_SHOP_CFG = get_catalogs().shop()
CONFIRM_FROM = _SHOP_CFG.confirm_from  # この金額以上の購入は、確認ボタンを挟む
STATUS_NICK_MINUTES = _SHOP_CFG.status_nick_minutes  # 身分のニックネームを付け外しする確認の間隔
STATUS_NICK_PER_GUILD = _SHOP_CFG.status_nick_per_guild  # 1回の確認で、1サーバーあたり扱う人数の上限


class ShopCog(commands.Cog, IShopTitleBridge):
    CONFIRM_FROM = CONFIRM_FROM

    def __init__(
        self,
        bot: commands.Bot,
        config: dict[str, Any],
        shop_svc: ShopService,
        guild_repo: IGuildRepo,
        sessions: ISessionGuard,
        p2p_bridge: Optional[IP2PNavigationBridge] = None,
    ) -> None:
        self.bot = bot
        self._config = config
        self.service = shop_svc
        self._guild_repo = guild_repo
        self._sessions = sessions
        self._p2p_bridge = p2p_bridge
        self._role_locks: dict[int, asyncio.Lock] = {}  # サーバーごとのロール操作の鍵（同名ロールの二重作成を防ぐ）
        self._nick_locks: dict[
            int, asyncio.Lock
        ] = {}  # 利用者ごとのニックネーム操作の鍵（連打で古い名前を元に計算しない）

    def _role_lock(self, guild_id: int) -> asyncio.Lock:
        return self._role_locks.setdefault(guild_id, asyncio.Lock())

    async def sync_status_nick(self, guild: discord.Guild, member: discord.Member) -> None:
        """身分に合わせて、ニックネームの末尾の【地下労働者】などを付ける・外す。"""
        tag = self.service.nick_tag(guild.id, member.id)
        if tag is None and not self._guild_repo.nick_managed(guild.id, member.id):
            return
        if tag in teiai.STATUS_NAME.values():
            self._guild_repo.ensure_nick_row(guild.id, member.id)
        async with self._nick_locks.setdefault(member.id, asyncio.Lock()):
            await apply_title_nick(guild, member, self.service.nick_tag(guild.id, member.id), self._guild_repo)

    async def sync_title_nick(
        self, user_id: int, here: discord.Guild | None = None, member: discord.Member | None = None
    ) -> tuple[str, int]:
        """称号の変更を、ニックネーム表示をONにしているサーバーへ反映する。"""
        async with self._nick_locks.setdefault(user_id, asyncio.Lock()):
            problem, others_failed = "", 0
            for gid in self._guild_repo.nick_pref_guilds(user_id):
                guild = self.bot.get_guild(gid)
                if guild is None or (here is not None and gid != here.id):
                    continue
                title = self.service.nick_tag(gid, user_id)
                is_here = here is not None and gid == here.id
                try:  # 1サーバー分のニックネーム反映。失敗時は (理由, 他サーバー分の失敗数) を集める
                    m = (
                        member
                        if is_here and member is not None
                        else (guild.get_member(user_id) or await guild.fetch_member(user_id))
                    )
                    await apply_title_nick(guild, m, title, self._guild_repo)
                    p, failed = "", 0
                except discord.NotFound:
                    p, failed = "", 0
                except ShopError as exc:
                    p, failed = (str(exc), 0) if is_here else ("", 1)
                except discord.HTTPException as exc:
                    _log.warning(f"称号のニックネーム反映に失敗: guild={gid} user={user_id}: {exc!r}")
                    p, failed = ("Discord 側のエラーで失敗しました。", 0) if is_here else ("", 1)
                problem = p or problem
                others_failed += failed
            return problem, others_failed

    async def cog_load(self) -> None:
        self.status_nick_tick.start()

    async def cog_unload(self) -> None:
        self.status_nick_tick.cancel()
        bridges = getattr(self.bot, "bridges", None)
        if isinstance(bridges, BotBridges):
            bridges.unregister(IShopTitleBridge, self)

    @tasks.loop(minutes=STATUS_NICK_MINUTES)
    async def status_nick_tick(self):
        try:
            await self.run_status_nick()
        except Exception as exc:
            _log.warning(f"身分のニックネームの更新に失敗: {exc!r}")

    @status_nick_tick.before_loop
    async def _before_status_nick(self):
        await self.bot.wait_until_ready()

    async def after_bankruptcy(self, guild: discord.Guild, member: discord.Member) -> None:
        """自己破産の直後に、名前の色ロールを外し、【破産者】を付ける。"""
        try:
            async with self._role_lock(guild.id):
                await set_color_role(guild, member, None)
        except (ShopError, discord.HTTPException) as exc:
            logthrottle.warn(("bankrupt-role", guild.id), f"破産後の色ロールを外せません: guild={guild.id}: {exc!r}")
        try:
            await self.sync_status_nick(guild, member)
        except (ShopError, discord.HTTPException) as exc:
            logthrottle.warn(
                ("bankrupt-nick", guild.id), f"破産後のニックネームを更新できません: guild={guild.id}: {exc!r}"
            )

    async def run_status_nick(self) -> None:
        """借金・延滞・破産者の人のニックネームに【地下労働者】などを付け、カタギに戻った人から外す。"""
        for guild in allowed_guilds(self.bot, self._guild_repo, self._config):
            for user_id in self._guild_repo.for_guild(guild.id).status_nick_targets()[:STATUS_NICK_PER_GUILD]:
                member = guild.get_member(user_id)
                if member is None:
                    continue
                try:
                    await self.sync_status_nick(guild, member)
                except ShopError:
                    pass
                except discord.HTTPException as exc:
                    logthrottle.warn(
                        ("status-nick", guild.id), f"身分のニックネームを更新できません: guild={guild.id}: {exc!r}"
                    )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        self._guild_repo.delete_guild_nick_prefs(guild.id)

    @app_commands.command(name="ショップ", description="🛍️ 景品を見る・買う・身につける（ボタンで操作）")
    @app_commands.guild_only()
    async def shop_panel(self, interaction: discord.Interaction):
        view = ShopView(self, interaction.user.id, ui.guild_id_of(interaction))
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


async def setup(bot: "CasinoBot") -> None:
    cog = ShopCog(bot, bot.config, bot.services.shop, bot.repos.guild, bot.sessions)
    bot.bridges.register(IShopTitleBridge, cog)
    await bot.add_cog(cog)
