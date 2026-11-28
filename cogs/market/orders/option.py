"""オプション（コール/プット/ペア）の買い・裸売り・途中決済。"""

import math
from collections.abc import Callable
from typing import TYPE_CHECKING

import discord

from cogs.market import orders
from cogs.market.bond_embeds import due_text
from cogs.market.embeds import option_line, option_target_text
from cogs.shared import ui
from cogs.shared.messages import format_failure
from domain import market
from domain import options as opt
from domain.errors import Failure

from .common import CONFIRM_FROM, run_trade

if TYPE_CHECKING:
    from cogs.market.cog import MarketCog


async def option_buy(
    cog: "MarketCog",
    interaction: discord.Interaction,
    kind: str,
    symbol: str,
    amount: int,
    symbol2: str | None = None,
    target: int | None = None,
    *,
    confirm_fn: Callable | None = None,
) -> None:
    """爆騰コール・暴落プット・ペア・ワラントを買う。満期は6時間後。損失は掛け金だけ。"""
    sym, (name, emoji, _hint) = market.BY_KEY[symbol], opt.KINDS[kind]
    pair = kind == opt.PAIR
    if pair and (symbol2 is None or symbol2 == symbol or symbol2 not in market.BY_KEY):
        await ui.reply_error(
            interaction,
            "「銘柄」と、ちがう銘柄を「相手」に選んでください。",
            title="ペア・ワラントは、2つの銘柄が必要です",
        )
        return
    other = market.BY_KEY[symbol2] if pair and symbol2 is not None else None
    title_sym = f"{sym.name} vs {other.name}" if other is not None else sym.name
    seconds = opt.OPTION_HOURS * 3600
    strike_pct = target / 100 if target else opt.default_strike_pct(symbol)
    fn = orders.default_confirmed_reply if confirm_fn is None else confirm_fn

    def quote(price: float) -> tuple:
        now = cog._market_svc.clock.now_ts()
        events = cog._market_svc.option_events(now)
        if pair and symbol2 is not None:
            rate = opt.pair_ask_rate(sym.key, symbol2, seconds, now, events)
            units = int(amount / rate) if rate > 0 else 0
            return price, units, math.ceil(round(units * rate, 6))
        strike = opt.strike_for(kind, price, strike_pct)
        per = opt.ask_per_unit(kind, sym.key, price, strike, seconds, now, events)
        units = int(amount / per) if per > 0 else 0
        return strike, units, math.ceil(round(units * per, 6))

    def target_text(price: float, strike: float, units: int, cost: int) -> str:
        return option_target_text(kind, sym, other, strike_pct, price, strike, units, cost)

    async def run(uid: int) -> None:
        gid = ui.guild_id_of(interaction)
        price = cog._market_svc.latest_price(sym.key)
        strike, units, cost = quote(price)
        if units < 1:
            await ui.reply_error(
                interaction, "その掛け金では 1口も買えません。掛け金を増やしてください。", title=f"{name}を買えません"
            )
            return
        reply = await fn(
            interaction,
            cost >= CONFIRM_FROM,
            f"{name}を買いますか？",
            f"{emoji} **{title_sym}**（{opt.OPTION_HOURS}時間満期）\n掛け金 **{ui.pelica(cost)}**（途中のロスカットはなく、最大の損失はこの掛け金だけ）\n"
            + target_text(price, strike, units, cost),
            f"{name}を買う",
        )
        if reply is None:
            return
        res_ok, res = cog._market_svc.option_buy(
            gid, uid, kind, sym.key, amount, symbol2=symbol2, strike_pct=None if pair else strike_pct
        )
        if not res_ok:
            await ui.reply_error(interaction, format_failure(res), title=f"{name}を買えませんでした")
            return
        assert not isinstance(res, Failure)
        await reply(
            embed=ui.ok_embed(
                f"{emoji} 【{name}】{title_sym}（{opt.OPTION_HOURS}時間満期）",
                f"掛け金: **{ui.pelica(res['cost'])}**（★途中ロスカットなし・最大損失はこの掛け金のみ）\n"
                + target_text(res["spot"], res["strike"], res["units"], res["cost"])
                + f"\n満期: {due_text(res['expiry'])}（満期の価格で自動で決済されます）\n"
                "満期の前でも、`/証券 保有` のパネルか `/証券 決済` で、いまの値段で売れます。",
            )
        )

    await run_trade(cog._sessions, interaction, run)


