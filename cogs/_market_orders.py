"""`/証券` の注文・決済フロー（cogs/market_cog.py から使う）。

現物・信用・オプション・債券の売買実行と確認ダイアログの進行を受け持つ（読み込まれる Cog ではない）。
"""

import math
import random
import time
from typing import Callable, Optional

import discord

from cogs._bond_embeds import due_text
from cogs._market_embeds import (
    bond_buy_embed,
    margin_position_text,
    margin_ratio_text,
    option_line,
    option_target_text,
    spot_position_text,
    tax_line,
)
from core import bonds, market, ui
from core import options as opt
from core.confirm import confirm_action, confirmed_reply, user_session
from core.economy import Economy

RNG = random.SystemRandom()
CONFIRM_FROM = 5_000  # この額以上の売買は確認ボタンを挟む


async def default_confirmed_reply(interaction: discord.Interaction, needed: bool, title: str, text: str, label: str):
    """大きな取引は、確認ボタンを出す。進んでよければ「返信に使う関数」を返し、やめたなら None を返す。"""
    return await confirmed_reply(interaction, needed, title, text, label, confirm_fn=confirm_action)


async def reject_lots_and_ratio(interaction: discord.Interaction, lots, ratio) -> bool:
    """「枚数」と「割合」を両方指定していたら、エラーを返して True を返す。"""
    if lots is None or ratio is None:
        return False
    await interaction.response.send_message(
        embed=ui.error_embed("どちらか1つにしてください", "「枚数」と「割合」は、どちらか一方だけ指定できます。"),
        ephemeral=True,
    )
    return True


