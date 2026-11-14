"""プレイヤー間の市場と競り。ショップの「🕴️ 帝愛の競売」の画面（cogs/shop_cog.py）に出て、そこから使う。

- 出品: 競売の画面の「📤 出品する」。品・馬を選び、値段・方式を決める。出した品・馬は、出品の間、預かる。
- 買う・入札: 競売の画面の「🤝 プレイヤーの出品」から、出品を選んで、「買う」「入札する」。入札のお金は、残高から預かる（抜かれたら返る）。
- 売れると、売り手の受け取りから 5% を引く（帝愛銀行の収入）。同じ2人の取引は、24時間で合計 10万ペリカまで。
- 締切は、Bot が定期的に確認して精算する（競りの落札は、告知チャンネルに知らせる）。
- コマンドはない（競売の画面に一本化した）。このファイルは、画面の部品・買う／入札の処理・締切の精算を持つ。
"""

import re
from typing import Any

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
from cogs.shared import ui
from cogs.shared.announce import announce_channel
from cogs.shared.cog_ports import IP2PCogPort, get_cog_port
from cogs.shared.confirm import confirm_action, confirmed_reply, user_session
from cogs.shared.messages import format_failure
from domain import approval, p2p
from domain import shop as shop_mod
from infra import catalogs, logthrottle

CONFIRM_FROM = catalogs.economy().p2p.confirm_from  # この金額以上の購入・入札は、確認ボタンを挟む
SHOWN = 25
NONE = discord.AllowedMentions.none()


class ListingButton(discord.ui.DynamicItem[discord.ui.Button], template=r"p2p:open:(?P<id>\d+)"):
    """告知の投稿の「見る・買う・入札」。再起動しても動くよう、出品の番号を custom_id に入れる。"""

    def __init__(self, listing_id: int):
        super().__init__(
            discord.ui.Button(
                label="🔍 見る・買う・入札", style=discord.ButtonStyle.success, custom_id=f"p2p:open:{listing_id}"
            )
        )
        self.listing_id = listing_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        cog = get_cog_port(ui.bot_of(interaction), "P2PCog", IP2PCogPort)
        if cog is None:
            return await interaction.response.send_message(
                embed=ui.error_embed("いまは使えません", "しばらくしてから、もう一度試してください。"), ephemeral=True
            )
        await cog.open_detail(interaction, self.listing_id)


def listing_view(listing_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ListingButton(listing_id))
    return view


class BidModal(discord.ui.Modal):
    def __init__(self, view: "DetailView", need: int):
        super().__init__(title="入札")
        self.view = view
        self.amount = discord.ui.TextInput(label=f"入札額（{need:,}ペリカ以上）", max_length=9, default=str(need))
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.amount.value.replace(",", "").strip())
        except ValueError:
            await interaction.response.send_message(
                embed=ui.error_embed("入札できません", "数字で入れてください。"), ephemeral=True
            )
            return
        await self.view.cog.do_bid(interaction, self.view.listing_id, amount)


class DetailView(ui.OwnerView):
    """出品の詳細。「買う」（定価）か「入札する」（競り）。"""

    def __init__(self, cog: "P2PCog", user_id: int, listing_id: int, mode: str):
        super().__init__(user_id, timeout=300)
        self.cog, self.user_id, self.listing_id = cog, user_id, listing_id
        label = "🛒 買う" if mode == "fixed" else "🔨 入札する"
        go = discord.ui.Button(label=label, style=discord.ButtonStyle.success)
        go.callback = self._on_go
        self.add_item(go)

    async def _on_go(self, interaction: discord.Interaction):
        eco = self.cog.eco_of(interaction)
        row = eco.p2p_get(self.listing_id)
        if row is None or row["status"] != "active":
            return await interaction.response.send_message(
                embed=ui.error_embed("もうありません", "その出品は、終わりました。"), ephemeral=True
            )
        if row["mode"] == "fixed":
            return await self.cog.do_buy(interaction, self.listing_id)
        await interaction.response.send_modal(BidModal(self, p2p.min_next_bid(row["price"], row["bid"])))


