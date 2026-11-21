"""現物の売買（買い・売り）。"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Optional

import discord

from cogs.market import orders
from cogs.market.embeds import spot_position_text, tax_line
from cogs.shared import ui
from cogs.shared.messages import format_failure
from domain import market

from .common import CONFIRM_FROM, run_trade, units_from, worth

if TYPE_CHECKING:
    from cogs.market.cog import MarketCog


async def buy_spot(
    cog: "MarketCog",
    interaction: discord.Interaction,
    symbol: str,
    amount: int,
    *,
    confirm_fn: Optional[Callable] = None,
) -> None:
    """現物の購入（手数料なし・小数で買える）。amount ペリカの範囲で買えるだけ買う。"""
    sym = market.BY_KEY[symbol]
    fn = orders.default_confirmed_reply if confirm_fn is None else confirm_fn

    async def run(uid: int) -> None:
        gid = ui.guild_id_of(interaction)
        price = cog._market_svc.latest_price(sym.key)
        balance = cog._economy_svc.balance_of(gid, uid)
        if balance < amount:
            await ui.reply_error(
                interaction,
                f"{ui.pelica(amount)} に対して、残高は {ui.pelica(balance)} です。",
                title="ペリカが足りません",
            )
            return
        units = market.buy_quote(amount, price)[0]
        reply = await fn(
            interaction,
            amount >= CONFIRM_FROM,
            "買いますか？",
            f"**{sym.name}** を、いまの価格 **{market.pel(price)}** で、約 **{market.lots(units)}** 買います。\n{ui.pelica(amount)} 以内。価格は動くので、実際の価格で買います。",
            "買う",
        )
        if reply is None:
            return
        price = cog._market_svc.latest_price(sym.key)  # 確認を待つ間に動いた価格で、確定する
        res = cog._market_svc.crypto_buy(gid, uid, amount, price, sym.key)
        if not res.ok:
            await ui.reply_error(interaction, format_failure(res.reason), title="買えませんでした")
            return
        await reply(
            embed=ui.ok_embed(
                f"{sym.name}を買いました",
                f"**{market.lots(res.units)}**（価格 {market.pel(price)}）\n"
                f"支払い: {ui.pelica(res.money)}\n残高: **{ui.pelica(res.balance)}**",
            )
        )

    await run_trade(cog._sessions, interaction, run)


async def sell_spot(
    cog: "MarketCog",
    interaction: discord.Interaction,
    symbol: str,
    percent: Optional[int] = None,
    *,
    confirm_fn: Optional[Callable] = None,
) -> None:
    """現物の売却。percent（1〜100）で割合を決める。省略すると全部。"""
    sym = market.BY_KEY[symbol]
    fn = orders.default_confirmed_reply if confirm_fn is None else confirm_fn

    async def run(uid: int) -> None:
        gid = ui.guild_id_of(interaction)
        have, cost = cog._market_svc.crypto_holding(gid, uid, sym.key)
        if have <= 0:
            await ui.reply_error(
                interaction, f"{sym.name}を持っていません。`/証券 注文 現物` で買えます。", title="売れません"
            )
            return
        units = units_from(have, percent)
        price = cog._market_svc.latest_price(sym.key)
        shown = market.lots(have if units is None else units)
        reply = await fn(
            interaction,
            worth(have, units, price) >= CONFIRM_FROM,
            "売りますか？",
            f"**{shown}** を売ります。\n\n{spot_position_text(have, cost, price)}",
            "売る",
        )
        if reply is None:
            return
        price = cog._market_svc.latest_price(sym.key)
        res = cog._market_svc.crypto_sell(gid, uid, units, price, sym.key)
        if not res.ok:
            await ui.reply_error(interaction, format_failure(res.reason), title="売れませんでした")
            return
        left, left_cost = cog._market_svc.crypto_holding(gid, uid, sym.key)
        pct = market.pnl_percent(price, have, cost)  # 売った分の、取得価格に対する増減
        rest = f"\n\n残りの持ち分\n{spot_position_text(left, left_cost, price)}" if left > 0 else "\n全部売りました。"
        await reply(
            embed=ui.embed(
                f"{sym.name}を売りました",
                f"**{market.lots(res.units)}**（価格 {market.pel(price)}）\n"
                f"取得 {market.fmt(market.avg_price(have, cost))} → 売値 {market.fmt(price)}　{'🔺' if pct >= 0 else '🔻'}**{pct:+.1f}%**\n"
                f"受け取り: {ui.pelica(res.money)}\n"
                f"買った額との差: **{res.profit:+,}ペリカ**\n{tax_line(res)}残高: **{ui.pelica(res.balance)}**{rest}",
                ui.OK if res.profit >= 0 else ui.ERROR,
            )
        )

    await run_trade(cog._sessions, interaction, run)
