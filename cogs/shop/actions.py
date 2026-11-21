"""ショップの購入・競売・使用・装備・解除・称号表示切替のアクション群。"""

from typing import TYPE_CHECKING, Any, Callable, Optional, cast

import discord

from cogs.shared import ui
from cogs.shared.confirm import confirm_action, confirmed_reply, user_session
from cogs.shared.messages import format_failure, format_memo
from cogs.shop.discord_ops import ShopError, apply_title_nick, nick_blocker, set_color_role
from cogs.shop.embeds import SLOT_OF_KIND, UNEQUIP_NAMES, market_label, stock_text
from domain import relics, shop

if TYPE_CHECKING:
    from cogs.shop.cog import ShopCog


async def _complete_purchase(reply: Callable[..., Any], title: str, target_text: str, res: Any, hint: str) -> None:
    if not res.ok:
        await reply(embed=ui.error_embed("買えませんでした", format_failure(res.reason)), ephemeral=True)
        return
    await reply(
        embed=ui.ok_embed(title, f"{target_text}\n残高: **{ui.pelica(res.balance)}**\n{hint}"),
        ephemeral=True,
    )


def _item_hint(unique: bool) -> str:
    return "「🎒 持ち物」の画面で身につけましょう。" if unique else "「🎒 持ち物」の画面で使えます。"


async def buy_auction_horse(
    cog: "ShopCog", interaction: discord.Interaction, horse_id: int, *, confirm_fn: Optional[Callable] = None
) -> None:
    """競売の馬を買う（買い物の間は「プレイ中」にする）。"""
    fn = confirm_action if confirm_fn is None else confirm_fn
    async with user_session(cog._sessions, interaction, "いまは買えません") as ok:
        if not ok:
            return
        gid = ui.guild_id_of(interaction)
        uid = interaction.user.id
        horse = next((h for h in cog.service.auction_horses(gid) if h["id"] == horse_id), None)
        if horse is None:
            await ui.reply_error(interaction, "その馬は、もう売れてしまいました。", title="買えませんでした")
            return
        reply = await confirmed_reply(
            interaction,
            horse["price"] >= cog.CONFIRM_FROM,
            "競売で買いますか？",
            f"{horse['silks']} **{horse['name']}**\n約 {ui.pelica(horse['price'])} かかります。",
            "買う",
            confirm_fn=fn,
        )
        if reply is None:
            return
        res = cog.service.auction_buy_horse(gid, uid, horse_id)
        await _complete_purchase(
            reply,
            "競売で競走馬を買いました",
            f"{horse['silks']} **{horse['name']}**",
            res,
            "`/競馬 厩舎` で、出走を申し込めます。",
        )


async def buy_auction(
    cog: "ShopCog",
    interaction: discord.Interaction,
    item_id: str,
    qty: int = 1,
    *,
    confirm_fn: Optional[Callable] = None,
) -> None:
    """競売で買う。買い物の間は「プレイ中」にする。"""
    fn = confirm_action if confirm_fn is None else confirm_fn
    it = shop.BY_ID.get(item_id)
    if it is None:
        await ui.reply_error(interaction, "競売の画面で選んでください。", title="品が見つかりません")
        return
    async with user_session(cog._sessions, interaction, "いまは買えません") as ok:
        if not ok:
            return
        gid = ui.guild_id_of(interaction)
        uid = interaction.user.id
        qty = 1 if it.unique else qty
        entry = next((x for x in cog.service.auction_list(gid) if x["item_id"] == item_id), None)
        if entry is None:
            await ui.reply_error(interaction, "その品は、もう売れてしまいました。", title="買えませんでした")
            return
        name_qty = f"**{shop.label(it)}**" + (f" ×{qty}" if qty > 1 else "")
        reply = await confirmed_reply(
            interaction,
            entry["price"] * min(qty, entry["qty"]) >= cog.CONFIRM_FROM,
            "競売で買いますか？",
            f"{name_qty}\n約 {ui.pelica(entry['price'] * qty)} かかります。",
            "買う",
            confirm_fn=fn,
        )
        if reply is None:
            return
        res = cog.service.auction_buy(gid, uid, item_id, qty)
        await _complete_purchase(reply, "競売で買いました", name_qty, res, _item_hint(it.unique))