async def option_write(
    cog: "MarketCog",
    interaction: discord.Interaction,
    kind: str,
    symbol: str,
    amount: int,
    target: int | None = None,
    *,
    confirm_fn: Callable | None = None,
) -> None:
    """コール・プットの裸売り。帝愛に売って、プレミアムを先にもらう。証拠金（行使価格の20%）を預け、満期に損失を払う。"""
    sym, label = market.BY_KEY[symbol], opt.SHORT_NAMES[kind]
    sign = 1 if kind == opt.CALL else -1
    seconds = opt.OPTION_HOURS * 3600
    strike_pct = target / 100 if target else opt.default_strike_pct(symbol)
    fn = orders.default_confirmed_reply if confirm_fn is None else confirm_fn

    def quote(price: float) -> tuple:
        now = cog._market_svc.clock.now_ts()
        strike = opt.strike_for(kind, price, strike_pct)
        units = int(amount / opt.margin_per_unit(strike))
        bid = opt.bid_per_unit(kind, sym.key, price, strike, seconds, now, cog._market_svc.option_events(now))
        return strike, units, math.ceil(round(units * opt.margin_per_unit(strike), 6)), int(bid * units)

    async def run(uid: int) -> None:
        gid = ui.guild_id_of(interaction)
        price = cog._market_svc.latest_price(sym.key)
        strike, units, margin, premium = quote(price)
        if units < 1:
            await ui.reply_error(
                interaction, "その証拠金では 1口も売れません。証拠金を増やしてください。", title=f"{label}できません"
            )
            return
        m1, m2 = (0.10, 0.20) if strike_pct < 0.20 else (round(strike_pct + 0.05, 2), round(strike_pct + 0.15, 2))
        loss1 = opt.intrinsic(kind, units, price * (1 + sign * m1), strike)
        loss2 = opt.intrinsic(kind, units, price * (1 + sign * m2), strike)
        cond = "を超えて上がる" if kind == opt.CALL else "を下回る"
        reply = await fn(
            interaction,
            True,
            f"{label}しますか？（損失が証拠金を超えると借金になります）",
            f"**{sym.name}** の{'コール' if kind == opt.CALL else 'プット'}を、帝愛に売ります（{opt.OPTION_HOURS}時間満期）。\n"
            f"今すぐ受け取る **プレミアム {ui.pelica(premium)}**　預ける **証拠金 {ui.pelica(margin)}**\n"
            f"**{market.fmt(strike)}**（現在の{sign * strike_pct * 100:+.1f}%）{cond}と、その差が**あなたの損失**です。"
            f"価格が{sign * round(m1 * 100):+d}%で損失 約{loss1:,}ペリカ、{sign * round(m2 * 100):+d}%で約{loss2:,}ペリカ。"
            "**証拠金を超えた分は、帝愛の借金になります。**相場が動かなければ、プレミアムが丸ごと利益です。"
            "途中で買い戻して閉じることもできます。",
            f"{label}する",
        )
        if reply is None:
            return
        ok, res = cog._market_svc.option_write(gid, uid, kind, sym.key, amount, strike_pct=strike_pct)
        if not ok:
            await ui.reply_error(interaction, format_failure(res), title=f"{label}できませんでした")
            return
        assert not isinstance(res, Failure)
        await reply(
            embed=ui.ok_embed(
                f"🎰 【{label}】{sym.name}（{opt.OPTION_HOURS}時間満期）",
                f"プレミアム **{ui.pelica(res['premium'])}** を受け取りました。証拠金 **{ui.pelica(res['margin'])}** を預けています。\n"
                f"満期の価格が **{market.fmt(res['strike'])}** を{'超えない' if kind == opt.CALL else '下回らない'}かぎり、損失はありません。\n"
                f"満期: {due_text(res['expiry'])}（自動で決済。損失は証拠金から払い、超えた分は借金）\n"
                "満期の前でも、`/証券 保有` のパネルか `/証券 決済` で、買い戻して閉じられます。",
            )
        )

    await run_trade(cog._sessions, interaction, run)


