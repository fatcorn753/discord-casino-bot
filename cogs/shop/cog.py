"""/ショップ — ペリカで称号・カード・消耗品・名前の色を交換する。景品はすべてゲーム内・サーバー内で完結する。

`/ショップ` ひとつで、操作パネル（選択肢とボタン）を開く。購入・装備・解除の実処理は actions.py へ分離。
"""

import asyncio

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.shared import ui
from cogs.shared.confirm import confirm_action as confirm_action  # noqa: F401
from cogs.shop import actions
from cogs.shop.discord_ops import ShopError, apply_title_nick, set_color_role
from cogs.shop.embeds import (  # noqa: F401
    EFFECT_OF_ITEM,
    MAX_QTY,
    P2P_FILTERS,
    SECTION_NAME,
    SHOWN,
    SLOT_OF_KIND,
    TAB_NOTE,
    TABS,
    TIERS,
    UNEQUIP_NAMES,
    auction_embed,
    catalog_embed,
    inventory_embed,
    item_line,
    kind_name,
    shop_slots,
    stock_text,
    tab_items,
)
from cogs.shop.views import ShopView
from domain import approval, shop, teiai
from infra import catalogs, logthrottle

_SHOP_CFG = catalogs.shop()
CONFIRM_FROM = _SHOP_CFG.confirm_from  # この金額以上の購入は、確認ボタンを挟む
STATUS_NICK_MINUTES = _SHOP_CFG.status_nick_minutes  # 身分のニックネームを付け外しする確認の間隔
STATUS_NICK_PER_GUILD = _SHOP_CFG.status_nick_per_guild  # 1回の確認で、1サーバーあたり扱う人数の上限