def units_from(have: int, lots, ratio) -> Optional[int]:
    """売る・買い戻す口数。枚数の指定ならその口数、割合（1〜100の数値。「半分」は50）ならその割合、どちらも省略（または100%）なら None（全部）。"""
    if lots is not None:
        return int(round(lots * market.UNIT))
    if ratio is not None:
        value = ratio.value
        if value == "half":
            value = 50
        if isinstance(value, int) and 0 < value < 100:
            return max(1, have * value // 100)
    return None


def worth(have: int, units: Optional[int], price: float) -> float:
    """売買する分（units が None なら全部）の、いまの価格での金額。確認を挟むかの判定に使う。"""
    return (have if units is None else min(units, have)) * price // market.UNIT


async def run_trade(bot, interaction: discord.Interaction, action) -> None:
    """売買（action(uid)）の間は「プレイ中」にして、ゲームの最中に残高を動かせないようにする。"""
    async with user_session(bot, interaction, "いまは取引できません") as ok:
        if ok:
            await action(interaction.user.id)


async def buy_spot(
    cog,
    interaction: discord.Interaction,
    symbol: str,
    amount: int,
    *,
    confirm_fn: Callable = default_confirmed_reply,
) -> None:
    """現物の購入（手数料なし・小数で買える）。amount ペリカの範囲で買えるだけ買う。"""
    sym = market.BY_KEY[symbol]

    async def run(uid: int) -> None:
        price = cog._price(sym.key)
        balance = cog.g(interaction).balance(uid)
        if balance < amount:
            await interaction.response.send_message(
                embed=ui.error_embed(
                    "ペリカが足りません", f"{ui.pelica(amount)} に対して、残高は {ui.pelica(balance)} です。"
                ),
                ephemeral=True,
            )
            return
        units = market.buy_quote(amount, price)[0]
        reply = await confirm_fn(
            interaction,
            amount >= CONFIRM_FROM,
            "買いますか？",
            f"**{sym.name}** を、いまの価格 **{market.pel(price)}** で、約 **{market.lots(units)}** 買います。\n{ui.pelica(amount)} 以内。価格は動くので、実際の価格で買います。",
            "買う",
        )
        if reply is None:
            return
        price = cog._price(sym.key)  # 確認を待つ間に動いた価格で、確定する
        res = cog.g(interaction).crypto_buy(uid, amount, price, sym.key)
        if not res.ok:
            await reply(embed=ui.error_embed("買えませんでした", res.reason), ephemeral=True)
            return
        await reply(
            embed=ui.ok_embed(
                f"{sym.name}を買いました",
                f"**{market.lots(res.units)}**（価格 {market.pel(price)}）\n"
                f"支払い: {ui.pelica(res.money)}\n残高: **{ui.pelica(res.balance)}**",
            )
        )

    await run_trade(cog.bot, interaction, run)


async def margin_close(
    cog,
    interaction: discord.Interaction,
    side: str,
    symbol: str,
    lots,
    ratio,
    *,
    confirm_fn: Callable = default_confirmed_reply,
) -> None:
    """信用取引の決済（空売りは買い戻し、信用買いは売り戻し）。枚数か割合で指定する。どちらも省略すると全部。"""
    if await reject_lots_and_ratio(interaction, lots, ratio):
        return

    sym, label = market.BY_KEY[symbol], Economy.SIDE_NAME[side]
    verb = "買い戻し" if side == "short" else "売り戻し"
    cannot = "買い戻せません" if side == "short" else "売り戻せません"

    async def run(uid: int) -> None:
        have, entry_value, margin = cog.g(interaction).margin_position(uid, sym.key, side)
        if have <= 0:
            await interaction.response.send_message(
                embed=ui.error_embed(cannot, f"{sym.name}の{label}を建てていません。`/証券 注文` で建てられます。"),
                ephemeral=True,
            )
            return
        units = units_from(have, lots, ratio)
        price = cog._price(sym.key)
        shown = market.lots(have if units is None else units)
        reply = await confirm_fn(
            interaction,
            worth(have, units, price) >= CONFIRM_FROM,
            f"{verb}ますか？",
            f"**{shown}** を{verb}て、{label}を閉じます。\n\n{margin_position_text(side, have, entry_value, margin, price)}",
            f"{verb}る",
        )
        if reply is None:
            return
        price = cog._price(sym.key)
        res = cog.g(interaction).margin_close(uid, side, units, price, sym.key)
        if not res.ok:
            await reply(embed=ui.error_embed(f"{cannot[:-2]}ませんでした", res.reason), ephemeral=True)
            return
        left, left_entry, left_margin = cog.g(interaction).margin_position(uid, sym.key, side)
        rest = (
            f"\n\n残りの建玉\n{margin_position_text(side, left, left_entry, left_margin, price)}"
            if left > 0
            else f"\n{label}を全部閉じました。"
        )
        debt = cog.g(interaction).debt(uid)
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

    await run_trade(cog.bot, interaction, run)


async def sell_spot(
    cog,
    interaction: discord.Interaction,
    symbol: str,
    lots,
    ratio,
    *,
    confirm_fn: Callable = default_confirmed_reply,
) -> None:
    """現物の売却。枚数か割合（半分・全部）で指定する。どちらも省略すると全部。"""
    if await reject_lots_and_ratio(interaction, lots, ratio):
        return
    sym = market.BY_KEY[symbol]

    async def run(uid: int) -> None:
        have, cost = cog.g(interaction).crypto_holding(uid, sym.key)
        if have <= 0:
            await interaction.response.send_message(
                embed=ui.error_embed("売れません", f"{sym.name}を持っていません。`/証券 注文` で買えます。"),
                ephemeral=True,
            )
            return
        units = units_from(have, lots, ratio)
        price = cog._price(sym.key)
        shown = market.lots(have if units is None else units)
        reply = await confirm_fn(
            interaction,
            worth(have, units, price) >= CONFIRM_FROM,
            "売りますか？",
            f"**{shown}** を売ります。\n\n{spot_position_text(have, cost, price)}",
            "売る",
        )
        if reply is None:
            return
        price = cog._price(sym.key)
        res = cog.g(interaction).crypto_sell(uid, units, price, sym.key)
        if not res.ok:
            await reply(embed=ui.error_embed("売れませんでした", res.reason), ephemeral=True)
            return
        left, left_cost = cog.g(interaction).crypto_holding(uid, sym.key)
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

    await run_trade(cog.bot, interaction, run)


async def margin_order(
    cog,
    interaction: discord.Interaction,
    side: str,
    symbol: str,
    lots=None,
    amount: Optional[int] = None,
    leverage: int = 1,
    *,
    confirm_fn: Callable = default_confirmed_reply,
) -> None:
    """信用取引（空売り＝売り建て、信用買い＝買い建て）。担保 = 総額 ÷ レバレッジ を預ける。枚数か金額（建てる総額）で指定する。"""
    label = Economy.SIDE_NAME[side]
    if (lots is None) == (amount is None):
        await interaction.response.send_message(
            embed=ui.error_embed(
                "どちらか1つを指定してください", f"{label}する量は、「枚数」か「金額」のどちらか一方で指定します。"
            ),
            ephemeral=True,
        )
        return
    sym = market.BY_KEY[symbol]

    def units_at(price: float) -> int:
        if lots is not None:
            return int(round(lots * market.UNIT))
        assert amount is not None
        return int(amount * market.UNIT // price)  # 金額のときは、その額を超えない口数

    async def run(uid: int) -> None:
        price = cog._price(sym.key)
        units = units_at(price)
        if units < 1:
            await interaction.response.send_message(
                embed=ui.error_embed(
                    f"{label}できません", "その金額では 0.001枚にもなりません。金額を増やしてください。"
                ),
                ephemeral=True,
            )
            return
        notional = units * price / market.UNIT
        margin = math.ceil(round(notional / leverage, 6))
        direction = "売り建て" if side == "short" else "買い建て"
        gain = "下がると利益、上がると損失" if side == "short" else "上がると利益、下がると損失"
        reply = await confirm_fn(
            interaction,
            notional >= CONFIRM_FROM,
            f"{label}しますか？",
            f"**{sym.name}** を、いまの価格 **{market.pel(price)}** で、**{market.lots(units)}**（約 {ui.pelica(int(notional))}）を{direction}ます。\n"
            f"レバレッジ **{leverage}倍**・担保 **{ui.pelica(margin)}**\n"
            f"価格が{gain}です。口座ぜんたいの維持率が {market.LOSSCUT_RATIO:.0%} を下回ると、損の大きい建玉から強制決済されます。",
            f"{label}する",
        )
        if reply is None:
            return
        price = cog._price(sym.key)
        units = units_at(price)  # 確認を待つ間に動いた価格で、確定する
        eco = cog.g(interaction)
        res = (eco.short_open if side == "short" else eco.long_open)(uid, units, price, leverage, sym.key)
        if not res.ok:
            await reply(embed=ui.error_embed(f"{label}できませんでした", res.reason), ephemeral=True)
            return
        have, entry_value, margin_all = eco.margin_position(uid, sym.key, side)
        await reply(
            embed=ui.ok_embed(
                f"{sym.name}を{label}しました",
                f"**{market.lots(res.units)}**（価格 {market.pel(price)}・{leverage}倍）\n"
                f"預けた担保: {ui.pelica(res.money)}\n残高: **{ui.pelica(res.balance)}**\n\n{margin_position_text(side, have, entry_value, margin_all, price)}\n\n"
                f"{margin_ratio_text(eco.margin_status(uid, cog._prices()))}\n"
                "閉じるには `/証券 保有` のボタンか、`/証券 決済` を使います。",
            )
        )

    await run_trade(cog.bot, interaction, run)


async def option_buy(
    cog,
    interaction: discord.Interaction,
    kind: str,
    symbol: str,
    amount: int,
    symbol2: Optional[str] = None,
    target: Optional[int] = None,
    *,
    confirm_fn: Callable = default_confirmed_reply,
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

    def quote(price: float) -> tuple:
        now = time.time()
        events = cog.bot.economy.market_events(now)
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
        price = cog._price(sym.key)
        strike, units, cost = quote(price)
        if units < 1:
            await interaction.response.send_message(
                embed=ui.error_embed(
                    f"{name}を買えません", "その掛け金では 1口も買えません。掛け金を増やしてください。"
                ),
                ephemeral=True,
            )
            return
        reply = await confirm_fn(
            interaction,
            cost >= CONFIRM_FROM,
            f"{name}を買いますか？",
            f"{emoji} **{title_sym}**（{opt.OPTION_HOURS}時間満期）\n掛け金 **{ui.pelica(cost)}**（途中のロスカットはなく、最大の損失はこの掛け金だけ）\n"
            + target_text(price, strike, units, cost),
            f"{name}を買う",
        )
        if reply is None:
            return
        res_ok, res = cog.g(interaction).option_buy(
            uid, kind, sym.key, amount, symbol2=symbol2, strike_pct=None if pair else strike_pct
        )
        if not res_ok:
            await reply(embed=ui.error_embed(f"{name}を買えませんでした", res), ephemeral=True)
            return
        await reply(
            embed=ui.ok_embed(
                f"{emoji} 【{name}】{title_sym}（{opt.OPTION_HOURS}時間満期）",
                f"掛け金: **{ui.pelica(res['cost'])}**（★途中ロスカットなし・最大損失はこの掛け金のみ）\n"
                + target_text(res["strike"] if not pair else res["spot"], res["strike"], res["units"], res["cost"])
                + f"\n満期: {due_text(res['expiry'])}（満期の価格で自動で決済されます）\n"
                "満期の前でも、`/証券 保有` のボタンか `/証券 決済` で、いまの値段で売れます。",
            )
        )

    await run_trade(cog.bot, interaction, run)


async def option_write(
    cog,
    interaction: discord.Interaction,
    kind: str,
    symbol: str,
    amount: int,
    target: Optional[int] = None,
    *,
    confirm_fn: Callable = default_confirmed_reply,
) -> None:
    """コール・プットの裸売り。帝愛に売って、プレミアムを先にもらう。証拠金（行使価格の20%）を預け、満期に損失を払う。"""
    sym, label = market.BY_KEY[symbol], opt.SHORT_NAMES[kind]
    sign = 1 if kind == opt.CALL else -1
    seconds = opt.OPTION_HOURS * 3600
    strike_pct = target / 100 if target else opt.default_strike_pct(symbol)

    def quote(price: float) -> tuple:
        now = time.time()
        strike = opt.strike_for(kind, price, strike_pct)
        units = int(amount / opt.margin_per_unit(strike))
        bid = opt.bid_per_unit(kind, sym.key, price, strike, seconds, now, cog.bot.economy.market_events(now))
        return strike, units, math.ceil(round(units * opt.margin_per_unit(strike), 6)), int(bid * units)

    async def run(uid: int) -> None:
        price = cog._price(sym.key)
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
        reply = await confirm_fn(
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
        ok, res = cog.g(interaction).option_write(uid, kind, sym.key, amount, strike_pct=strike_pct)
        if not ok:
            await reply(embed=ui.error_embed(f"{label}できませんでした", res), ephemeral=True)
            return
        await reply(
            embed=ui.ok_embed(
                f"🎰 【{label}】{sym.name}（{opt.OPTION_HOURS}時間満期）",
                f"プレミアム **{ui.pelica(res['premium'])}** を受け取りました。証拠金 **{ui.pelica(res['margin'])}** を預けています。\n"
                f"満期の価格が **{market.fmt(res['strike'])}** を{'超えない' if kind == opt.CALL else '下回らない'}かぎり、損失はありません。\n"
                f"満期: {due_text(res['expiry'])}（自動で決済。損失は証拠金から払い、超えた分は借金）\n"
                "満期の前でも、`/証券 保有` のボタンか `/証券 決済` で、買い戻して閉じられます。",
            )
        )

    await run_trade(cog.bot, interaction, run)


async def option_sell(
    cog,
    interaction: discord.Interaction,
    option_id: int,
    *,
    confirm_fn: Callable = default_confirmed_reply,
) -> None:
    """オプションを、満期の前に手放す。買いはいまの値段（売値）で売り、裸売りは買値で買い戻す。"""
    eco = cog.g(interaction)
    eco.option_settle(time.time())
    row = next((r for r in eco.option_list(interaction.user.id)["active"] if r["id"] == option_id), None)
    if row is None:
        await interaction.response.send_message(
            embed=ui.error_embed("売れません", "その番号のオプションは持っていません。"), ephemeral=True
        )
        return
    short, prices, now = row["side"] == "short", cog._prices(), time.time()
    if short:
        cost = eco.option_close_cost(row, now, prices)
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
        value = eco.option_value(row, now, prices)
        text = f"{option_line(row, prices)}\nいま売ると **{ui.pelica(value)}** です（掛け金 {ui.pelica(row['premium'])}）。"
        needs = value >= CONFIRM_FROM
    reply = await confirm_fn(
        interaction,
        needs,
        "オプションを買い戻しますか？" if short else "オプションを売りますか？",
        text,
        "買い戻す" if short else "売る",
    )
    if reply is None:
        return
    ok, got = eco.option_sell(interaction.user.id, option_id)
    if not ok:
        await reply(embed=ui.error_embed("できませんでした", got), ephemeral=True)
        return
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
    body += f"\n残高: **{ui.pelica(eco.balance(interaction.user.id))}**"
    await reply(
        embed=ui.embed("買い戻しました" if short else "オプションを売りました", body, ui.OK if good else ui.ERROR)
    )


async def bond_buy(cog, interaction: discord.Interaction, kind_key: str, amount: int) -> None:
    eco = cog.g(interaction)
    eco.bond_settle(time.time(), RNG)  # 満期の分を先に入金して、買える額を正しくする
    if amount < bonds.MIN_AMOUNT:
        await interaction.response.send_message(
            embed=ui.error_embed("買えません", f"債券は {bonds.MIN_AMOUNT:,}ペリカ以上から買えます。"),
            ephemeral=True,
        )
        return
    ok, reason = eco.bond_buy(interaction.user.id, kind_key, amount, time.time())
    if not ok:
        await interaction.response.send_message(embed=ui.error_embed("買えません", reason), ephemeral=True)
        return
    await interaction.response.send_message(embed=bond_buy_embed(kind_key, amount, time.time()))


async def bond_cancel(cog, interaction: discord.Interaction, bond_id: int) -> None:
    ok, result = cog.g(interaction).bond_cancel(interaction.user.id, bond_id)
    if not ok:
        await interaction.response.send_message(embed=ui.error_embed("売れません", result), ephemeral=True)
        return
    await interaction.response.send_message(  # 公開（ほかの売買の結果と同じ）
        embed=ui.ok_embed(
            f"{interaction.user.display_name} が債券を売りました", f"{ui.pelica(result)}を残高に戻しました。"
        )
    )
