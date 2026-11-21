"""`/証券` の注文・決済フロー（cogs/market/cog.py から使う）。

現物・信用・オプション・債券の売買実行と確認ダイアログの進行を受け持つ（読み込まれる Cog ではない）。
"""

import math
import random
from typing import TYPE_CHECKING, Callable, Optional

import discord

from cogs.market.bond_embeds import due_text
from cogs.market.embeds import (
    bond_buy_embed,
    margin_position_text,
    margin_ratio_text,
    option_line,
    option_target_text,
    spot_position_text,
    tax_line,
)
from cogs.shared import ui
from cogs.shared.confirm import confirm_action, confirmed_reply, user_session
from cogs.shared.messages import format_failure
from domain import bonds, market
from domain import options as opt
from domain.errors import Failure
from domain.ports import get_catalogs
from domain.ports.session import ISessionGuard

if TYPE_CHECKING:
    from cogs.market.cog import MarketCog

RNG = random.SystemRandom()
CONFIRM_FROM = get_catalogs().market().market.confirm_from  # この額以上の売買は確認ボタンを挟む
MAX_MARGIN_NOTIONAL = 10_000_000  # 信用取引の、1回に建てられる総額の上限(#78)


def margin_quote(collateral: int, leverage: int, price: float) -> tuple[int, float, int]:
    """証拠金 × レバレッジ の総額で建てられる (口数, 実際の総額, 担保)。担保の式は repo（infra/db/margin.py）と同じ。"""
    units = int(collateral * leverage * market.UNIT // price)
    notional = units * price / market.UNIT
    return units, notional, math.ceil(round(notional / leverage, 6))


async def default_confirmed_reply(interaction: discord.Interaction, needed: bool, title: str, text: str, label: str):
    """大きな取引は、確認ボタンを出す。進んでよければ「返信に使う関数」を返し、やめたなら None を返す。"""
    return await confirmed_reply(interaction, needed, title, text, label, confirm_fn=confirm_action)


def units_from(have: int, percent: Optional[int]) -> Optional[int]:
    """売る・買い戻す口数。percent（1〜100）ならその割合（端数でも最低1口）、省略か100以上なら None（全部）。"""
    if percent is not None and 0 < percent < 100:
        return max(1, have * percent // 100)
    return None


def worth(have: int, units: Optional[int], price: float) -> float:
    """売買する分（units が None なら全部）の、いまの価格での金額。確認を挟むかの判定に使う。"""
    return (have if units is None else min(units, have)) * price // market.UNIT


async def run_trade(sessions: ISessionGuard, interaction: discord.Interaction, action) -> None:
    """売買（action(uid)）の間は「プレイ中」にして、ゲームの最中に残高を動かせないようにする。"""
    async with user_session(sessions, interaction, "いまは取引できません") as ok:
        if ok:
            await action(interaction.user.id)


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
    fn = default_confirmed_reply if confirm_fn is None else confirm_fn

    async def run(uid: int) -> None:
        gid = ui.guild_id_of(interaction)
        price = cog._market_svc.latest_price(sym.key)
        balance = cog._economy_svc.balance_of(gid, uid)
        if balance < amount:
            await interaction.response.send_message(
                embed=ui.error_embed(
                    "ペリカが足りません", f"{ui.pelica(amount)} に対して、残高は {ui.pelica(balance)} です。"
                ),
                ephemeral=True,
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
            await reply(embed=ui.error_embed("買えませんでした", format_failure(res.reason)), ephemeral=True)
            return
        await reply(
            embed=ui.ok_embed(
                f"{sym.name}を買いました",
                f"**{market.lots(res.units)}**（価格 {market.pel(price)}）\n"
                f"支払い: {ui.pelica(res.money)}\n残高: **{ui.pelica(res.balance)}**",
            )
        )

    await run_trade(cog._sessions, interaction, run)


async def margin_close(
    cog: "MarketCog",
    interaction: discord.Interaction,
    side: str,
    symbol: str,
    percent: Optional[int] = None,
    *,
    confirm_fn: Optional[Callable] = None,
) -> None:
    """信用取引の決済（空売りは買い戻し、信用買いは売り戻し）。percent（1〜100）で割合を決める。省略すると全部。"""
    sym, label = market.BY_KEY[symbol], market.SIDE_NAME[side]
    verb = "買い戻し" if side == "short" else "売り戻し"
    cannot = "買い戻せません" if side == "short" else "売り戻せません"
    fn = default_confirmed_reply if confirm_fn is None else confirm_fn

    async def run(uid: int) -> None:
        gid = ui.guild_id_of(interaction)
        have, entry_value, margin = cog._market_svc.margin_position(gid, uid, sym.key, side)
        if have <= 0:
            await interaction.response.send_message(
                embed=ui.error_embed(
                    cannot, f"{sym.name}の{label}を建てていません。`/証券 注文 信用` で建てられます。"
                ),
                ephemeral=True,
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
            await reply(embed=ui.error_embed(f"{cannot[:-2]}ませんでした", format_failure(res.reason)), ephemeral=True)
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
    fn = default_confirmed_reply if confirm_fn is None else confirm_fn

    async def run(uid: int) -> None:
        gid = ui.guild_id_of(interaction)
        have, cost = cog._market_svc.crypto_holding(gid, uid, sym.key)
        if have <= 0:
            await interaction.response.send_message(
                embed=ui.error_embed("売れません", f"{sym.name}を持っていません。`/証券 注文 現物` で買えます。"),
                ephemeral=True,
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
            await reply(embed=ui.error_embed("売れませんでした", format_failure(res.reason)), ephemeral=True)
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


async def margin_order(
    cog: "MarketCog",
    interaction: discord.Interaction,
    side: str,
    symbol: str,
    collateral: int,
    leverage: int = 1,
    *,
    confirm_fn: Optional[Callable] = None,
) -> None:
    """信用取引（空売り＝売り建て、信用買い＝買い建て）。collateral（預ける証拠金）× レバレッジ の総額まで建てる(#78)。

    口数は価格で割り切れない分を切り捨てるので、実際の担保は collateral 以下になる（入れた額より多くは引かれない）。
    """
    label = market.SIDE_NAME[side]
    sym = market.BY_KEY[symbol]
    fn = default_confirmed_reply if confirm_fn is None else confirm_fn
    target_notional = collateral * leverage

    async def refuse(text: str) -> None:
        await interaction.response.send_message(embed=ui.error_embed(f"{label}できません", text), ephemeral=True)

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
            f"価格が{gain}です。口座ぜんたいの維持率が {market.LOSSCUT_RATIO:.0%} を下回ると、損の大きい建玉から強制決済されます。",
            f"{label}する",
        )
        if reply is None:
            return
        price = cog._market_svc.latest_price(sym.key)
        units = margin_quote(collateral, leverage, price)[0]  # 確認を待つ間に動いた価格で、確定する
        res = cog._market_svc.margin_open(gid, uid, side, units, price, leverage, sym.key)
        if not res.ok:
            await reply(embed=ui.error_embed(f"{label}できませんでした", format_failure(res.reason)), ephemeral=True)
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


async def option_buy(
    cog: "MarketCog",
    interaction: discord.Interaction,
    kind: str,
    symbol: str,
    amount: int,
    symbol2: Optional[str] = None,
    target: Optional[int] = None,
    *,
    confirm_fn: Optional[Callable] = None,
) -> None:
    """爆騰コール・暴落プット・ペア・ワラントを買う。満期は6時間後。損失は掛け金だけ。"""
    sym, (name, emoji, _hint) = market.BY_KEY[symbol], opt.KINDS[kind]
    pair = kind == opt.PAIR
    if pair and (symbol2 is None or symbol2 == symbol or symbol2 not in market.BY_KEY):
        await interaction.response.send_message(
            embed=ui.error_embed(
                "ペア・ワラントは、2つの銘柄が必要です", "「銘柄」と、ちがう銘柄を「相手」に選んでください。"
            ),
            ephemeral=True,
        )
        return
    other = market.BY_KEY[symbol2] if pair and symbol2 is not None else None
    title_sym = f"{sym.name} vs {other.name}" if other is not None else sym.name
    seconds = opt.OPTION_HOURS * 3600
    strike_pct = target / 100 if target else opt.default_strike_pct(symbol)
    fn = default_confirmed_reply if confirm_fn is None else confirm_fn

    def quote(price: float) -> tuple:
        now = cog._market_svc.clock.now_ts()
        events = cog._market_svc.market_events(now)
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
            await interaction.response.send_message(
                embed=ui.error_embed(
                    f"{name}を買えません", "その掛け金では 1口も買えません。掛け金を増やしてください。"
                ),
                ephemeral=True,
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
            await reply(embed=ui.error_embed(f"{name}を買えませんでした", format_failure(res)), ephemeral=True)
            return
        assert not isinstance(res, Failure)
        await reply(
            embed=ui.ok_embed(
                f"{emoji} 【{name}】{title_sym}（{opt.OPTION_HOURS}時間満期）",
                f"掛け金: **{ui.pelica(res['cost'])}**（★途中ロスカットなし・最大損失はこの掛け金のみ）\n"
                + target_text(res["strike"] if not pair else res["spot"], res["strike"], res["units"], res["cost"])
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
    target: Optional[int] = None,
    *,
    confirm_fn: Optional[Callable] = None,
) -> None:
    """コール・プットの裸売り。帝愛に売って、プレミアムを先にもらう。証拠金（行使価格の20%）を預け、満期に損失を払う。"""
    sym, label = market.BY_KEY[symbol], opt.SHORT_NAMES[kind]
    sign = 1 if kind == opt.CALL else -1
    seconds = opt.OPTION_HOURS * 3600
    strike_pct = target / 100 if target else opt.default_strike_pct(symbol)
    fn = default_confirmed_reply if confirm_fn is None else confirm_fn

    def quote(price: float) -> tuple:
        now = cog._market_svc.clock.now_ts()
        strike = opt.strike_for(kind, price, strike_pct)
        units = int(amount / opt.margin_per_unit(strike))
        bid = opt.bid_per_unit(kind, sym.key, price, strike, seconds, now, cog._market_svc.market_events(now))
        return strike, units, math.ceil(round(units * opt.margin_per_unit(strike), 6)), int(bid * units)

    async def run(uid: int) -> None:
        gid = ui.guild_id_of(interaction)
        price = cog._market_svc.latest_price(sym.key)
        strike, units, margin, premium = quote(price)
        if units < 1:
            await interaction.response.send_message(
                embed=ui.error_embed(
                    f"{label}できません", "その証拠金では 1口も売れません。証拠金を増やしてください。"
                ),
                ephemeral=True,
            )
            return
        loss10 = opt.intrinsic(kind, units, price * (1 + sign * 0.10), strike)
        loss20 = opt.intrinsic(kind, units, price * (1 + sign * 0.20), strike)
        cond = "上がる" if kind == opt.CALL else "下がる"
        reply = await fn(
            interaction,
            True,
            f"{label}しますか？（損失が証拠金を超えると借金になります）",
            f"**{sym.name}** の{'コール' if kind == opt.CALL else 'プット'}を、帝愛に売ります（{opt.OPTION_HOURS}時間満期）。\n"
            f"今すぐ受け取る **プレミアム {ui.pelica(premium)}**　預ける **証拠金 {ui.pelica(margin)}**\n"
            f"**{market.fmt(strike)}**（現在の{sign * strike_pct * 100:+.1f}%）を超えて{cond}と、その差が**あなたの損失**です。"
            f"価格が{sign * 10:+d}%で損失 約{loss10:,}ペリカ、{sign * 20:+d}%で約{loss20:,}ペリカ。"
            "**証拠金を超えた分は、帝愛の借金になります。**相場が動かなければ、プレミアムが丸ごと利益です。"
            "途中で買い戻して閉じることもできます。",
            f"{label}する",
        )
        if reply is None:
            return
        ok, res = cog._market_svc.option_write(gid, uid, kind, sym.key, amount, strike_pct=strike_pct)
        if not ok:
            await reply(embed=ui.error_embed(f"{label}できませんでした", format_failure(res)), ephemeral=True)
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
    confirm_fn: Optional[Callable] = None,
) -> None:
    """オプションを、満期の前に手放す。買いはいまの値段（売値）で売り、裸売りは買値で買い戻す。"""
    gid = ui.guild_id_of(interaction)
    cog._market_svc.option_settle(gid, cog._market_svc.clock.now_ts())
    row = next((r for r in cog._market_svc.active_options(gid, interaction.user.id) if r["id"] == option_id), None)
    if row is None:
        await interaction.response.send_message(
            embed=ui.error_embed("売れません", "その番号のオプションは持っていません。"), ephemeral=True
        )
        return
    short, prices, now = row["side"] == "short", cog._market_svc.latest_prices(), cog._market_svc.clock.now_ts()
    fn = default_confirmed_reply if confirm_fn is None else confirm_fn
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
    ok, got = cog._market_svc.option_sell(gid, interaction.user.id, option_id)
    if not ok:
        await reply(embed=ui.error_embed("できませんでした", format_failure(got)), ephemeral=True)
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
    body += f"\n残高: **{ui.pelica(cog._economy_svc.balance_of(gid, interaction.user.id))}**"
    await reply(
        embed=ui.embed("買い戻しました" if short else "オプションを売りました", body, ui.OK if good else ui.ERROR)
    )


async def bond_buy(cog: "MarketCog", interaction: discord.Interaction, kind_key: str, amount: int) -> None:
    gid = ui.guild_id_of(interaction)
    cog._market_svc.bond_settle(
        gid, cog._market_svc.clock.now_ts(), RNG
    )  # 満期の分を先に入金して、買える額を正しくする
    if amount < bonds.MIN_AMOUNT:
        await interaction.response.send_message(
            embed=ui.error_embed("買えません", f"債券は {bonds.MIN_AMOUNT:,}ペリカ以上から買えます。"),
            ephemeral=True,
        )
        return
    now = cog._market_svc.clock.now_ts()
    quote = cog._market_svc.bond_quote(gid, kind_key, amount, now)  # 買う直前の金利（買うときに、この利率で固定される）
    ok, reason = cog._market_svc.bond_buy(gid, interaction.user.id, kind_key, amount, now)
    if not ok:
        await interaction.response.send_message(
            embed=ui.error_embed("買えません", format_failure(reason)), ephemeral=True
        )
        return
    await interaction.response.send_message(embed=bond_buy_embed(kind_key, amount, now, quote))


async def bond_cancel(cog: "MarketCog", interaction: discord.Interaction, bond_id: int) -> None:
    ok, result = cog._market_svc.bond_cancel(ui.guild_id_of(interaction), interaction.user.id, bond_id)
    if not ok:
        await interaction.response.send_message(
            embed=ui.error_embed("売れません", format_failure(result)), ephemeral=True
        )
        return
    assert not isinstance(result, Failure)
    await interaction.response.send_message(  # 公開（ほかの売買の結果と同じ）
        embed=ui.ok_embed(
            f"{interaction.user.display_name} が債券を売りました", f"{ui.pelica(result)}を残高に戻しました。"
        )
    )
