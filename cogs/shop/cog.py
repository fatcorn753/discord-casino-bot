"""/ショップ — ペリカで称号・カード・消耗品・名前の色を交換する。景品はすべてゲーム内・サーバー内で完結する。

`/ショップ` ひとつで、操作パネル（選択肢とボタン）を開く。見る・買う・身につける・外す・称号の表示を、パネルから行う。
パネルのボタンは、ShopCog の buy_item / use_item / unequip_item / set_title_display を呼ぶ。
"""

import asyncio
from typing import cast

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.shared import ui
from cogs.shared.confirm import confirm_action, confirmed_reply, user_session
from cogs.shop.discord_ops import ShopError, apply_title_nick, nick_blocker, set_color_role
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
from domain import approval, relics, shop, teiai
from infra import catalogs, logthrottle

_SHOP_CFG = catalogs.shop()
CONFIRM_FROM = _SHOP_CFG.confirm_from  # この金額以上の購入は、確認ボタンを挟む
STATUS_NICK_MINUTES = _SHOP_CFG.status_nick_minutes  # 身分のニックネームを付け外しする確認の間隔
STATUS_NICK_PER_GUILD = (
    _SHOP_CFG.status_nick_per_guild
)  # 1回の確認で、1サーバーあたり扱う人数の上限（API を使いすぎない）


