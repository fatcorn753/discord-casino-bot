"""/暗号 — CSトークンの売買と、価格のチャート。価格は全サーバー共通で、1分ごとに動く。"""

import math
import random
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import logthrottle, market, ui
from core.economy import Economy
from core.confirm import confirm_action

RNG = random.SystemRandom()
TICK_MINUTES = 1
KEEP_DAYS = 30                  # 価格の履歴を残す日数
HOLDER_ROWS = 10               # チャートに載せる保有者の数
LIVE_MINUTES = 60               # 出したチャートを自動で更新する時間（最後に使ってから）
CONFIRM_FROM = 5_000            # この額以上の売買は確認ボタンを挟む
EVENT_TEXT = {1: "📈 急騰！", -1: "📉 急落！"}
PERIOD_CHOICES = [app_commands.Choice(name=label, value=key) for key, (label, _) in market.PERIODS.items()]


class MarketCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def g(self, interaction: discord.Interaction):
        return self.bot.economy.for_guild(interaction.guild.id)

    @property
    def eco(self):
        return self.bot.economy

    group = app_commands.Group(name="暗号", description=f"📈 {market.NAME}の売買とチャート（ゲーム内の通貨・現実のお金とは無関係）")

    # ------------------------------ 価格の更新 ------------------------------ #
    def _price(self) -> float:
        latest = self.eco.market_latest()
        if latest is None:
            self.eco.market_set(time.time(), market.START_PRICE, 0)
            return market.START_PRICE
        return latest[1]

    def step_price(self) -> tuple:
        """価格を1つ進めて、(新しい価格, 出来事) を返す。"""
        latest = self.eco.market_latest()
        new, event = market.next_price(self._price(), RNG)
        self.eco.market_set(max(time.time(), (latest[0] + 0.001) if latest else 0), new, event)      # 時計が戻っても、記録の順序を崩さない
        return new, event

    async def cog_load(self):
        self._price()               # 初回は開始価格を入れる（起動直後でもコマンドが使えるように）
        self.tick.start()

    def cog_unload(self):
        self.tick.cancel()

    @tasks.loop(minutes=TICK_MINUTES)
    async def tick(self):
        try:
            price, _event = self.step_price()
            for guild_id, user_id, loss in self.eco.short_losscut(price):
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
            except Exception as exc:       # 1つのチャンネルの失敗で、他の更新を止めない
                print(f"チャートの更新に失敗: {post}: {exc!r}")

    @tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()
        try:
            await self._retire_old_boards()
        except Exception as exc:
            print(f"古いチャート掲示の後始末に失敗: {exc!r}")

    # ------------------------------ 表示 ------------------------------ #
    def chart_embed(self, period: str = "24h", title_prefix: str = "", guild_id: Optional[int] = None,
                    live_until: Optional[float] = None, ended: bool = False) -> discord.Embed:
        label, seconds = market.PERIODS[period]
        hist = self.eco.market_history(time.time() - seconds)
        prices = [p for _, p, _ in hist]
        if not prices:
            return ui.embed(f"📈 {market.NAME}", "まだ価格のデータがありません。", ui.INFO)
        s = market.summary(prices)
        last_event = next((e for _, _, e in reversed(hist[-5:]) if e), 0)
        color = ui.OK if s["change"] >= 0 else ui.ERROR
        arrow = "▲" if s["change"] >= 0 else "▼"
        body = (f"```\n{market.render_chart(prices)}\n```\n"
                f"現在 **{market.pel(s['last'])}**　{arrow} **{s['change']:+.1f}%**（{label}）\n"
                f"高値 {market.fmt(s['high'])}　安値 {market.fmt(s['low'])}" + (f"\n**{EVENT_TEXT[last_event]}**" if last_event else ""))
        e = ui.embed(f"📈 {title_prefix}{market.NAME}（{label}）", body, color)
        if guild_id is not None:
            holders = self.eco.for_guild(guild_id).crypto_holders(HOLDER_ROWS)
            if holders:
                lines = []
                for uid, units, cost in holders:
                    pct = market.pnl_percent(s["last"], units, cost)
                    lines.append(f"<@{uid}> {market.lots(units)}　取得 {market.fmt(market.avg_price(units, cost))} → {'🔺' if pct >= 0 else '🔻'}**{pct:+.1f}%**")
                e.add_field(name=f"👥 保有者（このサーバー・上位{HOLDER_ROWS}人）", value="\n".join(lines), inline=False)
            shorts = self.eco.for_guild(guild_id).short_holders(HOLDER_ROWS)
            if shorts:
                lines = []
                for uid, units, entry_value in shorts:
                    avg = entry_value * market.UNIT / units
                    pct = (avg - s["last"]) / avg * 100          # 下がると利益
                    lines.append(f"<@{uid}> {market.lots(units)}　建値 {market.fmt(avg)} → {'🔺' if pct >= 0 else '🔻'}**{pct:+.1f}%**")
                e.add_field(name=f"📉 空売り中（このサーバー・上位{HOLDER_ROWS}人）", value="\n".join(lines), inline=False)
        if ended:
            note = "更新は終了しました。最新は /暗号 チャート で"
        elif live_until is not None:
            note = f"{TICK_MINUTES}分ごとに自動で更新（あと{max(1, int((live_until - time.time()) // 60))}分）"
        else:
            note = f"{TICK_MINUTES}分ごとに値が動きます"
        e.set_footer(text=f"{note} ｜ 売買の手数料はありません ｜ ゲーム内の通貨です")
        return e

    @group.command(name="価格", description=f"{market.NAME}のいまの価格")
    async def price(self, interaction: discord.Interaction):
        hist = self.eco.market_history(time.time() - 86400)
        prices = [p for _, p, _ in hist] or [self._price()]
        s = market.summary(prices)
        await interaction.response.send_message(embed=ui.embed(
            f"📈 {market.NAME}", f"現在 **{market.pel(s['last'])}**（1枚）\n24時間: **{s['change']:+.1f}%**　高値 {market.fmt(s['high'])}　安値 {market.fmt(s['low'])}\n"
            f"`/暗号 チャート` で値動きを見られます。", ui.GOLD), ephemeral=True)

    @group.command(name="チャート", description=f"{market.NAME}の値動きをチャートで表示（{LIVE_MINUTES}分間、自動で更新）")
    @app_commands.rename(period="期間")
    @app_commands.describe(period="どの期間を見るか（省略時: 24時間）")
    @app_commands.choices(period=PERIOD_CHOICES)
    async def chart(self, interaction: discord.Interaction, period: Optional[app_commands.Choice[str]] = None):
        key, now = (period.value if period else "24h"), time.time()
        await interaction.response.send_message(embed=self.chart_embed(key, guild_id=interaction.guild.id, live_until=now + LIVE_MINUTES * 60),
                                                allowed_mentions=discord.AllowedMentions.none())
        # チャートは1チャンネルに1つだけ。前に出したチャートは消し、新しいものを、一定時間だけ自動で更新する
        try:
            message = await interaction.original_response()
            prev = self.eco.swap_chart_post(message.channel.id, message.id, interaction.guild.id, key, now)
            if prev is not None and prev != message.id:
                await message.channel.get_partial_message(prev).delete()
        except discord.HTTPException:
            pass            # 前のチャートがすでに無い・消せないときは、そのままにする

    # ------------------------------ 売買 ------------------------------ #
    async def _trade(self, interaction: discord.Interaction, build):
        """売買の間は「プレイ中」にして、ゲームの最中に残高を動かせないようにする（ショップの購入と同じ）。"""
        uid = interaction.user.id
        reason = self.bot.sessions.try_start(uid)
        if reason:
            return await interaction.response.send_message(embed=ui.error_embed("いまは取引できません", reason), ephemeral=True)
        try:
            await build(uid)
        finally:
            self.bot.sessions.end(uid)

    @group.command(name="買う", description=f"ペリカで{market.NAME}を買う（手数料なし・小数で買えます）")
    @app_commands.rename(amount="金額")
    @app_commands.describe(amount="使うペリカ（この範囲で買えるだけ買います）")
    async def buy(self, interaction: discord.Interaction, amount: app_commands.Range[int, 10, 1_000_000]):
        async def run(uid):
            price = self._price()
            balance = self.g(interaction).balance(uid)
            if balance < amount:
                return await interaction.response.send_message(embed=ui.error_embed(
                    "ペリカが足りません", f"{ui.pelica(amount)} に対して、残高は {ui.pelica(balance)} です。"), ephemeral=True)
            reply = interaction.response.send_message
            if amount >= CONFIRM_FROM:
                units, total, fee = market.buy_quote(amount, price)
                ok = await confirm_action(interaction, "買いますか？", f"いまの価格 **{market.pel(price)}** で、約 **{market.lots(units)}** 買います。\n"
                                          f"{ui.pelica(amount)} 以内。価格は動くので、実際の価格で買います。", "買う")
                if ok is not True:
                    return
                reply = interaction.followup.send
            price = self._price()      # 確認を待つ間に動いた価格で、確定する
            res = self.g(interaction).crypto_buy(uid, amount, price)
            if not res.ok:
                return await reply(embed=ui.error_embed("買えませんでした", res.reason), ephemeral=True)
            await reply(embed=ui.ok_embed(f"{market.NAME}を買いました", f"**{market.lots(res.units)}**（価格 {market.pel(price)}）\n"
                                          f"支払い: {ui.pelica(res.money)}\n残高: **{ui.pelica(res.balance)}**"))
        await self._trade(interaction, run)

    @group.command(name="買い戻し", description=f"空売りした{market.NAME}を買い戻して、建玉を閉じる")
    @app_commands.rename(lots="枚数", ratio="割合")
    @app_commands.describe(lots="買い戻す枚数（小数も可）", ratio="半分か全部（枚数のかわりに指定。省略で全部）")
    @app_commands.choices(ratio=[app_commands.Choice(name="半分", value="half"), app_commands.Choice(name="全部", value="all")])
    async def cover(self, interaction: discord.Interaction, lots: Optional[app_commands.Range[float, 0.001, 1_000_000.0]] = None,
                    ratio: Optional[app_commands.Choice[str]] = None):
        await self._cover(interaction, lots, ratio)

    async def _cover(self, interaction, lots, ratio) -> None:
        """空売りの買い戻し（決済）。枚数か割合で指定する。どちらも省略すると全部。"""
        if lots is not None and ratio is not None:
            return await interaction.response.send_message(embed=ui.error_embed("どちらか1つにしてください", "「枚数」と「割合」は、どちらか一方だけ指定できます。"), ephemeral=True)

        async def run(uid):
            have, entry_value, margin = self.g(interaction).short_position(uid)
            if have <= 0:
                return await interaction.response.send_message(embed=ui.error_embed("買い戻せません", "空売りを建てていません。`/暗号 空売り` で建てられます。"), ephemeral=True)
            if lots is not None:
                units = int(round(lots * market.UNIT))
            elif ratio is not None and ratio.value == "half":
                units = max(1, have // 2)
            else:
                units = None
            price = self._price()
            reply = interaction.response.send_message
            if (have if units is None else min(units, have)) * price // market.UNIT >= CONFIRM_FROM:
                shown = market.lots(have if units is None else units)
                ok = await confirm_action(interaction, "買い戻しますか？", f"**{shown}** を買い戻して、空売りを閉じます。\n\n{self._short_position(have, entry_value, margin, price)}", "買い戻す")
                if ok is not True:
                    return
                reply = interaction.followup.send
            price = self._price()
            res = self.g(interaction).short_close(uid, units, price)
            if not res.ok:
                return await reply(embed=ui.error_embed("買い戻せませんでした", res.reason), ephemeral=True)
            left, left_entry, left_margin = self.g(interaction).short_position(uid)
            rest = f"\n\n残りの建玉\n{self._short_position(left, left_entry, left_margin, price)}" if left > 0 else "\n空売りを全部閉じました。"
            await reply(embed=ui.embed(f"{market.NAME}を買い戻しました（空売りの決済）", f"**{market.lots(res.units)}**（価格 {market.pel(price)}）\n"
                                       f"戻ってきた額: {ui.pelica(res.money)}（担保 + 損益）\n損益: **{res.profit:+,}ペリカ**\n残高: **{ui.pelica(res.balance)}**{rest}",
                                       ui.OK if res.profit >= 0 else ui.ERROR))
        await self._trade(interaction, run)

    @staticmethod
    def _short_position(units: int, entry_value: float, margin: int, price: float) -> str:
        """空売りの建玉の状態（建値・いまの価格・損益・担保・ロスカットされる価格）。"""
        avg = entry_value * market.UNIT / units
        pnl = Economy.short_pnl(units, entry_value, price)
        pct = (avg - price) / avg * 100
        cut = (entry_value + Economy.LOSSCUT * margin) * market.UNIT / units
        return (f"建てている: **{market.lots(units)}**\n建値 {market.fmt(avg)} → いま {market.fmt(price)}　{'🔺' if pct >= 0 else '🔻'}**{pct:+.1f}%**（下がると利益）\n"
                f"含み損益: **{pnl:+,}ペリカ**　担保: {ui.pelica(margin)}\n⚠️ 価格が **{market.fmt(cut)}** 以上になると強制決済（ロスカット）")

    @staticmethod
    def _position(units: int, cost: int, price: int) -> str:
        """持ち分の状態（保有・取得価格・いまの価格と％・評価損益）。"""
        pct = market.pnl_percent(price, units, cost)
        value, _fee = market.sell_quote(units, price)
        diff = value - cost
        return (f"保有: **{market.lots(units)}**\n取得 {market.fmt(market.avg_price(units, cost))} → いま {market.fmt(price)}　"
                f"{'🔺' if pct >= 0 else '🔻'}**{pct:+.1f}%**\n評価損益: **{diff:+,}ペリカ**（売ったときの手取り {ui.pelica(value)}）")

    @group.command(name="売る", description=f"持っている{market.NAME}を売る")
    @app_commands.rename(lots="枚数", ratio="割合")
    @app_commands.describe(lots="売る枚数（小数も可）", ratio="半分か全部（枚数のかわりに指定。省略で全部）")
    @app_commands.choices(ratio=[app_commands.Choice(name="半分", value="half"), app_commands.Choice(name="全部", value="all")])
    async def sell(self, interaction: discord.Interaction, lots: Optional[app_commands.Range[float, 0.001, 1_000_000.0]] = None,
                   ratio: Optional[app_commands.Choice[str]] = None):
        if lots is not None and ratio is not None:
            return await interaction.response.send_message(embed=ui.error_embed("どちらか1つにしてください", "「枚数」と「割合」は、どちらか一方だけ指定できます。"), ephemeral=True)

        async def run(uid):
            have, cost = self.g(interaction).crypto_holding(uid)
            if lots is not None:
                units = int(round(lots * market.UNIT))
            elif ratio is not None and ratio.value == "half":
                units = max(1, have // 2)
            else:
                units = None
            if have <= 0:
                return await interaction.response.send_message(embed=ui.error_embed("売れません", f"{market.NAME}を持っていません。`/暗号 買う` で買えます。"), ephemeral=True)
            price = self._price()
            reply = interaction.response.send_message
            if (have if units is None else min(units, have)) * price // market.UNIT >= CONFIRM_FROM:
                shown = market.lots(have if units is None else units)
                ok = await confirm_action(interaction, "売りますか？", f"**{shown}** を売ります。\n\n{self._position(have, cost, price)}", "売る")
                if ok is not True:
                    return
                reply = interaction.followup.send
            price = self._price()
            res = self.g(interaction).crypto_sell(uid, units, price)
            if not res.ok:
                return await reply(embed=ui.error_embed("売れませんでした", res.reason), ephemeral=True)
            left, left_cost = self.g(interaction).crypto_holding(uid)
            pct = market.pnl_percent(price, have, cost)         # 売った分の、取得価格に対する増減
            rest = f"\n\n残りの持ち分\n{self._position(left, left_cost, price)}" if left > 0 else "\n全部売りました。"
            await reply(embed=ui.embed(f"{market.NAME}を売りました", f"**{market.lots(res.units)}**（価格 {market.pel(price)}）\n"
                                       f"取得 {market.fmt(market.avg_price(have, cost))} → 売値 {market.fmt(price)}　{'🔺' if pct >= 0 else '🔻'}**{pct:+.1f}%**\n"
                                       f"受け取り: {ui.pelica(res.money)}\n"
                                       f"買った額との差: **{res.profit:+,}ペリカ**\n残高: **{ui.pelica(res.balance)}**{rest}",
                                       ui.OK if res.profit >= 0 else ui.ERROR))
        await self._trade(interaction, run)

    @group.command(name="空売り", description=f"{market.NAME}を空売り。価格が下がると儲かる（担保が必要）")
    @app_commands.rename(lots="枚数", amount="金額", leverage="レバレッジ")
    @app_commands.describe(lots="空売りする枚数（小数も可）", amount="空売りする額（枚数のかわりに指定）",
                           leverage="倍率（大きいほど損益が大きく、強制決済されやすい。省略で1倍）")
    @app_commands.choices(leverage=[app_commands.Choice(name=f"{n}倍", value=n) for n in Economy.SHORT_LEVERAGES])
    async def short(self, interaction: discord.Interaction, lots: Optional[app_commands.Range[float, 0.001, 1_000_000.0]] = None,
                    amount: Optional[app_commands.Range[int, 10, 10_000_000]] = None, leverage: Optional[app_commands.Choice[int]] = None):
        """空売り（売り建て）。担保 = 総額 ÷ レバレッジ を預ける。枚数か金額（売り建てる総額）で指定する。"""
        if (lots is None) == (amount is None):
            return await interaction.response.send_message(embed=ui.error_embed("どちらか1つを指定してください", "空売りする量は、「枚数」か「金額」のどちらか一方で指定します。"), ephemeral=True)
        leverage = leverage.value if leverage else 1

        def units_at(price: float) -> int:
            return int(round(lots * market.UNIT)) if lots is not None else int(amount * market.UNIT // price)      # 金額のときは、その額を超えない口数

        async def run(uid):
            price = self._price()
            units = units_at(price)
            if units < 1:
                return await interaction.response.send_message(embed=ui.error_embed("空売りできません", "その金額では 0.001枚にもなりません。金額を増やしてください。"), ephemeral=True)
            notional = units * price / market.UNIT
            margin = math.ceil(round(notional / leverage, 6))
            reply = interaction.response.send_message
            if notional >= CONFIRM_FROM:
                ok = await confirm_action(interaction, "空売りしますか？", (
                    f"いまの価格 **{market.pel(price)}** で、**{market.lots(units)}**（約 {ui.pelica(int(notional))}）を売り建てます。\n"
                    f"レバレッジ **{leverage}倍**・担保 **{ui.pelica(margin)}**\n"
                    f"価格が下がると利益、上がると損失です。損失が担保の{int(Economy.LOSSCUT * 100)}%になると強制決済されます。"), "空売りする")
                if ok is not True:
                    return
                reply = interaction.followup.send
            price = self._price()
            units = units_at(price)           # 確認を待つ間に動いた価格で、確定する
            res = self.g(interaction).short_open(uid, units, price, leverage)
            if not res.ok:
                return await reply(embed=ui.error_embed("空売りできませんでした", res.reason), ephemeral=True)
            have, entry_value, margin_all = self.g(interaction).short_position(uid)
            await reply(embed=ui.ok_embed(f"{market.NAME}を空売りしました", f"**{market.lots(res.units)}**（価格 {market.pel(price)}・{leverage}倍）\n"
                                          f"預けた担保: {ui.pelica(res.money)}\n残高: **{ui.pelica(res.balance)}**\n\n{self._short_position(have, entry_value, margin_all, price)}\n\n"
                                          f"閉じるには `/暗号 買い戻し` を使います。"))
        await self._trade(interaction, run)

    @group.command(name="保有", description=f"持っている{market.NAME}と、空売りの建玉・いまの評価額")
    async def holdings(self, interaction: discord.Interaction):
        units, cost = self.g(interaction).crypto_holding(interaction.user.id)
        short = self.g(interaction).short_position(interaction.user.id)
        if units <= 0 and short[0] <= 0:
            return await interaction.response.send_message(embed=ui.embed(f"💼 {market.NAME}", "まだ持っていません。`/暗号 買う` で買えます。", ui.INFO), ephemeral=True)
        price = self._price()
        parts, diff = [], 0
        if units > 0:
            value, _fee = market.sell_quote(units, price)            # 売ったら受け取れる額
            diff = value - cost
            parts.append(f"保有: **{market.lots(units)}**\n価格: {market.pel(price)}\n評価額: **{ui.pelica(value)}**（売ったときの手取り）\n"
                         f"買った額: {ui.pelica(cost)}\n損益: **{'+' if diff >= 0 else ''}{diff:,}ペリカ**")
        if short[0] > 0:
            parts.append(f"📉 **空売り**\n{self._short_position(short[0], short[1], short[2], price)}")
            diff += Economy.short_pnl(short[0], short[1], price)
        await interaction.response.send_message(embed=ui.embed(f"💼 {market.NAME}", "\n\n".join(parts), ui.OK if diff >= 0 else ui.ERROR), ephemeral=True)

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
            key = (post["guild_id"], post["period"], expired, int(ends // 60))
            if key not in cache:                           # 同じ表示は、1回だけ作って使い回す（重い集計を重ねない）
                cache[key] = self.chart_embed(post["period"], guild_id=post["guild_id"], live_until=None if expired else ends, ended=expired)
            try:       # 履歴を読む権限が要らないよう、投稿を取得せずに直接書き換える
                await channel.get_partial_message(post["message_id"]).edit(embed=cache[key])
            except discord.NotFound:
                self.eco.delete_chart_post(post["channel_id"])      # 消されたチャートは、もう更新しない
                return
            except discord.HTTPException as exc:
                logthrottle.warn(("chart", post["channel_id"]), f"チャートを更新できません: channel={post['channel_id']}: {exc!r}")
        if expired:
            self.eco.end_chart_post(post["channel_id"])

    async def _retire_old_boards(self) -> None:
        """廃止した「チャート掲示」が残っていれば、メッセージを消して、設定も捨てる（1回だけ）。"""
        for board in self.eco.list_chart_boards():
            channel = self.bot.get_channel(board["channel_id"])
            if channel is not None:
                try:
                    await channel.get_partial_message(board["message_id"]).delete()
                except discord.HTTPException:
                    pass
            self.eco.delete_chart_board(board["guild_id"])

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        self.eco.delete_guild_chart_posts(guild.id)


async def setup(bot):
    await bot.add_cog(MarketCog(bot))