async def option_sell(
    cog: "MarketCog",
    interaction: discord.Interaction,
    option_id: int,
    *,
    confirm_fn: Callable | None = None,
) -> None:
    """オプションを、満期の前に手放す。買いはいまの値段（売値）で売り、裸売りは買値で買い戻す。"""
    fn = orders.default_confirmed_reply if confirm_fn is None else confirm_fn

    async def run(uid: int) -> None:
        gid = ui.guild_id_of(interaction)
        cog._market_svc.option_settle(gid, cog._market_svc.clock.now_ts())
        row = next((r for r in cog._market_svc.active_options(gid, uid) if r["id"] == option_id), None)
        if row is None:
            await ui.reply_error(interaction, "その番号のオプションは持っていません。", title="売れません")
            return
        short, prices, now = row["side"] == "short", cog._market_svc.latest_prices(), cog._market_svc.clock.now_ts()
        if short:
            cost = cog._market_svc.option_close_cost(gid, row, now, prices)
            text = (
                f"{option_line(row, prices)}\n買い戻しの費用 **{ui.pelica(cost)}**（預けた証拠金 {ui.pelica(row['margin'])}）。"
                + (
                    f"\n⚠️ 費用が証拠金を **{cost - row['margin']:,}ペリカ** 超えるので、その分は借金になります。"
                    if cost > row["margin"]
                    else f"証拠金から払って、**{ui.pelica(row['margin'] - cost)}** が戻ります。"
                )
            )
            needs = cost >= CONFIRM_FROM or cost > row["margin"]
        else:
            value = cog._market_svc.option_value(gid, row, now, prices)
            text = f"{option_line(row, prices)}\nいま売ると **{ui.pelica(value)}** です（掛け金 {ui.pelica(row['premium'])}）。"
            needs = value >= CONFIRM_FROM
        reply = await fn(
            interaction,
            needs,
            "オプションを買い戻しますか？" if short else "オプションを売りますか？",
            text,
            "買い戻す" if short else "売る",
        )
        if reply is None:
            return
        ok, got = cog._market_svc.option_sell(gid, uid, option_id)
        if not ok:
            await ui.reply_error(interaction, format_failure(got), title="できませんでした")
            return
        assert not isinstance(got, Failure)
        if short:
            pnl = row["premium"] + got - row["margin"]  # got は、証拠金の戻り（マイナスなら、借金にした額）
            body = f"買い戻しました。証拠金の戻り: **{ui.pelica(max(0, got))}**" + (
                f"\n借金にした額: **{ui.pelica(-got)}**" if got < 0 else ""
            )
            body += f"\n受け取ったプレミアム {ui.pelica(row['premium'])} と合わせた損益: **{pnl:+,}ペリカ**"
            good = pnl >= 0
        else:
            body = f"受け取り: **{ui.pelica(got)}**（掛け金との差 {got - row['premium']:+,}ペリカ）"
            good = got >= row["premium"]
        body += f"\n残高: **{ui.pelica(cog._economy_svc.balance_of(gid, uid))}**"
        await reply(
            embed=ui.embed("買い戻しました" if short else "オプションを売りました", body, ui.OK if good else ui.ERROR)
        )

    await run_trade(cog._sessions, interaction, run)