class SellModal(discord.ui.Modal):
    """出品の値段などを入れる。方式（定価 / 競り）は、押したボタンで決まっている。"""

    def __init__(self, view: "SellView", mode: str):
        super().__init__(title="定価で出す" if mode == "fixed" else "競りに出す")
        self.view, self.mode = view, mode
        assert view.target is not None
        kind, _, ref = view.target.partition(":")
        lo, hi = p2p.price_range("horse") if kind == "horse" else p2p.price_range("item", ref, 1)
        self.price = discord.ui.TextInput(
            label=("定価" if mode == "fixed" else "開始価格")
            + f"（ペリカ。1個あたりでなく、まとめて。{lo:,}〜{hi:,}）",
            max_length=9,
        )
        self.add_item(self.price)
        self.qty = None
        if kind == "item" and not shop_mod.BY_ID[ref].unique:  # 消耗品・発掘品だけ、個数を選べる
            self.qty = discord.ui.TextInput(label="個数（省略で1）", required=False, max_length=3, default="1")
            self.add_item(self.qty)
        self.hours = None
        if mode == "auction":
            self.hours = discord.ui.TextInput(
                label=f"競りの長さ（時間。{'・'.join(map(str, p2p.AUCTION_HOURS))} のどれか）",
                max_length=3,
                default="24",
            )
            self.add_item(self.hours)

    async def on_submit(self, interaction: discord.Interaction):
        def num(field, default=None):
            text = (field.value or "").replace(",", "").strip() if field is not None else ""
            return int(text) if text else default

        try:
            price, qty, hours = num(self.price), num(self.qty, 1), num(self.hours)
        except ValueError:
            await interaction.response.send_message(
                embed=ui.error_embed("出品できません", "数字で入れてください。"), ephemeral=True
            )
            return
        assert self.view.target is not None
        kind, _, ref = self.view.target.partition(":")
        eco = self.view.cog.eco_of(interaction)
        ok, result = eco.p2p_list(interaction.user.id, kind, ref, qty or 1, self.mode, price, hours)
        if not ok:
            await interaction.response.send_message(
                embed=ui.error_embed("出品できません", format_failure(result)), ephemeral=True
            )
            return
        row = eco.p2p_get(result)
        assert row is not None
        self.view.stop()
        posted = await self.view.cog.announce(getattr(interaction, "guild", None), result)  # 告知チャンネルに投稿する
        await interaction.response.send_message(
            embed=ui.ok_embed(
                "出品しました",
                f"{line_of(row, eco.p2p_label(row))}\n売れる（落札される）と、{p2p.FEE_RATE:.0%} の手数料を引いた額が入ります。"
                "取り消しは、競売の画面の「📋 自分の取引」から。"
                + ("\n📣 告知チャンネルに、出品を投稿しました。" if posted else ""),
            ),
            ephemeral=True,
        )


