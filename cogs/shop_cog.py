"""/ショップ — ペリカで称号・カード・消耗品・名前の色を交換する。景品はすべてゲーム内・サーバー内で完結する。

`/ショップ` ひとつで、操作パネル（選択肢とボタン）を開く。見る・買う・身につける・外す・称号の表示を、パネルから行う。
パネルのボタンは、ShopCog の buy_item / use_item / unequip_item / set_title_display を呼ぶ。
"""

import asyncio

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import approval, logthrottle, nick, shop, teiai, ui
from core.confirm import confirm_action

CONFIRM_FROM = 10_000  # この金額以上の購入は、確認ボタンを挟む
MAX_QTY = 10
STATUS_NICK_MINUTES = 2  # 身分のニックネームを付け外しする確認の間隔
STATUS_NICK_PER_GUILD = 200  # 1回の確認で、1サーバーあたり扱う人数の上限（API を使いすぎない）

SLOT_OF_KIND = {
    shop.TITLE: "title",
    shop.CARD_COLOR: "card_color",
    shop.CARD_ICON: "card_icon",
    shop.SKIN_DICE: "skin_dice",
    shop.SKIN_TABLE: "skin_table",
}
EFFECT_OF_ITEM = {shop.DAILY_DOUBLE: "daily_boost", shop.INSURANCE: "insurance"}
UNEQUIP_NAMES = {
    "title": "称号",
    "card_color": "カードの色",
    "card_icon": "カードのアイコン",
    "role": "名前の色（ロール）",
    "skin_dice": "演出（チンチロ用）",
    "skin_table": "演出（パチンコ・ブラックジャック用）",
}  # 「外す」で選べるもの


def shop_slots() -> tuple:
    """持ち物の画面で「装備中」を判定する、プロフィールの装備の列（色ロールは Discord 側のロールなので入らない）。"""
    return tuple(dict.fromkeys(SLOT_OF_KIND.values()))


class ShopError(Exception):
    """利用者に伝える形のエラー（メッセージはそのまま画面に出す）。"""


def kind_name(kind: str) -> str:
    return shop.KIND_LABEL[kind].split(" ", 1)[1]


# ショップの売り場（種類をまとめたもの）。画面上部のボタンで切り替える
TABS = {
    "home": ("🏠 トップ", ()),
    "title": ("🏷️ 称号", (shop.TITLE,)),
    "card": ("🎴 カード", (shop.CARD_COLOR, shop.CARD_ICON)),
    "item": ("🎟️ 消耗品", (shop.CONSUMABLE,)),
    "horse": ("🐎 競馬", (shop.RACING,)),
    "market": ("🕴️ 帝愛市", (shop.MARKET,)),
    "style": ("✨ 演出・名前の色", (shop.SKIN_DICE, shop.SKIN_TABLE, shop.COLOR_ROLE)),
    "collection": ("🏆 コレクション", (shop.COLLECTION,)),
}
TAB_NOTE = {
    "title": "名前の横に出る称号です（1つだけ装備できます）。",
    "card": "`/ペリカ 残高` のカードの、色とアイコンです。",
    "item": "使うと減る品です。デイリー2倍券・保険券は「🎒 持ち物」で使い、連続守り券は持っているだけで自動で使われます。栄養ドリンコは、`/地下 作業` の画面で飲みます。",
    "horse": "競走馬を迎える購入券と、ステータスを動かすにんじんです。どちらも `/競馬 厩舎` で使います。馬そのものは、使うと減る品ではありません。",
    "market": "帝愛が、情報を売る市です。決算の裏情報メモ（確度 80%・2割はガセ）が買えます。持ち物には増えず、買うとその場で、本人にだけ見えます。",
    "style": "ゲーム画面の演出と、名前の色（ロール）です。勝率と配当は変わりません。",
    "collection": "持っているだけで、`/ペリカ 残高` に飾られる自慢の品です。",
}
TIERS = (
    (10_000, "🌱 入門"),
    (100_000, "🥉 中堅"),
    (1_000_000, "🥈 大物"),
    (None, "🥇 頂点"),
)  # 値段の段階（称号・コレクション）
SECTION_NAME = {
    shop.CARD_COLOR: "🎨 色",
    shop.CARD_ICON: "🖼️ アイコン",
    shop.CONSUMABLE: "品ぞろえ（安い順）",
    shop.RACING: "品ぞろえ（安い順）",
    shop.SKIN_DICE: "🎲 チンチロの演出",
    shop.SKIN_TABLE: "🃏 パチンコ・ブラックジャックの演出",
    shop.COLOR_ROLE: "🌈 名前の色（ロール）",
}


def stock_text(stock: dict) -> str:
    """在庫の表示（1人ずつ別々）。例: 在庫 3/10（次の補充 <t:…:R>）。"""
    text = f"在庫 {stock['stock']}/{stock['max']}"
    return text + (f"（次の補充 <t:{int(stock['next_at'])}:R>）" if stock["next_at"] else "")


def item_line(item, inv: dict, stock: dict = None) -> str:
    """売り場の1行: 持っていれば ✅、値段をそろえて、名前。説明があれば、次の行に。在庫のある品は、このプレイヤーの在庫も。"""
    icon = item.value if item.kind in (shop.CARD_ICON, shop.COLLECTION) else item.mark
    mark = "✅" if item.unique and inv.get(item.id, 0) > 0 else "▫️"
    line = f"{mark} `{item.price:>10,}`　{icon}{item.name}"
    if item.kind in (shop.CONSUMABLE, shop.RACING) and inv.get(item.id, 0) > 0:
        line += f"　（持っている: {inv[item.id]}）"
    line += f"\n　　📦 {stock_text(stock)}" if stock else ""
    return line + (f"\n　　└ {item.desc}" if item.desc else "")


