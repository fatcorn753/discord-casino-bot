"""信用取引（空売り・信用買い）の建てと決済。"""

from collections.abc import Callable
from typing import TYPE_CHECKING

import discord

from cogs.market import orders
from cogs.market.embeds import margin_position_text, margin_ratio_text, tax_line
from cogs.shared import ui
from cogs.shared.messages import format_failure
from domain import market

from .common import CONFIRM_FROM, MAX_MARGIN_NOTIONAL, margin_quote, run_trade, units_from, worth

if TYPE_CHECKING:
    from cogs.market.cog import MarketCog


async def margin_close(
    cog: "MarketCog",
    interaction: discord.Interaction,
    side: str,
    symbol: str,
    percent: int | None = None,
    *,
    confirm_fn: Callable | None = None,
) -> None:
    """信用取引の決済（空売りは買い戻し、信用買いは売り戻し）。percent（1〜100）で割合を決める。省略すると全部。"""
    sym, label = market.BY_KEY[symbol], market.SIDE_NAME[side]
    verb = "買い戻し" if side == "short" else "売り戻し"
    cannot = "買い戻せません" if side == "short" else "売り戻せません"
    fn = orders.default_confirmed_reply if confirm_fn is None else confirm_fn

    async def run(uid: int) -> None:
        gid = ui.guild_id_of(interaction)
        have, entry_value, margin = cog._market_svc.margin_position(gid, uid, sym.key, side)
        if have <= 0:
            await ui.reply_error(
                interaction, f"{sym.name}の{label}を建てていません。`/証券 注文 信用` で建てられます。", title=cannot
            )
            return
        units = units_from(have, percent)
        price = cog._market_svc.latest_price(sym.key)
        shown = market.lots(have if units is None else units)
        reply = await fn(
            interaction,
            worth(have, units, price) >= CONFIRM_FROM,
            f"{verb}ますか？",
            f"**{shown}** を{verb}て、{label}を閉じます。\n\n{margin_position_text(side, have, entry_value, margin, price)}",
            f"{verb}る",
        )
        if reply is None:
            return
        price = cog._market_svc.latest_price(sym.key)
        res = cog._market_svc.margin_close(gid, uid, side, units, price, sym.key)
        if not res.ok:
            await ui.reply_error(interaction, format_failure(res.reason), title=f"{cannot[:-2]}ませんでした")
            return
        left, left_entry, left_margin = cog._market_svc.margin_position(gid, uid, sym.key, side)
        rest = (
            f"\n\n残りの建玉\n{margin_position_text(side, left, left_entry, left_margin, price)}"
            if left > 0
            else f"\n{label}を全部閉じました。"
        )
        debt = cog._teiai.debt_of(gid, uid)
        owed = (
            f"\n⚠️ 担保を超える損失は、手元の資産から取り立てられました（借金 {ui.pelica(debt)}）。" if debt > 0 else ""
        )
        await reply(
            embed=ui.embed(
                f"{sym.name}を{verb}ました（{label}の決済）",
                f"**{market.lots(res.units)}**（価格 {market.pel(price)}）\n"
                f"戻ってきた額: {ui.pelica(res.money)}（担保 + 損益）\n損益: **{res.profit:+,}ペリカ**\n{tax_line(res)}残高: **{ui.pelica(res.balance)}**{owed}{rest}",
                ui.OK if res.profit >= 0 else ui.ERROR,
            )
        )

    await run_trade(cog._sessions, interaction, run)


async def margin_order(
    cog: "MarketCog",
    interaction: discord.Interaction,
    side: str,
    symbol: str,
    collateral: int,
    leverage: int = 1,
    *,
    confirm_fn: Callable | None = None,
) -> None:
    """信用取引（空売り＝売り建て、信用買い＝買い建て）。collateral（預ける証拠金）× レバレッジ の総額まで建てる(#78)。

    口数は価格で割り切れない分を切り捨てるので、実際の担保は collateral 以下になる（入れた額より多くは引かれない）。
    """
    label = market.SIDE_NAME[side]
    sym = market.BY_KEY[symbol]
    fn = orders.default_confirmed_reply if confirm_fn is None else confirm_fn
    target_notional = collateral * leverage

    async def refuse(text: str) -> None:
        await ui.reply_error(interaction, text, title=f"{label}できません")

    async def run(uid: int) -> None:
        gid = ui.guild_id_of(interaction)
        if target_notional > MAX_MARGIN_NOTIONAL:
            await refuse(
                f"建てる総額（証拠金 × レバレッジ）は、1回に {ui.pelica(MAX_MARGIN_NOTIONAL)} までです。"
                f"いまは {ui.pelica(target_notional)} です。証拠金を減らすか、レバレッジを下げてください。"
            )
            return
        price = cog._market_svc.latest_price(sym.key)
        units, notional, margin = margin_quote(collateral, leverage, price)
        if units < 1:
            await refuse(
                "その証拠金とレバレッジでは 0.001枚も建てられません。証拠金を増やすか、レバレッジを上げてください。"
            )
            return
        if margin < market.SHORT_MIN_MARGIN:
            # 高い銘柄では切り捨てで担保が最低額を割ることがある。確認の前に、証拠金の言葉で断る
            await refuse(
                f"価格で割り切れない分を切り捨てると、担保が {market.SHORT_MIN_MARGIN}ペリカ 未満になります。証拠金を増やしてください。"
            )
            return
        direction = "売り建て" if side == "short" else "買い建て"
        gain = "下がると利益、上がると損失" if side == "short" else "上がると利益、下がると損失"
        reply = await fn(
            interaction,
            notional >= CONFIRM_FROM,  # 確認を挟むかは、建てる総額で決める（証拠金ではない）
            f"{label}しますか？",
            f"**{sym.name}** を、いまの価格 **{market.pel(price)}** で{direction}ます。\n"
            f"証拠金 {ui.pelica(collateral)} × レバレッジ **{leverage}倍** → 建玉総額 約 {ui.pelica(target_notional)}"
            f"（実際は **{market.lots(units)}**・{ui.pelica(int(notional))}）\n"
            f"預ける担保 **{ui.pelica(margin)}**（価格で割り切れない分は切り捨てるので、証拠金以下）\n"
            f"価格が{gain}です。口座ぜんたいの余力が {market.LOSSCUT_RATIO:.0%} を下回ると、損の大きい建玉から強制決済されます。",
            f"{label}する",
        )
        if reply is None:
            return
        price = cog._market_svc.latest_price(sym.key)
        units = margin_quote(collateral, leverage, price)[0]  # 確認を待つ間に動いた価格で、確定する
        res = cog._market_svc.margin_open(gid, uid, side, units, price, leverage, sym.key)
        if not res.ok:
            await ui.reply_error(interaction, format_failure(res.reason), title=f"{label}できませんでした")
            return
        have, entry_value, margin_all = cog._market_svc.margin_position(gid, uid, sym.key, side)
        await reply(
            embed=ui.ok_embed(
                f"{sym.name}を{label}しました",
                f"**{market.lots(res.units)}**（価格 {market.pel(price)}・{leverage}倍）\n"
                f"預けた担保: {ui.pelica(res.money)}\n残高: **{ui.pelica(res.balance)}**\n\n{margin_position_text(side, have, entry_value, margin_all, price)}\n\n"
                f"{margin_ratio_text(cog._market_svc.margin_status(gid, uid, cog._market_svc.latest_prices()))}\n"
                "閉じるには `/証券 保有` のパネルか、`/証券 決済` を使います。",
            )
        )

    await run_trade(cog._sessions, interaction, run)
