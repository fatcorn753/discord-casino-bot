"""/暗号 — CSトークンの売買と、価格のチャート。価格は全サーバー共通で、1分ごとに動く。"""

import random
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.economy_cog import manage_guild_or_owner
from core import logthrottle, market, ui
from core.confirm import confirm_action

RNG = random.SystemRandom()
TICK_MINUTES = 1
KEEP_DAYS = 30                  # 価格の履歴を残す日数
HOLDER_ROWS = 10               # チャートに載せる保有者の数
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
            self.step_price()
            self.eco.market_prune(time.time() - KEEP_DAYS * 86400)
        except Exception as exc:
            print(f"価格の更新に失敗: {exc!r}")
            return
        for board in self.eco.list_chart_boards():
            try:
                await self._refresh_board(board)
            except Exception as exc:       # 1つのサーバーの失敗で、他のサーバーの更新を止めない
                print(f"チャート掲示板の更新に失敗: {board}: {exc!r}")

    @tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()

    # ------------------------------ 表示 ------------------------------ #
    def chart_embed(self, period: str = "24h", title_prefix: str = "", guild_id: Optional[int] = None) -> discord.Embed:
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
        e.set_footer(text=f"{TICK_MINUTES}分ごとに値が動きます ｜ 売買の手数料はありません ｜ ゲーム内の通貨です")
        return e

    @group.command(name="価格", description=f"{market.NAME}のいまの価格")
    async def price(self, interaction: discord.Interaction):
        hist = self.eco.market_history(time.time() - 86400)
        prices = [p for _, p, _ in hist] or [self._price()]
        s = market.summary(prices)
        await interaction.response.send_message(embed=ui.embed(
            f"📈 {market.NAME}", f"現在 **{market.pel(s['last'])}**（1枚）\n24時間: **{s['change']:+.1f}%**　高値 {market.fmt(s['high'])}　安値 {market.fmt(s['low'])}\n"
            f"`/暗号 チャート` で値動きを見られます。", ui.GOLD), ephemeral=True)

    @group.command(name="チャート", description=f"{market.NAME}の値動きをチャートで表示（みんなに見える）")
    @app_commands.rename(period="期間")
    @app_commands.describe(period="どの期間を見るか（省略時: 24時間）")
    @app_commands.choices(period=PERIOD_CHOICES)
    async def chart(self, interaction: discord.Interaction, period: Optional[app_commands.Choice[str]] = None):
        await interaction.response.send_message(embed=self.chart_embed(period.value if period else "24h", guild_id=interaction.guild.id),
                                                allowed_mentions=discord.AllowedMentions.none())

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

    @staticmethod
    def _position(units: int, cost: int, price: int) -> str:
        """持ち分の状態（保有・取得価格・いまの価格と％・評価損益）。"""
        pct = market.pnl_percent(price, units, cost)
        value, _fee = market.sell_quote(units, price)
        diff = value - cost
        return (f"保有: **{market.lots(units)}**\n取得 {market.fmt(market.avg_price(units, cost))} → いま {market.fmt(price)}　"
                f"{'🔺' if pct >= 0 else '🔻'}**{pct:+.1f}%**\n評価損益: **{diff:+,}ペリカ**（売ったときの手取り {ui.pelica(value)}）")

    @group.command(name="売る", description=f"持っている{market.NAME}を売る")
    @app_commands.rename(lots="枚数")
    @app_commands.describe(lots="売る枚数（小数も可。省略すると全部売ります）")
    async def sell(self, interaction: discord.Interaction, lots: Optional[app_commands.Range[float, 0.001, 1_000_000.0]] = None):
        async def run(uid):
            units = None if lots is None else int(round(lots * market.UNIT))
            have, cost = self.g(interaction).crypto_holding(uid)
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

    @group.command(name="保有", description=f"持っている{market.NAME}と、いまの評価額")
    async def holdings(self, interaction: discord.Interaction):
        units, cost = self.g(interaction).crypto_holding(interaction.user.id)
        if units <= 0:
            return await interaction.response.send_message(embed=ui.embed(f"💼 {market.NAME}", "まだ持っていません。`/暗号 買う` で買えます。", ui.INFO), ephemeral=True)
        price = self._price()
        value, _fee = market.sell_quote(units, price)            # 売ったら受け取れる額
        diff = value - cost
        await interaction.response.send_message(embed=ui.embed(
            f"💼 {market.NAME}",
            f"保有: **{market.lots(units)}**\n価格: {market.pel(price)}\n評価額: **{ui.pelica(value)}**（売ったときの手取り）\n"
            f"買った額: {ui.pelica(cost)}\n損益: **{'+' if diff >= 0 else ''}{diff:,}ペリカ**",
            ui.OK if diff >= 0 else ui.ERROR), ephemeral=True)

    # ------------------------------ チャート掲示 ------------------------------ #
    async def _refresh_board(self, board: dict) -> None:
        guild = self.bot.get_guild(board["guild_id"])
        if guild is None:
            return
        channel = guild.get_channel(board["channel_id"])
        if channel is None:
            self.eco.delete_chart_board(guild.id)
            return
        embed = self.chart_embed("24h", guild_id=guild.id)
        try:       # 履歴を読む権限が要らないよう、投稿を取得せずに直接書き換える
            await channel.get_partial_message(board["message_id"]).edit(embed=embed)
        except discord.NotFound:
            try:
                new = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                self.eco.set_chart_board(guild.id, channel.id, new.id)
            except discord.HTTPException as exc:
                logthrottle.warn(("chart-repost", guild.id), f"チャート掲示板を投稿し直せません: guild={guild.id}: {exc!r}")
        except discord.HTTPException as exc:
            logthrottle.warn(("chart", guild.id), f"チャート掲示板を更新できません: guild={guild.id}: {exc!r}")

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        self.eco.delete_chart_board(guild.id)

    @group.command(name="チャート掲示", description="チャートを1つ投稿し、自動で更新し続ける（サーバー管理）")
    @app_commands.rename(channel="チャンネル")
    @app_commands.describe(channel=f"チャートを置くチャンネル（{TICK_MINUTES}分ごとに自動で更新。1サーバーに1つ）")
    @manage_guild_or_owner()
    async def board_set(self, interaction: discord.Interaction, channel: discord.TextChannel):
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(embed=ui.error_embed(
                "掲示できません", f"Bot が {channel.mention} に埋め込みを投稿できません。チャンネルの権限を確認してください。"), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        old = self.eco.get_chart_board(interaction.guild.id)
        message = await channel.send(embed=self.chart_embed("24h", guild_id=interaction.guild.id), allowed_mentions=discord.AllowedMentions.none())
        self.eco.set_chart_board(interaction.guild.id, channel.id, message.id)
        if old:
            old_channel = interaction.guild.get_channel(old["channel_id"])
            if old_channel is not None:
                try:
                    await old_channel.get_partial_message(old["message_id"]).delete()
                except discord.HTTPException:
                    pass
        await interaction.followup.send(embed=ui.ok_embed(
            "チャートを掲示しました", f"{message.jump_url}\n{TICK_MINUTES}分ごとに、この投稿を自動で書き換えます。止めるときは `/暗号 チャート掲示解除` を使ってください。"), ephemeral=True)

    @group.command(name="チャート掲示解除", description="チャートの掲示を止めて、投稿を削除する（サーバー管理）")
    @manage_guild_or_owner()
    async def board_unset(self, interaction: discord.Interaction):
        board = self.eco.get_chart_board(interaction.guild.id)
        if board is None:
            return await interaction.response.send_message(embed=ui.warn_embed("掲示していません", "このサーバーには、チャートの掲示がありません。"), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        channel = interaction.guild.get_channel(board["channel_id"])
        if channel is not None:
            try:
                await channel.get_partial_message(board["message_id"]).delete()
            except discord.HTTPException:
                pass
        self.eco.delete_chart_board(interaction.guild.id)
        await interaction.followup.send(embed=ui.ok_embed("掲示を止めました", "チャートの自動更新を止めて、投稿を削除しました。"), ephemeral=True)


async def setup(bot):
    await bot.add_cog(MarketCog(bot))
