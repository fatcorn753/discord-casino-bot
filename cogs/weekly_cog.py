"""週間ランキングの自動発表。毎週月曜0時（設定の時差）に、先週の稼ぎ1位へ限定称号を贈り、結果を投稿する。

発表の場所は、ランキング掲示をしているチャンネル。無ければサーバーのシステムチャンネル。
Bot が止まっていて月曜0時を逃しても、次に起動したときに1回だけ発表する（二重には発表しない）。
"""

import discord
from discord.ext import commands, tasks

from cogs.economy_cog import local_now
from core import approval, logthrottle, shop, ui
from core import weekly

CHECK_MINUTES = 5
KING = shop.BY_ID[shop.WEEKLY_KING]


class WeeklyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        tz = float(self.bot.config.get("utc_offset_hours", 9))
        done = self.bot.economy.backfill_weekly(tz)         # この機能の前に遊んだ分を、台帳から補う（初回だけ）
        if done:
            print(f"週間ランキング: 過去の {done} ゲーム分を台帳から補いました")
        self.check_weeks.start()

    def cog_unload(self):
        self.check_weeks.cancel()

    @tasks.loop(minutes=CHECK_MINUTES)
    async def check_weeks(self):
        try:
            await self.run_once()
        except Exception as exc:
            print(f"週間ランキングの確認に失敗: {exc!r}")

    @check_weeks.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def run_once(self) -> None:
        today = local_now(self.bot).date()
        prev_week = weekly.previous_week_id(today)
        for guild in list(self.bot.guilds):
            if not approval.is_allowed(self.bot.economy, self.bot.config, guild.id, 0):
                continue
            eco = self.bot.economy.for_guild(guild.id)
            last = eco.weekly_last_announced()
            if last is None:
                eco.set_weekly_announced(prev_week)      # 初めて動いたときは、先週までを発表済みとして、次の月曜から発表する
            elif last < prev_week:
                try:
                    await self.announce(guild, eco, prev_week)
                except Exception as exc:                 # 1つのサーバーの失敗で、他のサーバーの発表を止めない
                    print(f"週間ランキングの発表に失敗: guild={guild.id}: {exc!r}")

    def _channel(self, guild: discord.Guild, eco):
        board = eco.get_leaderboard(guild.id)
        candidates = [guild.get_channel(board["channel_id"])] if board else []
        candidates.append(guild.system_channel)
        for ch in candidates:
            if ch is not None:
                perms = ch.permissions_for(guild.me)
                if perms.view_channel and perms.send_messages and perms.embed_links:
                    return ch
        return None

    def build_embed(self, week: str, result: dict) -> discord.Embed:
        lines = weekly.standings_lines(result["standings"])
        champ = result["champion"]
        if champ:
            lines += ["", f"👑 **<@{champ[0]}>** が **{KING.name}** を獲得しました！（通算{result['wins']}回目）",
                      f"今週の稼ぎ **{champ[1]:+,}ペリカ**（{champ[2]}戦）。`/ショップ 使う` で身につけられます（次の発表まで）。"]
        else:
            lines += ["", f"今週は、{weekly.MIN_PLAYS}ゲーム以上遊んでプラスだった人がいませんでした。王者はお休みです。"]
        if result["previous"]:
            lines.append("前の王者 " + "・".join(f"<@{u}>" for u in result["previous"]) + " の称号は外れました。")
        e = ui.embed(f"🏆 週間ランキング 発表！（{weekly.week_label(week)}）", "\n".join(lines), ui.GOLD)
        e.set_footer(text="毎週月曜0時に発表 ｜ 稼ぎ = その週のゲームの損益の合計")
        return e

    async def announce(self, guild: discord.Guild, eco, week: str) -> None:
        result = eco.weekly_award(week)
        if result is None or not result["standings"]:
            return                                        # 発表済み、またはその週は誰も遊んでいない
        channel = self._channel(guild, eco)
        if channel is not None:
            champ = result["champion"]
            mentions = discord.AllowedMentions(users=[discord.Object(id=champ[0])] if champ else [], roles=False, everyone=False)
            try:
                await channel.send(content=f"🎉 <@{champ[0]}>" if champ else None, embed=self.build_embed(week, result), allowed_mentions=mentions)
            except discord.HTTPException as exc:
                logthrottle.warn(("weekly", guild.id), f"週間ランキングを投稿できません: guild={guild.id}: {exc!r}")
        shop_cog = self.bot.get_cog("ShopCog")
        for uid in result["previous"]:                    # 外れた称号をニックネームからも消す
            if shop_cog is not None:
                try:
                    await shop_cog._sync_title_nick(uid, guild)
                except Exception as exc:
                    print(f"称号の取り消しをニックネームに反映できません: guild={guild.id} user={uid}: {exc!r}")


async def setup(bot):
    await bot.add_cog(WeeklyCog(bot))