class SellView(ui.OwnerView):
    """出品の画面。出す品・馬を選んで、「定価で出す」か「競りに出す」を押す。"""

    def __init__(self, cog: "P2PCog", user_id: int, guild_id: int):
        super().__init__(user_id, timeout=300)
        self.cog, self.user_id = cog, user_id
        self.target: str | None = None
        eco = cog.bot.economy.for_guild(guild_id)
        self.options = []
        for item_id, qty in eco.inventory(user_id).items():
            if p2p.tradable(item_id):
                label = f"{shop_mod.label(shop_mod.BY_ID[item_id])}（持っている {qty}）"
                self.options.append(discord.SelectOption(label=label[:100], value=f"item:{item_id}"))
        for horse in eco.horse_list(user_id):
            self.options.append(
                discord.SelectOption(
                    label=f"{horse['name']}（馬）"[:100], value=f"horse:{horse['id']}", emoji=horse["silks"]
                )
            )
        self.options = self.options[:25]
        if self.options:
            select = discord.ui.Select(placeholder="出す品・馬を選ぶ", options=self.options)
            select.callback = self._on_pick
            self.add_item(select)
            for label, mode in (("💴 定価で出す", "fixed"), ("🔨 競りに出す", "auction")):
                button = discord.ui.Button(label=label, style=discord.ButtonStyle.success, row=1)
                button.callback = self._make_go(mode)
                self.add_item(button)

    def embed(self) -> discord.Embed:
        return ui.embed(
            "📤 出品する",
            "出す品・馬を選んで、「定価で出す」（すぐ買える）か「競りに出す」（入札で上がる）を押します。\n"
            f"出した間は、品・馬を預かります（1人 {p2p.MAX_ACTIVE} 件まで）。売れると {p2p.FEE_RATE:.0%} の手数料を引きます。",
            ui.INFO,
        )

    async def _on_pick(self, interaction: discord.Interaction):
        self.target = ui.selected_values(interaction)[0]
        for option in self.options:  # 選んだものを、選択肢に残して見せる
            option.default = option.value == self.target
        await interaction.response.edit_message(view=self)

    def _make_go(self, mode: str):
        async def callback(interaction: discord.Interaction):
            if self.target is None:
                return await interaction.response.send_message(
                    embed=ui.error_embed("先に選んでください", "出す品・馬を、選択肢から選んでください。"),
                    ephemeral=True,
                )
            await interaction.response.send_modal(SellModal(self, mode))

        return callback


class MineView(ui.OwnerView):
    """自分の取引。出している品（取り消せる）と、入札中の競り。"""

    def __init__(self, cog: "P2PCog", user_id: int, guild_id: int):
        super().__init__(user_id, timeout=300)
        self.cog, self.user_id, self.guild_id = cog, user_id, guild_id
        eco = cog.bot.economy.for_guild(guild_id)
        self.data = eco.p2p_mine(user_id)
        cancellable = [r for r in self.data["selling"] if not r["bidder_id"]]  # 入札が入った競りは、取り消せない
        if cancellable:
            select = discord.ui.Select(
                placeholder="取り消す出品を選ぶ",
                options=[
                    discord.SelectOption(label=f"#{r['id']} {eco.p2p_label(r)}"[:100], value=str(r["id"]))
                    for r in cancellable[:25]
                ],
            )
            select.callback = self._on_cancel
            self.add_item(select)

    def embed(self) -> discord.Embed:
        eco = self.cog.bot.economy.for_guild(self.guild_id)
        sell = "\n".join(line_of(r, eco.p2p_label(r)) for r in self.data["selling"]) or "出していません。"
        bid = "\n".join(line_of(r, eco.p2p_label(r)) for r in self.data["bidding"]) or "入札中のものは、ありません。"
        e = ui.embed("📋 自分の取引", "", ui.INFO)
        e.add_field(name="出している（入札が入った競りは、取り消せません）", value=sell[:1024], inline=False)
        e.add_field(name="入札中（あなたが最高入札者）", value=bid[:1024], inline=False)
        return e

    async def _on_cancel(self, interaction: discord.Interaction):
        eco = self.cog.bot.economy.for_guild(self.guild_id)
        listing_id = int(ui.selected_values(interaction)[0])
        ok, why = eco.p2p_cancel(self.user_id, listing_id)
        if not ok:
            await interaction.response.send_message(
                embed=ui.error_embed("取り消せません", format_failure(why)), ephemeral=True
            )
            return
        self.stop()
        await self.cog.refresh_post(getattr(interaction, "guild", None), listing_id)
        await interaction.response.send_message(
            embed=ui.ok_embed("取り消しました", "出品を取り消して、品・馬を戻しました。"), ephemeral=True
        )


