"""ショップの購入・競売・使用・装備・解除・称号表示切替のアクション群。"""

from typing import Callable, cast

import discord

from cogs.shared import ui
from cogs.shared.confirm import confirm_action as default_confirm_action
from cogs.shared.confirm import confirmed_reply, user_session
from cogs.shared.messages import format_failure, format_memo
from cogs.shop.discord_ops import ShopError, apply_title_nick, nick_blocker, set_color_role
from cogs.shop.embeds import EFFECT_OF_ITEM, SLOT_OF_KIND, UNEQUIP_NAMES, stock_text
from domain import relics, shop


async def buy_auction_horse(
    cog, interaction: discord.Interaction, horse_id: int, *, confirm_fn: Callable = default_confirm_action
) -> None:
    """競売の馬を買う（買い物の間は「プレイ中」にする）。"""
    async with user_session(cog.bot, interaction, "いまは買えません") as ok:
        if not ok:
            return
        uid = interaction.user.id
        eco = cog.g(interaction)
        horse = next((h for h in eco.auction_horses() if h["id"] == horse_id), None)
        if horse is None:
            await interaction.response.send_message(
                embed=ui.error_embed("買えませんでした", "その馬は、もう売れてしまいました。"), ephemeral=True
            )
            return
        reply = await confirmed_reply(
            interaction,
            horse["price"] >= cog.CONFIRM_FROM,
            "競売で買いますか？",
            f"{horse['silks']} **{horse['name']}**\n約 {ui.pelica(horse['price'])} かかります。",
            "買う",
            confirm_fn=confirm_fn,
        )
        if reply is None:
            return
        res = eco.auction_buy_horse(uid, horse_id)
        if not res.ok:
            await reply(embed=ui.error_embed("買えませんでした", format_failure(res.reason)), ephemeral=True)
            return
        await reply(
            embed=ui.ok_embed(
                "競売で競走馬を買いました",
                f"{horse['silks']} **{horse['name']}**\n残高: **{ui.pelica(res.balance)}**\n`/競馬 厩舎` で、出走を申し込めます。",
            ),
            ephemeral=True,
        )


async def buy_auction(
    cog, interaction: discord.Interaction, item_id: str, qty: int = 1, *, confirm_fn: Callable = default_confirm_action
) -> None:
    """競売で買う。買い物の間は「プレイ中」にする。"""
    it = shop.BY_ID.get(item_id)
    if it is None:
        await interaction.response.send_message(
            embed=ui.error_embed("品が見つかりません", "競売の画面で選んでください。"), ephemeral=True
        )
        return
    async with user_session(cog.bot, interaction, "いまは買えません") as ok:
        if not ok:
            return
        uid = interaction.user.id
        qty = 1 if it.unique else qty
        entry = next((x for x in cog.g(interaction).auction_list() if x["item_id"] == item_id), None)
        if entry is None:
            await interaction.response.send_message(
                embed=ui.error_embed("買えませんでした", "その品は、もう売れてしまいました。"), ephemeral=True
            )
            return
        reply = await confirmed_reply(
            interaction,
            entry["price"] * min(qty, entry["qty"]) >= cog.CONFIRM_FROM,
            "競売で買いますか？",
            f"**{shop.label(it)}**"
            + (f" ×{qty}" if qty > 1 else "")
            + f"\n約 {ui.pelica(entry['price'] * qty)} かかります。",
            "買う",
            confirm_fn=confirm_fn,
        )
        if reply is None:
            return
        res = cog.g(interaction).auction_buy(uid, item_id, qty)
        if not res.ok:
            await reply(embed=ui.error_embed("買えませんでした", format_failure(res.reason)), ephemeral=True)
            return
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


