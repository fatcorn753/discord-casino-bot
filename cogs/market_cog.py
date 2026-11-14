"""/証券（注文・保有・市況・決済）— 4銘柄（CSトークン・TEIO重工・NUMAバイオ・GOLD地金）と債券の売買、価格のチャート。価格は全サーバー共通で、1分ごとに、相関をもって動く。"""

import math
import random
import time
from types import SimpleNamespace
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs._amount import balance_choices, own_balance, typed_amount
from cogs._bond_embeds import KIND_CHOICES, due_text
from cogs._market_embeds import (
    CORRELATION_MINUTES,  # noqa: F401
    EVENT_TEXT,  # noqa: F401
    HOLDER_ROWS,  # noqa: F401
    LIVE_MINUTES,
    TICK_MINUTES,
    all_prices,
    bond_buy_embed,
    chart_embed,
    correlation_lines,
    correlation_table,
    cut_lines,
    earnings_announce_embed,
    earnings_lines,
    earnings_result_embed,
    event_lines,
    holdings_embed,
    latest_price,
    margin_position_text,
    margin_ratio_text,
    option_line,
    option_target_text,
    pin_lines,
    spot_position_text,
    summary_embed,
    tax_line,
)
from cogs._market_embeds import alert_lines as _alert_lines
from cogs._market_views import HoldingsView, MarketView
from cogs._market_views import OptionAmountModal as OptionAmountModal  # noqa: F401
from cogs._shared import announce_channel
from core import approval, bonds, earnings, logthrottle, market, market_events, ui
from core import options as opt
from core.confirm import confirm_action, confirmed_reply, user_session
from core.eco_options import name_of
from core.economy import Economy

RNG = random.SystemRandom()
KEEP_DAYS = 30  # 価格の履歴を残す日数
CONFIRM_FROM = 5_000  # この額以上の売買は確認ボタンを挟む
PERIOD_CHOICES = [app_commands.Choice(name=label, value=key) for key, (label, _) in market.PERIODS.items()]
SYMBOL_CHOICES = [app_commands.Choice(name=f"{s.emoji} {s.name}", value=s.key) for s in market.SYMBOLS]
BOND_PREFIX = "bond_"
ORDER_CHOICES = SYMBOL_CHOICES + [
    app_commands.Choice(name=f"債券: {c.name}", value=BOND_PREFIX + c.value) for c in KIND_CHOICES
]


async def _confirmed_reply(interaction: discord.Interaction, needed: bool, title: str, text: str, label: str):
    """大きな取引は、確認ボタンを出す。進んでよければ「返信に使う関数」を返し、やめたなら None を返す。
    確認を挟んだときは、最初の返信がすでに確認画面に使われているので、あとの返信は followup になる。"""
    return await confirmed_reply(interaction, needed, title, text, label, confirm_fn=confirm_action)


async def _reject_lots_and_ratio(interaction: discord.Interaction, lots, ratio) -> bool:
    """「枚数」と「割合」を両方指定していたら、エラーを返して True を返す。"""
    if lots is None or ratio is None:
        return False
    await interaction.response.send_message(
        embed=ui.error_embed("どちらか1つにしてください", "「枚数」と「割合」は、どちらか一方だけ指定できます。"),
        ephemeral=True,
    )
    return True


def _units_from(have: int, lots, ratio) -> Optional[int]:
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


def _worth(have: int, units: Optional[int], price: float) -> float:
    """売買する分（units が None なら全部）の、いまの価格での金額。確認を挟むかの判定に使う。"""
    return (have if units is None else min(units, have)) * price // market.UNIT


class MarketCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._earnings_results = []  # 発表した決算（チャンネルへの投稿待ち）

    def g(self, interaction: discord.Interaction):
        return self.bot.economy.for_guild(interaction.guild.id)

    group = app_commands.Group(
        name="証券", description="📈 注文・保有・市況・決済（4銘柄と債券。ゲーム内の通貨・現実のお金とは無関係）"
    )

    # ------------------------------ 価格の更新 ------------------------------ #
    def _price(self, symbol: str = market.DEFAULT) -> float:
        return latest_price(self.bot.economy, symbol)

    def _prices(self) -> dict:
        return all_prices(self.bot.economy)

    def _day_reference(self, now: float) -> dict:
        """値幅制限の基準になる、約24時間前の価格 {銘柄: 価格}。履歴がまだ無い銘柄は含めない。"""
        out = {}
        for sym in market.SYMBOLS:
            hist = self.bot.economy.market_history(now - 86400, sym.key)
            if hist:
                out[sym.key] = hist[0][1]
        return out

    def step_prices(self) -> tuple:
        """全銘柄の価格を1つ進めて、(新しい価格 {銘柄: 価格}, 有事の出来事) を返す。"""
        prices = self._prices()  # 画面に出ている価格（値幅制限のかかったもの）
        fair = {
            k: self.bot.economy.market_fair_get(k) or p for k, p in prices.items()
        }  # 裏の理論価格（制限なしで動く）
        last_ts = max((self.bot.economy.market_latest(k) or (0,))[0] for k in prices)
        now = time.time()
        announced = self.bot.economy.event_step(now, RNG)  # ボラティリティ予告の抽選
        if announced is not None:
            print(
                f"ボラティリティ予告: {announced['symbol']} {market_events.LEAD_MIN}分後から{market_events.DURATION_MIN}分"
            )
        tz = float(self.bot.config.get("utc_offset_hours", 9))
        self.bot.economy.earnings_ensure(now, tz)  # 次の決算（毎週火曜13:30）を決めておく
        new_fair, event = market.next_prices(fair, RNG, self.bot.economy.vol_multipliers(now))
        for r in self.bot.economy.earnings_release_due(
            now
        ):  # 決算の発表: 理論価格が、一気に動く（値幅制限は、画面の価格にだけかかる）
            hit = new_fair[r["symbol"]] * earnings.factor(r["direction"], r["size"])
            new_fair[r["symbol"]] = min(market.MAX_PRICE, max(market.MIN_PRICE, round(hit, 2)))
            self._earnings_results.append(r)
        ref = self._day_reference(now)
        new = {
            k: round(v, 2) for k, v in market.limit_to_day(new_fair, ref).items()
        }  # 値幅制限（理論価格は、外へ突き抜けてよい）
        pins = market.pin_states(
            new_fair, ref
        )  # 理論価格が値幅の外なら、張り付き（翌日も、次の値幅の端に張り付く＝2連ストップ）
        ts = max(time.time(), last_ts + 0.001)  # 時計が戻っても、記録の順序を崩さない
        for key, price in new.items():
            self.bot.economy.market_set(ts, price, event, key)
            self.bot.economy.market_fair_set(key, new_fair[key], pins.get(key, 0), ts)
        return new, event

    async def cog_load(self):
        self._prices()  # 初回は開始価格を入れる（起動直後でもコマンドが使えるように）
        self.tick.start()

    def cog_unload(self):
        self.tick.cancel()

    @tasks.loop(minutes=TICK_MINUTES)
    async def tick(self):
        try:
            prices, _event = self.step_prices()
            self.bot.economy.margin_interest(time.time())  # 信用金利を、担保から引く（そのあとで、維持率を調べる）
            for guild_id, user_id, symbol, side, loss in self.bot.economy.margin_losscut(prices):
                print(f"{Economy.SIDE_NAME[side]}をロスカット: guild={guild_id} user={user_id} {symbol} 損失={loss:,}")
            self.bot.economy.market_prune(time.time() - KEEP_DAYS * 86400)
            self.bot.economy.prune_chart_posts(time.time() - KEEP_DAYS * 86400)
        except Exception as exc:
            print(f"価格の更新に失敗: {exc!r}")
            return
        try:
            await self._earnings_posts()
        except Exception as exc:
            print(f"決算の告知に失敗: {exc!r}")
        now, cache = time.time(), {}
        for post in self.bot.economy.live_chart_posts():
            try:
                await self._refresh_post(post, now, cache)
            except Exception as exc:  # 1つのチャンネルの失敗で、他の更新を止めない
                print(f"チャートの更新に失敗: {post}: {exc!r}")

    async def _earnings_posts(self) -> None:
        """決算の予告（月曜9:00）と発表の結果を、各サーバーの告知チャンネルに投稿する。"""
        now = time.time()
        embeds = [earnings_announce_embed(r) for r in self.bot.economy.earnings_to_announce(now)]
        results, self._earnings_results = self._earnings_results, []
        for r in results:
            embeds.append(earnings_result_embed(r, self.bot.economy.market_pin(r["symbol"])))
        if not embeds:
            return
        for guild in list(self.bot.guilds):
            if not approval.is_allowed(self.bot.economy, self.bot.config, guild.id, 0):
                continue
            channel = announce_channel(guild, self.bot.economy.for_guild(guild.id))
            if channel is None:
                continue
            for embed in embeds:
                try:
                    await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException as exc:
                    logthrottle.warn(("earnings-post", guild.id), f"決算の投稿に失敗: guild={guild.id}: {exc!r}")

    @tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()
        try:
            await self._retire_old_boards()
        except Exception as exc:
            print(f"古いチャート掲示の後始末に失敗: {exc!r}")

    # ------------------------------ 表示 ------------------------------ #
    def correlation_lines(self, symbol: str) -> str:
        return correlation_lines(self.bot.economy, symbol)

    def chart_embed(
        self,
        period: str = "24h",
        title_prefix: str = "",
        guild_id: Optional[int] = None,
        live_until: Optional[float] = None,
        ended: bool = False,
        symbol: str = market.DEFAULT,
    ) -> discord.Embed:
        return chart_embed(
            self.bot.economy,
            period=period,
            title_prefix=title_prefix,
            guild_id=guild_id,
            live_until=live_until,
            ended=ended,
            symbol=symbol,
        )

    def pin_lines(self, symbol: str) -> str:
        return pin_lines(self.bot.economy, symbol)

    def correlation_table(self) -> str:
        return correlation_table(self.bot.economy)

    def event_lines(self) -> str:
        return event_lines(self.bot.economy)

    def earnings_lines(self) -> str:
        return earnings_lines(self.bot.economy)

    def alert_lines(self) -> str:
        return _alert_lines(self.bot.economy)

    def cut_lines(self, guild_id: int) -> str:
        return cut_lines(self.bot.economy, guild_id)

    def summary_embed(self, guild_id: Optional[int] = None) -> discord.Embed:
        return summary_embed(self.bot.economy, guild_id=guild_id)

    @group.command(
        name="市況", description=f"価格・連動・チャート。銘柄を選ぶと、{LIVE_MINUTES}分間、自動で更新するチャートを出す"
    )
    @app_commands.rename(symbol="銘柄", period="期間")
    @app_commands.describe(
        symbol="チャートを出す銘柄（省略すると、全銘柄の価格と連動の一覧）", period="どの期間を見るか（省略時: 24時間）"
    )
    @app_commands.choices(symbol=SYMBOL_CHOICES, period=PERIOD_CHOICES)
    async def board(
        self,
        interaction: discord.Interaction,
        symbol: Optional[app_commands.Choice[str]] = None,
        period: Optional[app_commands.Choice[str]] = None,
    ):
        if symbol is None:
            view = MarketView(self, interaction.user.id, interaction.guild.id, period.value if period else "24h")
            return await interaction.response.send_message(
                embed=self.summary_embed(interaction.guild.id), view=view, ephemeral=True
            )
        await self._post_chart(interaction, symbol.value, period.value if period else "24h")

    async def _post_chart(self, interaction: discord.Interaction, sym: str, key: str) -> None:
        now = time.time()
        await interaction.response.send_message(
            embed=self.chart_embed(key, guild_id=interaction.guild.id, live_until=now + LIVE_MINUTES * 60, symbol=sym),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        # チャートは1チャンネルに1つだけ。前に出したチャートは消し、新しいものを、一定時間だけ自動で更新する
        try:
            message = await interaction.original_response()
            prev = self.bot.economy.swap_chart_post(message.channel.id, message.id, interaction.guild.id, key, now, sym)
            if prev is not None and prev != message.id:
                await message.channel.get_partial_message(prev).delete()
        except discord.HTTPException as exc:  # 前のチャートがすでに無い・消せないときは、そのままにする（記録だけ残す）
            logthrottle.warn(
                ("chart-swap", interaction.guild.id), f"前のチャートを消せません: guild={interaction.guild.id}: {exc!r}"
            )

    # ------------------------------ 売買 ------------------------------ #
    async def _trade(self, interaction: discord.Interaction, action):
        """売買（action(uid)）の間は「プレイ中」にして、ゲームの最中に残高を動かせないようにする（ショップの購入と同じ）。"""
        async with user_session(self.bot, interaction, "いまは取引できません") as ok:
            if ok:
                await action(interaction.user.id)

    async def _buy_amount_autocomplete(self, interaction: discord.Interaction, current: str):
        """買うのに使う金額の欄に、残高を見せる。何も打っていない（数字でない）ときは、残高の25・50・75・100%の候補も出す。"""
        choices = balance_choices(self.bot, interaction, current, 10, 1_000_000)
        balance = own_balance(self.bot, interaction)
        if balance is not None and typed_amount(current) is None:
            for pct in (25, 50, 75, 100):
                value = min(10_000_000, balance * pct // 100)
                if value >= 10:
                    choices.append(app_commands.Choice(name=f"残高の{pct}%（{value:,}ペリカ）", value=value))
        return choices[:25]

    async def _buy(self, interaction: discord.Interaction, symbol: str, amount: int) -> None:
        """現物の購入（手数料なし・小数で買える）。amount ペリカの範囲で買えるだけ買う。"""
        sym = market.BY_KEY[symbol]

        async def run(uid):
            price = self._price(sym.key)
            balance = self.g(interaction).balance(uid)
            if balance < amount:
                return await interaction.response.send_message(
                    embed=ui.error_embed(
                        "ペリカが足りません", f"{ui.pelica(amount)} に対して、残高は {ui.pelica(balance)} です。"
                    ),
                    ephemeral=True,
                )
            units = market.buy_quote(amount, price)[0]
            reply = await _confirmed_reply(
                interaction,
                amount >= CONFIRM_FROM,
                "買いますか？",
                f"**{sym.name}** を、いまの価格 **{market.pel(price)}** で、約 **{market.lots(units)}** 買います。\n{ui.pelica(amount)} 以内。価格は動くので、実際の価格で買います。",
                "買う",
            )
            if reply is None:
                return
            price = self._price(sym.key)  # 確認を待つ間に動いた価格で、確定する
            res = self.g(interaction).crypto_buy(uid, amount, price, sym.key)
            if not res.ok:
                return await reply(embed=ui.error_embed("買えませんでした", res.reason), ephemeral=True)
            await reply(
                embed=ui.ok_embed(
                    f"{sym.name}を買いました",
                    f"**{market.lots(res.units)}**（価格 {market.pel(price)}）\n"
                    f"支払い: {ui.pelica(res.money)}\n残高: **{ui.pelica(res.balance)}**",
                )
            )

        await self._trade(interaction, run)

    async def _cover(self, interaction, symbol: str, lots, ratio) -> None:
        await self._margin_close(interaction, "short", symbol, lots, ratio)

    async def _margin_close(self, interaction, side: str, symbol: str, lots, ratio) -> None:
        """信用取引の決済（空売りは買い戻し、信用買いは売り戻し）。枚数か割合で指定する。どちらも省略すると全部。"""
        if await _reject_lots_and_ratio(interaction, lots, ratio):
            return

        sym, label = market.BY_KEY[symbol], Economy.SIDE_NAME[side]
        verb = "買い戻し" if side == "short" else "売り戻し"
        cannot = "買い戻せません" if side == "short" else "売り戻せません"

        async def run(uid):
            have, entry_value, margin = self.g(interaction).margin_position(uid, sym.key, side)
            if have <= 0:
                return await interaction.response.send_message(
                    embed=ui.error_embed(cannot, f"{sym.name}の{label}を建てていません。`/証券 注文` で建てられます。"),
                    ephemeral=True,
                )
            units = _units_from(have, lots, ratio)
            price = self._price(sym.key)
            shown = market.lots(have if units is None else units)
            reply = await _confirmed_reply(
                interaction,
                _worth(have, units, price) >= CONFIRM_FROM,
                f"{verb}ますか？",
                f"**{shown}** を{verb}て、{label}を閉じます。\n\n{margin_position_text(side, have, entry_value, margin, price)}",
                f"{verb}る",
            )
            if reply is None:
                return
            price = self._price(sym.key)
            res = self.g(interaction).margin_close(uid, side, units, price, sym.key)
            if not res.ok:
                return await reply(embed=ui.error_embed(f"{cannot[:-2]}ませんでした", res.reason), ephemeral=True)
            left, left_entry, left_margin = self.g(interaction).margin_position(uid, sym.key, side)
            rest = (
                f"\n\n残りの建玉\n{margin_position_text(side, left, left_entry, left_margin, price)}"
                if left > 0
                else f"\n{label}を全部閉じました。"
            )
            debt = self.g(interaction).debt(uid)
            owed = (
                f"\n⚠️ 担保を超える損失は、手元の資産から取り立てられました（借金 {ui.pelica(debt)}）。"
                if debt > 0
                else ""
            )
            await reply(
                embed=ui.embed(
                    f"{sym.name}を{verb}ました（{label}の決済）",
                    f"**{market.lots(res.units)}**（価格 {market.pel(price)}）\n"
                    f"戻ってきた額: {ui.pelica(res.money)}（担保 + 損益）\n損益: **{res.profit:+,}ペリカ**\n{tax_line(res)}残高: **{ui.pelica(res.balance)}**{owed}{rest}",
                    ui.OK if res.profit >= 0 else ui.ERROR,
                )
            )

        await self._trade(interaction, run)

    async def _sell(self, interaction: discord.Interaction, symbol: str, lots, ratio) -> None:
        """現物の売却。枚数か割合（半分・全部）で指定する。どちらも省略すると全部。"""
        if await _reject_lots_and_ratio(interaction, lots, ratio):
            return
        sym = market.BY_KEY[symbol]

        async def run(uid):
            have, cost = self.g(interaction).crypto_holding(uid, sym.key)
            if have <= 0:
                return await interaction.response.send_message(
                    embed=ui.error_embed("売れません", f"{sym.name}を持っていません。`/証券 注文` で買えます。"),
                    ephemeral=True,
                )
            units = _units_from(have, lots, ratio)
            price = self._price(sym.key)
            shown = market.lots(have if units is None else units)
            reply = await _confirmed_reply(
                interaction,
                _worth(have, units, price) >= CONFIRM_FROM,
                "売りますか？",
                f"**{shown}** を売ります。\n\n{spot_position_text(have, cost, price)}",
                "売る",
            )
            if reply is None:
                return
            price = self._price(sym.key)
            res = self.g(interaction).crypto_sell(uid, units, price, sym.key)
            if not res.ok:
                return await reply(embed=ui.error_embed("売れませんでした", res.reason), ephemeral=True)
            left, left_cost = self.g(interaction).crypto_holding(uid, sym.key)
            pct = market.pnl_percent(price, have, cost)  # 売った分の、取得価格に対する増減
            rest = (
                f"\n\n残りの持ち分\n{spot_position_text(left, left_cost, price)}" if left > 0 else "\n全部売りました。"
            )
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

        await self._trade(interaction, run)

    async def _short(
        self, interaction: discord.Interaction, symbol: str, lots=None, amount: Optional[int] = None, leverage: int = 1
    ) -> None:
        await self._margin_order(interaction, "short", symbol, lots, amount, leverage)

    async def _margin_order(
        self,
        interaction: discord.Interaction,
        side: str,
        symbol: str,
        lots=None,
        amount: Optional[int] = None,
        leverage: int = 1,
    ) -> None:
        """信用取引（空売り＝売り建て、信用買い＝買い建て）。担保 = 総額 ÷ レバレッジ を預ける。枚数か金額（建てる総額）で指定する。"""
        label = Economy.SIDE_NAME[side]
        if (lots is None) == (amount is None):
            return await interaction.response.send_message(
                embed=ui.error_embed(
                    "どちらか1つを指定してください", f"{label}する量は、「枚数」か「金額」のどちらか一方で指定します。"
                ),
                ephemeral=True,
            )
        sym = market.BY_KEY[symbol]

        def units_at(price: float) -> int:
            return (
                int(round(lots * market.UNIT)) if lots is not None else int(amount * market.UNIT // price)
            )  # 金額のときは、その額を超えない口数

        async def run(uid):
            price = self._price(sym.key)
            units = units_at(price)
            if units < 1:
                return await interaction.response.send_message(
                    embed=ui.error_embed(
                        f"{label}できません", "その金額では 0.001枚にもなりません。金額を増やしてください。"
                    ),
                    ephemeral=True,
                )
            notional = units * price / market.UNIT
            margin = math.ceil(round(notional / leverage, 6))
            direction = "売り建て" if side == "short" else "買い建て"
            gain = "下がると利益、上がると損失" if side == "short" else "上がると利益、下がると損失"
            reply = await _confirmed_reply(
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
            price = self._price(sym.key)
            units = units_at(price)  # 確認を待つ間に動いた価格で、確定する
            eco = self.g(interaction)
            res = (eco.short_open if side == "short" else eco.long_open)(uid, units, price, leverage, sym.key)
            if not res.ok:
                return await reply(embed=ui.error_embed(f"{label}できませんでした", res.reason), ephemeral=True)
            have, entry_value, margin_all = eco.margin_position(uid, sym.key, side)
            await reply(
                embed=ui.ok_embed(
                    f"{sym.name}を{label}しました",
                    f"**{market.lots(res.units)}**（価格 {market.pel(price)}・{leverage}倍）\n"
                    f"預けた担保: {ui.pelica(res.money)}\n残高: **{ui.pelica(res.balance)}**\n\n{margin_position_text(side, have, entry_value, margin_all, price)}\n\n"
                    f"{margin_ratio_text(eco.margin_status(uid, self._prices()))}\n"
                    "閉じるには `/証券 保有` のボタンか、`/証券 決済` を使います。",
                )
            )

        await self._trade(interaction, run)

    async def _option_buy(
        self,
        interaction: discord.Interaction,
        kind: str,
        symbol: str,
        amount: int,
        symbol2: Optional[str] = None,
        target: Optional[int] = None,
    ) -> None:
        """爆騰コール・暴落プット・ペア・ワラントを買う。満期は6時間後。損失は掛け金だけ。

        コール・プットの権利行使価格は、現在値の ±目標割合（省略すると、銘柄ごとの既定値）。ペア・ワラントは、symbol が symbol2 より上がると当たり（基準はなし）。
        """
        sym, (name, emoji, _hint) = market.BY_KEY[symbol], opt.KINDS[kind]
        pair = kind == opt.PAIR
        if pair and (symbol2 is None or symbol2 == symbol or symbol2 not in market.BY_KEY):
            return await interaction.response.send_message(
                embed=ui.error_embed(
                    "ペア・ワラントは、2つの銘柄が必要です", "「銘柄」と、ちがう銘柄を「相手」に選んでください。"
                ),
                ephemeral=True,
            )
        other = market.BY_KEY[symbol2] if pair else None
        title_sym = f"{sym.name} vs {other.name}" if pair else sym.name
        seconds = opt.OPTION_HOURS * 3600
        strike_pct = target / 100 if target else opt.default_strike_pct(symbol)  # 目標割合（省略は、銘柄ごとの既定値）

        def quote(price: float) -> tuple:
            now = time.time()
            events = self.bot.economy.market_events(now)
            if pair:
                rate = opt.pair_ask_rate(sym.key, symbol2, seconds, now, events)
                units = int(amount / rate) if rate > 0 else 0
                return price, units, math.ceil(round(units * rate, 6))
            strike = opt.strike_for(kind, price, strike_pct)
            per = opt.ask_per_unit(kind, sym.key, price, strike, seconds, now, events)
            units = int(amount / per) if per > 0 else 0
            return strike, units, math.ceil(round(units * per, 6))

        def target_text(price: float, strike: float, units: int, cost: int) -> str:
            return option_target_text(kind, sym, other, strike_pct, price, strike, units, cost)

        async def run(uid):
            price = self._price(sym.key)
            strike, units, cost = quote(price)
            if units < 1:
                return await interaction.response.send_message(
                    embed=ui.error_embed(
                        f"{name}を買えません", "その掛け金では 1口も買えません。掛け金を増やしてください。"
                    ),
                    ephemeral=True,
                )
            reply = await _confirmed_reply(
                interaction,
                cost >= CONFIRM_FROM,
                f"{name}を買いますか？",
                f"{emoji} **{title_sym}**（{opt.OPTION_HOURS}時間満期）\n掛け金 **{ui.pelica(cost)}**（途中のロスカットはなく、最大の損失はこの掛け金だけ）\n"
                + target_text(price, strike, units, cost),
                f"{name}を買う",
            )
            if reply is None:
                return
            res_ok, res = self.g(interaction).option_buy(  # 確認を待つ間に動いた価格で、確定する
                uid, kind, sym.key, amount, symbol2=symbol2, strike_pct=None if pair else strike_pct
            )
            if not res_ok:
                return await reply(embed=ui.error_embed(f"{name}を買えませんでした", res), ephemeral=True)
            await reply(
                embed=ui.ok_embed(
                    f"{emoji} 【{name}】{title_sym}（{opt.OPTION_HOURS}時間満期）",
                    f"掛け金: **{ui.pelica(res['cost'])}**（★途中ロスカットなし・最大損失はこの掛け金のみ）\n"
                    + target_text(res["strike"] if not pair else res["spot"], res["strike"], res["units"], res["cost"])
                    + f"\n満期: {due_text(res['expiry'])}（満期の価格で自動で決済されます）\n"
                    "満期の前でも、`/証券 保有` のボタンか `/証券 決済` で、いまの値段で売れます。",
                )
            )

        await self._trade(interaction, run)

    async def _option_write(
        self, interaction: discord.Interaction, kind: str, symbol: str, amount: int, target: Optional[int] = None
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
            bid = opt.bid_per_unit(kind, sym.key, price, strike, seconds, now, self.bot.economy.market_events(now))
            return strike, units, math.ceil(round(units * opt.margin_per_unit(strike), 6)), int(bid * units)

        async def run(uid):
            price = self._price(sym.key)
            strike, units, margin, premium = quote(price)
            if units < 1:
                return await interaction.response.send_message(
                    embed=ui.error_embed(
                        f"{label}できません", "その証拠金では 1口も売れません。証拠金を増やしてください。"
                    ),
                    ephemeral=True,
                )
            loss10 = opt.intrinsic(kind, units, price * (1 + sign * 0.10), strike)
            loss20 = opt.intrinsic(kind, units, price * (1 + sign * 0.20), strike)
            cond = "上がる" if kind == opt.CALL else "下がる"
            reply = await _confirmed_reply(
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
            ok, res = self.g(interaction).option_write(uid, kind, sym.key, amount, strike_pct=strike_pct)
            if not ok:
                return await reply(embed=ui.error_embed(f"{label}できませんでした", res), ephemeral=True)
            await reply(
                embed=ui.ok_embed(
                    f"🎰 【{label}】{sym.name}（{opt.OPTION_HOURS}時間満期）",
                    f"プレミアム **{ui.pelica(res['premium'])}** を受け取りました。証拠金 **{ui.pelica(res['margin'])}** を預けています。\n"
                    f"満期の価格が **{market.fmt(res['strike'])}** を{'超えない' if kind == opt.CALL else '下回らない'}かぎり、損失はありません。\n"
                    f"満期: {due_text(res['expiry'])}（自動で決済。損失は証拠金から払い、超えた分は借金）\n"
                    "満期の前でも、`/証券 保有` のボタンか `/証券 決済` で、買い戻して閉じられます。",
                )
            )

        await self._trade(interaction, run)

    async def _option_sell(self, interaction: discord.Interaction, option_id: int) -> None:
        """オプションを、満期の前に手放す。買いはいまの値段（売値）で売り、裸売りは買値で買い戻す。"""
        eco = self.g(interaction)
        eco.option_settle(time.time())
        row = next((r for r in eco.option_list(interaction.user.id)["active"] if r["id"] == option_id), None)
        if row is None:
            return await interaction.response.send_message(
                embed=ui.error_embed("売れません", "その番号のオプションは持っていません。"), ephemeral=True
            )
        short, prices, now = row["side"] == "short", self._prices(), time.time()
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
        reply = await _confirmed_reply(
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
            return await reply(embed=ui.error_embed("できませんでした", got), ephemeral=True)
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

    # ------------------------------ 注文 ------------------------------ #
    @group.command(name="注文", description="買う・信用買い・空売り・債券を買う（銘柄と金額を選ぶだけ）")
    @app_commands.rename(
        symbol="銘柄",
        amount="金額",
        order_type="注文種別",
        leverage="レバレッジ",
        counterpart="相手",
        target="目標割合",
    )
    @app_commands.describe(
        symbol="買う銘柄か債券",
        amount="使うペリカ（信用買い・空売りは建てる総額、コール・プット・ペアは掛け金、裸売りは証拠金）",
        order_type="省略すると現物買い（債券は現物買いのみ）",
        leverage="信用買い・空売りの倍率（省略で1倍。大きいほど損益が大きく、強制決済されやすい）",
        counterpart="ペア・ワラントで、比べる相手の銘柄（銘柄が相手より上がると当たり）",
        target="コール・プットの、当たりの基準（現在値から何%動くか。1〜50。省略すると、銘柄ごとの既定値。大きいほど当たりにくく、当たると大きい）",
    )
    @app_commands.choices(
        symbol=ORDER_CHOICES,
        order_type=[
            app_commands.Choice(name="現物買い", value="spot"),
            app_commands.Choice(name="信用買い", value="long"),
            app_commands.Choice(name="空売り", value="short"),
            app_commands.Choice(name="爆騰コール（6時間・上がると当たり）", value="call"),
            app_commands.Choice(name="暴落プット（6時間・下がると当たり）", value="put"),
            app_commands.Choice(name="ペア・ワラント（銘柄が「相手」より上がると当たり）", value="pair"),
            app_commands.Choice(name="コール裸売り（プレミアムを先にもらう・上がると損）", value="scall"),
            app_commands.Choice(name="プット裸売り（プレミアムを先にもらう・下がると損）", value="sput"),
        ],
        leverage=[app_commands.Choice(name=f"{n}倍", value=n) for n in Economy.SHORT_LEVERAGES],
        counterpart=SYMBOL_CHOICES,
    )
    @app_commands.autocomplete(amount=_buy_amount_autocomplete)
    async def order(
        self,
        interaction: discord.Interaction,
        symbol: app_commands.Choice[str],
        amount: app_commands.Range[int, 10, 10_000_000],
        order_type: Optional[app_commands.Choice[str]] = None,
        leverage: Optional[app_commands.Choice[int]] = None,
        counterpart: Optional[app_commands.Choice[str]] = None,
        target: Optional[app_commands.Range[int, 1, opt.MAX_TARGET_PCT]] = None,
    ):
        kind = order_type.value if order_type else "spot"
        lev = leverage.value if leverage else 1
        if symbol.value.startswith(BOND_PREFIX):
            if kind != "spot" or lev != 1:
                return await interaction.response.send_message(
                    embed=ui.error_embed(
                        "債券は現物買いだけです",
                        "債券には、信用買いも空売りも、コール・プットも、レバレッジもありません。",
                    ),
                    ephemeral=True,
                )
            return await self._bond_buy(interaction, symbol.value[len(BOND_PREFIX) :], amount)
        if kind in (opt.CALL, opt.PUT, opt.PAIR, "scall", "sput"):
            if lev != 1:
                return await interaction.response.send_message(
                    embed=ui.error_embed(
                        "コール・プット・ペア・裸売りにレバレッジはありません",
                        "買いの損失は払った掛け金だけ、裸売りは証拠金（行使価格の20%）で決まります。",
                    ),
                    ephemeral=True,
                )
            if target is not None and kind in (opt.PAIR,):
                return await interaction.response.send_message(
                    embed=ui.error_embed(
                        "目標割合は、コール・プットだけです", "ペア・ワラントには、行使価格の基準がありません。"
                    ),
                    ephemeral=True,
                )
            if kind in ("scall", "sput"):
                return await self._option_write(interaction, kind[1:], symbol.value, amount, target)
            return await self._option_buy(
                interaction, kind, symbol.value, amount, counterpart.value if counterpart else None, target
            )
        if kind in ("short", "long"):
            return await self._margin_order(interaction, kind, symbol.value, amount=amount, leverage=lev)
        if lev != 1:
            return await interaction.response.send_message(
                embed=ui.error_embed(
                    "現物買いにレバレッジはありません", "レバレッジは、信用買いと空売りのときだけ選べます。"
                ),
                ephemeral=True,
            )
        if amount > 1_000_000:
            return await interaction.response.send_message(
                embed=ui.error_embed("金額が大きすぎます", "現物は、1回に1,000,000ペリカまで買えます。"), ephemeral=True
            )
        await self._buy(interaction, symbol.value, amount)

    # ------------------------------ 保有 ------------------------------ #
    def positions(self, uid: int, guild_id: int) -> list:
        """決済できる持ち物 [(コード, ボタンの名前), ...]。コードは spot:銘柄 / long:銘柄 / short:銘柄 / bond:番号。"""
        eco, out = self.bot.economy.for_guild(guild_id), []
        for symbol, _units, _cost in eco.holdings(uid):
            out.append((f"spot:{symbol}", f"{market.BY_KEY[symbol].emoji} {market.BY_KEY[symbol].name} 全売却"))
        for side, symbol, *_ in eco.margin_positions(uid):
            name = market.BY_KEY[symbol]
            out.append((f"{side}:{symbol}", f"{name.emoji} {name.name} {Economy.SIDE_NAME[side]}全決済"))
        for r in eco.option_list(uid)["active"]:
            short = r["side"] == "short"
            icon = "🎰" if short else opt.KINDS[r["kind"]][1]
            out.append((f"opt:{r['id']}", f"{icon} {name_of(r)} #{r['id']} {'買い戻し' if short else '売却'}"))
        for r in eco.bond_list(uid)["active"]:
            k = bonds.KINDS[r["kind"]]
            if k.sellable:
                out.append((f"bond:{r['id']}", f"{k.emoji} {k.name} #{r['id']} 売却"))
        return out

    async def settle_position(self, interaction: discord.Interaction, code: str, ratio=None) -> None:
        """positions のコードで指す持ち物を決済する（現物は売る・信用取引は反対売買・オプションと債券は満期前に売却）。ratio は半分か全部。"""
        kind, _, target = code.partition(":")
        if kind == "spot" and target in market.BY_KEY:
            return await self._sell(interaction, target, None, ratio)
        if kind in ("short", "long") and target in market.BY_KEY:
            return await self._margin_close(interaction, kind, target, None, ratio)
        if kind == "opt" and target.isdigit():
            return await self._option_sell(interaction, int(target))
        if kind == "bond" and target.isdigit():
            return await self._bond_cancel(interaction, int(target))
        await interaction.response.send_message(
            embed=ui.error_embed("決済できません", "決済する持ち物を、候補から選んでください。"), ephemeral=True
        )

    def holdings_embed(self, uid: int, guild_id: int) -> discord.Embed:
        return holdings_embed(self.bot.economy, uid, guild_id, RNG)

    @group.command(name="保有", description="現物・信用取引・債券をまとめて見る（ボタンで、すぐ決済できる）")
    async def holdings(self, interaction: discord.Interaction):
        uid, gid = interaction.user.id, interaction.guild.id
        embed = self.holdings_embed(uid, gid)
        view = HoldingsView(self, uid, gid)
        # ボタンが無いときは、view を渡さない（None を渡すと discord.py が落ちる）
        kwargs = {"view": view} if view.children else {}
        await interaction.response.send_message(embed=embed, ephemeral=True, **kwargs)

    # ------------------------------ 決済 ------------------------------ #
    async def _position_autocomplete(self, interaction: discord.Interaction, current: str):
        found = [
            app_commands.Choice(name=label, value=code)
            for code, label in self.positions(interaction.user.id, interaction.guild.id)
            if current.lower() in label.lower()
        ]
        return found[:25]

    @group.command(name="決済", description="持っている現物・空売り・債券を、割合（%）を決めて手放す")
    @app_commands.rename(position="ポジション", ratio="数量", percent="割合")
    @app_commands.describe(
        position="決済する持ち物（候補から選ぶ）",
        ratio="半分か全部（省略で全部。債券は全部だけ）",
        percent="手放す割合（1〜100%の数字。省略で全部。債券は全部だけ）",
    )
    @app_commands.choices(
        ratio=[app_commands.Choice(name="半分", value="half"), app_commands.Choice(name="全部", value="all")]
    )
    @app_commands.autocomplete(position=_position_autocomplete)
    async def settle(
        self,
        interaction: discord.Interaction,
        position: str,
        ratio: Optional[app_commands.Choice[str]] = None,
        percent: Optional[app_commands.Range[int, 1, 100]] = None,
    ):
        if ratio is not None and percent is not None:
            return await interaction.response.send_message(
                embed=ui.error_embed(
                    "どちらか1つにしてください", "「数量」と「割合」は、どちらか一方だけ指定できます。"
                ),
                ephemeral=True,
            )
        await self.settle_position(interaction, position, ratio if percent is None else SimpleNamespace(value=percent))

    # ------------------------------ 債券 ------------------------------ #
    async def _bond_buy(self, interaction: discord.Interaction, kind_key: str, amount: int) -> None:
        eco = self.g(interaction)
        eco.bond_settle(time.time(), RNG)  # 満期の分を先に入金して、買える額を正しくする
        if amount < bonds.MIN_AMOUNT:
            return await interaction.response.send_message(
                embed=ui.error_embed("買えません", f"債券は {bonds.MIN_AMOUNT:,}ペリカ以上から買えます。"),
                ephemeral=True,
            )
        ok, reason = eco.bond_buy(interaction.user.id, kind_key, amount, time.time())
        if not ok:
            return await interaction.response.send_message(embed=ui.error_embed("買えません", reason), ephemeral=True)
        await interaction.response.send_message(embed=bond_buy_embed(kind_key, amount, time.time()))

    async def _bond_cancel(self, interaction: discord.Interaction, bond_id: int) -> None:
        ok, result = self.g(interaction).bond_cancel(interaction.user.id, bond_id)
        if not ok:
            return await interaction.response.send_message(embed=ui.error_embed("売れません", result), ephemeral=True)
        await interaction.response.send_message(  # 公開（ほかの売買の結果と同じ）
            embed=ui.ok_embed(
                f"{interaction.user.display_name} が債券を売りました", f"{ui.pelica(result)}を残高に戻しました。"
            )
        )

    # ------------------------------ 出したチャートの自動更新 ------------------------------ #
    async def _refresh_post(self, post: dict, now: float, cache: dict) -> None:
        """出したチャートを、最新に書き換える。時間が来たら、最後に1回だけ書き換えて、更新を終える。"""
        ends = post["ts"] + LIVE_MINUTES * 60
        expired = now >= ends or post["guild_id"] <= 0
        channel = self.bot.get_channel(post["channel_id"])
        if channel is None:
            if expired or self.bot.get_guild(post["guild_id"]) is None:
                self.bot.economy.end_chart_post(post["channel_id"])
            return
        if post["guild_id"] > 0:
            key = (post["guild_id"], post["period"], post["symbol"], expired, int(ends // 60))
            if key not in cache:  # 同じ表示は、1回だけ作って使い回す（重い集計を重ねない）
                cache[key] = self.chart_embed(
                    post["period"],
                    guild_id=post["guild_id"],
                    live_until=None if expired else ends,
                    ended=expired,
                    symbol=post["symbol"],
                )
            try:  # 履歴を読む権限が要らないよう、投稿を取得せずに直接書き換える
                await channel.get_partial_message(post["message_id"]).edit(embed=cache[key])
            except discord.NotFound:
                self.bot.economy.delete_chart_post(post["channel_id"])  # 消されたチャートは、もう更新しない
                return
            except discord.HTTPException as exc:
                logthrottle.warn(
                    ("chart", post["channel_id"]), f"チャートを更新できません: channel={post['channel_id']}: {exc!r}"
                )
        if expired:
            self.bot.economy.end_chart_post(post["channel_id"])

    async def _retire_old_boards(self) -> None:
        """廃止した「チャート掲示」が残っていれば、メッセージを消して、設定も捨てる（1回だけ）。"""
        for board in self.bot.economy.list_chart_boards():
            channel = self.bot.get_channel(board["channel_id"])
            if channel is not None:
                try:
                    await channel.get_partial_message(board["message_id"]).delete()
                except discord.HTTPException as exc:  # すでに消えている場合など。設定は捨てるので、記録だけ残す
                    logthrottle.warn(
                        ("chart-board", board["guild_id"]),
                        f"廃止したチャート掲示を消せません: guild={board['guild_id']}: {exc!r}",
                    )
            self.bot.economy.delete_chart_board(board["guild_id"])

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        self.bot.economy.delete_guild_chart_posts(guild.id)


async def setup(bot):
    await bot.add_cog(MarketCog(bot))
