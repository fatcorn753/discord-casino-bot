"""プレイヤー間の市場と競り。ショップの「🕴️ 帝愛の競売」（cogs/shop/cog.py）に出て、そこから使う。

- 出品: 競売の画面の「📤 出品する」。品・馬を選び、値段・方式を決める。出した品・馬は、出品の間、預かる。
- 買う・入札: 競売の画面の「🤝 プレイヤーの出品」から、出品を選んで、「買う」「入札する」。入札のお金は、残高から預かる（抜かれたら返る）。
- 売れると、売り手の受け取りから 5% を引く（帝愛銀行の収入）。同じ2人の取引は、24時間で合計 10万ペリカまで。
- 締切は、Bot が定期的に確認して精算する（競りの落札は、告知チャンネルに知らせる）。
- コマンドはない（競売の画面に一本化した）。このファイルは、画面の部品・買う／入札の処理・締切の精算を持つ。
"""

from typing import TYPE_CHECKING, Any, Optional

import discord
from discord.ext import commands, tasks

from cogs.p2p.embeds import (  # noqa: F401
    ENDED_TEXT,
    detail_embed,
    entry_text,
    line_of,
    post_embed,
    price_text,
    when,
)
from cogs.p2p.views import (  # noqa: F401
    BidModal,
    DetailView,
    ListingButton,
    MineView,
    SellModal,
    SellView,
    listing_view,
)
from cogs.shared import approval_guard, logthrottle, ui
from cogs.shared.announce import announce_channel
from cogs.shared.bridges import BotBridges, IP2PListingBridge, IP2PNavigationBridge
from cogs.shared.confirm import confirm_action, confirmed_reply, user_session
from cogs.shared.messages import format_failure
from cogs.shared.tasks import run_for_allowed_guilds
from domain.ports import get_catalogs
from domain.ports.repos import IGuildRepo
from domain.ports.session import ISessionGuard
from services.p2p import P2PService

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot

CONFIRM_FROM = get_catalogs().economy().p2p.confirm_from  # この金額以上の購入・入札は、確認ボタンを挟む
SHOWN = 25
NONE = discord.AllowedMentions.none()


