"""運営口座の収支を、毎日0時（設定の時差）に公開する。

- 前日の収入・支出・収支・残高と、ゲーム別の内訳、資金追加と横領を、告知チャンネルに出す。個人の名前は出さない。
- Bot が止まっていて0時を逃しても、次に起動したときに、逃した日を1回ずつ出す（二重には出さない。最大 MAX_BACKLOG 日）。
- 口座を開いた日から数える。開いた日の分も、その翌日に出る。
"""

import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.economy_cog import local_now
from cogs.weekly_cog import announce_channel
from core import approval, logthrottle, ui
from core.eco_house import NON_OPERATING

MAX_BACKLOG = 3
DAILY_REPORT = False      # 毎日の収支公開。いまは止めている（復活するときは True にし、下の /運営口座 のコメントアウトも外す）


def day_range(day: datetime.date, tz: datetime.tzinfo) -> tuple:
    """その日の0時〜翌日の0時（epoch秒）。"""
    start = datetime.datetime.combine(day, datetime.time(0, 0), tzinfo=tz)
    return start.timestamp(), (start + datetime.timedelta(days=1)).timestamp()


def sign(n: int) -> str:
    return f"{n:+,}"


def report_embed(summary: dict, title: str) -> discord.Embed:
    """集計（eco.house_summary）から、公開用の埋め込みを作る。"""
    rows = summary["rows"]
    operating = {k: v for k, v in rows.items() if k not in NON_OPERATING}
    income = sum(t for _, t in operating.values() if t > 0)
    spending = sum(t for _, t in operating.values() if t < 0)
    net = income + spending
    lines = [f"収入 **{sign(income)}**　支出 **{sign(spending)}**　収支 **{sign(net)}**",
             f"残高 {ui.pelica(summary['opening'])} → **{ui.pelica(summary['closing'])}**"]
    games = {k.split(":", 1)[1]: v for k, v in operating.items() if k.startswith("ゲーム:")}
    e = ui.embed(title, "\n".join(lines), ui.OK if net >= 0 else ui.ERROR)
    if games:
        e.add_field(name="🎰 ゲーム別（ユーザーの負け − 勝ち）", value="\n".join(f"{g}: **{sign(t)}**（{n:,}件）" for g, (n, t) in sorted(games.items(), key=lambda kv: -abs(kv[1][1]))), inline=False)
    others = {k: v for k, v in operating.items() if not k.startswith("ゲーム:")}
    if others:
        e.add_field(name="📒 そのほか", value="\n".join(f"{k}: **{sign(t)}**" for k, (n, t) in sorted(others.items(), key=lambda kv: -abs(kv[1][1]))), inline=False)
    issued = summary.get("issued", {})
    if issued:
        e.add_field(name="🪙 システムの発行（口座の残高には影響しません）", value="\n".join(f"{k}: **{sign(t)}**（{n:,}件）" for k, (n, t) in sorted(issued.items(), key=lambda kv: -kv[1][1])), inline=False)
    special = [f"{k}: **{sign(rows[k][1])}**" for k in NON_OPERATING if k in rows]
    if special:
        e.add_field(name="🏦 資金の動き（収支には入れません）", value="\n".join(special), inline=False)
    if not rows and not issued:
        e.description += "\nこの日は、お金の動きがありませんでした。"
    e.set_footer(text="運営口座の収支です。ユーザー同士のお金とCSトークンの売買は、口座を通しません")
    return e


class HouseCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        if DAILY_REPORT:
            self.tick.start()

    # ---- 公開の /運営口座 は、いまは止めている（復活するときは、この行から下のコメントを外す。tests/test_house.py の該当テストも戻す） ----
    # @app_commands.command(name="運営口座", description="🏦 運営口座の残高と、きょう・きのうの収支を見る")
    # @app_commands.guild_only()
    # async def show(self, interaction: discord.Interaction):
    #     """だれでも使える。みんなに見える形で出す（毎日の公開と同じ集計）。"""
    #     eco = self.bot.economy.for_guild(interaction.guild_id)
    #     today = local_now(self.bot)
    #     today_start, tomorrow_start = day_range(today.date(), today.tzinfo)
    #     yesterday_start, _ = day_range(today.date() - datetime.timedelta(days=1), today.tzinfo)
    #     await interaction.response.send_message(embeds=[
    #         report_embed(eco.house_summary(today_start, tomorrow_start), "🏦 運営口座（総合）"),
    #         report_embed(eco.house_summary(yesterday_start, today_start), "🏦 運営口座（きのう）")])

    def cog_unload(self):
        if DAILY_REPORT:
            self.tick.cancel()

    @tasks.loop(minutes=5)
    async def tick(self):
        try:
            await self.run_once()
        except Exception as exc:
            print(f"運営口座の公開に失敗: {exc!r}")

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def run_once(self) -> None:
        now = local_now(self.bot)
        for guild in list(self.bot.guilds):
            if not approval.is_allowed(self.bot.economy, self.bot.config, guild.id, 0):
                continue
            try:
                await self.report_guild(guild, now)
            except Exception as exc:       # 1サーバーの失敗で、ほかを止めない
                print(f"運営口座の収支を公開できません: guild={guild.id}: {exc!r}")

    async def report_guild(self, guild: discord.Guild, now: datetime.datetime) -> None:
        eco = self.bot.economy.for_guild(guild.id)
        info = eco.house_info()
        today = now.date()
        if info["last_report"]:
            last = datetime.date.fromisoformat(info["last_report"])
        else:
            last = datetime.datetime.fromtimestamp(info["created"], now.tzinfo).date() - datetime.timedelta(days=1)      # 口座を開いた日から数える
        pending = [last + datetime.timedelta(days=i) for i in range(1, (today - last).days)]           # last の翌日 〜 昨日
        if not pending:
            return
        channel = announce_channel(guild, eco)
        for day in pending[-MAX_BACKLOG:]:
            start, end = day_range(day, now.tzinfo)
            if channel is not None:
                try:
                    await channel.send(embed=report_embed(eco.house_summary(start, end), f"🏦 運営口座の収支（{day.month}月{day.day}日）"),
                                       allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException as exc:
                    logthrottle.warn(("house", guild.id), f"運営口座の収支を投稿できません: guild={guild.id}: {exc!r}")
                    return                      # 投稿できなかった日は印を付けず、次の確認でやり直す
        eco.house_mark_reported((today - datetime.timedelta(days=1)).isoformat())


async def setup(bot):
    await bot.add_cog(HouseCog(bot))