async def buy_teiai_market(
    cog: "ShopCog",
    interaction: discord.Interaction,
    listing_id: int,
    qty: int = 1,
    *,
    confirm_fn: Optional[Callable] = None,
) -> None:
    """帝愛の期間限定の出品を買う(#72)。闇市品は、買ったあとに本当の中身を知らせる。"""
    fn = confirm_action if confirm_fn is None else confirm_fn
    async with user_session(cog._sessions, interaction, "いまは買えません") as ok:
        if not ok:
            return
        gid = ui.guild_id_of(interaction)
        uid = interaction.user.id
        entry = next((x for x in cog.service.market_list(gid) if x["id"] == listing_id), None)
        if entry is None:
            await ui.reply_error(
                interaction, "その出品は、もう売り切れたか、期間が終わりました。", title="買えませんでした"
            )
            return
        it = shop.BY_ID[entry["item_id"]]
        qty = 1 if it.unique else min(qty, entry["qty"])
        name_qty = f"**{market_label(entry)}**" + (f" ×{qty}" if qty > 1 else "")
        warn = "\n闇市品です。中身は買うまで分からず、返品はできません。" if entry["black"] else ""
        reply = await confirmed_reply(
            interaction,
            entry["black"] or entry["price"] * qty >= cog.CONFIRM_FROM,
            "帝愛の出品を買いますか？",
            f"{name_qty}\n約 {ui.pelica(entry['price'] * qty)} かかります。{warn}",
            "買う",
            confirm_fn=fn,
        )
        if reply is None:
            return
        res = cog.service.market_buy(gid, uid, listing_id, qty)
        if not res.ok:
            await ui.reply_error(interaction, format_failure(res.reason), title="買えませんでした")
            return
        got = shop.BY_ID[res.got]
        if res.outcome == "worse":
            reveal = f"\n😱 鑑定の結果……中身は **{shop.label(got)}** でした。闇市なので、返品はできません。"
        elif res.outcome == "better":
            reveal = f"\n✨ 鑑定の結果……中身はなんと **{shop.label(got)}** でした！"
        elif entry["black"]:
            reveal = "\n🔍 鑑定の結果、表示どおりの品でした。"
        else:
            reveal = ""
        await reply(
            embed=ui.ok_embed(
                "帝愛の出品を買いました",
                f"{name_qty}{reveal}\n残高: **{ui.pelica(res.balance)}**\n{_item_hint(got.unique)}",
            ),
            ephemeral=True,
        )


async def buy_insider_memo(
    cog: "ShopCog", interaction: discord.Interaction, it, *, confirm_fn: Optional[Callable] = None
) -> None:
    """帝愛市の、裏情報メモ。持ち物には増えず、買うとその場で、本人にだけメモが見える。"""
    fn = confirm_action if confirm_fn is None else confirm_fn
    async with user_session(cog._sessions, interaction, "いまは買えません") as ok:
        if not ok:
            return
        gid = ui.guild_id_of(interaction)
        uid = interaction.user.id
        stock = cog.service.insider_stock(gid)
        if stock is None or stock["left"] <= 0:
            why = (
                "いま、売れる情報はありません（次の決算が決まるまで、お待ちください）。"
                if stock is None
                else "この決算の裏情報メモは、売り切れです（サーバーで5枚まで）。"
            )
            await ui.reply_error(interaction, why, title="買えません")
            return
        reply = await confirmed_reply(
            interaction,
            True,
            "購入しますか？",
            f"**{it.name}**\n{ui.pelica(it.price)} かかります（残り {stock['left']}/{stock['total']} 枚）。\n"
            "確度は 80% で、2割はガセです。",
            "買う",
            confirm_fn=fn,
        )
        if reply is None:
            return
        done, result = cog.service.insider_buy(gid, uid)
        if not done:
            await ui.reply_error(interaction, format_failure(result), title="買えませんでした")
            return
        await reply(embed=ui.embed("🕵️ 裏情報メモ（帝愛市）", format_memo(result), ui.DARK), ephemeral=True)


