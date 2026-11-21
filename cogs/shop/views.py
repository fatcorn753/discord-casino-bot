"""`/ショップ` の操作パネル（View）。"""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Optional

import discord

from cogs.shared import ui
from cogs.shared.bridges import IP2PNavigationBridge, resolve_bridge
from cogs.shop import actions
from cogs.shop.embeds import (
    MAX_QTY,
    P2P_FILTERS,
    P2P_ORDERS,
    SHOWN,
    TABS,
    UNEQUIP_NAMES,
    market_label,
    tab_items,
)
from domain import shop
from domain.ports.contracts import ListingRow

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
        self.auction_tab: str | None = None  # 競売の画面のタブ（players / teiai）。None は、開いたときに決める
        self.p2p_filter, self.p2p_order = "all", "end"  # プレイヤーの出品の、絞り込みと並び順
        self._screen_data: Mapping[str, Any] = {}
        self._build()

    # ---- 見た目 ---- #
    def embed(self) -> discord.Embed:
        from cogs.shop.embeds import auction_embed, catalog_embed, inventory_embed

        if self.screen == "inv":
            return inventory_embed(self._screen_data["inv"], self._screen_data["prof"])
        if self.screen == "auction":
            return auction_embed(
                self._screen_data["balance"],
                tab=self._screen_data["tab"],
                p2p_filter=self.p2p_filter,
                p2p_order=self.p2p_order,
                p2p_rows=self._screen_data["p2p_rows"],
                listed=self._screen_data["listed"],
                horses=self._screen_data["horses"],
                market=self._screen_data.get("market", []),
            )
        return catalog_embed(
            self._screen_data["inv"],
            self._screen_data["balance"],
            tab=self.tab,
            waiting=self._screen_data["waiting"],
            stocks=self._screen_data["stocks"],
        )

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
        snap = self.cog.service.catalog_snapshot(self.guild_id, self.user_id, self.tab, items)
        self._screen_data = snap
        if items:
            inv = snap["inv"]
            stocks = snap["stocks"]
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
                        + (f"　在庫 {st['stock']}/{st['max']}" if (st := stocks.get(i.id)) else "")
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

    def _p2p(self) -> Optional[IP2PNavigationBridge]:
        """プレイヤー間の取引ブリッジ。読み込まれていなければ None（画面に出さない）。"""
        return resolve_bridge(self.cog.bot, IP2PNavigationBridge, self.cog._p2p_bridge)

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
            horses = self._screen_data.get("horses")
            if horses is None:
                horses = self.cog.service.auction_horses(self.guild_id)
            horse = next((h for h in horses if str(h["id"]) == value), None)
            return horse["name"] if horse else "競走馬"
        item = shop.BY_ID.get(value)
        return item.name if item else "景品"

    def _make_tab(self, tab: str):
        async def callback(interaction: discord.Interaction):
            self.tab, self.qty, self.picked = tab, 1, None
            await self._redraw(interaction)

        return callback

    def _build_auction(self) -> None:
        p2p_bridge = self._p2p()
        _label, kind, mode = self.P2P_FILTERS[self.p2p_filter]
        snap = self.cog.service.auction_snapshot(
            self.guild_id,
            self.user_id,
            self.auction_tab,
            p2p_bridge is not None,
            kind,
            mode,
            self.p2p_order,
            SHOWN,
        )
        self._screen_data = snap
        self.auction_tab = snap["tab"]
        listed = snap["listed"]
        market = snap.get("market", [])
        horses = snap["horses"]
        # 上の段: タブ（いまのタブは青）と、出品・自分の取引・ショップへもどる
        if p2p_bridge is not None:
            p2p_total = snap["p2p_total"]
            for tab, label in (
                ("players", f"🤝 プレイヤーの出品（{p2p_total}）"),
                ("teiai", f"🕴️ 帝愛の棚（{len(listed) + len(market) + len(horses)}）"),
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
        back = discord.ui.Button(label="🛍️ ショップ", style=discord.ButtonStyle.secondary, row=0)
        back.callback = self._to_shop
        self.add_item(back)
        if self.auction_tab == "players":
            self._build_auction_players(snap["p2p_rows"])
        else:
            self._build_auction_teiai(listed, horses, market)

    def _make_auction_tab(self, tab: str):
        async def callback(interaction: discord.Interaction):
            self.auction_tab, self.picked = tab, None
            await self._redraw(interaction)

        return callback

    def _build_auction_players(self, p2p_rows: Sequence[tuple[ListingRow, str]]) -> None:
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
        if p2p_rows:
            pick = discord.ui.Select(
                placeholder="見る出品を選ぶ（選ぶと、買う・入札できる）",
                row=3,
                options=[
                    discord.SelectOption(
                        label=item_label[:100],
                        value=str(r["id"]),
                        description=(
                            f"定価 {r['price']:,}ペリカ"
                            if r["mode"] == "fixed"
                            else f"競り {(r['bid'] or r['price']):,}ペリカ" + ("（入札あり）" if r["bid"] else "")
                        ),
                    )
                    for r, item_label in p2p_rows
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

    def _build_auction_teiai(self, listed: list[Any], horses: list[Any], market: Optional[list[Any]] = None) -> None:
        """帝愛の棚のタブ（帝愛の期間限定の出品と、破産で差し押さえた品・馬。差し押さえ品の値段は日ごとに下がる）。

        Discord の行は5つまでなので、帝愛の出品は差し押さえ品と同じ選択肢に並べる（値は "tm:出品ID"）。
        """
        market = market or []
        if listed or market:
            market_opts = [
                discord.SelectOption(
                    label=market_label(m)[:100],
                    value=f"tm:{m['id']}",
                    default=self.picked == ("auction", f"tm:{m['id']}"),
                    description=(
                        f"{m['price']:,}ペリカ（{'競売・値下がり中' if m['mode'] == 'auction' else '定価販売'}）"
                        + (f"　残り{m['qty']}" if not shop.BY_ID[m["item_id"]].unique else "")
                    )[:100],
                )
                for m in market
            ]
            buy = discord.ui.Select(
                placeholder="買う品を選ぶ",
                row=1,
                options=market_opts
                + [
                    discord.SelectOption(
                        label=shop.label(shop.BY_ID[e["item_id"]])[:100],
                        value=e["item_id"],
                        default=self.picked == ("auction", e["item_id"]),
                        description=f"{e['price']:,}ペリカ（定価の{e['rate']:.0%}）　残り{e['qty']}",
                    )
                    for e in listed[: 25 - len(market_opts)]
                ],
            )
            buy.callback = self._on_buy_auction
            self.add_item(buy)
            if any(not shop.BY_ID[e["item_id"]].unique for e in [*listed, *market]):
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
        inv = self.cog.service.inventory(self.guild_id, self.user_id)
        self._screen_data = {"inv": inv, "prof": self.cog.service.profile(self.guild_id, self.user_id)}
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
        await ui.reply_error(interaction, "`/ショップ` で自分のパネルを開けます。", title="これは他の人のパネルです")

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
            return await ui.reply_error(
                interaction, "買う景品を選んでから、「🛒 購入」を押してください。", title="景品を選んでください"
            )
        kind, value = self.picked
        if kind == "horse":
            await actions.buy_auction_horse(self.cog, interaction, int(value))
        elif kind == "auction" and value.startswith("tm:"):
            await actions.buy_teiai_market(self.cog, interaction, int(value[3:]), self.qty)
        elif kind == "auction":
            await actions.buy_auction(self.cog, interaction, value, self.qty)
        else:
            await actions.buy_item(self.cog, interaction, value, self.qty if self.tab in ("item", "horse") else 1)

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
        await actions.use_item(self.cog, interaction, ui.selected_values(interaction)[0])

    async def _on_unequip(self, interaction: discord.Interaction):
        await actions.unequip_item(self.cog, interaction, ui.selected_values(interaction)[0])

    def _make_nick(self, mode: str):
        async def callback(interaction: discord.Interaction):
            await actions.set_title_display(self.cog, interaction, mode)

        return callback