def tab_items(tab: str) -> list:
    """売り場の商品を、値段の安い順に（限定品は出さない）。"""
    items = [i for k in TABS[tab][1] for i in shop.of_kind(k)]
    return sorted(items, key=lambda i: i.price)  # 同じ値段は、カタログの順のまま


def _is_ours(role: discord.Role) -> bool:
    """Bot が管理する色ロールか。名前が「接頭辞＋色の名前」に完全一致し、権限が空で、連携用でないものだけ。

    運営が同じ名前で作った権限つきのロールや、「ペリカ・VIP」のような別の用途のロールは、触らない。
    """
    names = {shop.ROLE_PREFIX + i.name for i in shop.of_kind(shop.COLOR_ROLE)}
    return role.name in names and role.permissions.value == 0 and not role.managed


async def set_color_role(guild: discord.Guild, member: discord.Member, item) -> None:
    """名前の色ロールを付け替える（item=None なら外すだけ）。

    色ロールは権限を持たず、Bot のロールのすぐ下に置く。サーバーの名前の色は「ロール一覧で一番上にある、
    色つきのロール」の色になるため、Bot より上にある運営用ロールなどの色はそのまま優先される。
    """
    me = guild.me
    if not me.guild_permissions.manage_roles:
        raise ShopError(
            "Bot に「ロールの管理」権限がないため、名前の色を変えられません。サーバーの管理者に、Bot のロールへ権限を足してもらってください。"
        )
    mine = [r for r in member.roles if _is_ours(r)]
    target = None
    if item is not None:
        name = shop.ROLE_PREFIX + item.name
        same_name = [r for r in guild.roles if r.name == name]
        reusable = [r for r in same_name if _is_ours(r)]
        if same_name and not reusable:
            # 同じ名前のロールがすでにあるが、権限を持っている／連携用などで、色ロールとしては使えない
            raise ShopError(
                f"「{name}」という名前のロールがすでにあり、権限を持っているため使えません。サーバーの管理者に、そのロールの名前を変えてもらってください。"
            )
        target = reusable[0] if reusable else None
        try:
            if target is None:
                target = await guild.create_role(
                    name=name,
                    colour=discord.Colour(item.value),
                    permissions=discord.Permissions.none(),
                    hoist=False,
                    mentionable=False,
                    reason="ペリカショップ: 名前の色",
                )
            elif target.colour.value != item.value:
                await target.edit(colour=discord.Colour(item.value), reason="ペリカショップ: 色を元に戻す")
        except discord.Forbidden:
            raise ShopError("色のロールを作れませんでした。Bot の「ロールの管理」権限を確認してください。")
        wanted = max(1, me.top_role.position - 1)
        if target.position != wanted:
            try:
                await target.edit(position=wanted, reason="ペリカショップ: Bot のロールのすぐ下に置く")
            except discord.Forbidden:
                pass  # 動かせなかった場合は、下の「Bot のロールより上」の検査で案内する
        if target >= me.top_role:
            raise ShopError(
                "色のロールが Bot のロールより上にあるため、付けられません。サーバー設定のロールで、Bot のロールを上に動かしてください。"
            )
    try:
        remove = [r for r in mine if r != target]
        if remove:
            await member.remove_roles(*remove, reason="ペリカショップ: 名前の色の変更")
        if target is not None and target not in member.roles:
            await member.add_roles(target, reason="ペリカショップ: 名前の色")
    except discord.Forbidden:
        raise ShopError("ロールを付け替えられませんでした。Bot の権限と、Bot のロールの位置を確認してください。")


def nick_blocker(guild: discord.Guild, member: discord.Member) -> str:
    """ニックネームを Bot が変えられない理由を返す（変えられるなら空文字）。"""
    me = guild.me
    if not me.guild_permissions.manage_nicknames:
        return "Bot に「ニックネームの管理」権限がありません。サーバーの管理者に、Bot のロールへ権限を足してもらってください。"
    if member.id == guild.owner_id:
        return "サーバーの持ち主のニックネームは、Bot からは変えられません（Discord の仕様です）。"
    if member.top_role >= me.top_role:
        return "あなたのロールが Bot のロールより上（または同じ）なので、ニックネームを変えられません。サーバー設定のロールで、Bot のロールを上に動かしてもらってください。"
    return ""


async def apply_title_nick(guild: discord.Guild, member: discord.Member, title: str, eco) -> None:
    """ニックネームの後ろに称号を付ける（title=None なら外して元に戻す）。付ける前の名前を記録しておく。"""
    reason = nick_blocker(guild, member)
    if reason:
        raise ShopError(reason)
    titles = [i.name for i in shop.of_kind(shop.TITLE, include_limited=True)] + list(teiai.STATUS_NAME.values())
    stored = eco.get_nick_state(guild.id, member.id)
    state = nick.NickState(*stored) if stored else None
    new, new_state = nick.plan(member.nick, state, member.global_name or member.name, titles, title)
    if new != member.nick:
        try:
            await member.edit(nick=new, reason="ペリカショップ: 称号の表示")
        except discord.Forbidden:
            raise ShopError("ニックネームを変えられませんでした。Bot の権限と、Bot のロールの位置を確認してください。")
        except discord.HTTPException:
            raise ShopError(
                "Discord 側のエラーで、ニックネームを変えられませんでした。時間をおいて、もう一度お試しください。"
            )
    if eco.nick_managed(
        guild.id, member.id
    ):  # 称号の表示を選んだ人と、身分を付けている人。元のニックネームを記録しておく
        eco.set_nick_state(
            guild.id, member.id, new_state.original if new_state else None, new_state.applied if new_state else None
        )


