"""/ショップ — ペリカで称号・カード・消耗品・名前の色を交換する。景品はすべてゲーム内・サーバー内で完結する。

`/ショップ` ひとつで、操作パネル（選択肢とボタン）を開く。見る・買う・身につける・外す・称号の表示を、パネルから行う。
パネルのボタンは、ShopCog の buy_item / use_item / unequip_item / set_title_display を呼ぶ。
"""

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from core import nick, shop, ui
from core.confirm import confirm_action

CONFIRM_FROM = 10_000  # この金額以上の購入は、確認ボタンを挟む
MAX_QTY = 10

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
    titles = [i.name for i in shop.of_kind(shop.TITLE, include_limited=True)]
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
    if eco.nick_pref(guild.id, member.id):
        eco.set_nick_state(
            guild.id, member.id, new_state.original if new_state else None, new_state.applied if new_state else None
        )


class ShopView(discord.ui.View):
    """`/ショップ` の操作パネル。「ショップ」と「持ち物」の2つの画面を、同じメッセージの中で切り替える。

    見る画面は、同じメッセージを書き換える。買う・使う・外す・称号の表示は、既存の処理を呼び出し、
    結果は別のメッセージ（自分だけに見える）で返す。パネルを開いた本人だけが操作できる。
    """

    def __init__(self, cog: "ShopCog", user_id: int, guild_id: int, screen: str = "shop", kind: str = None):
        super().__init__(timeout=600)
        self.cog, self.user_id, self.guild_id, self.screen, self.kind, self.qty = (
            cog,
            user_id,
            guild_id,
            screen,
            kind,
            1,
        )
        self._build()

    # ---- 見た目 ---- #
    def embed(self) -> discord.Embed:
        if self.screen == "inv":
            return self.cog._inventory_embed(self.guild_id, self.user_id)
        return self.cog._catalog_embed(self.guild_id, self.user_id, [self.kind] if self.kind else None)

    def _build(self) -> None:
        self.clear_items()
        if self.screen == "inv":
            self._build_inventory()
        else:
            self._build_shop()

    def _build_shop(self) -> None:
        options = [discord.SelectOption(label="すべて", value="*", default=self.kind is None)]
        options += [discord.SelectOption(label=kind_name(k), value=k, default=self.kind == k) for k in shop.KIND_LABEL]
        kind_select = discord.ui.Select(placeholder="種類を選ぶ", options=options, row=0)
        kind_select.callback = self._on_kind
        self.add_item(kind_select)
        items = [i for i in shop.of_kind(self.kind) if not i.limited] if self.kind else []
        if items:
            buy = discord.ui.Select(
                placeholder="買う景品を選ぶ",
                row=1,
                options=[
                    discord.SelectOption(
                        label=shop.label(i)[:100],
                        value=i.id,
                        description=f"{i.price:,}ペリカ" + (f"　{i.desc}" if i.desc else "")[:60],
                    )
                    for i in items[:25]
                ],
            )
            buy.callback = self._on_buy
            self.add_item(buy)
        if self.kind == shop.CONSUMABLE:
            qty = discord.ui.Select(
                placeholder=f"個数（いま {self.qty} 個）",
                row=2,
                options=[
                    discord.SelectOption(label=f"{n}個", value=str(n), default=n == self.qty)
                    for n in range(1, MAX_QTY + 1)
                ],
            )
            qty.callback = self._on_qty
            self.add_item(qty)
        go = discord.ui.Button(label="🎒 持ち物", style=discord.ButtonStyle.primary, row=3)
        go.callback = self._to_inventory
        self.add_item(go)

    def _build_inventory(self) -> None:
        eco = self.cog.bot.economy.for_guild(self.guild_id)
        inv = eco.inventory(self.user_id)
        owned = [i for i in (shop.BY_ID.get(item_id) for item_id in inv) if i is not None and i.kind != shop.COLLECTION]
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

    async def _on_kind(self, interaction: discord.Interaction):
        value = interaction.data["values"][0]
        self.kind, self.qty = (None if value == "*" else value), 1
        await self._redraw(interaction)

    async def _on_qty(self, interaction: discord.Interaction):
        self.qty = int(interaction.data["values"][0])
        await self._redraw(interaction)

    async def _on_buy(self, interaction: discord.Interaction):
        await self.cog.buy_item(
            interaction, interaction.data["values"][0], self.qty if self.kind == shop.CONSUMABLE else 1
        )

    async def _to_inventory(self, interaction: discord.Interaction):
        self.screen = "inv"
        await self._redraw(interaction)

    async def _to_shop(self, interaction: discord.Interaction):
        self.screen = "shop"
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
                title = self._equipped_title(gid, user_id)  # そのサーバーで、鍵の中で最新の装備を読む
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

    def _catalog_embed(self, guild_id: int, user_id: int, kinds=None) -> discord.Embed:
        """景品の一覧（kinds が None ならすべての種類）。残高つき。"""
        eco = self.bot.economy.for_guild(guild_id)
        inv = eco.inventory(user_id)
        e = ui.embed(
            "🛍️ ペリカショップ",
            "景品はすべて、ゲーム内・サーバー内で完結するものです（現実の価値はありません）。\n"
            "種類を選ぶと、下の選択肢で買えます。身につけるのは「🎒 持ち物」の画面です。",
            ui.GOLD,
        )
        for k in kinds or list(shop.KIND_LABEL):
            items = shop.of_kind(k)
            if k == shop.COLOR_ROLE:
                owned = [i.name for i in items if i.id in inv]
                value = f"{'・'.join(i.name for i in items)}\n**各 {ui.pelica(items[0].price)}**" + (
                    f"\n持っている: {'・'.join(owned)}" if owned else ""
                )
            else:
                value = "\n".join(
                    f"{'✅ ' if i.id in inv and i.unique else ''}{i.value if k == shop.CARD_ICON else ''}{i.name} — {ui.pelica(i.price)}"
                    + (f"\n　└ {i.desc}" if i.desc else "")
                    for i in items
                )
            e.add_field(name=shop.KIND_LABEL[k], value=value[:1024], inline=False)
        me = eco.peek_stats(user_id)
        e.set_footer(text=f"あなたの残高: {ui.pelica(me['balance'] if me else 0)}")
        return e

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

    # ------------------------------ 持ち物 ------------------------------ #
    def _inventory_embed(self, guild_id: int, user_id: int) -> discord.Embed:
        """持ち物と、装備中のもの、使用中の効果。何も持っていなければ、その案内。"""
        eco = self.bot.economy.for_guild(guild_id)
        inv = eco.inventory(user_id)
        prof = eco.get_profile(user_id)
        if not inv and not prof["daily_boost"] and not prof["insurance"]:
            return ui.embed("🎒 持ち物", "まだ何も持っていません。「🛍️ ショップ」の画面で、景品を買えます。", ui.INFO)
        equipped = {prof[slot] for slot in shop_slots()}
        e = ui.embed("🎒 持ち物", color=ui.INFO)
        for k in shop.KIND_LABEL:
            lines = []
            for it in shop.of_kind(k, include_limited=True):
                if it.id in inv:
                    mark = " 【装備中】" if it.id in equipped else ""
                    lines.append(
                        f"{it.value if k in (shop.CARD_ICON, shop.COLLECTION) else ''}{it.name}"
                        + (f" ×{inv[it.id]}" if not it.unique else "")
                        + mark
                    )
            if lines:
                e.add_field(name=shop.KIND_LABEL[k], value="　".join(lines)[:1024], inline=False)
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