async def buy_insider_memo(
    cog, interaction: discord.Interaction, it, *, confirm_fn: Callable = default_confirm_action
) -> None:
    """帝愛市の、裏情報メモ。持ち物には増えず、買うとその場で、本人にだけメモが見える。"""
    async with user_session(cog.bot, interaction, "いまは買えません") as ok:
        if not ok:
            return
        uid = interaction.user.id
        eco = cog.g(interaction)
        stock = eco.insider_stock()
        if stock is None or stock["left"] <= 0:
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
            confirm_fn=confirm_fn,
        )
        if reply is None:
            return
        done, result = eco.insider_buy(uid)
        if not done:
            await reply(embed=ui.error_embed("買えませんでした", format_failure(result)), ephemeral=True)
            return
        await reply(embed=ui.embed("🕴️ 裏情報メモ（帝愛市）", format_memo(result), ui.DARK), ephemeral=True)


async def buy_item(
    cog, interaction: discord.Interaction, item: str, qty: int = 1, *, confirm_fn: Callable = default_confirm_action
) -> None:
    """景品を買う。item は ID か名前。消耗品だけ qty 個（1〜MAX_QTY）まとめて買える。"""
    it = shop.find(item)
    if it is None:
        await interaction.response.send_message(
            embed=ui.error_embed("景品が見つかりません", "`/ショップ` の画面で選んでください。"), ephemeral=True
        )
        return
    if it.limited:
        await interaction.response.send_message(
            embed=ui.error_embed(
                "買えません",
                f"**{it.name}** は買えない限定称号です。毎週月曜0時の週間ランキングで、1位の人に贈られます。",
            ),
            ephemeral=True,
        )
        return
    if it.id == shop.INSIDER_MEMO:
        await buy_insider_memo(cog, interaction, it, confirm_fn=confirm_fn)
        return
    async with user_session(cog.bot, interaction, "いまは買えません") as ok:
        if not ok:
            return
        uid = interaction.user.id
        if it.unique:
            qty = 1
        total = it.price * qty
        balance = cog.g(interaction).balance(uid)
        if it.unique and cog.g(interaction).inventory(uid).get(it.id, 0) > 0:
            await interaction.response.send_message(
                embed=ui.error_embed("買えませんでした", "すでに持っています。「🎒 持ち物」の画面で身につけられます。"),
                ephemeral=True,
            )
            return
        stock = cog.g(interaction).shop_stock(uid, it.id)
        if stock is not None and qty > stock["stock"]:
            await interaction.response.send_message(
                embed=ui.error_embed(
                    "在庫が足りません",
                    f"**{it.name}** の在庫は、いま {stock['stock']}個 です（{stock_text(stock)}）。\n"
                    "在庫は1人ずつ別々で、時間がたつと補充されます。",
                ),
                ephemeral=True,
            )
            return
        if balance < total:
            await interaction.response.send_message(
                embed=ui.error_embed(
                    "買えませんでした",
                    f"ペリカが足りません（{total:,}ペリカ必要・残高 {balance:,}ペリカ）。\n`/ペリカ デイリー` で6時間ごとにもらえます。",
                ),
                ephemeral=True,
            )
            return
        reply = await confirmed_reply(
            interaction,
            total >= cog.CONFIRM_FROM,
            "購入しますか？",
            f"**{shop.label(it)}**" + (f" ×{qty}" if qty > 1 else "") + f"\n{ui.pelica(total)} かかります。",
            "買う",
            confirm_fn=confirm_fn,
        )
        if reply is None:
            return
        res = cog.g(interaction).purchase(uid, it.id, shop.label(it), it.price, it.unique, qty)
        if not res.ok:
            await reply(embed=ui.error_embed("買えませんでした", format_failure(res.reason)), ephemeral=True)
            return
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