class ShopView(discord.ui.View):
    """`/ショップ` の操作パネル。「ショップ」と「持ち物」の2つの画面を、同じメッセージの中で切り替える。

    見る画面は、同じメッセージを書き換える。買う・使う・外す・称号の表示は、既存の処理を呼び出し、
    結果は別のメッセージ（自分だけに見える）で返す。パネルを開いた本人だけが操作できる。
    """

    def __init__(self, cog: "ShopCog", user_id: int, guild_id: int, screen: str = "shop", tab: str = "home"):
        super().__init__(timeout=600)
        self.cog, self.user_id, self.guild_id, self.screen, self.tab, self.qty = (
            cog,
            user_id,
            guild_id,
            screen,
            tab,
            1,
        )
        self.picked = None  # 選んだ景品 (種類, 値)。「🛒 購入」を押すまで、買わない。種類は shop / auction / horse
        self._build()

    # ---- 見た目 ---- #
    def embed(self) -> discord.Embed:
        if self.screen == "inv":
            return self.cog._inventory_embed(self.guild_id, self.user_id)
        if self.screen == "auction":
            return self.cog._auction_embed(self.guild_id, self.user_id)
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

    def _build_auction(self) -> None:
        eco = self.cog.bot.economy.for_guild(self.guild_id)
        listed = eco.auction_list()
        if listed:
            buy = discord.ui.Select(
                placeholder="買う品を選ぶ",
                row=0,
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
                    row=1,
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
                row=2,
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
        back = discord.ui.Button(label="🛍️ ショップ", style=discord.ButtonStyle.primary, row=3)
        back.callback = self._to_shop
        self.add_item(back)

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
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=ui.error_embed("これは他の人のパネルです", "`/ショップ` で自分のパネルを開けます。"),
                ephemeral=True,
            )
            return False
        return True

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _on_qty(self, interaction: discord.Interaction):
        self.qty = int(interaction.data["values"][0])
        await self._redraw(interaction)

    # 選ぶだけでは買わない。選んだものを覚えて、「🛒 購入」ボタンで買う
    async def _on_buy(self, interaction: discord.Interaction):
        self.picked = ("shop", interaction.data["values"][0])
        await self._redraw(interaction)

    async def _on_buy_horse(self, interaction: discord.Interaction):
        self.picked = ("horse", interaction.data["values"][0])
        await self._redraw(interaction)

    async def _on_buy_auction(self, interaction: discord.Interaction):
        self.picked = ("auction", interaction.data["values"][0])
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
        self.screen, self.qty, self.picked = "auction", 1, None
        await self._redraw(interaction)

    async def _to_inventory(self, interaction: discord.Interaction):
        self.screen, self.picked = "inv", None
        await self._redraw(interaction)

    async def _to_shop(self, interaction: discord.Interaction):
        self.screen, self.picked = "shop", None
        await self._redraw(interaction)

    async def _on_use(self, interaction: discord.Interaction):
        await self.cog.use_item(interaction, interaction.data["values"][0])

    async def _on_unequip(self, interaction: discord.Interaction):
        await self.cog.unequip_item(interaction, interaction.data["values"][0])

    def _make_nick(self, mode: str):
        async def callback(interaction: discord.Interaction):
            await self.cog.set_title_display(interaction, mode)

        return callback


class ShopCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._role_locks = {}  # サーバーごとのロール操作の鍵（同名ロールの二重作成を防ぐ）
        self._nick_locks = {}  # 利用者ごとのニックネーム操作の鍵（連打で古い名前を元に計算しない）

    def _role_lock(self, guild_id: int) -> asyncio.Lock:
        return self._role_locks.setdefault(guild_id, asyncio.Lock())

    def g(self, interaction: discord.Interaction):
        """このサーバーのペリカの記録（ペリカはサーバーごとに別々）。"""
        return self.bot.economy.for_guild(interaction.guild.id)

    @property
    def eco(self):
        return self.bot.economy

    def _equipped_title(self, guild_id: int, user_id: int):
        """そのサーバーで装備している称号（景品はサーバーごとに別々）。"""
        item = shop.BY_ID.get(self.bot.economy.for_guild(guild_id).get_profile(user_id)["title"])
        return item.name if item else None

    def _nick_tag(self, guild_id: int, user_id: int):
        """ニックネームの末尾に付ける名前。カタギでなければ身分（装備は差し押さえ中）、カタギで称号の表示を選んでいれば装備中の称号。"""
        eco = self.bot.economy.for_guild(guild_id)
        status = eco.status(user_id)
        if status != teiai.KATAGI:
            return teiai.STATUS_NAME[status]
        return self._equipped_title(guild_id, user_id) if eco.nick_pref(guild_id, user_id) else None

    async def sync_status_nick(self, guild: discord.Guild, member: discord.Member) -> None:
        """身分に合わせて、ニックネームの末尾の【地下労働者】などを付ける・外す（称号の表示を選んだ人は、その称号に戻す）。

        付けられない人（サーバーの持ち主・Bot より上のロールの人・権限がないとき）は、ShopError になる。
        """
        eco = self.bot.economy.for_guild(guild.id)
        tag = self._nick_tag(guild.id, member.id)
        if tag is None and not eco.nick_managed(guild.id, member.id):
            return  # 付ける身分も、元に戻すものもない
        if tag in teiai.STATUS_NAME.values():
            eco.ensure_nick_row(guild.id, member.id)  # 元のニックネームを記録して、あとで戻せるようにする
        async with self._nick_locks.setdefault(member.id, asyncio.Lock()):
            await apply_title_nick(guild, member, self._nick_tag(guild.id, member.id), eco)

    async def _sync_title_nick(self, user_id: int, here: discord.Guild = None, member: discord.Member = None) -> tuple:
        """称号の変更を、ニックネーム表示をONにしているサーバーへ反映する。

        称号はサーバーごとに別なので、here を渡したときはそのサーバーだけ反映する（here なしは全サーバー）。
        (ここ（here）のサーバーでの失敗理由, 他のサーバーで反映できなかった件数) を返す。
        利用者ごとの鍵で順番に処理するので、連打しても古い名前を元に計算しない。
        """
        async with self._nick_locks.setdefault(user_id, asyncio.Lock()):
            problem, others_failed = "", 0
            for gid in self.eco.nick_pref_guilds(user_id):
                guild = self.bot.get_guild(gid)
                if guild is None or (here is not None and gid != here.id):
                    continue
                title = self._nick_tag(gid, user_id)  # そのサーバーで、鍵の中で最新の装備と身分を読む
                is_here = here is not None and gid == here.id
                try:
                    m = member if is_here else (guild.get_member(user_id) or await guild.fetch_member(user_id))
                    await apply_title_nick(guild, m, title, self.eco)
                except discord.NotFound:
                    continue  # そのサーバーからもういない
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

    def cog_unload(self):
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
        """自己破産の直後に、名前の色ロールを外し、【破産者】を付ける（2分周期の更新を待たない）。失敗しても破産は有効（次の周期で再試行）。"""
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
        """借金・延滞・破産者の人のニックネームに【地下労働者】などを付け、カタギに戻った人から外す（サーバーごと、人ごとに失敗を切り離す）。"""
        for guild in list(self.bot.guilds):
            if not approval.is_allowed(self.bot.economy, self.bot.config, guild.id, 0):
                continue
            eco = self.bot.economy.for_guild(guild.id)
            for user_id in eco.status_nick_targets()[:STATUS_NICK_PER_GUILD]:
                member = guild.get_member(user_id)
                if member is None:  # キャッシュにいない人は、この回は飛ばす（API を使いすぎない）
                    continue
                try:
                    await self.sync_status_nick(guild, member)
                except ShopError:
                    pass  # 付けられない人（持ち主・上位ロール・権限なし）は、そのまま
                except discord.HTTPException as exc:
                    logthrottle.warn(
                        ("status-nick", guild.id), f"身分のニックネームを更新できません: guild={guild.id}: {exc!r}"
                    )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        self.eco.delete_guild_nick_prefs(
            guild.id
        )  # Bot が外れたら、そのサーバーの設定は消す（再参加で勝手に復活しない）

    @app_commands.command(name="ショップ", description="🛍️ 景品を見る・買う・身につける（ボタンで操作）")
    @app_commands.guild_only()
    async def shop_panel(self, interaction: discord.Interaction):
        view = ShopView(self, interaction.user.id, interaction.guild.id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    def _catalog_embed(self, guild_id: int, user_id: int, tab: str = "home") -> discord.Embed:
        """売り場の画面。トップは、売り場の案内だけ。各売り場は、値段の安い順に、持っている品に ✅ を付けて並べる。"""
        eco = self.bot.economy.for_guild(guild_id)
        inv = eco.inventory(user_id)
        me = eco.peek_stats(user_id)
        balance = me["balance"] if me else 0
        if tab == "home":
            e = ui.embed(
                "🛍️ ペリカショップ",
                "売り場を、上のボタンで選んでください。景品は、ゲーム内・サーバー内のものだけです（現実の価値はありません）。",
                ui.GOLD,
            )
            lines = []
            for key in ("title", "card", "item", "horse", "market", "style", "collection"):
                items = tab_items(key)
                lines.append(
                    f"**{TABS[key][0]}**　{len(items)}種　{items[0].price:,}〜{items[-1].price:,}ペリカ\n　└ {TAB_NOTE[key]}"
                )
            e.add_field(name="売り場", value="\n".join(lines)[:1024], inline=False)
            waiting = len(eco.auction_list()) + len(eco.auction_horses())
            e.add_field(
                name="🕴️ 帝愛の競売",
                value="破産で差し押さえた品が、定価の9割から安く出ます。"
                + (f"（いま **{waiting}種** 出品中）" if waiting else ""),
                inline=False,
            )
            e.add_field(name="🎒 持ち物", value="買った品を身につける・使うのは、こちらです。", inline=False)
        else:
            e = ui.embed(f"{TABS[tab][0]}", TAB_NOTE[tab] + "\n価格はペリカ。✅ は持っている品です。", ui.GOLD)
            items = tab_items(tab)
            stocks = {
                i.id: eco.shop_stock(user_id, i.id) for i in items if i.id in shop.STOCK_RULES
            }  # 1人ずつ別々の在庫
            if tab in ("title", "collection"):  # 値段の段階ごとに、まとめる
                low = 0
                for limit, name in TIERS:
                    group = [i for i in items if i.price >= low and (limit is None or i.price < limit)]
                    if group:
                        e.add_field(
                            name=name,
                            value="\n".join(item_line(i, inv, stocks.get(i.id)) for i in group)[:1024],
                            inline=False,
                        )
                    low = limit if limit is not None else low
            else:
                for kind in TABS[tab][1]:
                    group = [i for i in items if i.kind == kind]
                    if kind == shop.COLOR_ROLE and group:
                        owned = [i.name for i in group if inv.get(i.id, 0) > 0]
                        value = f"{'・'.join(i.mark + i.name for i in group)}\n**各 {group[0].price:,}**" + (
                            f"\n✅ 持っている: {'・'.join(owned)}" if owned else ""
                        )
                    else:
                        value = "\n".join(item_line(i, inv, stocks.get(i.id)) for i in group)
                    if group:
                        e.add_field(name=SECTION_NAME[kind], value=value[:1024], inline=False)
        e.set_footer(text=f"あなたの残高: {ui.pelica(balance)}")
        return e

    def _auction_embed(self, guild_id: int, user_id: int) -> discord.Embed:
        """帝愛の競売の棚（破産で差し押さえた品。値段は日ごとに下がる）。"""
        eco = self.bot.economy.for_guild(guild_id)
        listed = eco.auction_list()
        e = ui.embed(
            "🕴️ 帝愛の競売",
            "破産で差し押さえた品を、帝愛が売りに出します。**買えるのはカタギだけ**。"
            f"値段は定価の{teiai.AUCTION_START:.0%}から、1日ごとに{teiai.AUCTION_STEP:.0%}ずつ下がり、{teiai.AUCTION_FLOOR:.0%}で止まります。\n"
            "売上は、帝愛銀行の収入（預金者の利回り）になります。",
            ui.GOLD,
        )
        if not listed and not eco.auction_horses():
            e.add_field(name="棚", value="いまは、売りに出ている品はありません。", inline=False)
        else:
            e.add_field(
                name="棚",
                value="\n".join(
                    f"{shop.label(shop.BY_ID[x['item_id']])} — **{ui.pelica(x['price'])}**（定価の{x['rate']:.0%}）"
                    + (f" ×{x['qty']}" if not shop.BY_ID[x["item_id"]].unique else "")
                    for x in listed
                )[:1024],
                inline=False,
            )
        horses = eco.auction_horses()
        if horses:
            e.add_field(
                name="🐴 競走馬（名前とステータスは、そのまま引き継ぎます）",
                value="\n".join(
                    f"{h['silks']} **{h['name']}**（{h['coat']}）— **{ui.pelica(h['price'])}**（定価の{h['rate']:.0%}）"
                    f"　速{h['speed']} ス{h['stamina']} パ{h['power']} 根{h['guts']}　{h['races']}戦{h['wins']}勝"
                    for h in horses
                )[:1024],
                inline=False,
            )
        me = eco.peek_stats(user_id)
        e.set_footer(text=f"あなたの残高: {ui.pelica(me['balance'] if me else 0)}")
        return e

    async def buy_auction_horse(self, interaction: discord.Interaction, horse_id: int):
        """競売の馬を買う（買い物の間は「プレイ中」にする。buy_item と同じ理由）。"""
        uid = interaction.user.id
        reason = self.bot.sessions.try_start(uid)
        if reason:
            return await interaction.response.send_message(
                embed=ui.error_embed("いまは買えません", reason), ephemeral=True
            )
        try:
            eco = self.g(interaction)
            horse = next((h for h in eco.auction_horses() if h["id"] == horse_id), None)
            if horse is None:
                return await interaction.response.send_message(
                    embed=ui.error_embed("買えませんでした", "その馬は、もう売れてしまいました。"), ephemeral=True
                )
            if horse["price"] >= CONFIRM_FROM:
                ok = await confirm_action(
                    interaction,
                    "競売で買いますか？",
                    f"{horse['silks']} **{horse['name']}**\n約 {ui.pelica(horse['price'])} かかります。",
                    "買う",
                )
                if ok is not True:
                    return
                reply = interaction.followup.send
            else:
                reply = interaction.response.send_message
            res = eco.auction_buy_horse(uid, horse_id)
            if not res.ok:
                return await reply(embed=ui.error_embed("買えませんでした", res.reason), ephemeral=True)
            await reply(
                embed=ui.ok_embed(
                    "競売で競走馬を買いました",
                    f"{horse['silks']} **{horse['name']}**\n残高: **{ui.pelica(res.balance)}**\n`/競馬 厩舎` で、出走を申し込めます。",
                ),
                ephemeral=True,
            )
        finally:
            self.bot.sessions.end(uid)

    async def buy_auction(self, interaction: discord.Interaction, item_id: str, qty: int = 1):
        """競売で買う。買い物の間は「プレイ中」にする（buy_item と同じ理由）。"""
        it = shop.BY_ID.get(item_id)
        uid = interaction.user.id
        if it is None:
            return await interaction.response.send_message(
                embed=ui.error_embed("品が見つかりません", "競売の画面で選んでください。"), ephemeral=True
            )
        reason = self.bot.sessions.try_start(uid)
        if reason:
            return await interaction.response.send_message(
                embed=ui.error_embed("いまは買えません", reason), ephemeral=True
            )
        try:
            qty = 1 if it.unique else qty
            entry = next((x for x in self.g(interaction).auction_list() if x["item_id"] == item_id), None)
            if entry is None:
                return await interaction.response.send_message(
                    embed=ui.error_embed("買えませんでした", "その品は、もう売れてしまいました。"), ephemeral=True
                )
            if entry["price"] * min(qty, entry["qty"]) >= CONFIRM_FROM:
                ok = await confirm_action(
                    interaction,
                    "競売で買いますか？",
                    f"**{shop.label(it)}**"
                    + (f" ×{qty}" if qty > 1 else "")
                    + f"\n約 {ui.pelica(entry['price'] * qty)} かかります。",
                    "買う",
                )
                if ok is not True:
                    return
                reply = interaction.followup.send
            else:
                reply = interaction.response.send_message
            res = self.g(interaction).auction_buy(uid, item_id, qty)
            if not res.ok:
                return await reply(embed=ui.error_embed("買えませんでした", res.reason), ephemeral=True)
            hint = "「🎒 持ち物」の画面で身につけましょう。" if it.unique else "「🎒 持ち物」の画面で使えます。"
            await reply(
                embed=ui.ok_embed(
                    "競売で買いました",
                    f"**{shop.label(it)}**"
                    + (f" ×{qty}" if qty > 1 else "")
                    + f"\n残高: **{ui.pelica(res.balance)}**\n{hint}",
                ),
                ephemeral=True,
            )
        finally:
            self.bot.sessions.end(uid)

    # ------------------------------ 購入 ------------------------------ #
    async def buy_item(self, interaction: discord.Interaction, item: str, qty: int = 1):
        """景品を買う。item は ID か名前。消耗品だけ qty 個（1〜MAX_QTY）まとめて買える。"""
        it = shop.find(item)
        if it is None:
            return await interaction.response.send_message(
                embed=ui.error_embed("景品が見つかりません", "`/ショップ` の画面で選んでください。"), ephemeral=True
            )
        if it.limited:
            return await interaction.response.send_message(
                embed=ui.error_embed(
                    "買えません",
                    f"**{it.name}** は買えない限定称号です。毎週月曜0時の週間ランキングで、1位の人に贈られます。",
                ),
                ephemeral=True,
            )
        if it.id == shop.INSIDER_MEMO:
            return await self._buy_insider_memo(interaction, it)
        # 買い物の間は「プレイ中」にする。ゲーム中に残高を減らして倍払いを避ける／確認待ちの間にゲームを始める、を防ぐ
        uid = interaction.user.id
        reason = self.bot.sessions.try_start(uid)
        if reason:
            return await interaction.response.send_message(
                embed=ui.error_embed("いまは買えません", reason), ephemeral=True
            )
        try:
            if it.unique:
                qty = 1
            total = it.price * qty
            # 確認ボタンを出す前に、買えるかを先に調べる（足りないのに確認だけさせない）
            balance = self.g(interaction).balance(uid)  # 自分の口座なので、初めてなら初期支給つきで作ってよい
            if it.unique and self.g(interaction).inventory(uid).get(it.id, 0) > 0:
                return await interaction.response.send_message(
                    embed=ui.error_embed(
                        "買えませんでした", "すでに持っています。「🎒 持ち物」の画面で身につけられます。"
                    ),
                    ephemeral=True,
                )
            stock = self.g(interaction).shop_stock(uid, it.id)
            if stock is not None and qty > stock["stock"]:  # 確認ボタンを出す前に、在庫を調べる
                return await interaction.response.send_message(
                    embed=ui.error_embed(
                        "在庫が足りません",
                        f"**{it.name}** の在庫は、いま {stock['stock']}個 です（{stock_text(stock)}）。\n"
                        "在庫は1人ずつ別々で、時間がたつと補充されます。",
                    ),
                    ephemeral=True,
                )
            if balance < total:
                return await interaction.response.send_message(
                    embed=ui.error_embed(
                        "買えませんでした",
                        f"ペリカが足りません（{total:,}ペリカ必要・残高 {balance:,}ペリカ）。\n`/ペリカ デイリー` で6時間ごとにもらえます。",
                    ),
                    ephemeral=True,
                )
            if total >= CONFIRM_FROM:
                ok = await confirm_action(
                    interaction,
                    "購入しますか？",
                    f"**{shop.label(it)}**" + (f" ×{qty}" if qty > 1 else "") + f"\n{ui.pelica(total)} かかります。",
                    "買う",
                )
                if ok is not True:
                    return
                reply = interaction.followup.send
            else:
                reply = interaction.response.send_message
            res = self.g(interaction).purchase(uid, it.id, shop.label(it), it.price, it.unique, qty)
            if not res.ok:
                return await reply(embed=ui.error_embed("買えませんでした", res.reason), ephemeral=True)
            hint = "「🎒 持ち物」の画面で身につけましょう。" if it.unique else "「🎒 持ち物」の画面で使えます。"
            await reply(
                embed=ui.ok_embed(
                    "購入しました",
                    f"**{shop.label(it)}**"
                    + (f" ×{qty}" if qty > 1 else "")
                    + f"\n残高: **{ui.pelica(res.balance)}**\n{hint}",
                ),
                ephemeral=True,
            )
        finally:
            self.bot.sessions.end(uid)

    async def _buy_insider_memo(self, interaction: discord.Interaction, it) -> None:
        """帝愛市の、裏情報メモ。持ち物には増えず、買うとその場で、本人にだけメモが見える。"""
        uid = interaction.user.id
        reason = self.bot.sessions.try_start(uid)
        if reason:
            return await interaction.response.send_message(
                embed=ui.error_embed("いまは買えません", reason), ephemeral=True
            )
        try:
            eco = self.g(interaction)
            stock = eco.insider_stock()
            if stock is None or stock["left"] <= 0:  # 確認ボタンを出す前に、売っているかを調べる
                why = (
                    "いま、売れる情報はありません（次の決算が決まるまで、お待ちください）。"
                    if stock is None
                    else "この決算の裏情報メモは、売り切れです（サーバーで5枚まで）。"
                )
                return await interaction.response.send_message(embed=ui.error_embed("買えません", why), ephemeral=True)
            ok = await confirm_action(
                interaction,
                "購入しますか？",
                f"**{it.name}**\n{ui.pelica(it.price)} かかります（残り {stock['left']}/{stock['total']} 枚）。\n"
                "確度は 80% で、2割はガセです。",
                "買う",
            )
            if ok is not True:
                return
            done, text = eco.insider_buy(uid)
            if not done:
                return await interaction.followup.send(embed=ui.error_embed("買えませんでした", text), ephemeral=True)
            await interaction.followup.send(embed=ui.embed("🕴️ 裏情報メモ（帝愛市）", text, ui.DARK), ephemeral=True)
        finally:
            self.bot.sessions.end(uid)

    # ------------------------------ 持ち物 ------------------------------ #
    def _inventory_embed(self, guild_id: int, user_id: int) -> discord.Embed:
        """持ち物と、装備中のもの、使用中の効果。何も持っていなければ、その案内。"""
        eco = self.bot.economy.for_guild(guild_id)
        inv = eco.inventory(user_id)
        prof = eco.get_profile(user_id)
        if not inv and not prof["daily_boost"] and not prof["insurance"]:
            return ui.embed("🎒 持ち物", "まだ何も持っていません。「🛍️ ショップ」の画面で、景品を買えます。", ui.INFO)
        equipped = {prof[slot] for slot in shop_slots()}
        e = ui.embed(
            "🎒 持ち物",
            "下の選択肢から、身につける・使う品を選びます。【装備中】は、いま身につけている品です。",
            ui.INFO,
        )
        for k in shop.KIND_LABEL:
            lines = []
            for it in shop.of_kind(k, include_limited=True):
                if it.id in inv:
                    mark = " 【装備中】" if it.id in equipped else ""
                    lines.append(
                        "・"
                        + f"{it.value if k in (shop.CARD_ICON, shop.COLLECTION, shop.RELIC) else ''}{it.name}"
                        + (f" ×{inv[it.id]}" if not it.unique else "")
                        + mark
                    )
            if lines:
                e.add_field(name=shop.KIND_LABEL[k], value="\n".join(lines)[:1024], inline=False)
        active = []
        if prof["daily_boost"]:
            active.append(f"デイリー2倍券（次のデイリーが2倍）×{prof['daily_boost']}")
        if prof["insurance"]:
            active.append(f"保険券（次に負けたとき半額返金）×{prof['insurance']}")
        if active:
            e.add_field(name="⏳ 使用中の効果", value="\n".join(active), inline=False)
        return e

    # ------------------------------ 使う ------------------------------ #
    async def use_item(self, interaction: discord.Interaction, item: str):
        """景品を身につける・使う。item は ID か名前（持っているものを優先する）。"""
        uid = interaction.user.id
        inv = self.g(interaction).inventory(uid)
        # 「赤」のように同じ名前が複数ある場合は、持っているものを優先して選ぶ
        owned = [i for i in shop.find_all(item) if inv.get(i.id, 0) > 0]
        it = owned[0] if owned else shop.find(item)
        if it is None or inv.get(it.id, 0) <= 0:
            return await interaction.response.send_message(
                embed=ui.error_embed("持っていません", "「🎒 持ち物」の画面で確認できます。"), ephemeral=True
            )
        if it.id in shop.AUTO_USED:
            note = {
                shop.STREAK_GUARD: "持っているだけで、デイリーを受け取れなかった日があったとき、次のデイリーで自動で使われます（1枚で1日ぶん）。",
                shop.HORSE_VOUCHER: "`/競馬 厩舎` で使います（名前と見た目を決めて、競走馬を迎えます）。",
                shop.CARROT: "`/競馬 厩舎` で使います（馬のステータスを、5ポイント動かします）。",
                shop.GOLD_CARROT: "`/競馬 厩舎` で使います（馬のステータスの合計を、10ポイント増やします）。",
                shop.LABOR_DRINK: "`/地下 作業` の画面の「🥤 栄養ドリンコ」で飲みます（疲労を60回復します）。",
            }[it.id]
            return await interaction.response.send_message(
                embed=ui.ok_embed("ここでは使いません", f"**{it.name}**: {note}"), ephemeral=True
            )
        if it.kind == shop.CONSUMABLE:
            if self.bot.sessions.is_active(uid):
                # ゲームの結果を見てから保険券を使う、などを防ぐ
                return await interaction.response.send_message(
                    embed=ui.error_embed(
                        "使えません", "ゲームの途中は、消耗品を使えません。終わってからにしてください。"
                    ),
                    ephemeral=True,
                )
            if not self.g(interaction).arm(uid, it.id):
                return await interaction.response.send_message(
                    embed=ui.error_embed("使えません", "持っていません。"), ephemeral=True
                )
            active = self.g(interaction).get_profile(uid)[EFFECT_OF_ITEM[it.id]]
            return await interaction.response.send_message(
                embed=ui.ok_embed("使いました", f"**{it.name}**: {it.desc}\n有効な枚数: **{active}枚**"), ephemeral=True
            )
        if it.kind == shop.RELIC:
            return await interaction.response.send_message(
                embed=ui.ok_embed(
                    "発掘品です",
                    f"**{it.name}** は、持っているだけで飾りになります（使う品ではありません）。`/取引 出品` で、売れます。",
                ),
                ephemeral=True,
            )
        if it.kind == shop.COLLECTION:
            return await interaction.response.send_message(
                embed=ui.ok_embed(
                    "コレクションです",
                    f"**{shop.label(it)}** は、持っているだけで `/ペリカ 残高` に飾られます（装備はありません）。",
                ),
                ephemeral=True,
            )
        if it.kind == shop.COLOR_ROLE:
            await interaction.response.defer(ephemeral=True)
            try:
                async with self._role_lock(interaction.guild.id):
                    await set_color_role(interaction.guild, interaction.user, it)
            except ShopError as exc:
                return await interaction.followup.send(
                    embed=ui.error_embed("名前の色を変えられません", str(exc)), ephemeral=True
                )
            return await interaction.followup.send(
                embed=ui.ok_embed(
                    "名前の色を変えました",
                    f"**{it.name}** になりました。\n他の色つきロールがこれより上にあると、そちらの色が優先されます。",
                ),
                ephemeral=True,
            )
        self.g(interaction).equip(uid, SLOT_OF_KIND[it.kind], it.id)
        note = ""
        if it.kind == shop.TITLE:
            await interaction.response.defer(ephemeral=True)
            problem, others = await self._sync_title_nick(uid, interaction.guild, interaction.user)
            if problem:
                note = f"\n⚠️ ニックネームには反映できませんでした: {problem}"
            elif self.eco.nick_pref(interaction.guild.id, uid):
                note = "\nニックネームにも反映しました。"
            if others:
                note += f"\n※ 他のサーバー {others} 件では反映できませんでした。"
            return await interaction.followup.send(
                embed=ui.ok_embed("装備しました", f"**{shop.label(it)}**\n`/ペリカ 残高` で確認できます。{note}"),
                ephemeral=True,
            )
        await interaction.response.send_message(
            embed=ui.ok_embed("装備しました", f"**{shop.label(it)}**\n`/ペリカ 残高` で確認できます。"), ephemeral=True
        )

    # ------------------------------ 外す ------------------------------ #
    async def unequip_item(self, interaction: discord.Interaction, slot: str):
        """slot（UNEQUIP_NAMES のキー）の装備を外す。"""
        name = UNEQUIP_NAMES[slot]
        if slot == "role":
            await interaction.response.defer(ephemeral=True)
            try:
                async with self._role_lock(interaction.guild.id):
                    await set_color_role(interaction.guild, interaction.user, None)
            except ShopError as exc:
                return await interaction.followup.send(embed=ui.error_embed("外せません", str(exc)), ephemeral=True)
            return await interaction.followup.send(
                embed=ui.ok_embed("外しました", "名前の色を元に戻しました。"), ephemeral=True
            )
        self.g(interaction).equip(interaction.user.id, slot, None)
        if slot == "title":
            await interaction.response.defer(ephemeral=True)
            problem, others = await self._sync_title_nick(interaction.user.id, interaction.guild, interaction.user)
            note = f"\n⚠️ ニックネームを戻せませんでした: {problem}" if problem else ""
            if others:
                note += f"\n※ 他のサーバー {others} 件では戻せませんでした。"
            return await interaction.followup.send(
                embed=ui.ok_embed("外しました", f"{name}を外しました。{note}"), ephemeral=True
            )
        await interaction.response.send_message(
            embed=ui.ok_embed("外しました", f"{name}を外しました。"), ephemeral=True
        )

    # ------------------------- 称号をニックネームに ------------------------- #
    async def set_title_display(self, interaction: discord.Interaction, mode: str):
        """称号をニックネームに付ける（mode="on"）／付けない（"off"）。"""
        uid, guild = interaction.user.id, interaction.guild
        was_on = self.eco.nick_pref(guild.id, uid)
        if mode == "on":
            reason = nick_blocker(guild, interaction.user)
            if reason:  # 付けられない人・サーバーでは、設定自体を有効にしない
                return await interaction.response.send_message(
                    embed=ui.error_embed("ニックネームに付けられません", reason), ephemeral=True
                )
            self.eco.set_nick_pref(guild.id, uid, True)
            await interaction.response.defer(ephemeral=True)
            problem, others = await self._sync_title_nick(uid, guild, interaction.user)
            title = self._equipped_title(guild.id, uid)
            if problem:
                if not was_on:  # もともとONだった人の設定は、失敗しても消さない
                    self.eco.set_nick_pref(guild.id, uid, False)
                return await interaction.followup.send(
                    embed=ui.error_embed("ニックネームに付けられません", problem), ephemeral=True
                )
            msg = (
                f"称号 **{title}** をニックネームの後ろに付けました。"
                if title
                else "称号を装備すると、ニックネームの後ろに付きます。「🎒 持ち物」の画面で称号を選んでください。"
            )
            return await interaction.followup.send(
                embed=ui.ok_embed(
                    "ニックネームに付けます",
                    msg
                    + "\n自分でニックネームを変えると、称号が消えることがあります。そのときは「🎒 持ち物」の画面で付け直せます。",
                ),
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)
        problem = ""
        if was_on:  # ONにしていた人だけ、元のニックネームに戻す（ONにしたことがない人の名前には触らない）
            try:
                await apply_title_nick(guild, interaction.user, None, self.eco)
            except ShopError as exc:
                problem = f"\n⚠️ ニックネームを元に戻せませんでした（手動で直してください）: {exc}"
            self.eco.set_nick_pref(guild.id, uid, False)
        await interaction.followup.send(
            embed=ui.ok_embed(
                "ニックネームには付けません",
                f"称号の表示をやめて、ニックネームを元に戻しました。{problem}"
                if was_on
                else "もともと付けていません。",
            ),
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(ShopCog(bot))
