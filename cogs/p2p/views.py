"""プレイヤー間市場（P2P）の View・Modal・Button。"""

import re
from typing import TYPE_CHECKING, Any, cast

import discord

from cogs.p2p.embeds import line_of
from cogs.shared import ui
from cogs.shared.amount import optional_amount, typed_amount
from cogs.shared.bridges import IP2PListingBridge
from cogs.shared.messages import format_failure
from domain import p2p
from domain import shop as shop_mod
from domain.errors import Failure

if TYPE_CHECKING:
    from cogs.p2p.cog import P2PCog
    from cogs.shared.bot import CasinoBot


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
        bot = cast("CasinoBot", interaction.client)
        bridge = bot.bridges.resolve(IP2PListingBridge)
        if bridge is None:
            return await ui.reply_error(
                interaction, "しばらくしてから、もう一度試してください。", title="いまは使えません"
            )
        await bridge.open_detail(interaction, self.listing_id)


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
        amount = typed_amount(self.amount.value)
        if amount is None:
            await ui.reply_error(interaction, "数字で入れてください。", title="入札できません")
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
        row = self.cog.p2p.get(ui.guild_id_of(interaction), self.listing_id)
        if row is None or row["status"] != "active":
            return await ui.reply_error(interaction, "その出品は、終わりました。", title="もうありません")
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
            ok, value = optional_amount(field.value or "") if field is not None else (True, None)
            if not ok:
                raise ValueError("数字ではない")
            return default if value is None else value

        try:
            price, qty, hours = num(self.price), num(self.qty, 1), num(self.hours)
        except ValueError:
            await ui.reply_error(interaction, "数字で入れてください。", title="出品できません")
            return
        if price is None:
            await ui.reply_error(interaction, "数字で入れてください。", title="出品できません")
            return
        assert self.view.target is not None
        kind, _, ref = self.view.target.partition(":")
        gid = ui.guild_id_of(interaction)
        p2p_svc = self.view.cog.p2p
        ok, result = p2p_svc.list_item(gid, interaction.user.id, kind, ref, qty or 1, self.mode, price, hours)
        if not ok:
            await ui.reply_error(interaction, format_failure(result), title="出品できません")
            return
        assert not isinstance(result, Failure)
        row = p2p_svc.get(gid, result)
        assert row is not None
        self.view.stop()
        posted = await self.view.cog.announce(getattr(interaction, "guild", None), result)  # 告知チャンネルに投稿する
        await ui.reply_ok(
            interaction,
            f"{line_of(row, p2p_svc.label(gid, row))}\n売れる（落札される）と、{p2p.FEE_RATE:.0%} の手数料を引いた額が入ります。"
            "取り消しは、競売の画面の「📋 自分の取引」から。"
            + ("\n📣 告知チャンネルに、出品を投稿しました。" if posted else ""),
            title="出品しました",
        )


class SellView(ui.OwnerView):
    """出品の画面。出す品・馬を選んで、「定価で出す」か「競りに出す」を押す。"""

    def __init__(self, cog: "P2PCog", user_id: int, guild_id: int):
        super().__init__(user_id, timeout=300)
        self.cog, self.user_id = cog, user_id
        self.target: str | None = None
        self.options = []
        for item_id, qty in cog.p2p.sellable_items(guild_id, user_id).items():
            if p2p.tradable(item_id):
                label = f"{shop_mod.label(shop_mod.BY_ID[item_id])}（持っている {qty}）"
                self.options.append(discord.SelectOption(label=label[:100], value=f"item:{item_id}"))
        for horse in cog.p2p.sellable_horses(guild_id, user_id):
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
                return await ui.reply_error(
                    interaction, "出す品・馬を、選択肢から選んでください。", title="先に選んでください"
                )
            await interaction.response.send_modal(SellModal(self, mode))

        return callback


class MineView(ui.OwnerView):
    """自分の取引。出している品（取り消せる）と、入札中の競り。"""

    def __init__(self, cog: "P2PCog", user_id: int, guild_id: int):
        super().__init__(user_id, timeout=300)
        self.cog, self.user_id, self.guild_id = cog, user_id, guild_id
        self.data = cog.p2p.mine(guild_id, user_id)
        cancellable = [r for r in self.data["selling"] if not r["bidder_id"]]  # 入札が入った競りは、取り消せない
        if cancellable:
            select = discord.ui.Select(
                placeholder="取り消す出品を選ぶ",
                options=[
                    discord.SelectOption(label=f"#{r['id']} {cog.p2p.label(guild_id, r)}"[:100], value=str(r["id"]))
                    for r in cancellable[:25]
                ],
            )
            select.callback = self._on_cancel
            self.add_item(select)

    def embed(self) -> discord.Embed:
        p2p_svc = self.cog.p2p
        sell = (
            "\n".join(line_of(r, p2p_svc.label(self.guild_id, r)) for r in self.data["selling"]) or "出していません。"
        )
        bid = (
            "\n".join(line_of(r, p2p_svc.label(self.guild_id, r)) for r in self.data["bidding"])
            or "入札中のものは、ありません。"
        )
        e = ui.embed("📋 自分の取引", "", ui.INFO)
        e.add_field(name="出している（入札が入った競りは、取り消せません）", value=sell[:1024], inline=False)
        e.add_field(name="入札中（あなたが最高入札者）", value=bid[:1024], inline=False)
        return e

    async def _on_cancel(self, interaction: discord.Interaction):
        listing_id = int(ui.selected_values(interaction)[0])
        ok, why = self.cog.p2p.cancel(self.guild_id, self.user_id, listing_id)
        if not ok:
            await ui.reply_error(interaction, format_failure(why), title="取り消せません")
            return
        self.stop()
        await self.cog.refresh_post(getattr(interaction, "guild", None), listing_id)
        await ui.reply_ok(interaction, "出品を取り消して、品・馬を戻しました。", title="取り消しました")