async def use_item(cog, interaction: discord.Interaction, item: str) -> None:
    """景品を身につける・使う。item は ID か名前（持っているものを優先する）。"""
    uid = interaction.user.id
    inv = cog.g(interaction).inventory(uid)
    owned = [i for i in shop.find_all(item) if inv.get(i.id, 0) > 0]
    it = owned[0] if owned else shop.find(item)
    if it is None or inv.get(it.id, 0) <= 0:
        await interaction.response.send_message(
            embed=ui.error_embed("持っていません", "「🎒 持ち物」の画面で確認できます。"), ephemeral=True
        )
        return
    if it.id in shop.AUTO_USED:
        note = {
            shop.STREAK_GUARD: "持っているだけで、デイリーを受け取れなかった日があったとき、次のデイリーで自動で使われます（1枚で1日ぶん）。",
            shop.HORSE_VOUCHER: "`/競馬 厩舎` で使います（名前と見た目を決めて、競走馬を迎えます）。",
            shop.CARROT: "`/競馬 厩舎` で使います（馬のステータスを、5ポイント動かします）。",
            shop.GOLD_CARROT: "`/競馬 厩舎` で使います（馬のステータスの合計を、10ポイント増やします）。",
            shop.LABOR_DRINK: "`/地下 作業` の画面の「🥤 栄養ドリンコ」で飲みます（疲労を60回復します）。",
        }[it.id]
        await interaction.response.send_message(
            embed=ui.ok_embed("ここでは使いません", f"**{it.name}**: {note}"), ephemeral=True
        )
        return
    if it.kind == shop.CONSUMABLE:
        if cog.bot.sessions.is_active(uid):
            await interaction.response.send_message(
                embed=ui.error_embed("使えません", "ゲームの途中は、消耗品を使えません。終わってからにしてください。"),
                ephemeral=True,
            )
            return
        if not cog.g(interaction).arm(uid, it.id):
            await interaction.response.send_message(
                embed=ui.error_embed("使えません", "持っていません。"), ephemeral=True
            )
            return
        active = cog.g(interaction).get_profile(uid)[EFFECT_OF_ITEM[it.id]]
        await interaction.response.send_message(
            embed=ui.ok_embed("使いました", f"**{it.name}**: {it.desc}\n有効な枚数: **{active}枚**"), ephemeral=True
        )
        return
    if it.kind == shop.RELIC:
        await interaction.response.send_message(
            embed=ui.ok_embed(
                "発掘品です",
                f"**{it.name}** は、使う品ではありません。{relics.buff_text(it.id)}（出品している間は、効きません）。\n"
                "競売の画面の「📤 出品する」で、売れます。",
            ),
            ephemeral=True,
        )
        return
    if it.kind == shop.COLLECTION:
        await interaction.response.send_message(
            embed=ui.ok_embed(
                "コレクションです",
                f"**{shop.label(it)}** は、持っているだけで `/ペリカ 残高` に飾られます（装備はありません）。",
            ),
            ephemeral=True,
        )
        return
    if it.kind == shop.COLOR_ROLE:
        await interaction.response.defer(ephemeral=True)
        guild = cast(discord.Guild, interaction.guild)
        member = cast(discord.Member, interaction.user)
        try:
            async with cog._role_lock(ui.guild_id_of(interaction)):
                await set_color_role(guild, member, it)
        except ShopError as exc:
            await interaction.followup.send(embed=ui.error_embed("名前の色を変えられません", str(exc)), ephemeral=True)
            return
        await interaction.followup.send(
            embed=ui.ok_embed(
                "名前の色を変えました",
                f"**{it.name}** になりました。\n他の色つきロールがこれより上にあると、そちらの色が優先されます。",
            ),
            ephemeral=True,
        )
        return
    cog.g(interaction).equip(uid, SLOT_OF_KIND[it.kind], it.id)
    note = ""
    if it.kind == shop.TITLE:
        await interaction.response.defer(ephemeral=True)
        guild = cast(discord.Guild, interaction.guild)
        member = cast(discord.Member, interaction.user)
        gid = ui.guild_id_of(interaction)
        problem, others = await cog._sync_title_nick(uid, guild, member)
        if problem:
            note = f"\n⚠️ ニックネームには反映できませんでした: {problem}"
        elif cog.bot.economy.nick_pref(gid, uid):
            note = "\nニックネームにも反映しました。"
        if others:
            note += f"\n※ 他のサーバー {others} 件では反映できませんでした。"
        await interaction.followup.send(
            embed=ui.ok_embed("装備しました", f"**{shop.label(it)}**\n`/ペリカ 残高` で確認できます。{note}"),
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        embed=ui.ok_embed("装備しました", f"**{shop.label(it)}**\n`/ペリカ 残高` で確認できます。"), ephemeral=True
    )


