"""/証券（注文・保有・市況・決済）— 4銘柄（CSトークン・TEIO重工・NUMAバイオ・GOLD地金）と債券の売買、価格のチャート。価格は全サーバー共通で、1分ごとに、相関をもって動く。"""

import random
import time
from types import SimpleNamespace
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs import _market_orders as orders
from cogs._amount import balance_choices, own_balance, typed_amount
from cogs._bond_embeds import KIND_CHOICES
from cogs._market_embeds import (
    CORRELATION_MINUTES,
    EVENT_TEXT,  # noqa: F401
    HOLDER_ROWS,
    LIVE_MINUTES,
    TICK_MINUTES,
    chart_embed,
    correlation_lines,
    correlation_table,
    cut_lines,
    earnings_announce_embed,
    earnings_lines,
    earnings_result_embed,
    event_lines,
    holdings_embed,
    pin_lines,
    summary_embed,
)
from cogs._market_embeds import alert_lines as _alert_lines
from cogs._market_views import HoldingsView, MarketView
from cogs._market_views import OptionAmountModal as OptionAmountModal  # noqa: F401
from cogs._shared import announce_channel
from core import approval, bonds, catalogs, earnings, logthrottle, market, market_events, ui
from core import options as opt
from core.eco_options import name_of
from core.economy import Economy

RNG = random.SystemRandom()
KEEP_DAYS = catalogs.market().market.keep_days  # 価格の履歴を残す日数
CONFIRM_FROM = orders.CONFIRM_FROM
PERIOD_CHOICES = [app_commands.Choice(name=label, value=key) for key, (label, _) in market.PERIODS.items()]
SYMBOL_CHOICES = [app_commands.Choice(name=f"{s.emoji} {s.name}", value=s.key) for s in market.SYMBOLS]
BOND_PREFIX = "bond_"
ORDER_CHOICES = SYMBOL_CHOICES + [
    app_commands.Choice(name=f"債券: {c.name}", value=BOND_PREFIX + c.value) for c in KIND_CHOICES
]

_confirmed_reply = orders.default_confirmed_reply
_reject_lots_and_ratio = orders.reject_lots_and_ratio
_units_from = orders.units_from
_worth = orders.worth


class MarketCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._earnings_results = []  # 発表した決算（チャンネルへの投稿待ち）

    def g(self, interaction: discord.Interaction):
        return self.bot.economy.for_guild(ui.guild_id_of(interaction))

    group = app_commands.Group(
        name="証券", description="📈 注文・保有・市況・決済（4銘柄と債券。ゲーム内の通貨・現実のお金とは無関係）"
    )

    # ------------------------------ 価格の更新 ------------------------------ #
    def _price(self, symbol: str = market.DEFAULT) -> float:
        latest = self.bot.economy.market_latest(symbol)
        if latest is None:
            start = market.BY_KEY[symbol].start
            self.bot.economy.market_set(time.time(), start, 0, symbol)
            return start
        return latest[1]

    def _prices(self) -> dict[str, float]:
        return {s.key: self._price(s.key) for s in market.SYMBOLS}

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

    async def cog_unload(self) -> None:
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
                    await ui.send_channel(channel, embed=embed, allowed_mentions=discord.AllowedMentions.none())
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
    def _histories_24h(self, now: float) -> dict[str, list]:
        since = now - CORRELATION_MINUTES * 60
        return {s.key: self.bot.economy.market_history(since, s.key) for s in market.SYMBOLS}

    def correlation_lines(self, symbol: str) -> str:
        return correlation_lines(symbol, self._histories_24h(time.time()))

    def chart_embed(
        self,
        period: str = "24h",
        title_prefix: str = "",
        guild_id: Optional[int] = None,
        live_until: Optional[float] = None,
        ended: bool = False,
        symbol: str = market.DEFAULT,
    ) -> discord.Embed:
        now = time.time()
        _label, seconds = market.PERIODS[period]
        hist = self.bot.economy.market_history(now - seconds, symbol)
        pin = self.bot.economy.market_pin(symbol)
        hist_24h = self._histories_24h(now)
        holders = (
            self.bot.economy.for_guild(guild_id).crypto_holders(HOLDER_ROWS, symbol) if guild_id is not None else ()
        )
        shorts = self.bot.economy.for_guild(guild_id).short_holders(HOLDER_ROWS, symbol) if guild_id is not None else ()
        return chart_embed(
            symbol,
            period,
            hist,
            pin,
            hist_24h,
            holders=holders,
            shorts=shorts,
            title_prefix=title_prefix,
            live_until=live_until,
            ended=ended,
            now=now,
        )

    def pin_lines(self, symbol: str) -> str:
        return pin_lines(self.bot.economy.market_pin(symbol))

    def correlation_table(self) -> str:
        return correlation_table(self._histories_24h(time.time()))

    def event_lines(self) -> str:
        now = time.time()
        return event_lines(self.bot.economy.market_events(now), now)

    def earnings_lines(self) -> str:
        return earnings_lines(self.bot.economy.earnings_upcoming(time.time()))

    def alert_lines(self) -> str:
        since = time.time() - (market_events.ALERT_WINDOW + 5) * 60
        history = {s.key: [p for _, p, _ in self.bot.economy.market_history(since, s.key)] for s in market.SYMBOLS}
        return _alert_lines(history)

    def cut_lines(self, guild_id: int) -> str:
        prices = self._prices()
        return cut_lines(prices, self.bot.economy.for_guild(guild_id).margin_cut_map(prices))

    def summary_embed(self, guild_id: Optional[int] = None) -> discord.Embed:
        now = time.time()
        hist_24h = self._histories_24h(now)
        latest = self._prices()
        upcoming = self.bot.economy.earnings_upcoming(now)
        ev_rows = self.bot.economy.market_events(now)
        since_alert = now - (market_events.ALERT_WINDOW + 5) * 60
        alert_histories = {
            s.key: [p for _, p, _ in self.bot.economy.market_history(since_alert, s.key)] for s in market.SYMBOLS
        }
        cut_rows = self.bot.economy.for_guild(guild_id).margin_cut_map(latest) if guild_id else []
        return summary_embed(hist_24h, latest, upcoming, ev_rows, alert_histories, cut_rows, now)

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
        gid = ui.guild_id_of(interaction)
        if symbol is None:
            view = MarketView(self, interaction.user.id, gid, period.value if period else "24h")
            return await interaction.response.send_message(embed=self.summary_embed(gid), view=view, ephemeral=True)
        await self._post_chart(interaction, symbol.value, period.value if period else "24h")

    async def _post_chart(self, interaction: discord.Interaction, sym: str, key: str) -> None:
        now = time.time()
        gid = ui.guild_id_of(interaction)
        await interaction.response.send_message(
            embed=self.chart_embed(key, guild_id=gid, live_until=now + LIVE_MINUTES * 60, symbol=sym),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        # チャートは1チャンネルに1つだけ。前に出したチャートは消し、新しいものを、一定時間だけ自動で更新する
        try:
            message = await interaction.original_response()
            prev = self.bot.economy.swap_chart_post(message.channel.id, message.id, gid, key, now, sym)
            if prev is not None and prev != message.id:
                await ui.partial_message(message.channel, prev).delete()
        except discord.HTTPException as exc:  # 前のチャートがすでに無い・消せないときは、そのままにする（記録だけ残す）
            logthrottle.warn(("chart-swap", gid), f"前のチャートを消せません: guild={gid}: {exc!r}")

    # ------------------------------ 売買 ------------------------------ #
    async def _trade(self, interaction: discord.Interaction, action):
        """売買（action(uid)）の間は「プレイ中」にして、ゲームの最中に残高を動かせないようにする（ショップの購入と同じ）。"""
        await orders.run_trade(self.bot, interaction, action)

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
        await orders.buy_spot(self, interaction, symbol, amount, confirm_fn=_confirmed_reply)

    async def _cover(self, interaction, symbol: str, lots, ratio) -> None:
        await self._margin_close(interaction, "short", symbol, lots, ratio)

    async def _margin_close(self, interaction, side: str, symbol: str, lots, ratio) -> None:
        await orders.margin_close(self, interaction, side, symbol, lots, ratio, confirm_fn=_confirmed_reply)

    async def _sell(self, interaction: discord.Interaction, symbol: str, lots, ratio) -> None:
        await orders.sell_spot(self, interaction, symbol, lots, ratio, confirm_fn=_confirmed_reply)

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
        await orders.margin_order(
            self, interaction, side, symbol, lots=lots, amount=amount, leverage=leverage, confirm_fn=_confirmed_reply
        )

    async def _option_buy(
        self,
        interaction: discord.Interaction,
        kind: str,
        symbol: str,
        amount: int,
        symbol2: Optional[str] = None,
        target: Optional[int] = None,
    ) -> None:
        await orders.option_buy(
            self, interaction, kind, symbol, amount, symbol2=symbol2, target=target, confirm_fn=_confirmed_reply
        )

    async def _option_write(
        self, interaction: discord.Interaction, kind: str, symbol: str, amount: int, target: Optional[int] = None
    ) -> None:
        await orders.option_write(self, interaction, kind, symbol, amount, target=target, confirm_fn=_confirmed_reply)

    async def _option_sell(self, interaction: discord.Interaction, option_id: int) -> None:
        await orders.option_sell(self, interaction, option_id, confirm_fn=_confirmed_reply)

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
        counterpart=SYMBOL_CHOICES,
    )
    @app_commands.choices(leverage=[app_commands.Choice(name=f"{n}倍", value=n) for n in Economy.SHORT_LEVERAGES])
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
        now = time.time()
        guild_eco = self.bot.economy.for_guild(guild_id)
        guild_eco.bond_settle(now, RNG)  # 満期の分を先に入金する
        guild_eco.option_settle(now)
        prices = self._prices()
        options = guild_eco.option_list(uid)
        spots = {symbol: (units, cost) for symbol, units, cost in guild_eco.holdings(uid)}
        margins = {
            (side, symbol): (units, entry, margin)
            for side, symbol, units, entry, margin in guild_eco.margin_positions(uid)
        }
        bond_data = guild_eco.bond_list(uid)
        option_quotes = {
            r["id"]: (
                guild_eco.option_close_cost(r, now, prices)
                if r["side"] == "short"
                else guild_eco.option_value(r, now, prices)
            )
            for r in options["active"]
        }
        bond_quotes = {
            r["id"]: guild_eco.bond_price(r, now) for r in bond_data["active"] if bonds.KINDS[r["kind"]].sellable
        }
        margin_status = guild_eco.margin_status(uid, prices) if margins else None
        return holdings_embed(prices, spots, margins, options, bond_data, option_quotes, bond_quotes, margin_status)

    @group.command(name="保有", description="現物・信用取引・債券をまとめて見る（ボタンで、すぐ決済できる）")
    async def holdings(self, interaction: discord.Interaction):
        uid, gid = interaction.user.id, ui.guild_id_of(interaction)
        embed = self.holdings_embed(uid, gid)
        view = HoldingsView(self, uid, gid)
        # ボタンが無いときは、view を渡さない（None を渡すと discord.py が落ちる）
        if view.children:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------ 決済 ------------------------------ #
    async def _position_autocomplete(self, interaction: discord.Interaction, current: str):
        found = [
            app_commands.Choice(name=label, value=code)
            for code, label in self.positions(interaction.user.id, ui.guild_id_of(interaction))
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
        await orders.bond_buy(self, interaction, kind_key, amount)

    async def _bond_cancel(self, interaction: discord.Interaction, bond_id: int) -> None:
        await orders.bond_cancel(self, interaction, bond_id)

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
                await ui.partial_message(channel, post["message_id"]).edit(embed=cache[key])
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
                    await ui.partial_message(channel, board["message_id"]).delete()
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