async def buy_item(
    cog: "ShopCog", interaction: discord.Interaction, item: str, qty: int = 1, *, confirm_fn: Optional[Callable] = None
) -> None:
    """景品を買う。item は ID か名前。消耗品だけ qty 個（1〜MAX_QTY）まとめて買える。"""
    fn = confirm_action if confirm_fn is None else confirm_fn
    it = shop.find(item)
    if it is None:
        await ui.reply_error(interaction, "`/ショップ` の画面で選んでください。", title="景品が見つかりません")
        return
    if it.limited:
        await ui.reply_error(
            interaction,
            f"**{it.name}** は買えない限定称号です。毎週月曜0時の週間ランキングで、1位の人に贈られます。",
            title="買えません",
        )
        return
    if it.id == shop.INSIDER_MEMO:
        await buy_insider_memo(cog, interaction, it, confirm_fn=fn)
        return
    async with user_session(cog._sessions, interaction, "いまは買えません") as ok:
        if not ok:
            return
        gid = ui.guild_id_of(interaction)
        uid = interaction.user.id
        if it.unique:
            qty = 1
        pre = cog.service.precheck_buy(gid, uid, it, qty)
        if not pre.ok:
            if pre.status == "owned":
                await ui.reply_error(
                    interaction, "すでに持っています。「🎒 持ち物」の画面で身につけられます。", title="買えませんでした"
                )
            elif pre.status == "out_of_stock":
                assert pre.stock is not None
                await ui.reply_error(
                    interaction,
                    f"**{it.name}** の在庫は、いま {pre.stock['stock']}個 です（{stock_text(pre.stock)}）。\n"
                    "在庫は1人ずつ別々で、時間がたつと補充されます。",
                    title="在庫が足りません",
                )
            else:
                await ui.reply_error(
                    interaction,
                    f"ペリカが足りません（{pre.total:,}ペリカ必要・残高 {pre.balance:,}ペリカ）。\n`/ペリカ デイリー` で6時間ごとにもらえます。",
                    title="買えませんでした",
                )
            return
        name_qty = f"**{shop.label(it)}**" + (f" ×{qty}" if qty > 1 else "")
        reply = await confirmed_reply(
            interaction,
            pre.total >= cog.CONFIRM_FROM,
            "購入しますか？",
            f"{name_qty}\n{ui.pelica(pre.total)} かかります。",
            "買う",
            confirm_fn=fn,
        )
        if reply is None:
            return
        res = cog.service.purchase(gid, uid, it, qty)
        await _complete_purchase(reply, "購入しました", name_qty, res, _item_hint(it.unique))


async def use_item(cog: "ShopCog", interaction: discord.Interaction, item: str) -> None:
    """景品を身につける・使う。item は ID か名前（持っているものを優先する）。"""
    gid = ui.guild_id_of(interaction)
    uid = interaction.user.id
    inv = cog.service.inventory(gid, uid)
    owned = [i for i in shop.find_all(item) if inv.get(i.id, 0) > 0]
    it = owned[0] if owned else shop.find(item)
    if it is None or inv.get(it.id, 0) <= 0:
        await ui.reply_error(interaction, "「🎒 持ち物」の画面で確認できます。", title="持っていません")
        return
    if it.id in shop.AUTO_USED:
        note = {
            shop.STREAK_GUARD: "持っているだけで、デイリーを受け取れなかった日があったとき、次のデイリーで自動で使われます（1枚で1日ぶん）。",
            shop.HORSE_VOUCHER: "`/競馬 厩舎` で使います（名前と見た目を決めて、競走馬を迎えます）。",
            shop.CARROT: "`/競馬 厩舎` で使います（馬のステータスを、5ポイント動かします）。",
            shop.GOLD_CARROT: "`/競馬 厩舎` で使います（馬のステータスの合計を、10ポイント増やします）。",
            shop.LABOR_DRINK: "`/地下 作業` の画面の「🥤 栄養ドリンク」で飲みます（疲労を60回復します）。",
        }[it.id]
        await ui.reply_ok(interaction, f"**{it.name}**: {note}", title="ここでは使いません")
        return
    if it.kind == shop.CONSUMABLE:
        if cog._sessions.is_active(uid):
            await ui.reply_error(
                interaction, "ゲームの途中は、消耗品を使えません。終わってからにしてください。", title="使えません"
            )
            return
        active = cog.service.arm_consumable(gid, uid, it.id)
        if active is None:
            await ui.reply_error(interaction, "持っていません。", title="使えません")
            return
        await ui.reply_ok(interaction, f"**{it.name}**: {it.desc}\n有効な枚数: **{active}枚**", title="使いました")
        return
    if it.kind == shop.RELIC:
        await ui.reply_ok(
            interaction,
            f"**{it.name}** は、使う品ではありません。{relics.buff_text(it.id)}（出品している間は、効きません）。\n"
            "競売の画面の「📤 出品する」で、売れます。",
            title="発掘品です",
        )
        return
    if it.kind == shop.COLLECTION:
        await ui.reply_ok(
            interaction,
            f"**{shop.label(it)}** は、持っているだけで `/ペリカ 残高` に飾られます（装備はありません）。",
            title="コレクションです",
        )
        return
    if it.kind == shop.COLOR_ROLE:
        await interaction.response.defer(ephemeral=True)
        guild = cast(discord.Guild, interaction.guild)
        member = cast(discord.Member, interaction.user)
        try:
            async with cog._role_lock(gid):
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
    cog.service.equip(gid, uid, SLOT_OF_KIND[it.kind], it.id)
    note = ""
    if it.kind == shop.TITLE:
        await interaction.response.defer(ephemeral=True)
        guild = cast(discord.Guild, interaction.guild)
        member = cast(discord.Member, interaction.user)
        problem, others = await cog.sync_title_nick(uid, guild, member)
        if problem:
            note = f"\n⚠️ ニックネームには反映できませんでした: {problem}"
        elif cog._guild_repo.nick_pref(gid, uid):
            note = "\nニックネームにも反映しました。"
        if others:
            note += f"\n※ 他のサーバー {others} 件では反映できませんでした。"
        await interaction.followup.send(
            embed=ui.ok_embed("装備しました", f"**{shop.label(it)}**\n`/ペリカ 残高` で確認できます。{note}"),
            ephemeral=True,
        )
        return
    await ui.reply_ok(interaction, f"**{shop.label(it)}**\n`/ペリカ 残高` で確認できます。", title="装備しました")