class ShopCog(commands.Cog):
    CONFIRM_FROM = CONFIRM_FROM

    def __init__(self, bot):
        self.bot = bot
        self._role_locks = {}  # サーバーごとのロール操作の鍵（同名ロールの二重作成を防ぐ）
        self._nick_locks = {}  # 利用者ごとのニックネーム操作の鍵（連打で古い名前を元に計算しない）

    def _role_lock(self, guild_id: int) -> asyncio.Lock:
        return self._role_locks.setdefault(guild_id, asyncio.Lock())

    def g(self, interaction: discord.Interaction):
        """このサーバーのペリカの記録（ペリカはサーバーごとに別々）。"""
        return self.bot.economy.for_guild(ui.guild_id_of(interaction))

    def _equipped_title(self, guild_id: int, user_id: int) -> str | None:
        """そのサーバーで装備している称号（景品はサーバーごとに別々）。"""
        item = shop.BY_ID.get(self.bot.economy.for_guild(guild_id).get_profile(user_id)["title"])
        return item.name if item else None

    def _nick_tag(self, guild_id: int, user_id: int) -> str | None:
        """ニックネームの末尾に付ける名前。"""
        eco = self.bot.economy.for_guild(guild_id)
        status = eco.status(user_id)
        if status != teiai.KATAGI:
            return teiai.STATUS_NAME[status]
        if eco.nick_pref(guild_id, user_id):
            return self._equipped_title(guild_id, user_id)  # 自分で選んだ称号の表示を、富豪より優先する
        return teiai.STATUS_NAME[teiai.WEALTHY] if eco.display_status(user_id) == teiai.WEALTHY else None

    async def sync_status_nick(self, guild: discord.Guild, member: discord.Member) -> None:
        """身分に合わせて、ニックネームの末尾の【地下労働者】などを付ける・外す。"""
        eco = self.bot.economy.for_guild(guild.id)
        tag = self._nick_tag(guild.id, member.id)
        if tag is None and not eco.nick_managed(guild.id, member.id):
            return
        if tag in teiai.STATUS_NAME.values():
            eco.ensure_nick_row(guild.id, member.id)
        async with self._nick_locks.setdefault(member.id, asyncio.Lock()):
            await apply_title_nick(guild, member, self._nick_tag(guild.id, member.id), eco)

    async def sync_title_nick(
        self, user_id: int, here: discord.Guild | None = None, member: discord.Member | None = None
    ) -> tuple[str, int]:
        return await self._sync_title_nick(user_id, here, member)

    async def _sync_title_nick(
        self, user_id: int, here: discord.Guild | None = None, member: discord.Member | None = None
    ) -> tuple[str, int]:
        """称号の変更を、ニックネーム表示をONにしているサーバーへ反映する。"""
        async with self._nick_locks.setdefault(user_id, asyncio.Lock()):
            problem, others_failed = "", 0
            for gid in self.bot.economy.nick_pref_guilds(user_id):
                guild = self.bot.get_guild(gid)
                if guild is None or (here is not None and gid != here.id):
                    continue
                title = self._nick_tag(gid, user_id)
                is_here = here is not None and gid == here.id
                try:
                    m = (
                        member
                        if is_here and member is not None
                        else (guild.get_member(user_id) or await guild.fetch_member(user_id))
                    )
                    await apply_title_nick(guild, m, title, self.bot.economy)
                except discord.NotFound:
                    continue
                except ShopError as exc:
                    if is_here:
                        problem = str(exc)
                    else:
                        others_failed += 1
                except discord.HTTPException as exc:
                    print(f"称号のニックネーム反映に失敗: guild={gid} user={user_id}: {exc!r}")
                    if is_here:
                        problem = "Discord 側のエラーで失敗しました。"
                    else:
                        others_failed += 1
            return problem, others_failed

    async def cog_load(self):
        self.status_nick_tick.start()

    async def cog_unload(self) -> None:
        self.status_nick_tick.cancel()

    @tasks.loop(minutes=STATUS_NICK_MINUTES)
    async def status_nick_tick(self):
        try:
            await self.run_status_nick()
        except Exception as exc:
            print(f"身分のニックネームの更新に失敗: {exc!r}")

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
        for guild in list(self.bot.guilds):
            if not approval.is_allowed(self.bot.economy, self.bot.config, guild.id, 0):
                continue
            eco = self.bot.economy.for_guild(guild.id)
            for user_id in eco.status_nick_targets()[:STATUS_NICK_PER_GUILD]:
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
        self.bot.economy.delete_guild_nick_prefs(guild.id)

    @app_commands.command(name="ショップ", description="🛍️ 景品を見る・買う・身につける（ボタンで操作）")
    @app_commands.guild_only()
    async def shop_panel(self, interaction: discord.Interaction):
        view = ShopView(self, interaction.user.id, ui.guild_id_of(interaction))
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    def _catalog_embed(self, guild_id: int, user_id: int, tab: str = "home") -> discord.Embed:
        eco = self.bot.economy.for_guild(guild_id)
        inv = eco.inventory(user_id)
        me = eco.peek_stats(user_id)
        balance = me["balance"] if me else 0
        waiting = len(eco.auction_list()) + len(eco.auction_horses()) if tab == "home" else 0
        stocks = (
            {i.id: eco.shop_stock(user_id, i.id) for i in tab_items(tab) if i.id in shop.STOCK_RULES}
            if tab != "home"
            else None
        )
        return catalog_embed(inv, balance, tab=tab, waiting=waiting, stocks=stocks)

    def _auction_embed(
        self, guild_id: int, user_id: int, tab: str = "teiai", p2p_filter: str = "all", p2p_order: str = "end"
    ) -> discord.Embed:
        eco = self.bot.economy.for_guild(guild_id)
        me = eco.peek_stats(user_id)
        balance = me["balance"] if me else 0
        if tab == "players":
            _label, kind, mode = P2P_FILTERS[p2p_filter]
            rows = eco.p2p_active(kind, limit=SHOWN, mode=mode, order=p2p_order)
            p2p_rows = [(r, eco.p2p_label(r)) for r in rows]
            return auction_embed(
                balance,
                tab=tab,
                p2p_filter=p2p_filter,
                p2p_order=p2p_order,
                p2p_rows=p2p_rows,
            )
        return auction_embed(
            balance,
            tab=tab,
            p2p_filter=p2p_filter,
            p2p_order=p2p_order,
            listed=eco.auction_list(),
            horses=eco.auction_horses(),
        )

    def _inventory_embed(self, guild_id: int, user_id: int) -> discord.Embed:
        eco = self.bot.economy.for_guild(guild_id)
        return inventory_embed(eco.inventory(user_id), eco.get_profile(user_id))

    async def buy_auction_horse(self, interaction: discord.Interaction, horse_id: int):
        await actions.buy_auction_horse(self, interaction, horse_id, confirm_fn=confirm_action)

    async def buy_auction(self, interaction: discord.Interaction, item_id: str, qty: int = 1):
        await actions.buy_auction(self, interaction, item_id, qty, confirm_fn=confirm_action)

    async def buy_item(self, interaction: discord.Interaction, item: str, qty: int = 1):
        await actions.buy_item(self, interaction, item, qty, confirm_fn=confirm_action)

    async def _buy_insider_memo(self, interaction: discord.Interaction, it) -> None:
        await actions.buy_insider_memo(self, interaction, it, confirm_fn=confirm_action)

    async def use_item(self, interaction: discord.Interaction, item: str):
        await actions.use_item(self, interaction, item)

    async def unequip_item(self, interaction: discord.Interaction, slot: str):
        await actions.unequip_item(self, interaction, slot)

    async def set_title_display(self, interaction: discord.Interaction, mode: str):
        await actions.set_title_display(self, interaction, mode)


async def setup(bot):
    await bot.add_cog(ShopCog(bot))
