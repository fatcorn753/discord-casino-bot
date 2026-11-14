"""ショップの Discord ロール・ニックネーム操作。"""

import discord

from core import nick, shop, teiai


class ShopError(Exception):
    """利用者に伝える形のエラー（メッセージはそのまま画面に出す）。"""


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


async def apply_title_nick(guild: discord.Guild, member: discord.Member, title: str | None, eco) -> None:
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
