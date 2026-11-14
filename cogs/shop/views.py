"""`/ショップ` の操作パネル（View）。"""

from typing import TYPE_CHECKING

import discord

from cogs.shared import ui
from cogs.shared.cog_ports import IP2PCogPort, get_cog_port
from cogs.shop.embeds import (
    MAX_QTY,
    P2P_FILTERS,
    P2P_ORDERS,
    SHOWN,
    TABS,
    UNEQUIP_NAMES,
    tab_items,
)
from domain import shop

if TYPE_CHECKING:
    from cogs.shop.cog import ShopCog


class ShopView(ui.OwnerView):
    """`/ショップ` の操作パネル。「ショップ」と「持ち物」の2つの画面を、同じメッセージの中で切り替える。

    見る画面は、同じメッセージを書き換える。買う・使う・外す・称号の表示は、既存の処理を呼び出し、
    結果は別のメッセージ（自分だけに見える）で返す。パネルを開いた本人だけが操作できる。
    """

    P2P_FILTERS = P2P_FILTERS
    P2P_ORDERS = P2P_ORDERS

    def __init__(self, cog: "ShopCog", user_id: int, guild_id: int, screen: str = "shop", tab: str = "home"):
        super().__init__(user_id, timeout=600)
        self.cog, self.user_id, self.guild_id, self.screen, self.tab, self.qty = (
            cog,
            user_id,
            guild_id,
            screen,
            tab,
            1,
        )
        self.picked: tuple[str, str] | None = (
            None  # 選んだ景品 (種類, 値)。「🛒 購入」を押すまで、買わない。種類は shop / auction / horse
        )
        self.auction_tab = None  # 競売の画面のタブ（players / teiai）。None は、開いたときに決める
        self.p2p_filter, self.p2p_order = "all", "end"  # プレイヤーの出品の、絞り込みと並び順
        self._build()

    # ---- 見た目 ---- #
    def embed(self) -> discord.Embed:
        if self.screen == "inv":
            return self.cog._inventory_embed(self.guild_id, self.user_id)
        if self.screen == "auction":
            return self.cog._auction_embed(
                self.guild_id,
                self.user_id,
                tab=self.auction_tab or self._default_auction_tab(),
                p2p_filter=self.p2p_filter,
                p2p_order=self.p2p_order,
            )
        return self.cog._catalog_embed(self.guild_id, self.user_id, self.tab)

    def _build(self) -> None:
        self.clear_items()
        if self.screen == "inv":
            self._build_inventory()
        elif self.screen == "auction":
            self._build_auction()
        else:
            self._build_shop()

    def _build_shop(self) -> None:
        """上の2段が売り場のボタン（いまの売り場は青）。その下に、買う景品の選択肢（トップでは出ない）。"""
        for tab, row in (
            ("home", 0),
            ("title", 0),
            ("card", 0),
            ("item", 0),
            ("style", 0),
            ("collection", 1),
            ("horse", 1),
        ):
            style = discord.ButtonStyle.primary if tab == self.tab else discord.ButtonStyle.secondary
            btn = discord.ui.Button(label=TABS[tab][0], style=style, row=row)
            btn.callback = self._make_tab(tab)
            self.add_item(btn)
        auction = discord.ui.Button(label="🕴️ 帝愛の競売", style=discord.ButtonStyle.danger, row=1)
        auction.callback = self._to_auction
        self.add_item(auction)
        go = discord.ui.Button(label="🎒 持ち物", style=discord.ButtonStyle.success, row=1)
        go.callback = self._to_inventory
        self.add_item(go)
        items = tab_items(self.tab) if self.tab != "home" else []
        if items:
            eco = self.cog.bot.economy.for_guild(self.guild_id)
            inv = eco.inventory(self.user_id)
            stocks = {i.id: eco.shop_stock(self.user_id, i.id) for i in items if i.id in shop.STOCK_RULES}
            mixed = len(TABS[self.tab][1]) > 1
            buy = discord.ui.Select(
                placeholder="買う景品を選ぶ（安い順）",
                row=2,
                options=[
                    discord.SelectOption(
                        label=("✅ " if i.unique and inv.get(i.id, 0) > 0 else "")
                        + (shop.label(i) if mixed else i.name)[:90],
                        value=i.id,
                        default=self.picked == ("shop", i.id),
                        description=f"{i.price:,}ペリカ"
                        + (f"　在庫 {stocks[i.id]['stock']}/{stocks[i.id]['max']}" if stocks.get(i.id) else "")
                        + (f"　{i.desc}" if i.desc else "")[:60],
                        emoji=i.mark or None,
                    )
                    for i in items[:25]
                ],
            )
            buy.callback = self._on_buy
            self.add_item(buy)
            self._add_buy_button()
        if self.tab in ("item", "horse"):
            qty = discord.ui.Select(
                placeholder=f"個数（いま {self.qty} 個）",
                row=3,
                options=[
                    discord.SelectOption(label=f"{n}個", value=str(n), default=n == self.qty)
                    for n in range(1, MAX_QTY + 1)
                ],
            )
            qty.callback = self._on_qty
            self.add_item(qty)

    def _p2p(self) -> IP2PCogPort | None:
        """プレイヤー間の取引ポート。読み込まれていなければ None（画面に出さない）。"""
        return get_cog_port(self.cog.bot, "P2PCog", IP2PCogPort)

    async def _on_player(self, interaction: discord.Interaction):
        p2p = self._p2p()
        if p2p is not None:
            await p2p.open_detail(interaction, int(ui.selected_values(interaction)[0]))

    async def _on_sell(self, interaction: discord.Interaction):
        p2p = self._p2p()
        if p2p is not None:
            await p2p.open_sell(interaction)

    async def _on_mine(self, interaction: discord.Interaction):
        p2p = self._p2p()
        if p2p is not None:
            await p2p.open_mine(interaction)

    def _add_buy_button(self) -> None:
        """選んだ景品を買うボタン。何も選んでいないうちは押せない。"""
        btn = discord.ui.Button(
            label="🛒 購入" if self.picked is None else f"🛒 購入（{self._picked_name()}）"[:80],
            style=discord.ButtonStyle.success,
            disabled=self.picked is None,
            row=4,
        )
        btn.callback = self._on_purchase
        self.add_item(btn)

    def _picked_name(self) -> str:
        assert self.picked is not None
        kind, value = self.picked
        if kind == "horse":
            horse = next(
                (h for h in self.cog.bot.economy.for_guild(self.guild_id).auction_horses() if str(h["id"]) == value),
                None,
            )
            return horse["name"] if horse else "競走馬"
        item = shop.BY_ID.get(value)
        return item.name if item else "景品"

    def _make_tab(self, tab: str):
        async def callback(interaction: discord.Interaction):
            self.tab, self.qty, self.picked = tab, 1, None
            await self._redraw(interaction)

        return callback

    def _default_auction_tab(self) -> str:
        """競売の画面を開いたときのタブ。プレイヤーの出品があれば、そちら。なければ、帝愛の棚。"""
        eco = self.cog.bot.economy.for_guild(self.guild_id)
        return "players" if self._p2p() is not None and eco.p2p_count() else "teiai"

    def _build_auction(self) -> None:
        if self.auction_tab is None:
            self.auction_tab = self._default_auction_tab()
        eco = self.cog.bot.economy.for_guild(self.guild_id)
        p2p_cog = self._p2p()
        # 上の段: タブ（いまのタブは青）と、出品・自分の取引・ショップへもどる
        if p2p_cog is not None:
            for tab, label in (
                ("players", f"🤝 プレイヤーの出品（{eco.p2p_count()}）"),
                ("teiai", f"🕴️ 帝愛の棚（{len(eco.auction_list()) + len(eco.auction_horses())}）"),
            ):
                btn = discord.ui.Button(
                    label=label,
                    style=discord.ButtonStyle.primary if tab == self.auction_tab else discord.ButtonStyle.secondary,
                    row=0,
                )
                btn.callback = self._make_auction_tab(tab)
                self.add_item(btn)
            sell = discord.ui.Button(label="📤 出品する", style=discord.ButtonStyle.success, row=0)
            sell.callback = self._on_sell
            self.add_item(sell)
            mine = discord.ui.Button(label="📋 自分の取引", style=discord.ButtonStyle.secondary, row=0)
            mine.callback = self._on_mine
            self.add_item(mine)
        else:
            self.auction_tab = "teiai"
        back = discord.ui.Button(label="🛍️ ショップ", style=discord.ButtonStyle.secondary, row=0)
        back.callback = self._to_shop
        self.add_item(back)
        if self.auction_tab == "players":
            self._build_auction_players(eco)
        else:
            self._build_auction_teiai(eco)

    def _make_auction_tab(self, tab: str):
        async def callback(interaction: discord.Interaction):
            self.auction_tab, self.picked = tab, None
            await self._redraw(interaction)

        return callback

    def _p2p_rows(self, eco) -> list:
        _label, kind, mode = self.P2P_FILTERS[self.p2p_filter]
        return eco.p2p_active(kind, limit=SHOWN, mode=mode, order=self.p2p_order)

    def _build_auction_players(self, eco) -> None:
        """プレイヤーの出品のタブ: 絞り込み・並べ替え・出品の選択肢。"""
        for key, (label, _kind, _mode) in self.P2P_FILTERS.items():
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary if key == self.p2p_filter else discord.ButtonStyle.secondary,
                row=1,
            )
            btn.callback = self._make_p2p_filter(key)
            self.add_item(btn)
        order = discord.ui.Select(
            placeholder=f"並び順: {self.P2P_ORDERS[self.p2p_order]}",
            row=2,
            options=[
                discord.SelectOption(label=name, value=key, default=key == self.p2p_order)
                for key, name in self.P2P_ORDERS.items()
            ],
        )
        order.callback = self._on_p2p_order
        self.add_item(order)
        rows = self._p2p_rows(eco)
        if rows:
            pick = discord.ui.Select(
                placeholder="見る出品を選ぶ（選ぶと、買う・入札できる）",
                row=3,
                options=[
                    discord.SelectOption(
                        label=eco.p2p_label(r)[:100],
                        value=str(r["id"]),
                        description=(
                            f"定価 {r['price']:,}ペリカ"
                            if r["mode"] == "fixed"
                            else f"競り {(r['bid'] or r['price']):,}ペリカ" + ("（入札あり）" if r["bid"] else "")
                        ),
                    )
                    for r in rows
                ],
            )
            pick.callback = self._on_player
            self.add_item(pick)

    def _make_p2p_filter(self, key: str):
        async def callback(interaction: discord.Interaction):
            self.p2p_filter = key
            await self._redraw(interaction)

        return callback

    async def _on_p2p_order(self, interaction: discord.Interaction):
        self.p2p_order = ui.selected_values(interaction)[0]
        await self._redraw(interaction)

    def _build_auction_teiai(self, eco) -> None:
        """帝愛の棚のタブ（破産で差し押さえた品・馬。値段は日ごとに下がる）。"""
        listed = eco.auction_list()
        if listed:
            buy = discord.ui.Select(
                placeholder="買う品を選ぶ",
                row=1,
                options=[
                    discord.SelectOption(
                        label=shop.label(shop.BY_ID[e["item_id"]])[:100],
                        value=e["item_id"],
                        default=self.picked == ("auction", e["item_id"]),
                        description=f"{e['price']:,}ペリカ（定価の{e['rate']:.0%}）　残り{e['qty']}",
                    )
                    for e in listed[:25]
                ],
            )
            buy.callback = self._on_buy_auction
            self.add_item(buy)
            if any(not shop.BY_ID[e["item_id"]].unique for e in listed):
                qty = discord.ui.Select(
                    placeholder=f"消耗品の個数（いま {self.qty} 個）",
                    row=2,
                    options=[
                        discord.SelectOption(label=f"{n}個", value=str(n), default=n == self.qty)
                        for n in range(1, MAX_QTY + 1)
                    ],
                )
                qty.callback = self._on_qty
                self.add_item(qty)
        horses = eco.auction_horses()
        if horses:
            pick = discord.ui.Select(
                placeholder="競走馬を買う",
                row=3,
                options=[
                    discord.SelectOption(
                        label=f"{h['name']}（{h['coat']}）"[:100],
                        value=str(h["id"]),
                        default=self.picked == ("horse", str(h["id"])),
                        emoji=h["silks"],
                        description=f"{h['price']:,}ペリカ（定価の{h['rate']:.0%}）　速{h['speed']} ス{h['stamina']} パ{h['power']} 根{h['guts']}"[
                            :100
                        ],
                    )
                    for h in horses[:25]
                ],
            )
            pick.callback = self._on_buy_horse
            self.add_item(pick)
        if listed or horses:
            self._add_buy_button()

    def _build_inventory(self) -> None:
        eco = self.cog.bot.economy.for_guild(self.guild_id)
        inv = eco.inventory(self.user_id)
        owned = [
            i
            for i in (shop.BY_ID.get(item_id) for item_id in inv)
            if i is not None and i.kind not in (shop.COLLECTION, shop.RELIC)  # 持っているだけの品は、選択肢に出さない
        ]
        if owned:
            use = discord.ui.Select(
                placeholder="身につける・使う景品を選ぶ",
                row=0,
                options=[
                    discord.SelectOption(
                        label=shop.label(i)[:100], value=i.id, description=(f"×{inv[i.id]}" if not i.unique else "")
                    )
                    for i in owned[:25]
                ],
            )
            use.callback = self._on_use
            self.add_item(use)
        unequip = discord.ui.Select(
            placeholder="外すものを選ぶ",
            row=1,
            options=[discord.SelectOption(label=name, value=value) for value, name in UNEQUIP_NAMES.items()],
        )
        unequip.callback = self._on_unequip
        self.add_item(unequip)
        for label, mode, row in (("称号を名前の後ろに付ける", "on", 2), ("称号を名前に付けない", "off", 2)):
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, row=row)
            btn.callback = self._make_nick(mode)
            self.add_item(btn)
        back = discord.ui.Button(label="🛍️ ショップ", style=discord.ButtonStyle.primary, row=3)
        back.callback = self._to_shop
        self.add_item(back)

    # ---- 操作 ---- #
    async def _reject_non_owner(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=ui.error_embed("これは他の人のパネルです", "`/ショップ` で自分のパネルを開けます。"),
            ephemeral=True,
        )

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _on_qty(self, interaction: discord.Interaction):
        self.qty = int(ui.selected_values(interaction)[0])
        await self._redraw(interaction)

    # 選ぶだけでは買わない。選んだものを覚えて、「🛒 購入」ボタンで買う
    async def _on_buy(self, interaction: discord.Interaction):
        self.picked = ("shop", ui.selected_values(interaction)[0])
        await self._redraw(interaction)

    async def _on_buy_horse(self, interaction: discord.Interaction):
        self.picked = ("horse", ui.selected_values(interaction)[0])
        await self._redraw(interaction)

    async def _on_buy_auction(self, interaction: discord.Interaction):
        self.picked = ("auction", ui.selected_values(interaction)[0])
        await self._redraw(interaction)

    async def _on_purchase(self, interaction: discord.Interaction):
        if self.picked is None:
            return await interaction.response.send_message(
                embed=ui.error_embed("景品を選んでください", "買う景品を選んでから、「🛒 購入」を押してください。"),
                ephemeral=True,
            )
        kind, value = self.picked
        if kind == "horse":
            await self.cog.buy_auction_horse(interaction, int(value))
        elif kind == "auction":
            await self.cog.buy_auction(interaction, value, self.qty)
        else:
            await self.cog.buy_item(interaction, value, self.qty if self.tab in ("item", "horse") else 1)

    async def _to_auction(self, interaction: discord.Interaction):
        self.screen, self.qty, self.picked, self.auction_tab = "auction", 1, None, None  # タブは、開くたびに決め直す
        await self._redraw(interaction)

    async def _to_inventory(self, interaction: discord.Interaction):
        self.screen, self.picked = "inv", None
        await self._redraw(interaction)

    async def _to_shop(self, interaction: discord.Interaction):
        self.screen, self.picked = "shop", None
        await self._redraw(interaction)

    async def _on_use(self, interaction: discord.Interaction):
        await self.cog.use_item(interaction, ui.selected_values(interaction)[0])

    async def _on_unequip(self, interaction: discord.Interaction):
        await self.cog.unequip_item(interaction, ui.selected_values(interaction)[0])

    def _make_nick(self, mode: str):
        async def callback(interaction: discord.Interaction):
            await self.cog.set_title_display(interaction, mode)

        return callback
