"""/証券 — 4銘柄（CSトークン・TEIO重工・NUMAバイオ・GOLD地金）と債券の売買、価格のチャート。価格は全サーバー共通で、1分ごとに、相関をもって動く。"""

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
CORRELATION_MINUTES = 24 * 60  # 銘柄どうしの連動を調べる、直近の長さ（分）


class MarketCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def g(self, interaction: discord.Interaction):
        return self.bot.economy.for_guild(interaction.guild.id)

    @property
    def eco(self):
        return self.bot.economy

    group = app_commands.Group(
        name="証券", description="📈 4銘柄と債券の売買・チャート（ゲーム内の通貨・現実のお金とは無関係）"
    )
    bond_group = app_commands.Group(
        name="債券", description="🏦 ペリカを預けて満期に受け取る（通常債・ジャンク債）", parent=group
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
            for guild_id, user_id, loss in self.eco.short_losscut(prices):
                print(f"空売りをロスカット: guild={guild_id} user={user_id} 損失={loss:,}")
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
            note = "更新は終了しました。最新は /証券 チャート で"
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

    @group.command(name="価格", description="全銘柄のいまの価格と、銘柄どうしの連動")
    async def price(self, interaction: discord.Interaction):
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
        await interaction.response.send_message(
            embed=ui.embed("📈 証券（全銘柄）", body + "\n`/証券 チャート` で値動きを見られます。", ui.GOLD),
            ephemeral=True,
        )

    @group.command(name="チャート", description=f"銘柄の値動きをチャートで表示（{LIVE_MINUTES}分間、自動で更新）")
    @app_commands.rename(symbol="銘柄", period="期間")
    @app_commands.describe(symbol="見る銘柄（省略時: CSトークン）", period="どの期間を見るか（省略時: 24時間）")
    @app_commands.choices(symbol=SYMBOL_CHOICES, period=PERIOD_CHOICES)
    async def chart(
        self,
        interaction: discord.Interaction,
        symbol: Optional[app_commands.Choice[str]] = None,
        period: Optional[app_commands.Choice[str]] = None,
    ):
        key, now, sym = (period.value if period else "24h"), time.time(), (symbol.value if symbol else market.DEFAULT)
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

    @group.command(name="買う", description="ペリカで銘柄を買う（手数料なし・小数で買えます）")
    @app_commands.rename(symbol="銘柄", amount="金額")
    @app_commands.describe(symbol="買う銘柄", amount="使うペリカ（この範囲で買えるだけ買います）")
    @app_commands.choices(symbol=SYMBOL_CHOICES)
    @app_commands.autocomplete(amount=_buy_amount_autocomplete)
    async def buy(
        self,
        interaction: discord.Interaction,
        symbol: app_commands.Choice[str],
        amount: app_commands.Range[int, 10, 1_000_000],
    ):
        sym = market.BY_KEY[symbol.value]

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

    @group.command(name="買い戻し", description="空売りした銘柄を買い戻して、建玉を閉じる")
    @app_commands.rename(symbol="銘柄", lots="枚数", ratio="割合")
    @app_commands.describe(
        symbol="買い戻す銘柄", lots="買い戻す枚数（小数も可）", ratio="半分か全部（枚数のかわりに指定。省略で全部）"
    )
    @app_commands.choices(
        symbol=SYMBOL_CHOICES,
        ratio=[app_commands.Choice(name="半分", value="half"), app_commands.Choice(name="全部", value="all")],
    )
    async def cover(
        self,
        interaction: discord.Interaction,
        symbol: app_commands.Choice[str],
        lots: Optional[app_commands.Range[float, 0.001, 1_000_000.0]] = None,
        ratio: Optional[app_commands.Choice[str]] = None,
    ):
        await self._cover(interaction, symbol.value, lots, ratio)

    async def _cover(self, interaction, symbol: str, lots, ratio) -> None:
        """空売りの買い戻し（決済）。枚数か割合で指定する。どちらも省略すると全部。"""
        if await self._reject_lots_and_ratio(interaction, lots, ratio):
            return

        sym = market.BY_KEY[symbol]

        async def run(uid):
            have, entry_value, margin = self.g(interaction).short_position(uid, sym.key)
            if have <= 0:
                return await interaction.response.send_message(
                    embed=ui.error_embed(
                        "買い戻せません", f"{sym.name}の空売りを建てていません。`/証券 空売り` で建てられます。"
                    ),
                    ephemeral=True,
                )
            units = self._units_from(have, lots, ratio)
            price = self._price(sym.key)
            shown = market.lots(have if units is None else units)
            reply = await self._confirmed_reply(
                interaction,
                self._worth(have, units, price) >= CONFIRM_FROM,
                "買い戻しますか？",
                f"**{shown}** を買い戻して、空売りを閉じます。\n\n{self._short_position(have, entry_value, margin, price)}",
                "買い戻す",
            )
            if reply is None:
                return
            price = self._price(sym.key)
            res = self.g(interaction).short_close(uid, units, price, sym.key)
            if not res.ok:
                return await reply(embed=ui.error_embed("買い戻せませんでした", res.reason), ephemeral=True)
            left, left_entry, left_margin = self.g(interaction).short_position(uid, sym.key)
            rest = (
                f"\n\n残りの建玉\n{self._short_position(left, left_entry, left_margin, price)}"
                if left > 0
                else "\n空売りを全部閉じました。"
            )
            await reply(
                embed=ui.embed(
                    f"{sym.name}を買い戻しました（空売りの決済）",
                    f"**{market.lots(res.units)}**（価格 {market.pel(price)}）\n"
                    f"戻ってきた額: {ui.pelica(res.money)}（担保 + 損益）\n損益: **{res.profit:+,}ペリカ**\n残高: **{ui.pelica(res.balance)}**{rest}",
                    ui.OK if res.profit >= 0 else ui.ERROR,
                )
            )

        await self._trade(interaction, run)

    @staticmethod
    def _short_position(units: int, entry_value: float, margin: int, price: float) -> str:
        """空売りの建玉の状態（建値・いまの価格・損益・担保・ロスカットされる価格）。"""
        avg = entry_value * market.UNIT / units
        pnl = Economy.short_pnl(units, entry_value, price)
        pct = (avg - price) / avg * 100
        cut = (entry_value + Economy.LOSSCUT * margin) * market.UNIT / units
        return (
            f"建てている: **{market.lots(units)}**\n建値 {market.fmt(avg)} → いま {market.fmt(price)}　{'🔺' if pct >= 0 else '🔻'}**{pct:+.1f}%**（下がると利益）\n"
            f"含み損益: **{pnl:+,}ペリカ**　担保: {ui.pelica(margin)}\n⚠️ 価格が **{market.fmt(cut)}** 以上になると強制決済（ロスカット）"
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

    @group.command(name="売る", description="持っている銘柄を売る")
    @app_commands.rename(symbol="銘柄", lots="枚数", ratio="割合")
    @app_commands.describe(
        symbol="売る銘柄", lots="売る枚数（小数も可）", ratio="半分か全部（枚数のかわりに指定。省略で全部）"
    )
    @app_commands.choices(
        symbol=SYMBOL_CHOICES,
        ratio=[app_commands.Choice(name="半分", value="half"), app_commands.Choice(name="全部", value="all")],
    )
    async def sell(
        self,
        interaction: discord.Interaction,
        symbol: app_commands.Choice[str],
        lots: Optional[app_commands.Range[float, 0.001, 1_000_000.0]] = None,
        ratio: Optional[app_commands.Choice[str]] = None,
    ):
        if await self._reject_lots_and_ratio(interaction, lots, ratio):
            return
        sym = market.BY_KEY[symbol.value]

        async def run(uid):
            have, cost = self.g(interaction).crypto_holding(uid, sym.key)
            if have <= 0:
                return await interaction.response.send_message(
                    embed=ui.error_embed("売れません", f"{sym.name}を持っていません。`/証券 買う` で買えます。"),
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

    @group.command(name="空売り", description="銘柄を空売り。価格が下がると儲かる（担保が必要）")
    @app_commands.rename(symbol="銘柄", lots="枚数", amount="金額", leverage="レバレッジ")
    @app_commands.describe(
        symbol="空売りする銘柄",
        lots="空売りする枚数（小数も可）",
        amount="空売りする額（枚数のかわりに指定）",
        leverage="倍率（大きいほど損益が大きく、強制決済されやすい。省略で1倍）",
    )
    @app_commands.choices(
        symbol=SYMBOL_CHOICES, leverage=[app_commands.Choice(name=f"{n}倍", value=n) for n in Economy.SHORT_LEVERAGES]
    )
    async def short(
        self,
        interaction: discord.Interaction,
        symbol: app_commands.Choice[str],
        lots: Optional[app_commands.Range[float, 0.001, 1_000_000.0]] = None,
        amount: Optional[app_commands.Range[int, 10, 10_000_000]] = None,
        leverage: Optional[app_commands.Choice[int]] = None,
    ):
        """空売り（売り建て）。担保 = 総額 ÷ レバレッジ を預ける。枚数か金額（売り建てる総額）で指定する。"""
        if (lots is None) == (amount is None):
            return await interaction.response.send_message(
                embed=ui.error_embed(
                    "どちらか1つを指定してください", "空売りする量は、「枚数」か「金額」のどちらか一方で指定します。"
                ),
                ephemeral=True,
            )
        leverage = leverage.value if leverage else 1
        sym = market.BY_KEY[symbol.value]

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
                        "空売りできません", "その金額では 0.001枚にもなりません。金額を増やしてください。"
                    ),
                    ephemeral=True,
                )
            notional = units * price / market.UNIT
            margin = math.ceil(round(notional / leverage, 6))
            reply = await self._confirmed_reply(
                interaction,
                notional >= CONFIRM_FROM,
                "空売りしますか？",
                f"**{sym.name}** を、いまの価格 **{market.pel(price)}** で、**{market.lots(units)}**（約 {ui.pelica(int(notional))}）を売り建てます。\n"
                f"レバレッジ **{leverage}倍**・担保 **{ui.pelica(margin)}**\n"
                f"価格が下がると利益、上がると損失です。損失が担保の{int(Economy.LOSSCUT * 100)}%になると強制決済されます。",
                "空売りする",
            )
            if reply is None:
                return
            price = self._price(sym.key)
            units = units_at(price)  # 確認を待つ間に動いた価格で、確定する
            res = self.g(interaction).short_open(uid, units, price, leverage, sym.key)
            if not res.ok:
                return await reply(embed=ui.error_embed("空売りできませんでした", res.reason), ephemeral=True)
            have, entry_value, margin_all = self.g(interaction).short_position(uid, sym.key)
            await reply(
                embed=ui.ok_embed(
                    f"{sym.name}を空売りしました",
                    f"**{market.lots(res.units)}**（価格 {market.pel(price)}・{leverage}倍）\n"
                    f"預けた担保: {ui.pelica(res.money)}\n残高: **{ui.pelica(res.balance)}**\n\n{self._short_position(have, entry_value, margin_all, price)}\n\n"
                    "閉じるには `/証券 買い戻し` を使います。",
                )
            )

        await self._trade(interaction, run)

    @group.command(name="保有", description="持っている銘柄と、空売りの建玉・いまの評価額（全銘柄）")
    async def holdings(self, interaction: discord.Interaction):
        eco, uid = self.g(interaction), interaction.user.id
        spots = {symbol: (units, cost) for symbol, units, cost in eco.holdings(uid)}
        shorts = {symbol: (units, entry, margin) for symbol, units, entry, margin in eco.short_positions(uid)}
        if not spots and not shorts:
            return await interaction.response.send_message(
                embed=ui.embed("💼 証券", "まだ持っていません。`/証券 買う` で買えます。", ui.INFO), ephemeral=True
            )
        parts, total = [], 0
        for sym in market.SYMBOLS:
            if sym.key not in spots and sym.key not in shorts:
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
            if sym.key in shorts:
                units, entry, margin = shorts[sym.key]
                lines.append(f"📉 **空売り**\n{self._short_position(units, entry, margin, price)}")
                total += Economy.short_pnl(units, entry, price)
            parts.append("\n".join(lines))
        await interaction.response.send_message(
            embed=ui.embed("💼 証券", "\n\n".join(parts), ui.OK if total >= 0 else ui.ERROR), ephemeral=True
        )

    # ------------------------------ 債券（/証券 債券 …） ------------------------------ #
    async def _bond_amount_autocomplete(self, interaction: discord.Interaction, current: str):
        """預ける元本の欄に、残高を見せる。"""
        return balance_choices(self.bot, interaction, current, bonds.MIN_AMOUNT, 1_000_000)

    @bond_group.command(name="買う", description=f"債券を買う（{bonds.MIN_AMOUNT:,}ペリカ以上）")
    @app_commands.rename(kind="種類", amount="金額")
    @app_commands.describe(kind="通常債かジャンク債", amount=f"預ける元本（{bonds.MIN_AMOUNT:,}ペリカ以上）")
    @app_commands.choices(kind=KIND_CHOICES)
    @app_commands.autocomplete(amount=_bond_amount_autocomplete)
    async def bond_buy(
        self,
        interaction: discord.Interaction,
        kind: app_commands.Choice[str],
        amount: app_commands.Range[int, bonds.MIN_AMOUNT, 1_000_000],
    ):
        eco = self.g(interaction)
        eco.bond_settle(time.time(), RNG)  # 満期の分を先に入金して、買える額を正しくする
        ok, reason = eco.bond_buy(interaction.user.id, kind.value, amount, time.time())
        if not ok:
            return await interaction.response.send_message(embed=ui.error_embed("買えません", reason), ephemeral=True)
        k = bonds.KINDS[kind.value]
        lines = [
            f"元本: **{ui.pelica(amount)}**　満期: {k.hours // 24}日後（{due_text(time.time() + k.hours * 3600)}）",
            f"満期に受け取る額: **{ui.pelica(bonds.maturity_payout(k, amount))}**（利息 +{k.rate}%）",
        ]
        if k.default_pct:
            lines.append(f"⚠️ **{k.default_pct}%の確率で倒産**して、元本が戻りません。途中で解約もできません。")
        else:
            lines.append(f"元本保証です。途中解約は元本の{k.cancel_penalty}%が引かれます（`/証券 債券 解約`）。")
        await interaction.response.send_message(embed=ui.ok_embed(f"{k.emoji} {k.name}を買いました", "\n".join(lines)))

    @bond_group.command(name="保有", description="持っている債券と最近の結果を見る")
    async def bond_holdings(self, interaction: discord.Interaction):
        eco = self.g(interaction)
        eco.bond_settle(time.time(), RNG)
        data = eco.bond_list(interaction.user.id)
        lines = []
        for r in data["active"]:
            k = bonds.KINDS[r["kind"]]
            lines.append(
                f"`#{r['id']}` {k.emoji} {k.name} 元本 {ui.pelica(r['principal'])} → 満期 {due_text(r['due'])}（受取 {ui.pelica(bonds.maturity_payout(k, r['principal']))}）"
            )
        e = ui.embed(
            "🏦 保有中の債券", "\n".join(lines) or "債券は持っていません。`/証券 債券 買う` で買えます。", ui.INFO
        )
        if data["done"]:
            e.add_field(name="最近の決着", value="\n".join(describe_done(r) for r in data["done"]), inline=False)
        e.set_footer(text="ランキングの総資産には、預けている元本も含まれます")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @bond_group.command(name="解約", description="通常債を途中で解約する（違約金あり）")
    @app_commands.rename(bond_id="番号")
    @app_commands.describe(bond_id="`/証券 債券 保有` に出ている # の番号")
    async def bond_cancel(self, interaction: discord.Interaction, bond_id: int):
        ok, result = self.g(interaction).bond_cancel(interaction.user.id, bond_id)
        if not ok:
            return await interaction.response.send_message(
                embed=ui.error_embed("解約できません", result), ephemeral=True
            )
        await interaction.response.send_message(
            embed=ui.ok_embed("解約しました", f"{ui.pelica(result)}を残高に戻しました。"), ephemeral=True
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