async def unequip_item(cog: "ShopCog", interaction: discord.Interaction, slot: str) -> None:
    """slot（UNEQUIP_NAMES のキー）の装備を外す。"""
    name = UNEQUIP_NAMES[slot]
    guild = cast(discord.Guild, interaction.guild)
    member = cast(discord.Member, interaction.user)
    gid = ui.guild_id_of(interaction)
    if slot == "role":
        await interaction.response.defer(ephemeral=True)
        try:
            async with cog._role_lock(gid):
                await set_color_role(guild, member, None)
        except ShopError as exc:
            await interaction.followup.send(embed=ui.error_embed("外せません", str(exc)), ephemeral=True)
            return
        await interaction.followup.send(embed=ui.ok_embed("外しました", "名前の色を元に戻しました。"), ephemeral=True)
        return
    cog.service.equip(gid, interaction.user.id, slot, None)
    if slot == "title":
        await interaction.response.defer(ephemeral=True)
        problem, others = await cog.sync_title_nick(interaction.user.id, guild, member)
        note = f"\n⚠️ ニックネームを戻せませんでした: {problem}" if problem else ""
        if others:
            note += f"\n※ 他のサーバー {others} 件では戻せませんでした。"
        await interaction.followup.send(embed=ui.ok_embed("外しました", f"{name}を外しました。{note}"), ephemeral=True)
        return
    await ui.reply_ok(interaction, f"{name}を外しました。", title="外しました")


async def set_title_display(cog: "ShopCog", interaction: discord.Interaction, mode: str) -> None:
    """称号をニックネームに付ける（mode="on"）／付けない（"off"）。"""
    uid = interaction.user.id
    guild = cast(discord.Guild, interaction.guild)
    member = cast(discord.Member, interaction.user)
    gid = ui.guild_id_of(interaction)
    guild_repo = cog._guild_repo
    was_on = guild_repo.nick_pref(gid, uid)
    if mode == "on":
        reason = nick_blocker(guild, member)
        if reason:
            await ui.reply_error(interaction, reason, title="ニックネームに付けられません")
            return
        guild_repo.set_nick_pref(gid, uid, True)
        await interaction.response.defer(ephemeral=True)
        problem, _others = await cog.sync_title_nick(uid, guild, member)
        title = cog.service.equipped_title(gid, uid)
        if problem:
            if not was_on:
                guild_repo.set_nick_pref(gid, uid, False)
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
            await apply_title_nick(guild, member, None, guild_repo)
        except ShopError as exc:
            problem = f"\n⚠️ ニックネームを元に戻せませんでした（手動で直してください）: {exc}"
        guild_repo.set_nick_pref(gid, uid, False)
    await interaction.followup.send(
        embed=ui.ok_embed(
            "ニックネームには付けません",
            f"称号の表示をやめて、ニックネームを元に戻しました。{problem}" if was_on else "もともと付けていません。",
        ),
        ephemeral=True,
    )
