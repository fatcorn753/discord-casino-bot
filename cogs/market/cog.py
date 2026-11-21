"""/証券（注文・保有・市況・決済）— 4銘柄（CSトークン・TEIO重工・NUMAバイオ・GOLD地金）と債券の売買、価格のチャート。"""

import logging
import random
from typing import TYPE_CHECKING, Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.market import feed, orders
from cogs.market.bond_embeds import KIND_CHOICES
from cogs.market.embeds import (
    CORRELATION_MINUTES,
    HOLDER_ROWS,
    LIVE_MINUTES,
    TICK_MINUTES,
    chart_embed,
    holdings_embed,
    summary_embed,
)
from cogs.market.views import HoldingsView, MarketView
from cogs.market.views import OptionAmountModal as OptionAmountModal
from cogs.shared import logthrottle, ui
from cogs.shared.amount import balance_choices, own_balance, typed_amount
from cogs.shared.announce import announce_channel
from domain import market, market_events
from domain import options as opt
from domain.ports import get_catalogs
from domain.ports.repos import IGuildRepo
from domain.ports.session import ISessionGuard
from services.economy import EconomyService
from services.market import MarketService
from services.teiai import TeiaiService

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot

RNG = random.SystemRandom()
KEEP_DAYS = get_catalogs().market().market.keep_days  # 価格の履歴を残す日数
CONFIRM_FROM = orders.CONFIRM_FROM
PERIOD_CHOICES = [app_commands.Choice(name=label, value=key) for key, (label, _) in market.PERIODS.items()]
SYMBOL_CHOICES = [app_commands.Choice(name=f"{s.emoji} {s.name}", value=s.key) for s in market.SYMBOLS]
LEVERAGE_CHOICES = [app_commands.Choice(name=f"{n}倍", value=n) for n in market.SHORT_LEVERAGES]
SPOT_MAX = 1_000_000  # 現物は、1回にこの額まで買える
AMOUNT_MAX = 10_000_000  # 現物以外の金額の欄の上限
PERCENT_STEPS = (25, 50, 75, 100)  # 金額・割合の候補に出す %


class MarketCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        config: dict[str, Any],
        market_svc: MarketService,
        economy_svc: EconomyService,
        teiai_svc: TeiaiService,
        guild_repo: IGuildRepo,
        sessions: ISessionGuard,
    ):
        self.bot = bot
        self._config = config
        self._market_svc = market_svc
        self._economy_svc = economy_svc
        self._teiai = teiai_svc
        self._guild_repo = guild_repo
        self._sessions = sessions
        self._earnings_results = []  # 発表した決算（チャンネルへの投稿待ち）

    group = app_commands.Group(
        name="証券", description="📈 注文・保有・市況・決済（4銘柄と債券。ゲーム内の通貨・現実のお金とは無関係）"
    )
    # 種別ごとにサブコマンドを分け、その種別に要る欄だけを出す(#79)
    order_group = app_commands.Group(
        name="注文", description="現物・信用・コール・プット・ペア・裸売り・債券を注文する", parent=group
    )

    # ------------------------------ 価格の更新（MarketService へ委譲） ------------------------------ #
    def step_prices(self) -> tuple:
        return self._market_svc.step_prices(self._earnings_results, RNG)

    async def cog_load(self):
        self._market_svc.latest_prices()  # 初回は開始価格を入れる（起動直後でもコマンドが使えるように）
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    @tasks.loop(minutes=TICK_MINUTES)
    async def tick(self):
        try:
            prices, _event = self.step_prices()
            for guild_id, user_id, symbol, side, loss in self._market_svc.accrue_and_losscut(
                self._market_svc.clock.now_ts(), prices
            ):
                _log.info(
                    f"{market.SIDE_NAME[side]}をロスカット: guild={guild_id} user={user_id} {symbol} 損失={loss:,}"
                )
            self._market_svc.prune(self._market_svc.clock.now_ts() - KEEP_DAYS * 86400)
        except Exception as exc:
            _log.warning(f"価格の更新に失敗: {exc!r}")
            return
        try:
            await self._earnings_posts()
        except Exception as exc:
            _log.warning(f"決算の告知に失敗: {exc!r}")
        now, cache = self._market_svc.clock.now_ts(), {}
        for post in self._market_svc.live_chart_posts():
            try:
                await feed.refresh_post(self.bot, self._market_svc, self, post, now, cache)
            except Exception as exc:
                _log.warning(f"チャートの更新に失敗: {post}: {exc!r}")

    async def _earnings_posts(self) -> None:
        await feed.earnings_posts(
            self.bot,
            self._config,
            self._market_svc,
            self._guild_repo,
            self._earnings_results,
            announce_fn=announce_channel,
        )

    @tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()
        try:
            await feed.retire_old_boards(self.bot, self._market_svc)
        except Exception as exc:
            _log.warning(f"古いチャート掲示の後始末に失敗: {exc!r}")

    # ------------------------------ 表示 ------------------------------ #
    def chart_embed(
        self,
        period: str = "24h",
        title_prefix: str = "",
        guild_id: Optional[int] = None,
        live_until: Optional[float] = None,
        ended: bool = False,
        symbol: str = market.DEFAULT,
    ) -> discord.Embed:
        now = self._market_svc.clock.now_ts()
        _label, seconds = market.PERIODS[period]
        hist = self._market_svc.market_history(now - seconds, symbol)
        pin = self._market_svc.market_pin(symbol)
        hist_24h = self._market_svc.histories(CORRELATION_MINUTES, now)
        holders = self._market_svc.crypto_holders(guild_id, HOLDER_ROWS, symbol) if guild_id is not None else ()
        shorts = self._market_svc.short_holders(guild_id, HOLDER_ROWS, symbol) if guild_id is not None else ()
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

    def summary_embed(self, guild_id: Optional[int] = None) -> discord.Embed:
        now = self._market_svc.clock.now_ts()
        hist_24h = self._market_svc.histories(CORRELATION_MINUTES, now)
        latest = self._market_svc.latest_prices()
        upcoming = self._market_svc.earnings_upcoming(now)
        ev_rows = self._market_svc.market_events(now)
        since_alert = now - (market_events.ALERT_WINDOW + 5) * 60
        alert_histories = {
            s.key: [p for _, p, _ in self._market_svc.market_history(since_alert, s.key)] for s in market.SYMBOLS
        }
        cut_rows = self._market_svc.margin_cut_map(guild_id, latest) if guild_id else []
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
        now = self._market_svc.clock.now_ts()
        gid = ui.guild_id_of(interaction)
        await interaction.response.send_message(
            embed=self.chart_embed(key, guild_id=gid, live_until=now + LIVE_MINUTES * 60, symbol=sym),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            message = await interaction.original_response()
            prev = self._market_svc.swap_chart_post(message.channel.id, message.id, gid, key, now, sym)
            if prev is not None and prev != message.id:
                await ui.partial_message(message.channel, prev).delete()
        except discord.HTTPException as exc:
            logthrottle.warn(("chart-swap", gid), f"前のチャートを消せません: guild={gid}: {exc!r}")

    # ------------------------------ 金額・割合の候補 ------------------------------ #
    def _amount_choices(self, interaction: discord.Interaction, current: str, cap: int) -> list:
        """残高の1行に、何も打っていないときだけ、残高の25/50/75/100%（cap まで）を足す。"""
        choices = balance_choices(self._economy_svc, interaction, current, 10, cap)
        balance = own_balance(self._economy_svc, interaction.guild_id, interaction.user.id)
        if balance is not None and typed_amount(current) is None:
            for pct in PERCENT_STEPS:
                value = min(cap, balance * pct // 100)
                if value >= 10:
                    choices.append(app_commands.Choice(name=f"残高の{pct}%（{value:,}ペリカ）", value=value))
        return choices[:25]

    async def _buy_amount_autocomplete(self, interaction: discord.Interaction, current: str):
        return self._amount_choices(interaction, current, AMOUNT_MAX)

    async def _spot_amount_autocomplete(self, interaction: discord.Interaction, current: str):
        return self._amount_choices(interaction, current, SPOT_MAX)

    # ------------------------------ 注文 ------------------------------ #
    @order_group.command(name="現物", description="銘柄を、金額ぶん買う（手数料なし・小数で買える）")
    @app_commands.rename(symbol="銘柄", amount="金額")
    @app_commands.describe(symbol="買う銘柄", amount=f"使うペリカ（1回に{SPOT_MAX:,}まで）")
    @app_commands.choices(symbol=SYMBOL_CHOICES)
    @app_commands.autocomplete(amount=_spot_amount_autocomplete)
    async def order_spot(
        self,
        interaction: discord.Interaction,
        symbol: app_commands.Choice[str],
        amount: app_commands.Range[int, 10, SPOT_MAX],
    ):
        await orders.buy_spot(self, interaction, symbol.value, amount)

    @order_group.command(name="信用", description="信用買い（上がると儲かる）か空売り（下がると儲かる）を建てる")
    @app_commands.rename(symbol="銘柄", side="売買", amount="証拠金", leverage="レバレッジ")
    @app_commands.describe(
        symbol="建てる銘柄",
        side="信用買い（上がると利益）か空売り（下がると利益）",
        amount="預ける証拠金。建てる総額は 証拠金 × レバレッジ（価格で割り切れない分は切り捨て）",
        leverage="倍率（省略で1倍。大きいほど損益が大きく、強制決済されやすい）",
    )
    @app_commands.choices(
        symbol=SYMBOL_CHOICES,
        side=[
            app_commands.Choice(name="信用買い（上がると利益）", value="long"),
            app_commands.Choice(name="空売り（下がると利益）", value="short"),
        ],
    )
    @app_commands.choices(leverage=LEVERAGE_CHOICES)
    @app_commands.autocomplete(amount=_buy_amount_autocomplete)
    async def order_margin(
        self,
        interaction: discord.Interaction,
        symbol: app_commands.Choice[str],
        side: app_commands.Choice[str],
        amount: app_commands.Range[int, 10, AMOUNT_MAX],
        leverage: Optional[app_commands.Choice[int]] = None,
    ):
        lev = leverage.value if leverage else 1
        await orders.margin_order(self, interaction, side.value, symbol.value, amount, lev)

    @order_group.command(
        name="コール", description="爆騰コール（6時間後に、基準より上がっていれば当たり。損失は掛け金だけ）"
    )
    @app_commands.rename(symbol="銘柄", amount="掛け金", target="目標割合")
    @app_commands.describe(
        symbol="上がると思う銘柄",
        amount="払う掛け金（最大の損失はこの額だけ）",
        target="当たりの基準（現在値から何%上か。1〜50。省略すると銘柄ごとの既定値。大きいほど当たりにくく、当たると大きい）",
    )
    @app_commands.choices(symbol=SYMBOL_CHOICES)
    @app_commands.autocomplete(amount=_buy_amount_autocomplete)
    async def order_call(
        self,
        interaction: discord.Interaction,
        symbol: app_commands.Choice[str],
        amount: app_commands.Range[int, 10, AMOUNT_MAX],
        target: Optional[app_commands.Range[int, 1, opt.MAX_TARGET_PCT]] = None,
    ):
        await orders.option_buy(self, interaction, opt.CALL, symbol.value, amount, target=target)

    @order_group.command(
        name="プット", description="暴落プット（6時間後に、基準より下がっていれば当たり。損失は掛け金だけ）"
    )
    @app_commands.rename(symbol="銘柄", amount="掛け金", target="目標割合")
    @app_commands.describe(
        symbol="下がると思う銘柄",
        amount="払う掛け金（最大の損失はこの額だけ）",
        target="当たりの基準（現在値から何%下か。1〜50。省略すると銘柄ごとの既定値。大きいほど当たりにくく、当たると大きい）",
    )
    @app_commands.choices(symbol=SYMBOL_CHOICES)
    @app_commands.autocomplete(amount=_buy_amount_autocomplete)
    async def order_put(
        self,
        interaction: discord.Interaction,
        symbol: app_commands.Choice[str],
        amount: app_commands.Range[int, 10, AMOUNT_MAX],
        target: Optional[app_commands.Range[int, 1, opt.MAX_TARGET_PCT]] = None,
    ):
        await orders.option_buy(self, interaction, opt.PUT, symbol.value, amount, target=target)

    @order_group.command(name="ペア", description="ペア・ワラント（6時間後に、銘柄の上昇率が相手を上回れば当たり）")
    @app_commands.rename(symbol="銘柄", counterpart="相手", amount="掛け金")
    @app_commands.describe(
        symbol="相手より上がると思う銘柄",
        counterpart="比べる相手の銘柄（銘柄とちがうもの）",
        amount="払う掛け金（最大の損失はこの額だけ）",
    )
    @app_commands.choices(symbol=SYMBOL_CHOICES, counterpart=SYMBOL_CHOICES)
    @app_commands.autocomplete(amount=_buy_amount_autocomplete)
    async def order_pair(
        self,
        interaction: discord.Interaction,
        symbol: app_commands.Choice[str],
        counterpart: app_commands.Choice[str],
        amount: app_commands.Range[int, 10, AMOUNT_MAX],
    ):
        await orders.option_buy(self, interaction, opt.PAIR, symbol.value, amount, symbol2=counterpart.value)

    @order_group.command(
        name="裸売り", description="コール・プットを帝愛に売り、プレミアムを先にもらう（大きく動くと借金）"
    )
    @app_commands.rename(symbol="銘柄", kind="種別", amount="証拠金", target="目標割合")
    @app_commands.describe(
        symbol="売る銘柄",
        kind="コール裸売り（上がると損）かプット裸売り（下がると損）",
        amount="預ける証拠金（行使価格 × 口数の20%）",
        target="行使価格（現在値から何%離すか。1〜50。省略すると銘柄ごとの既定値）",
    )
    @app_commands.choices(
        symbol=SYMBOL_CHOICES,
        kind=[
            app_commands.Choice(name="コール裸売り（上がると損）", value=opt.CALL),
            app_commands.Choice(name="プット裸売り（下がると損）", value=opt.PUT),
        ],
    )
    @app_commands.autocomplete(amount=_buy_amount_autocomplete)
    async def order_write(
        self,
        interaction: discord.Interaction,
        symbol: app_commands.Choice[str],
        kind: app_commands.Choice[str],
        amount: app_commands.Range[int, 10, AMOUNT_MAX],
        target: Optional[app_commands.Range[int, 1, opt.MAX_TARGET_PCT]] = None,
    ):
        await orders.option_write(self, interaction, kind.value, symbol.value, amount, target=target)

    @order_group.command(name="債券", description="通常債（元本保証）かジャンク債（高利回り・倒産あり）を買う")
    @app_commands.rename(kind="種類", amount="金額")
    @app_commands.describe(kind="債券の種類", amount="預ける元本（1,000ペリカ以上）")
    @app_commands.choices(kind=KIND_CHOICES)
    @app_commands.autocomplete(amount=_buy_amount_autocomplete)
    async def order_bond(
        self,
        interaction: discord.Interaction,
        kind: app_commands.Choice[str],
        amount: app_commands.Range[int, 10, AMOUNT_MAX],
    ):
        await orders.bond_buy(self, interaction, kind.value, amount)

    # ------------------------------ 保有・決済 ------------------------------ #
    def positions(self, uid: int, guild_id: int) -> list:
        return self._market_svc.positions(guild_id, uid)

    async def settle_position(self, interaction: discord.Interaction, code: str, percent: Optional[int] = None) -> None:
        """持ち物（code）を、percent（1〜100。省略で全部）だけ決済する。債券・オプションは全部だけ。"""
        kind, _, target = code.partition(":")
        if kind == "spot" and target in market.BY_KEY:
            return await orders.sell_spot(self, interaction, target, percent)
        if kind in ("short", "long") and target in market.BY_KEY:
            return await orders.margin_close(self, interaction, kind, target, percent)
        if kind in ("opt", "bond") and target.isdigit():
            if percent is not None and percent < 100:  # 黙って全部を決済しない(#80)
                await ui.reply_error(
                    interaction, "割合を省略するか、100%にしてください。", title="債券・オプションは全部だけです"
                )
                return
            if kind == "opt":
                return await orders.option_sell(self, interaction, int(target))
            return await orders.bond_cancel(self, interaction, int(target))
        await ui.reply_error(interaction, "決済する持ち物を、候補から選んでください。", title="決済できません")

    def holdings_embed(self, uid: int, guild_id: int) -> discord.Embed:
        snap = self._market_svc.holdings_snapshot(guild_id, uid, RNG)
        return holdings_embed(
            snap["prices"],
            snap["spots"],
            snap["margins"],
            snap["options"],
            snap["bond_data"],
            snap["option_quotes"],
            snap["bond_quotes"],
            snap["margin_status"],
        )

    async def open_holdings(self, interaction: discord.Interaction) -> None:
        """本人にだけ、保有パネルを出す（/証券 保有 と、市況の「保有を見る」ボタンから）。"""
        uid, gid = interaction.user.id, ui.guild_id_of(interaction)
        view = HoldingsView(self, uid, gid, origin=interaction)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    async def open_market(self, interaction: discord.Interaction) -> None:
        """本人にだけ、全銘柄の市況（チャートの切り替えつき）を出す。"""
        gid = ui.guild_id_of(interaction)
        view = MarketView(self, interaction.user.id, gid)
        await interaction.response.send_message(embed=self.summary_embed(gid), view=view, ephemeral=True)

    @group.command(
        name="保有", description="現物・信用取引・オプション・債券をまとめて見る（パネルから、すぐ決済できる）"
    )
    async def holdings(self, interaction: discord.Interaction):
        await self.open_holdings(interaction)

    async def _position_autocomplete(self, interaction: discord.Interaction, current: str):
        found = [
            app_commands.Choice(name=label, value=code)
            for code, label in self.positions(interaction.user.id, ui.guild_id_of(interaction))
            if current.lower() in label.lower()
        ]
        return found[:25]

    async def _percent_autocomplete(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name="全部（100%）" if p == 100 else f"{p}%", value=p) for p in PERCENT_STEPS]

    @group.command(name="決済", description="持っている現物・信用取引・オプション・債券を、割合（%）を決めて手放す")
    @app_commands.rename(position="ポジション", percent="割合")
    @app_commands.describe(
        position="決済する持ち物（候補から選ぶ）",
        percent="手放す割合（1〜100%。省略で全部。債券・オプションは全部だけ）",
    )
    @app_commands.autocomplete(position=_position_autocomplete, percent=_percent_autocomplete)
    async def settle(
        self,
        interaction: discord.Interaction,
        position: str,
        percent: Optional[app_commands.Range[int, 1, 100]] = None,
    ):
        await self.settle_position(interaction, position, percent)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        self._market_svc.delete_guild_chart_posts(guild.id)


async def setup(bot: "CasinoBot"):
    await bot.add_cog(
        MarketCog(
            bot,
            config=bot.config,
            market_svc=bot.services.market,
            economy_svc=bot.services.economy,
            teiai_svc=bot.services.teiai,
            guild_repo=bot.repos.guild,
            sessions=bot.sessions,
        )
    )