class P2PCog(commands.Cog, IP2PNavigationBridge):
    def __init__(
        self,
        bot: commands.Bot,
        config: dict[str, Any],
        p2p_svc: P2PService,
        guild_repo: IGuildRepo,
        sessions: ISessionGuard,
    ) -> None:
        self.bot = bot
        self._config = config
        self.p2p = p2p_svc
        self._guild_repo = guild_repo
        self._sessions = sessions

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(ListingButton)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        bridges = getattr(self.bot, "bridges", None)
        if isinstance(bridges, BotBridges):
            bridges.unregister(IP2PListingBridge, self)
            bridges.unregister(IP2PNavigationBridge, self)

    def _post_args(self, guild_id: int, row: dict[str, Any]) -> tuple[str, Optional[dict[str, Any]]]:
        return self.p2p.label(guild_id, row), self.p2p.horse_of(guild_id, row)

    # ------------------------------ 出品の告知（チャンネルへの投稿） ------------------------------ #
    async def announce(self, guild: Optional[discord.Guild], listing_id: int) -> bool:
        """出品を、告知チャンネルに投稿する（投稿を覚えて、入札・売れたときに書き換える）。投稿できたら True。失敗しても、出品には影響しない。"""
        if guild is None:
            return False
        row = self.p2p.get(guild.id, listing_id)
        channel = announce_channel(guild, self._guild_repo) if row else None
        if row is None or channel is None:
            return False
        try:
            label, horse = self._post_args(guild.id, row)
            msg = await ui.send_channel(
                channel, embed=post_embed(row, label, horse), view=listing_view(listing_id), allowed_mentions=NONE
            )
        except discord.HTTPException as exc:
            logthrottle.warn(("p2p-announce", guild.id), f"出品の告知に失敗: guild={guild.id}: {exc!r}")
            return False
        self.p2p.set_post(guild.id, listing_id, channel.id, msg.id)
        return True

    async def refresh_post(self, guild: Optional[discord.Guild], listing_id: int) -> None:
        """告知の投稿を、いまの状態（最高入札・売れた・取り消し・終了）に書き換える。終わったら、ボタンを外す。"""
        if guild is None:
            return
        row = self.p2p.get(guild.id, listing_id)
        if row is None or not row["message_id"]:
            return
        ended = row["status"] != "active"
        channel = guild.get_channel(row["channel_id"])
        if channel is not None:
            try:
                label, horse = self._post_args(guild.id, row)
                kwargs: dict[str, Any] = {"embed": post_embed(row, label, horse)}
                if ended:
                    kwargs["view"] = None
                msg = ui.partial_message(channel, row["message_id"])
                if msg is not None:
                    await msg.edit(**kwargs)
            except discord.HTTPException as exc:  # 消されていても、取引には関係ない
                logthrottle.warn(
                    ("p2p-refresh", guild.id), f"出品の告知を書き換えられません: guild={guild.id}: {exc!r}"
                )
        if ended:
            self.p2p.mark_post_done(
                guild.id, listing_id
            )  # 終わった出品は、書き換えを1回で済ませる（チャンネルや投稿がなくても、くり返さない）

    # ------------------------------ 買う・入札 ------------------------------ #
    async def _trade_listing(
        self,
        interaction: discord.Interaction,
        listing_id: int,
        *,
        fail_title: str,
        busy_title: str,
        confirm_needed: bool,
        confirm_title: str,
        confirm_desc: str,
        confirm_label: str,
        error_title: str,
        ok_title: str,
        execute,
        ok_body,
    ) -> None:
        gid = ui.guild_id_of(interaction)
        row = self.p2p.get(gid, listing_id)
        if row is None or row["status"] != "active":
            await interaction.response.send_message(
                embed=ui.error_embed(fail_title, "その出品は、もうありません。"), ephemeral=True
            )
            return
        async with user_session(self._sessions, interaction, busy_title) as ok:
            if not ok:
                return
            uid = interaction.user.id
            label = self.p2p.label(gid, row)
            reply = await confirmed_reply(
                interaction,
                confirm_needed,
                confirm_title,
                f"**{label}**\n{confirm_desc}",
                confirm_label,
                confirm_fn=confirm_action,
            )
            if reply is None:
                return
            done, result = execute(gid, uid)
            if not done:
                await reply(embed=ui.error_embed(error_title, format_failure(result)), ephemeral=True)
                return
            balance = self.p2p.balance_of(gid, uid)
            await reply(
                embed=ui.ok_embed(
                    ok_title,
                    f"{ok_body(label, result)}\n残高: **{ui.pelica(balance)}**",
                ),
                ephemeral=True,
            )
            await self.refresh_post(getattr(interaction, "guild", None), listing_id)

    async def do_buy(self, interaction: discord.Interaction, listing_id: int) -> None:
        if not await approval_guard.client_feature_guard(interaction, "p2p"):
            return
        row = self.p2p.get(ui.guild_id_of(interaction), listing_id)
        price = row["price"] if row else 0
        await self._trade_listing(
            interaction,
            listing_id,
            fail_title="買えません",
            busy_title="いまは買えません",
            confirm_needed=price >= CONFIRM_FROM,
            confirm_title="購入しますか？",
            confirm_desc=f"{ui.pelica(price)} かかります。",
            confirm_label="買う",
            error_title="買えませんでした",
            ok_title="購入しました",
            execute=lambda gid, uid: self.p2p.buy(gid, uid, listing_id),
            ok_body=lambda label, result: f"**{label}** を {ui.pelica(result)} で買いました。",
        )

    async def do_bid(self, interaction: discord.Interaction, listing_id: int, amount: int) -> None:
        if not await approval_guard.client_feature_guard(interaction, "p2p"):
            return
        await self._trade_listing(
            interaction,
            listing_id,
            fail_title="入札できません",
            busy_title="いまは入札できません",
            confirm_needed=amount >= CONFIRM_FROM,
            confirm_title="入札しますか？",
            confirm_desc=f"{ui.pelica(amount)} を、残高から預かります（抜かれたら返ります）。",
            confirm_label="入札する",
            error_title="入札できませんでした",
            ok_title="入札しました",
            execute=lambda gid, uid: self.p2p.bid(gid, uid, listing_id, amount),
            ok_body=lambda label, result: f"**{label}** に **{ui.pelica(amount)}** で入札しました。締切 {when(result)}",
        )

    # ------------------------------ 画面（競売の画面から開く） ------------------------------ #
    async def open_detail(self, interaction: discord.Interaction, listing_id: int) -> None:
        """出品の詳細（「買う」「入札する」）を、本人にだけ見せる。"""
        gid = ui.guild_id_of(interaction)
        row = self.p2p.get(gid, listing_id)
        if row is None or row["status"] != "active":
            await interaction.response.send_message(
                embed=ui.error_embed("もうありません", "その出品は、終わりました。"), ephemeral=True
            )
            return
        view = DetailView(self, interaction.user.id, row["id"], row["mode"])
        if row["seller_id"] == interaction.user.id:
            view.clear_items()  # 自分の出品は、買えない
        label, horse = self._post_args(gid, row)
        await interaction.response.send_message(embed=detail_embed(row, label, horse), view=view, ephemeral=True)

    async def open_sell(self, interaction: discord.Interaction) -> None:
        """出品の画面（品・馬を選んで、定価か競りで出す）。"""
        if not await approval_guard.client_feature_guard(interaction, "p2p"):
            return

        view = SellView(self, interaction.user.id, ui.guild_id_of(interaction))
        if not view.options:
            await interaction.response.send_message(
                embed=ui.error_embed(
                    "出せるものがありません", "売れる品（消耗品・カードの色など・発掘品）か、馬を、持っていません。"
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    async def open_mine(self, interaction: discord.Interaction) -> None:
        """自分の出品と入札中のものを見せる（出品は、ここから取り消せる）。"""
        view = MineView(self, interaction.user.id, ui.guild_id_of(interaction))
        if view.children:
            await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)
        else:
            await interaction.response.send_message(embed=view.embed(), ephemeral=True)

    # ------------------------------ 締切の精算 ------------------------------ #
    @tasks.loop(seconds=30)
    async def tick(self):
        await run_for_allowed_guilds(
            self.bot,
            self._guild_repo,
            self._config,
            self._settle_guild,
            error_label="取引の精算に失敗",
        )

    @tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()

    async def _settle_guild(self, guild: discord.Guild) -> None:
        results = self.p2p.settle(guild.id)
        for row, _res in results:
            await self.refresh_post(guild, row["id"])  # 告知を、売れた・終了に書き換える
        for row in self.p2p.unsynced_posts(
            guild.id
        ):  # 取引停止（破産猶予・破産）などで、取り下げられた出品の告知も、書き換える
            await self.refresh_post(guild, row["id"])
        channel = (
            announce_channel(guild, self._guild_repo)
            if any(res == "sold" and r["mode"] == "auction" for r, res in results)
            else None
        )
        for row, res in results:
            if res != "sold" or row["mode"] != "auction" or channel is None:
                continue  # 落札だけ、告知チャンネルに知らせる
            done = self.p2p.get(guild.id, row["id"])
            if done is None:
                continue
            try:
                await ui.send_channel(
                    channel,
                    embed=ui.embed(
                        "🔨 競りの落札",
                        f"**{self.p2p.label(guild.id, row)}** を、<@{done['buyer_id']}> が **{ui.pelica(done['sold_price'])}** で落札しました（出品者 <@{row['seller_id']}>）。",
                        ui.GOLD,
                    ),
                    allowed_mentions=NONE,
                )
            except discord.HTTPException as exc:
                logthrottle.warn(("p2p-post", guild.id), f"落札の告知に失敗: guild={guild.id}: {exc!r}")


async def setup(bot: "CasinoBot") -> None:
    cog = P2PCog(
        bot,
        bot.config,
        bot.services.p2p,
        bot.repos.guild,
        bot.sessions,
    )
    bot.bridges.register(IP2PListingBridge, cog)
    bot.bridges.register(IP2PNavigationBridge, cog)
    await bot.add_cog(cog)