async def unequip_item(cog, interaction: discord.Interaction, slot: str) -> None:
    """slot（UNEQUIP_NAMES のキー）の装備を外す。"""
    name = UNEQUIP_NAMES[slot]
    guild = cast(discord.Guild, interaction.guild)
    member = cast(discord.Member, interaction.user)
    if slot == "role":
        await interaction.response.defer(ephemeral=True)
        try:
            async with cog._role_lock(ui.guild_id_of(interaction)):
                await set_color_role(guild, member, None)
        except ShopError as exc:
            await interaction.followup.send(embed=ui.error_embed("外せません", str(exc)), ephemeral=True)
            return
        await interaction.followup.send(embed=ui.ok_embed("外しました", "名前の色を元に戻しました。"), ephemeral=True)
        return
    cog.g(interaction).equip(interaction.user.id, slot, None)
    if slot == "title":
        await interaction.response.defer(ephemeral=True)
        problem, others = await cog._sync_title_nick(interaction.user.id, guild, member)
        note = f"\n⚠️ ニックネームを戻せませんでした: {problem}" if problem else ""
        if others:
            note += f"\n※ 他のサーバー {others} 件では戻せませんでした。"
        await interaction.followup.send(embed=ui.ok_embed("外しました", f"{name}を外しました。{note}"), ephemeral=True)
        return
    await interaction.response.send_message(embed=ui.ok_embed("外しました", f"{name}を外しました。"), ephemeral=True)


async def set_title_display(cog, interaction: discord.Interaction, mode: str) -> None:
    """称号をニックネームに付ける（mode="on"）／付けない（"off"）。"""
    uid = interaction.user.id
    guild = cast(discord.Guild, interaction.guild)
    member = cast(discord.Member, interaction.user)
    gid = ui.guild_id_of(interaction)
    was_on = cog.bot.economy.nick_pref(gid, uid)
    if mode == "on":
        reason = nick_blocker(guild, member)
        if reason:
            await interaction.response.send_message(
                embed=ui.error_embed("ニックネームに付けられません", reason), ephemeral=True
            )
            return
        cog.bot.economy.set_nick_pref(gid, uid, True)
        await interaction.response.defer(ephemeral=True)
        problem, others = await cog._sync_title_nick(uid, guild, member)
        title = cog._equipped_title(gid, uid)
        if problem:
            if not was_on:
                cog.bot.economy.set_nick_pref(gid, uid, False)
            await interaction.followup.send(
                embed=ui.error_embed("ニックネームに付けられません", problem), ephemeral=True
            )
            return
        msg = (
            f"称号 **{title}** をニックネームの後ろに付けました。"
            if title
            else "称号を装備すると、ニックネームの後ろに付きます。「🎒 持ち物」の画面で称号を選んでください。"
        )
        await interaction.followup.send(
            embed=ui.ok_embed(
                "ニックネームに付けます",
                msg
                + "\n自分でニックネームを変えると、称号が消えることがあります。そのときは「🎒 持ち物」の画面で付け直せます。",
            ),
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    problem = ""
    if was_on:
        try:
            await apply_title_nick(guild, member, None, cog.bot.economy)
        except ShopError as exc:
            problem = f"\n⚠️ ニックネームを元に戻せませんでした（手動で直してください）: {exc}"
        cog.bot.economy.set_nick_pref(gid, uid, False)
    await interaction.followup.send(
        embed=ui.ok_embed(
            "ニックネームには付けません",
            f"称号の表示をやめて、ニックネームを元に戻しました。{problem}" if was_on else "もともと付けていません。",
        ),
        ephemeral=True,
    )
