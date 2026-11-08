"""/証券（注文・保有・市況・決済）— 4銘柄（CSトークン・TEIO重工・NUMAバイオ・GOLD地金）と債券の売買、価格のチャート。価格は全サーバー共通で、1分ごとに、相関をもって動く。"""

import math
import random
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs._amount import balance_choices
from cogs.bond_cog import KIND_CHOICES, describe_done, due_text
from core import bonds, logthrottle, market, ui
from core import options as opt
from core.confirm import confirm_action
from core.economy import Economy

RNG = random.SystemRandom()
TICK_MINUTES = 1
KEEP_DAYS = 30  # 価格の履歴を残す日数
HOLDER_ROWS = 10  # チャートに載せる保有者の数
LIVE_MINUTES = 60  # 出したチャートを自動で更新する時間（最後に使ってから）
CONFIRM_FROM = 5_000  # この額以上の売買は確認ボタンを挟む
EVENT_TEXT = {1: "📈 急騰！", -1: "📉 急落！"}
PERIOD_CHOICES = [app_commands.Choice(name=label, value=key) for key, (label, _) in market.PERIODS.items()]
SYMBOL_CHOICES = [app_commands.Choice(name=f"{s.emoji} {s.name}", value=s.key) for s in market.SYMBOLS]
BOND_PREFIX = "bond_"
ORDER_CHOICES = SYMBOL_CHOICES + [
    app_commands.Choice(name=f"債券: {c.name}", value=BOND_PREFIX + c.value) for c in KIND_CHOICES
]
CORRELATION_MINUTES = 24 * 60  # 銘柄どうしの連動を調べる、直近の長さ（分）


class HoldingsView(discord.ui.View):
    """/証券 保有 の、ボタンで決済する画面（開いた本人だけが操作できる）。"""

    def __init__(self, cog, user_id: int, guild_id: int):
        super().__init__(timeout=300)
        self.cog, self.user_id = cog, user_id
        for code, label in cog.positions(user_id, guild_id)[:20]:
            button = discord.ui.Button(label=label[:80], style=discord.ButtonStyle.secondary)
            button.callback = self._make(code)
            self.add_item(button)

    def _make(self, code: str):
        async def callback(interaction: discord.Interaction):
            await self.cog.settle_position(interaction, code)

        return callback

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("自分の `/証券 保有` で操作してください。", ephemeral=True)
            return False
        return True


class MarketView(discord.ui.View):
    """/証券 市況 の、銘柄と期間のチャートを切り替える画面（自動更新はしない。開いた本人だけが操作できる）。"""

    def __init__(self, cog, user_id: int, guild_id: int, period: str = "24h"):
        super().__init__(timeout=300)
        self.cog, self.user_id, self.guild_id = cog, user_id, guild_id
        self.symbol, self.period = None, period
        self.symbol_select = discord.ui.Select(
            placeholder="銘柄を選ぶ",
            options=[discord.SelectOption(label="📋 全銘柄の一覧", value="all")]
            + [discord.SelectOption(label=f"{s.emoji} {s.name}", value=s.key) for s in market.SYMBOLS],
        )
        self.period_select = discord.ui.Select(
            placeholder="期間を選ぶ",
            options=[discord.SelectOption(label=label, value=key) for key, (label, _) in market.PERIODS.items()],
        )
        self.symbol_select.callback = self._on_symbol
        self.period_select.callback = self._on_period
        self.add_item(self.symbol_select)
        self.add_item(self.period_select)

    def _embed(self) -> discord.Embed:
        if self.symbol is None:
            return self.cog.summary_embed()
        return self.cog.chart_embed(self.period, guild_id=self.guild_id, symbol=self.symbol)

    async def _on_symbol(self, interaction: discord.Interaction):
        value = self.symbol_select.values[0]
        self.symbol = None if value == "all" else value
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _on_period(self, interaction: discord.Interaction):
        self.period = self.period_select.values[0]
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("自分の `/証券 市況` で操作してください。", ephemeral=True)
            return False
        return True


class MarketCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def g(self, interaction: discord.Interaction):
        return self.bot.economy.for_guild(interaction.guild.id)

    @property
    def eco(self):
        return self.bot.economy

    group = app_commands.Group(
        name="証券", description="📈 注文・保有・市況・決済（4銘柄と債券。ゲーム内の通貨・現実のお金とは無関係）"
    )

    # ------------------------------ 価格の更新 ------------------------------ #
    def _price(self, symbol: str = market.DEFAULT) -> float:
        latest = self.eco.market_latest(symbol)
        if latest is None:
            start = market.BY_KEY[symbol].start
            self.eco.market_set(time.time(), start, 0, symbol)
            return start
        return latest[1]

    def _prices(self) -> dict:
        return {s.key: self._price(s.key) for s in market.SYMBOLS}

    def step_prices(self) -> tuple:
        """全銘柄の価格を1つ進めて、(新しい価格 {銘柄: 価格}, 有事の出来事) を返す。"""
        prices = self._prices()
        last_ts = max((self.eco.market_latest(k) or (0,))[0] for k in prices)
        new, event = market.next_prices(prices, RNG)
        ts = max(time.time(), last_ts + 0.001)  # 時計が戻っても、記録の順序を崩さない
        for key, price in new.items():
            self.eco.market_set(ts, price, event, key)
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
            for guild_id, user_id, symbol, side, loss in self.eco.margin_losscut(prices):
                print(f"{Economy.SIDE_NAME[side]}をロスカット: guild={guild_id} user={user_id} {symbol} 損失={loss:,}")
            self.eco.market_prune(time.time() - KEEP_DAYS * 86400)
            self.eco.prune_chart_posts(time.time() - KEEP_DAYS * 86400)
        except Exception as exc:
            print(f"価格の更新に失敗: {exc!r}")
            return
        now, cache = time.time(), {}
        for post in self.eco.live_chart_posts():
            try:
                await self._refresh_post(post, now, cache)
            except Exception as exc:  # 1つのチャンネルの失敗で、他の更新を止めない
                print(f"チャートの更新に失敗: {post}: {exc!r}")

    @tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()
        try:
            await self._retire_old_boards()
        except Exception as exc:
            print(f"古いチャート掲示の後始末に失敗: {exc!r}")

    # ------------------------------ 表示 ------------------------------ #
    def correlation_lines(self, symbol: str) -> str:
        """その銘柄と、ほかの銘柄の連動（直近24時間の1分ごとの値動きの相関）。"""
        since = time.time() - CORRELATION_MINUTES * 60
        hist = self.eco.market_history(since, symbol)
        lines = []
        for other in market.SYMBOLS:
            if other.key != symbol:
                value = market.correlation_from_history(hist, self.eco.market_history(since, other.key))
                lines.append(f"{other.emoji} {other.name}: {market.describe_correlation(value)}")
        return "\n".join(lines)

    def chart_embed(
        self,
        period: str = "24h",
        title_prefix: str = "",
        guild_id: Optional[int] = None,
        live_until: Optional[float] = None,
        ended: bool = False,
        symbol: str = market.DEFAULT,
    ) -> discord.Embed:
        sym = market.BY_KEY[symbol]
        label, seconds = market.PERIODS[period]
        hist = self.eco.market_history(time.time() - seconds, symbol)
        prices = [p for _, p, _ in hist]
        if not prices:
            return ui.embed(f"📈 {sym.name}", "まだ価格のデータがありません。", ui.INFO)
        s = market.summary(prices)
        last_event = next((e for _, _, e in reversed(hist[-5:]) if e), 0)
        color = ui.OK if s["change"] >= 0 else ui.ERROR
        arrow = "▲" if s["change"] >= 0 else "▼"
        body = (
            f"```\n{market.render_chart(prices)}\n```\n"
            f"現在 **{market.pel(s['last'])}**　{arrow} **{s['change']:+.1f}%**（{label}）\n"
            f"高値 {market.fmt(s['high'])}　安値 {market.fmt(s['low'])}"
            + (f"\n**{EVENT_TEXT[last_event]}**" if last_event else "")
        )
        e = ui.embed(f"{sym.emoji} {title_prefix}{sym.name}（{label}）", body, color)
        e.add_field(name="🔗 ほかの銘柄との連動（直近24時間）", value=self.correlation_lines(symbol), inline=False)
        if guild_id is not None:
            holders = self.eco.for_guild(guild_id).crypto_holders(HOLDER_ROWS, symbol)
            if holders:
                lines = []
                for uid, units, cost in holders:
                    pct = market.pnl_percent(s["last"], units, cost)
                    lines.append(
                        f"<@{uid}> {market.lots(units)}　取得 {market.fmt(market.avg_price(units, cost))} → {'🔺' if pct >= 0 else '🔻'}**{pct:+.1f}%**"
                    )
                e.add_field(
                    name=f"👥 保有者（このサーバー・上位{HOLDER_ROWS}人）", value="\n".join(lines), inline=False
                )
            shorts = self.eco.for_guild(guild_id).short_holders(HOLDER_ROWS, symbol)
            if shorts:
                lines = []
                for uid, units, entry_value in shorts:
                    avg = entry_value * market.UNIT / units
                    pct = (avg - s["last"]) / avg * 100  # 下がると利益
                    lines.append(
                        f"<@{uid}> {market.lots(units)}　建値 {market.fmt(avg)} → {'🔺' if pct >= 0 else '🔻'}**{pct:+.1f}%**"
                    )
                e.add_field(
                    name=f"📉 空売り中（このサーバー・上位{HOLDER_ROWS}人）", value="\n".join(lines), inline=False
                )
        if ended:
            note = "更新は終了しました。最新は /証券 市況 で"
        elif live_until is not None:
            note = f"{TICK_MINUTES}分ごとに自動で更新（あと{max(1, int((live_until - time.time()) // 60))}分）"
        else:
            note = f"{TICK_MINUTES}分ごとに値が動きます"
        e.set_footer(text=f"{note} ｜ 売買の手数料はありません ｜ ゲーム内の通貨です")
        return e

    def correlation_table(self) -> str:
        """4銘柄どうしの連動を、行列の表にする（直近24時間。まだデータが少ないところは —）。"""
        since = time.time() - CORRELATION_MINUTES * 60
        hist = {s.key: self.eco.market_history(since, s.key) for s in market.SYMBOLS}
        head = " " * 8 + " ".join(f"{s.emoji:^5}" for s in market.SYMBOLS)
        rows = [head]
        for a in market.SYMBOLS:
            cells = []
            for b in market.SYMBOLS:
                value = 1.0 if a.key == b.key else market.correlation_from_history(hist[a.key], hist[b.key])
                cells.append(f"{value:+.2f}" if value is not None else "  — ")
            rows.append(f"{a.emoji} {a.key:<5} " + " ".join(f"{c:^5}" for c in cells))
        return "```\n" + "\n".join(rows) + "\n```"

    def summary_embed(self) -> discord.Embed:
        """全銘柄のいまの価格と、銘柄どうしの連動。"""
        lines = []
        for sym in market.SYMBOLS:
            hist = self.eco.market_history(time.time() - 86400, sym.key)
            prices = [p for _, p, _ in hist] or [self._price(sym.key)]
            s = market.summary(prices)
            arrow = "▲" if s["change"] >= 0 else "▼"
            lines.append(
                f"{sym.emoji} **{sym.name}**　**{market.pel(s['last'])}**（1枚）　{arrow} {s['change']:+.1f}%（24時間）　高値 {market.fmt(s['high'])} 安値 {market.fmt(s['low'])}"
            )
        body = "\n".join(lines) + "\n\n**銘柄どうしの連動（直近24時間の1分ごとの値動き）**\n" + self.correlation_table()
        return ui.embed("📈 市況（全銘柄）", body + "\n下のメニューで、銘柄と期間のチャートを見られます。", ui.GOLD)

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
            return await interaction.response.send_message(embed=self.summary_embed(), view=view, ephemeral=True)
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
            prev = self.eco.swap_chart_post(message.channel.id, message.id, interaction.guild.id, key, now, sym)
            if prev is not None and prev != message.id:
                await message.channel.get_partial_message(prev).delete()
        except discord.HTTPException as exc:  # 前のチャートがすでに無い・消せないときは、そのままにする（記録だけ残す）
            logthrottle.warn(
                ("chart-swap", interaction.guild.id), f"前のチャートを消せません: guild={interaction.guild.id}: {exc!r}"
            )

    # ------------------------------ 売買 ------------------------------ #
    async def _trade(self, interaction: discord.Interaction, action):
        """売買（action(uid)）の間は「プレイ中」にして、ゲームの最中に残高を動かせないようにする（ショップの購入と同じ）。"""
        uid = interaction.user.id
        reason = self.bot.sessions.try_start(uid)
        if reason:
            return await interaction.response.send_message(
                embed=ui.error_embed("いまは取引できません", reason), ephemeral=True
            )
        try:
            await action(uid)
        finally:
            self.bot.sessions.end(uid)

    @staticmethod
    async def _confirmed_reply(interaction: discord.Interaction, needed: bool, title: str, text: str, label: str):
        """大きな取引は、確認ボタンを出す。進んでよければ「返信に使う関数」を返し、やめたなら None を返す。
        確認を挟んだときは、最初の返信がすでに確認画面に使われているので、あとの返信は followup になる。"""
        if not needed:
            return interaction.response.send_message
        ok = await confirm_action(interaction, title, text, label)
        return interaction.followup.send if ok is True else None

    @staticmethod
    async def _reject_lots_and_ratio(interaction: discord.Interaction, lots, ratio) -> bool:
        """「枚数」と「割合」を両方指定していたら、エラーを返して True を返す。"""
        if lots is None or ratio is None:
            return False
        await interaction.response.send_message(
            embed=ui.error_embed("どちらか1つにしてください", "「枚数」と「割合」は、どちらか一方だけ指定できます。"),
            ephemeral=True,
        )
        return True

    @staticmethod
    def _units_from(have: int, lots, ratio) -> Optional[int]:
        """売る・買い戻す口数。枚数の指定ならその口数、「半分」ならその半分、どちらも省略（または全部）なら None（全部）。"""
        if lots is not None:
            return int(round(lots * market.UNIT))
        if ratio is not None and ratio.value == "half":
            return max(1, have // 2)
        return None

    @staticmethod
    def _worth(have: int, units: Optional[int], price: float) -> float:
        """売買する分（units が None なら全部）の、いまの価格での金額。確認を挟むかの判定に使う。"""
        return (have if units is None else min(units, have)) * price // market.UNIT

    async def _buy_amount_autocomplete(self, interaction: discord.Interaction, current: str):
        """買うのに使う金額の欄に、残高を見せる。"""
        return balance_choices(self.bot, interaction, current, 10, 1_000_000)

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
            reply = await self._confirmed_reply(
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
        if await self._reject_lots_and_ratio(interaction, lots, ratio):
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
            units = self._units_from(have, lots, ratio)
            price = self._price(sym.key)
            shown = market.lots(have if units is None else units)
            reply = await self._confirmed_reply(
                interaction,
                self._worth(have, units, price) >= CONFIRM_FROM,
                f"{verb}ますか？",
                f"**{shown}** を{verb}て、{label}を閉じます。\n\n{self._margin_position(side, have, entry_value, margin, price)}",
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
                f"\n\n残りの建玉\n{self._margin_position(side, left, left_entry, left_margin, price)}"
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
                    f"戻ってきた額: {ui.pelica(res.money)}（担保 + 損益）\n損益: **{res.profit:+,}ペリカ**\n残高: **{ui.pelica(res.balance)}**{owed}{rest}",
                    ui.OK if res.profit >= 0 else ui.ERROR,
                )
            )

        await self._trade(interaction, run)

    @staticmethod
    def _margin_position(side: str, units: int, entry_value: float, margin: int, price: float) -> str:
        """信用取引の建玉の状態（建値・いまの価格・損益・担保）。強制決済は口座ぜんたいの維持率で決まる。"""
        avg = entry_value * market.UNIT / units
        pnl = Economy.margin_pnl(side, units, entry_value, price)
        pct = (avg - price) / avg * 100 if side == "short" else (price - avg) / avg * 100
        hint = "下がると利益" if side == "short" else "上がると利益"
        return (
            f"建てている: **{market.lots(units)}**\n建値 {market.fmt(avg)} → いま {market.fmt(price)}　{'🔺' if pct >= 0 else '🔻'}**{pct:+.1f}%**（{hint}）\n"
            f"含み損益: **{pnl:+,}ペリカ**　担保: {ui.pelica(margin)}"
        )

    @staticmethod
    def margin_ratio_text(status: dict) -> str:
        """口座の維持率の表示（🟢 150%以上 / 🟡 強制決済ラインの手前 / 🔴 強制決済の対象）。建玉がなければ空文字。"""
        ratio = status["ratio"]
        if ratio is None:
            return ""
        mark = "🟢" if ratio >= 1.5 else "🟡" if ratio >= market.OPEN_RATIO else "🔴"
        return (
            f"{mark} **口座の維持率 {ratio:.0%}**　有効証拠金 {ui.pelica(int(status['equity']))} ÷ 必要証拠金 {ui.pelica(math.ceil(status['required']))}\n"
            f"（{market.LOSSCUT_RATIO:.0%} を下回ると、損の大きい建玉から強制決済。現物・債券は担保に数えます）"
        )

    @staticmethod
    def _position(units: int, cost: int, price: int) -> str:
        """持ち分の状態（保有・取得価格・いまの価格と％・評価損益）。"""
        pct = market.pnl_percent(price, units, cost)
        value, _fee = market.sell_quote(units, price)
        diff = value - cost
        return (
            f"保有: **{market.lots(units)}**\n取得 {market.fmt(market.avg_price(units, cost))} → いま {market.fmt(price)}　"
            f"{'🔺' if pct >= 0 else '🔻'}**{pct:+.1f}%**\n評価損益: **{diff:+,}ペリカ**（売ったときの手取り {ui.pelica(value)}）"
        )

    async def _sell(self, interaction: discord.Interaction, symbol: str, lots, ratio) -> None:
        """現物の売却。枚数か割合（半分・全部）で指定する。どちらも省略すると全部。"""
        if await self._reject_lots_and_ratio(interaction, lots, ratio):
            return
        sym = market.BY_KEY[symbol]

        async def run(uid):
            have, cost = self.g(interaction).crypto_holding(uid, sym.key)
            if have <= 0:
                return await interaction.response.send_message(
                    embed=ui.error_embed("売れません", f"{sym.name}を持っていません。`/証券 注文` で買えます。"),
                    ephemeral=True,
                )
            units = self._units_from(have, lots, ratio)
            price = self._price(sym.key)
            shown = market.lots(have if units is None else units)
            reply = await self._confirmed_reply(
                interaction,
                self._worth(have, units, price) >= CONFIRM_FROM,
                "売りますか？",
                f"**{shown}** を売ります。\n\n{self._position(have, cost, price)}",
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
            rest = f"\n\n残りの持ち分\n{self._position(left, left_cost, price)}" if left > 0 else "\n全部売りました。"
            await reply(
                embed=ui.embed(
                    f"{sym.name}を売りました",
                    f"**{market.lots(res.units)}**（価格 {market.pel(price)}）\n"
                    f"取得 {market.fmt(market.avg_price(have, cost))} → 売値 {market.fmt(price)}　{'🔺' if pct >= 0 else '🔻'}**{pct:+.1f}%**\n"
                    f"受け取り: {ui.pelica(res.money)}\n"
                    f"買った額との差: **{res.profit:+,}ペリカ**\n残高: **{ui.pelica(res.balance)}**{rest}",
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
            reply = await self._confirmed_reply(
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
                    f"預けた担保: {ui.pelica(res.money)}\n残高: **{ui.pelica(res.balance)}**\n\n{self._margin_position(side, have, entry_value, margin_all, price)}\n\n"
                    f"{self.margin_ratio_text(eco.margin_status(uid, self._prices()))}\n"
                    "閉じるには `/証券 保有` のボタンか、`/証券 決済` を使います。",
                )
            )

        await self._trade(interaction, run)

    async def _option_buy(self, interaction: discord.Interaction, kind: str, symbol: str, amount: int) -> None:
        """爆騰コール・暴落プットを買う。満期は6時間後、権利行使価格は現在値の ±5%（固定）。損失は掛け金だけ。"""
        sym, (name, emoji, _hint) = market.BY_KEY[symbol], opt.KINDS[kind]
        seconds = opt.OPTION_HOURS * 3600

        def quote(price: float) -> tuple:
            strike = opt.strike_for(kind, price)
            per = opt.ask_per_unit(kind, sym.key, price, strike, seconds)
            units = int(amount / per) if per > 0 else 0
            return strike, units, math.ceil(round(units * per, 6))

        def target_text(price: float, strike: float, units: int, cost: int) -> str:
            """勝利条件と、想定リターン（価格が +10%・+20% 動いたとき。コールは上、プットは下）。"""
            sign = 1 if kind == opt.CALL else -1
            lines = [
                f"勝利条件: **{opt.OPTION_HOURS}時間後**の価格が **{market.fmt(strike)}**（現在 {market.fmt(price)} の "
                f"{sign * opt.STRIKE_OTM * 100:+.0f}%）を{'超える' if kind == opt.CALL else '下回る'}こと"
            ]
            for move in (0.10, 0.20):
                final = price * (1 + sign * move)
                pay = min(opt.intrinsic(kind, units, final, strike), cost * opt.PAYOUT_CAP_MULT)
                lines.append(
                    f"想定リターン: 価格が {sign * move * 100:+.0f}% で **約 {pay:,}ペリカ（{pay / cost:.1f}倍）**"
                )
            return "\n".join(lines)

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
            reply = await self._confirmed_reply(
                interaction,
                cost >= CONFIRM_FROM,
                f"{name}を買いますか？",
                f"{emoji} **{sym.name}**（{opt.OPTION_HOURS}時間満期）\n掛け金 **{ui.pelica(cost)}**（途中のロスカットはなく、最大の損失はこの掛け金だけ）\n"
                + target_text(price, strike, units, cost),
                f"{name}を買う",
            )
            if reply is None:
                return
            res_ok, res = self.g(interaction).option_buy(
                uid, kind, sym.key, amount
            )  # 確認を待つ間に動いた価格で、確定する
            if not res_ok:
                return await reply(embed=ui.error_embed(f"{name}を買えませんでした", res), ephemeral=True)
            await reply(
                embed=ui.ok_embed(
                    f"{emoji} 【{name}】{sym.name}（{opt.OPTION_HOURS}時間満期）",
                    f"掛け金: **{ui.pelica(res['cost'])}**（★途中ロスカットなし・最大損失はこの掛け金のみ）\n"
                    + target_text(res["spot"], res["strike"], res["units"], res["cost"])
                    + f"\n満期: {due_text(res['expiry'])}（満期の価格で自動で決済されます）\n"
                    "満期の前でも、`/証券 保有` のボタンか `/証券 決済` で、いまの値段で売れます。",
                )
            )

        await self._trade(interaction, run)

    async def _option_sell(self, interaction: discord.Interaction, option_id: int) -> None:
        """オプションを、満期の前にいまの値段（売値）で売る。"""
        eco = self.g(interaction)
        eco.option_settle(time.time())
        row = next((r for r in eco.option_list(interaction.user.id)["active"] if r["id"] == option_id), None)
        if row is None:
            return await interaction.response.send_message(
                embed=ui.error_embed("売れません", "その番号のオプションは持っていません。"), ephemeral=True
            )
        value = eco.option_value(row, time.time(), self._prices())
        reply = await self._confirmed_reply(
            interaction,
            value >= CONFIRM_FROM,
            "オプションを売りますか？",
            f"{self._option_line(row, self._prices())}\nいま売ると **{ui.pelica(value)}** です（掛け金 {ui.pelica(row['premium'])}）。",
            "売る",
        )
        if reply is None:
            return
        ok, got = eco.option_sell(interaction.user.id, option_id)
        if not ok:
            return await reply(embed=ui.error_embed("売れませんでした", got), ephemeral=True)
        await reply(
            embed=ui.embed(
                "オプションを売りました",
                f"受け取り: **{ui.pelica(got)}**（掛け金との差 {got - row['premium']:+,}ペリカ）\n残高: **{ui.pelica(eco.balance(interaction.user.id))}**",
                ui.OK if got >= row["premium"] else ui.ERROR,
            )
        )

    @staticmethod
    def _describe_option_done(row: dict) -> str:
        name, emoji, _hint = opt.KINDS[row["kind"]]
        sym = market.BY_KEY[row["symbol"]]
        head = f"{emoji} {name} {sym.name} 掛け金 {ui.pelica(row['premium'])}"
        if row["status"] == "sold":
            return f"{head} → 売却 {ui.pelica(row['paid'])}（{row['paid'] - row['premium']:+,}）"
        if row["status"] == "exercised":
            return f"{head} → 🎯 **当たり** {ui.pelica(row['paid'])}（{row['paid'] - row['premium']:+,}）"
        return f"{head} → 外れ（満期の価格 {market.fmt(row['final'])}）"

    @staticmethod
    def _option_line(row: dict, prices: dict) -> str:
        """保有のオプションの1行（種類・銘柄・条件・満期）。"""
        name, emoji, _hint = opt.KINDS[row["kind"]]
        sym = market.BY_KEY[row["symbol"]]
        now_price = prices.get(row["symbol"])
        cond = "超える" if row["kind"] == opt.CALL else "下回る"
        return (
            f"`#{row['id']}` {emoji} {name} {sym.name}　満期 {due_text(row['expiry'])}\n"
            f"　{market.fmt(row['strike'])} を{cond}と当たり（いま {market.fmt(now_price) if now_price else '-'}）　掛け金 {ui.pelica(row['premium'])}"
        )

    # ------------------------------ 注文 ------------------------------ #
    @group.command(name="注文", description="買う・信用買い・空売り・債券を買う（銘柄と金額を選ぶだけ）")
    @app_commands.rename(symbol="銘柄", amount="金額", order_type="注文種別", leverage="レバレッジ")
    @app_commands.describe(
        symbol="買う銘柄か債券",
        amount="使うペリカ（信用買い・空売りは建てる総額、コール・プットは掛け金）",
        order_type="省略すると現物買い（債券は現物買いのみ）",
        leverage="信用買い・空売りの倍率（省略で1倍。大きいほど損益が大きく、強制決済されやすい）",
    )
    @app_commands.choices(
        symbol=ORDER_CHOICES,
        order_type=[
            app_commands.Choice(name="現物買い", value="spot"),
            app_commands.Choice(name="信用買い", value="long"),
            app_commands.Choice(name="空売り", value="short"),
            app_commands.Choice(name="爆騰コール（6時間・上がると当たり）", value="call"),
            app_commands.Choice(name="暴落プット（6時間・下がると当たり）", value="put"),
        ],
        leverage=[app_commands.Choice(name=f"{n}倍", value=n) for n in Economy.SHORT_LEVERAGES],
    )
    @app_commands.autocomplete(amount=_buy_amount_autocomplete)
    async def order(
        self,
        interaction: discord.Interaction,
        symbol: app_commands.Choice[str],
        amount: app_commands.Range[int, 10, 10_000_000],
        order_type: Optional[app_commands.Choice[str]] = None,
        leverage: Optional[app_commands.Choice[int]] = None,
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
        if kind in (opt.CALL, opt.PUT):
            if lev != 1:
                return await interaction.response.send_message(
                    embed=ui.error_embed("コール・プットにレバレッジはありません", "損失は、払った掛け金だけです。"),
                    ephemeral=True,
                )
            return await self._option_buy(interaction, kind, symbol.value, amount)
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
            name, emoji, _ = opt.KINDS[r["kind"]]
            out.append((f"opt:{r['id']}", f"{emoji} {name} #{r['id']} 売却"))
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
        eco = self.bot.economy.for_guild(guild_id)
        eco.bond_settle(time.time(), RNG)  # 満期の分を先に入金する
        eco.option_settle(time.time())
        options = eco.option_list(uid)
        spots = {symbol: (units, cost) for symbol, units, cost in eco.holdings(uid)}
        margins = {
            (side, symbol): (units, entry, margin) for side, symbol, units, entry, margin in eco.margin_positions(uid)
        }
        data = eco.bond_list(uid)
        if (
            not spots
            and not margins
            and not data["active"]
            and not data["done"]
            and not options["active"]
            and not options["done"]
        ):
            return ui.embed("💼 証券", "まだ持っていません。`/証券 注文` で買えます。", ui.INFO)
        parts, total = [], 0
        for sym in market.SYMBOLS:
            if sym.key not in spots and (("long", sym.key) not in margins) and (("short", sym.key) not in margins):
                continue
            price = self._price(sym.key)
            lines = [f"{sym.emoji} **{sym.name}**　価格 {market.pel(price)}"]
            if sym.key in spots:
                units, cost = spots[sym.key]
                value, _fee = market.sell_quote(units, price)  # 売ったら受け取れる額
                diff = value - cost
                total += diff
                lines.append(
                    f"保有: **{market.lots(units)}**　評価額 **{ui.pelica(value)}**（売ったときの手取り）\n"
                    f"買った額 {ui.pelica(cost)}　損益 **{'+' if diff >= 0 else ''}{diff:,}ペリカ**"
                )
            for side, icon in (("long", "📈"), ("short", "📉")):
                if (side, sym.key) in margins:
                    units, entry, margin = margins[(side, sym.key)]
                    lines.append(
                        f"{icon} **{Economy.SIDE_NAME[side]}**\n{self._margin_position(side, units, entry, margin, price)}"
                    )
                    total += Economy.margin_pnl(side, units, entry, price)
            parts.append("\n".join(lines))
        if options["active"]:
            prices, now = self._prices(), time.time()
            lines = [
                f"{self._option_line(r, prices)}\n　いま売ると **{ui.pelica(eco.option_value(r, now, prices))}**"
                for r in options["active"]
            ]
            parts.append("🎫 **オプション**（満期の価格で自動決済。損失は掛け金だけ）\n" + "\n".join(lines))
        if data["active"]:
            lines = []
            for r in data["active"]:
                k = bonds.KINDS[r["kind"]]
                lines.append(
                    f"`#{r['id']}` {k.emoji} {k.name} 元本 {ui.pelica(r['principal'])} → 満期 {due_text(r['due'])}（受取 {ui.pelica(bonds.maturity_payout(k, r['principal']))}）"
                    + (
                        f"　いま売ると {ui.pelica(eco.bond_price(r, time.time()))}"
                        if k.sellable
                        else "　⚠️ 倒産しうる・売れない"
                    )
                )
            parts.append("🏦 **債券**\n" + "\n".join(lines))
        ratio_text = self.margin_ratio_text(eco.margin_status(uid, self._prices())) if margins else ""
        if ratio_text:
            parts.append(ratio_text)
        e = ui.embed(
            "💼 証券", "\n\n".join(parts) or "いま持っている銘柄はありません。", ui.OK if total >= 0 else ui.ERROR
        )
        if options["done"]:
            e.add_field(
                name="オプションの最近の決着",
                value="\n".join(self._describe_option_done(r) for r in options["done"]),
                inline=False,
            )
        if data["done"]:
            e.add_field(name="債券の最近の決着", value="\n".join(describe_done(r) for r in data["done"]), inline=False)
        e.set_footer(
            text="下のボタンで、その持ち物を全部決済します（一部だけなら /証券 決済）。ランキングの総資産には債券の元本も含まれます"
        )
        return e

    @group.command(name="保有", description="現物・信用取引・債券をまとめて見る（ボタンで、すぐ決済できる）")
    async def holdings(self, interaction: discord.Interaction):
        uid, gid = interaction.user.id, interaction.guild.id
        embed = self.holdings_embed(uid, gid)
        view = HoldingsView(self, uid, gid)
        await interaction.response.send_message(embed=embed, view=view if view.children else None, ephemeral=True)

    # ------------------------------ 決済 ------------------------------ #
    async def _position_autocomplete(self, interaction: discord.Interaction, current: str):
        found = [
            app_commands.Choice(name=label, value=code)
            for code, label in self.positions(interaction.user.id, interaction.guild.id)
            if current.lower() in label.lower()
        ]
        return found[:25]

    @group.command(name="決済", description="持っている現物・空売り・債券を、半分か全部、手放す")
    @app_commands.rename(position="ポジション", ratio="数量")
    @app_commands.describe(position="決済する持ち物（候補から選ぶ）", ratio="半分か全部（省略で全部。債券は全部だけ）")
    @app_commands.choices(
        ratio=[app_commands.Choice(name="半分", value="half"), app_commands.Choice(name="全部", value="all")]
    )
    @app_commands.autocomplete(position=_position_autocomplete)
    async def settle(
        self, interaction: discord.Interaction, position: str, ratio: Optional[app_commands.Choice[str]] = None
    ):
        await self.settle_position(interaction, position, ratio)

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
        k = bonds.KINDS[kind_key]
        lines = [
            f"元本: **{ui.pelica(amount)}**　満期: {k.hours // 24}日後（{due_text(time.time() + k.hours * 3600)}）",
            f"満期に受け取る額: **{ui.pelica(bonds.maturity_payout(k, amount))}**（利息 +{k.rate}%）",
        ]
        if k.default_pct:
            lines.append(
                f"⚠️ 満期までに **{k.default_pct}%の確率で倒産**します（満期を待たず、**途中のどの瞬間にも**起こります）。倒産すると元本は戻りません。売ることもできません。"
            )
        else:
            sold = bonds.sale_value(k, amount, time.time(), time.time() + k.hours * 3600)
            lines.append(
                f"元本保証です。満期の前でも、残り時間で割り引いた値段で売れます（`/証券 決済`。いま売ると {ui.pelica(sold)}、待つほど満期の額に近づきます）。"
            )
        await interaction.response.send_message(embed=ui.ok_embed(f"{k.emoji} {k.name}を買いました", "\n".join(lines)))

    async def _bond_cancel(self, interaction: discord.Interaction, bond_id: int) -> None:
        ok, result = self.g(interaction).bond_cancel(interaction.user.id, bond_id)
        if not ok:
            return await interaction.response.send_message(embed=ui.error_embed("売れません", result), ephemeral=True)
        await interaction.response.send_message(
            embed=ui.ok_embed("債券を売りました", f"{ui.pelica(result)}を残高に戻しました。"), ephemeral=True
        )

    # ------------------------------ 出したチャートの自動更新 ------------------------------ #
    async def _refresh_post(self, post: dict, now: float, cache: dict) -> None:
        """出したチャートを、最新に書き換える。時間が来たら、最後に1回だけ書き換えて、更新を終える。"""
        ends = post["ts"] + LIVE_MINUTES * 60
        expired = now >= ends or post["guild_id"] <= 0
        channel = self.bot.get_channel(post["channel_id"])
        if channel is None:
            if expired or self.bot.get_guild(post["guild_id"]) is None:
                self.eco.end_chart_post(post["channel_id"])
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
                self.eco.delete_chart_post(post["channel_id"])  # 消されたチャートは、もう更新しない
                return
            except discord.HTTPException as exc:
                logthrottle.warn(
                    ("chart", post["channel_id"]), f"チャートを更新できません: channel={post['channel_id']}: {exc!r}"
                )
        if expired:
            self.eco.end_chart_post(post["channel_id"])

    async def _retire_old_boards(self) -> None:
        """廃止した「チャート掲示」が残っていれば、メッセージを消して、設定も捨てる（1回だけ）。"""
        for board in self.eco.list_chart_boards():
            channel = self.bot.get_channel(board["channel_id"])
            if channel is not None:
                try:
                    await channel.get_partial_message(board["message_id"]).delete()
                except discord.HTTPException as exc:  # すでに消えている場合など。設定は捨てるので、記録だけ残す
                    logthrottle.warn(
                        ("chart-board", board["guild_id"]),
                        f"廃止したチャート掲示を消せません: guild={board['guild_id']}: {exc!r}",
                    )
            self.eco.delete_chart_board(board["guild_id"])

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        self.eco.delete_guild_chart_posts(guild.id)


async def setup(bot):
    await bot.add_cog(MarketCog(bot))