class P2PCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_dynamic_items(ListingButton)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    def eco_of(self, interaction: discord.Interaction):
        return self.bot.economy.for_guild(ui.guild_id_of(interaction))

    def _post_args(self, eco, row: dict) -> tuple[str, dict | None]:
        horse = eco.horse_get(int(row["ref"])) if row["kind"] == "horse" else None
        return eco.p2p_label(row), horse

    # ------------------------------ 出品の告知（チャンネルへの投稿） ------------------------------ #
    async def announce(self, guild, listing_id: int) -> bool:
        """出品を、告知チャンネルに投稿する（投稿を覚えて、入札・売れたときに書き換える）。投稿できたら True。失敗しても、出品には影響しない。"""
        if guild is None:
            return False
        eco = self.bot.economy.for_guild(guild.id)
        row = eco.p2p_get(listing_id)
        channel = announce_channel(guild, eco) if row else None
        if row is None or channel is None:
            return False
        try:
            label, horse = self._post_args(eco, row)
            msg = await ui.send_channel(
                channel, embed=post_embed(row, label, horse), view=listing_view(listing_id), allowed_mentions=NONE
            )
        except discord.HTTPException as exc:
            logthrottle.warn(("p2p-announce", guild.id), f"出品の告知に失敗: guild={guild.id}: {exc!r}")
            return False
        eco.p2p_set_post(listing_id, channel.id, msg.id)
        return True

    async def refresh_post(self, guild, listing_id: int) -> None:
        """告知の投稿を、いまの状態（最高入札・売れた・取り消し・終了）に書き換える。終わったら、ボタンを外す。"""
        if guild is None:
            return
        eco = self.bot.economy.for_guild(guild.id)
        row = eco.p2p_get(listing_id)
        if row is None or not row["message_id"]:
            return
        ended = row["status"] != "active"
        channel = guild.get_channel(row["channel_id"])
        if channel is not None:
            try:
                label, horse = self._post_args(eco, row)
                kwargs: dict[str, Any] = {"embed": post_embed(row, label, horse)}
                if ended:
                    kwargs["view"] = None
                await ui.partial_message(channel, row["message_id"]).edit(**kwargs)
            except discord.HTTPException as exc:  # 消されていても、取引には関係ない
                logthrottle.warn(
                    ("p2p-refresh", guild.id), f"出品の告知を書き換えられません: guild={guild.id}: {exc!r}"
                )
        if ended:
            eco.p2p_mark_post_done(
                listing_id
            )  # 終わった出品は、書き換えを1回で済ませる（チャンネルや投稿がなくても、くり返さない）

    # ------------------------------ 買う・入札 ------------------------------ #
    async def do_buy(self, interaction: discord.Interaction, listing_id: int) -> None:
        eco = self.eco_of(interaction)
        row = eco.p2p_get(listing_id)
        if row is None or row["status"] != "active":
            await interaction.response.send_message(
                embed=ui.error_embed("買えません", "その出品は、もうありません。"), ephemeral=True
            )
            return
        async with user_session(self.bot, interaction, "いまは買えません") as ok:
            if not ok:
                return
            uid = interaction.user.id
            reply = await confirmed_reply(
                interaction,
                row["price"] >= CONFIRM_FROM,
                "購入しますか？",
                f"**{eco.p2p_label(row)}**\n{ui.pelica(row['price'])} かかります。",
                "買う",
                confirm_fn=confirm_action,
            )
            if reply is None:
                return
            done, result = eco.p2p_buy(uid, listing_id)
            if not done:
                await reply(embed=ui.error_embed("買えませんでした", format_failure(result)), ephemeral=True)
                return
            await reply(
                embed=ui.ok_embed(
                    "購入しました",
                    f"**{eco.p2p_label(row)}** を {ui.pelica(result)} で買いました。\n残高: **{ui.pelica(eco.balance(uid))}**",
                ),
                ephemeral=True,
            )
            await self.refresh_post(getattr(interaction, "guild", None), listing_id)  # 告知を「売れました」に

    async def do_bid(self, interaction: discord.Interaction, listing_id: int, amount: int) -> None:
        eco = self.eco_of(interaction)
        row = eco.p2p_get(listing_id)
        if row is None or row["status"] != "active":
            await interaction.response.send_message(
                embed=ui.error_embed("入札できません", "その出品は、もうありません。"), ephemeral=True
            )
            return
        async with user_session(self.bot, interaction, "いまは入札できません") as ok:
            if not ok:
                return
            uid = interaction.user.id
            reply = await confirmed_reply(
                interaction,
                amount >= CONFIRM_FROM,
                "入札しますか？",
                f"**{eco.p2p_label(row)}**\n{ui.pelica(amount)} を、残高から預かります（抜かれたら返ります）。",
                "入札する",
                confirm_fn=confirm_action,
            )
            if reply is None:
                return
            done, result = eco.p2p_bid(uid, listing_id, amount)
            if not done:
                await reply(embed=ui.error_embed("入札できませんでした", format_failure(result)), ephemeral=True)
                return
            await reply(
                embed=ui.ok_embed(
                    "入札しました",
                    f"**{eco.p2p_label(row)}** に **{ui.pelica(amount)}** で入札しました。締切 {when(result)}\n残高: **{ui.pelica(eco.balance(uid))}**",
                ),
                ephemeral=True,
            )
            await self.refresh_post(getattr(interaction, "guild", None), listing_id)  # 告知の最高入札・締切を更新

    # ------------------------------ 画面（競売の画面から開く） ------------------------------ #
    async def open_detail(self, interaction: discord.Interaction, listing_id: int) -> None:
        """出品の詳細（「買う」「入札する」）を、本人にだけ見せる。"""
        eco = self.eco_of(interaction)
        row = eco.p2p_get(listing_id)
        if row is None or row["status"] != "active":
            await interaction.response.send_message(
                embed=ui.error_embed("もうありません", "その出品は、終わりました。"), ephemeral=True
            )
            return
        view = DetailView(self, interaction.user.id, row["id"], row["mode"])
        if row["seller_id"] == interaction.user.id:
            view.clear_items()  # 自分の出品は、買えない
        label, horse = self._post_args(eco, row)
        await interaction.response.send_message(embed=detail_embed(row, label, horse), view=view, ephemeral=True)

    async def open_sell(self, interaction: discord.Interaction) -> None:
        """出品の画面（品・馬を選んで、定価か競りで出す）。"""
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
        for guild in list(self.bot.guilds):
            if not approval.is_allowed(self.bot.economy, self.bot.config, guild.id, 0):
                continue
            try:
                await self._settle_guild(guild)
            except Exception as exc:  # 1サーバーの失敗で、ほかを止めない
                print(f"取引の精算に失敗: guild={guild.id}: {exc!r}")

    @tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()

    async def _settle_guild(self, guild: discord.Guild) -> None:
        eco = self.bot.economy.for_guild(guild.id)
        results = eco.p2p_settle()
        for row, _res in results:
            await self.refresh_post(guild, row["id"])  # 告知を、売れた・終了に書き換える
        for row in eco.p2p_unsynced_posts():  # 取引停止（破産猶予・破産）などで、取り下げられた出品の告知も、書き換える
            await self.refresh_post(guild, row["id"])
        channel = (
            announce_channel(guild, eco)
            if any(res == "sold" and r["mode"] == "auction" for r, res in results)
            else None
        )
        for row, res in results:
            if res != "sold" or row["mode"] != "auction" or channel is None:
                continue  # 落札だけ、告知チャンネルに知らせる
            done = eco.p2p_get(row["id"])
            if done is None:
                continue
            try:
                await ui.send_channel(
                    channel,
                    embed=ui.embed(
                        "🔨 競りの落札",
                        f"**{eco.p2p_label(row)}** を、<@{done['buyer_id']}> が **{ui.pelica(done['sold_price'])}** で落札しました（出品者 <@{row['seller_id']}>）。",
                        ui.GOLD,
                    ),
                    allowed_mentions=NONE,
                )
            except discord.HTTPException as exc:
                logthrottle.warn(("p2p-post", guild.id), f"落札の告知に失敗: guild={guild.id}: {exc!r}")


async def setup(bot):
    await bot.add_cog(P2PCog(bot))