class ShopCog(commands.Cog):
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
        """ニックネームの末尾に付ける名前。カタギでなければ身分（装備は差し押さえ中）、カタギで称号の表示を選んでいれば装備中の称号、
        それ以外の富豪（総資産100万以上）は【富豪】。"""
        eco = self.bot.economy.for_guild(guild_id)
        status = eco.status(user_id)
        if status != teiai.KATAGI:
            return teiai.STATUS_NAME[status]
        if eco.nick_pref(guild_id, user_id):
            return self._equipped_title(guild_id, user_id)  # 自分で選んだ称号の表示を、富豪より優先する
        return teiai.STATUS_NAME[teiai.WEALTHY] if eco.display_status(user_id) == teiai.WEALTHY else None

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

    async def _sync_title_nick(
        self, user_id: int, here: discord.Guild | None = None, member: discord.Member | None = None
    ) -> tuple[str, int]:
        """称号の変更を、ニックネーム表示をONにしているサーバーへ反映する。

        称号はサーバーごとに別なので、here を渡したときはそのサーバーだけ反映する（here なしは全サーバー）。
        (ここ（here）のサーバーでの失敗理由, 他のサーバーで反映できなかった件数) を返す。
        利用者ごとの鍵で順番に処理するので、連打しても古い名前を元に計算しない。
        """
        async with self._nick_locks.setdefault(user_id, asyncio.Lock()):
            problem, others_failed = "", 0
            for gid in self.bot.economy.nick_pref_guilds(user_id):
                guild = self.bot.get_guild(gid)
                if guild is None or (here is not None and gid != here.id):
                    continue
                title = self._nick_tag(gid, user_id)  # そのサーバーで、鍵の中で最新の装備と身分を読む
                is_here = here is not None and gid == here.id
                try:
                    m = (
                        member
                        if is_here and member is not None
                        else (guild.get_member(user_id) or await guild.fetch_member(user_id))
                    )
                    await apply_title_nick(guild, m, title, self.bot.economy)
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
        self.bot.economy.delete_guild_nick_prefs(
            guild.id
        )  # Bot が外れたら、そのサーバーの設定は消す（再参加で勝手に復活しない）

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

    async def buy_auction_horse(self, interaction: discord.Interaction, horse_id: int):
        """競売の馬を買う（買い物の間は「プレイ中」にする。buy_item と同じ理由）。"""
        async with user_session(self.bot, interaction, "いまは買えません") as ok:
            if not ok:
                return
            uid = interaction.user.id
            eco = self.g(interaction)
            horse = next((h for h in eco.auction_horses() if h["id"] == horse_id), None)
            if horse is None:
                return await interaction.response.send_message(
                    embed=ui.error_embed("買えませんでした", "その馬は、もう売れてしまいました。"), ephemeral=True
                )
            reply = await confirmed_reply(
                interaction,
                horse["price"] >= CONFIRM_FROM,
                "競売で買いますか？",
                f"{horse['silks']} **{horse['name']}**\n約 {ui.pelica(horse['price'])} かかります。",
                "買う",
                confirm_fn=confirm_action,
            )
            if reply is None:
                return
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

    async def buy_auction(self, interaction: discord.Interaction, item_id: str, qty: int = 1):
        """競売で買う。買い物の間は「プレイ中」にする（buy_item と同じ理由）。"""
        it = shop.BY_ID.get(item_id)
        if it is None:
            return await interaction.response.send_message(
                embed=ui.error_embed("品が見つかりません", "競売の画面で選んでください。"), ephemeral=True
            )
        async with user_session(self.bot, interaction, "いまは買えません") as ok:
            if not ok:
                return
            uid = interaction.user.id
            qty = 1 if it.unique else qty
            entry = next((x for x in self.g(interaction).auction_list() if x["item_id"] == item_id), None)
            if entry is None:
                return await interaction.response.send_message(
                    embed=ui.error_embed("買えませんでした", "その品は、もう売れてしまいました。"), ephemeral=True
                )
            reply = await confirmed_reply(
                interaction,
                entry["price"] * min(qty, entry["qty"]) >= CONFIRM_FROM,
                "競売で買いますか？",
                f"**{shop.label(it)}**"
                + (f" ×{qty}" if qty > 1 else "")
                + f"\n約 {ui.pelica(entry['price'] * qty)} かかります。",
                "買う",
                confirm_fn=confirm_action,
            )
            if reply is None:
                return
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
        async with user_session(self.bot, interaction, "いまは買えません") as ok:
            if not ok:
                return
            uid = interaction.user.id
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
            reply = await confirmed_reply(
                interaction,
                total >= CONFIRM_FROM,
                "購入しますか？",
                f"**{shop.label(it)}**" + (f" ×{qty}" if qty > 1 else "") + f"\n{ui.pelica(total)} かかります。",
                "買う",
                confirm_fn=confirm_action,
            )
            if reply is None:
                return
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

    async def _buy_insider_memo(self, interaction: discord.Interaction, it) -> None:
        """帝愛市の、裏情報メモ。持ち物には増えず、買うとその場で、本人にだけメモが見える。"""
        async with user_session(self.bot, interaction, "いまは買えません") as ok:
            if not ok:
                return
            uid = interaction.user.id
            eco = self.g(interaction)
            stock = eco.insider_stock()
            if stock is None or stock["left"] <= 0:  # 確認ボタンを出す前に、売っているかを調べる
                why = (
                    "いま、売れる情報はありません（次の決算が決まるまで、お待ちください）。"
                    if stock is None
                    else "この決算の裏情報メモは、売り切れです（サーバーで5枚まで）。"
                )
                await interaction.response.send_message(embed=ui.error_embed("買えません", why), ephemeral=True)
                return
            reply = await confirmed_reply(
                interaction,
                True,
                "購入しますか？",
                f"**{it.name}**\n{ui.pelica(it.price)} かかります（残り {stock['left']}/{stock['total']} 枚）。\n"
                "確度は 80% で、2割はガセです。",
                "買う",
                confirm_fn=confirm_action,
            )
            if reply is None:
                return
            done, text = eco.insider_buy(uid)
            if not done:
                await reply(embed=ui.error_embed("買えませんでした", text), ephemeral=True)
                return
            await reply(embed=ui.embed("🕴️ 裏情報メモ（帝愛市）", text, ui.DARK), ephemeral=True)

    # ------------------------------ 持ち物 ------------------------------ #
    def _inventory_embed(self, guild_id: int, user_id: int) -> discord.Embed:
        eco = self.bot.economy.for_guild(guild_id)
        return inventory_embed(eco.inventory(user_id), eco.get_profile(user_id))

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
                    f"**{it.name}** は、使う品ではありません。{relics.buff_text(it.id)}（出品している間は、効きません）。\n"
                    "競売の画面の「📤 出品する」で、売れます。",
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
            guild = cast(discord.Guild, interaction.guild)
            member = cast(discord.Member, interaction.user)
            try:
                async with self._role_lock(ui.guild_id_of(interaction)):
                    await set_color_role(guild, member, it)
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
            guild = cast(discord.Guild, interaction.guild)
            member = cast(discord.Member, interaction.user)
            gid = ui.guild_id_of(interaction)
            problem, others = await self._sync_title_nick(uid, guild, member)
            if problem:
                note = f"\n⚠️ ニックネームには反映できませんでした: {problem}"
            elif self.bot.economy.nick_pref(gid, uid):
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
        guild = cast(discord.Guild, interaction.guild)
        member = cast(discord.Member, interaction.user)
        if slot == "role":
            await interaction.response.defer(ephemeral=True)
            try:
                async with self._role_lock(ui.guild_id_of(interaction)):
                    await set_color_role(guild, member, None)
            except ShopError as exc:
                return await interaction.followup.send(embed=ui.error_embed("外せません", str(exc)), ephemeral=True)
            return await interaction.followup.send(
                embed=ui.ok_embed("外しました", "名前の色を元に戻しました。"), ephemeral=True
            )
        self.g(interaction).equip(interaction.user.id, slot, None)
        if slot == "title":
            await interaction.response.defer(ephemeral=True)
            problem, others = await self._sync_title_nick(interaction.user.id, guild, member)
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
        uid = interaction.user.id
        guild = cast(discord.Guild, interaction.guild)
        member = cast(discord.Member, interaction.user)
        gid = ui.guild_id_of(interaction)
        was_on = self.bot.economy.nick_pref(gid, uid)
        if mode == "on":
            reason = nick_blocker(guild, member)
            if reason:  # 付けられない人・サーバーでは、設定自体を有効にしない
                return await interaction.response.send_message(
                    embed=ui.error_embed("ニックネームに付けられません", reason), ephemeral=True
                )
            self.bot.economy.set_nick_pref(gid, uid, True)
            await interaction.response.defer(ephemeral=True)
            problem, others = await self._sync_title_nick(uid, guild, member)
            title = self._equipped_title(gid, uid)
            if problem:
                if not was_on:  # もともとONだった人の設定は、失敗しても消さない
                    self.bot.economy.set_nick_pref(gid, uid, False)
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
                await apply_title_nick(guild, member, None, self.bot.economy)
            except ShopError as exc:
                problem = f"\n⚠️ ニックネームを元に戻せませんでした（手動で直してください）: {exc}"
            self.bot.economy.set_nick_pref(gid, uid, False)
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
