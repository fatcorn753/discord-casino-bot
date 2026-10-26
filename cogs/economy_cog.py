"""/ペリカ — 残高・デイリー・ランキング・送金・履歴。"""

import datetime
import math
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import ranking, shop, ui
from core.confirm import confirm_action
from core.economy import Economy

TRANSFER_FEE_RATE = 0.10        # 送る側が、送金額の10%を手数料として払う
TRANSFER_DAILY_LIMIT = 20_000   # 1日に送れる合計（手数料を含む）
TRANSFER_CONFIRM_FROM = 5_000   # この額以上は確認ボタンを挟む
BOARD_INTERVAL_MINUTES = 30      # ランキング掲示板を更新する間隔
TRANSFER_MIN_GAMES = 3          # 送金できるのは、ゲームで3回以上遊んだ人（複数アカウントで稼ぐのを防ぐ）


def local_now(bot) -> datetime.datetime:
    tz = datetime.timezone(datetime.timedelta(hours=float(bot.config.get("utc_offset_hours", 9))))
    return datetime.datetime.now(tz)


class EconomyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def eco(self) -> Economy:
        return self.bot.economy

    group = app_commands.Group(name="ペリカ", description="💴 残高・デイリー・ランキング・送金")

    @group.command(name="残高", description="ペリカの残高と成績を表示")
    @app_commands.rename(member="対象")
    @app_commands.describe(member="見たい人（省略時: 自分）")
    async def balance(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        # 他の人を見るだけで、その人の口座（初期支給つき）が作られないよう、読み取り専用で調べる
        s = self.eco.stats(member.id) if member.id == interaction.user.id else self.eco.peek_stats(member.id)
        if s is None:
            return await interaction.response.send_message(embed=ui.embed(
                f"💴 {member.display_name} の残高", "まだ遊んでいません（口座がありません）。", ui.INFO))
        rank = self.eco.rank_of(member.id)
        # ショップで買ったカードの色・アイコン・称号を反映する
        prof = self.eco.get_profile(member.id)
        color_item, icon_item, title_item = (shop.BY_ID.get(prof[k]) for k in ("card_color", "card_icon", "title"))
        icon = icon_item.value if icon_item else "💴"
        title_line = f"🏷️ **{title_item.name}**\n" if title_item else ""
        e = ui.embed(f"{icon} {member.display_name} の残高", f"{title_line}# {ui.pelica(s['balance'])}", color_item.value if color_item else ui.GOLD)
        e.set_thumbnail(url=member.display_avatar.url)
        if rank:
            e.add_field(name="全体の順位", value=f"{rank[0]}位 / {rank[2]}人", inline=True)
        e.add_field(name="遊んだ回数", value=f"{s['games_played']:,}回", inline=True)
        e.add_field(name="最大の勝ち", value=ui.pelica(s["biggest_win"]), inline=True)
        e.add_field(name="連続ログイン", value=f"{s['daily_streak']}日", inline=True)
        await interaction.response.send_message(embed=e)

    @group.command(name="デイリー", description="1日1回の無料ボーナスを受け取る（連続で増える）")
    async def daily(self, interaction: discord.Interaction):
        now = local_now(self.bot)
        result = self.eco.claim_daily(interaction.user.id, now.date())
        if not result.ok:
            tomorrow = datetime.datetime.combine(now.date() + datetime.timedelta(days=1), datetime.time(), tzinfo=now.tzinfo)
            return await interaction.response.send_message(embed=ui.warn_embed(
                "今日はもう受け取りました",
                f"次は <t:{int(tomorrow.timestamp())}:R>（日本時間 0:00）に受け取れます。\n連続ログイン: **{result.streak}日**"), ephemeral=True)
        next_bonus = Economy.DAILY_BASE + Economy.DAILY_STREAK_BONUS * min(result.streak, Economy.DAILY_STREAK_CAP)
        boost = "🎟️ デイリー2倍券を使った！\n" if result.boosted else ""
        await interaction.response.send_message(embed=ui.ok_embed(
            "デイリーボーナス",
            f"{boost}**+{ui.pelica(result.amount)}**\n連続ログイン: **{result.streak}日**（明日は {ui.pelica(next_bonus)}）\n残高: **{ui.pelica(result.balance)}**"))

    @group.command(name="ランキング", description="ペリカの多い順に並べる")
    @app_commands.rename(scope="範囲")
    @app_commands.describe(scope="どの範囲で並べるか（省略時: このサーバー）")
    @app_commands.choices(scope=[app_commands.Choice(name="このサーバー", value="guild"),
                                 app_commands.Choice(name="全サーバー", value="all")])
    async def ranking(self, interaction: discord.Interaction, scope: app_commands.Choice[str] = None):
        only_guild = (scope is None) or scope.value == "guild"
        ids = self.eco.guild_user_ids(interaction.guild.id) if only_guild else None
        title = "🏆 このサーバーのランキング" if only_guild else "🏆 全サーバーのランキング"
        e = ranking.build_embed(self.eco, ids, title, interaction.user.id,
                                footer="※ Bot が見たことのある人だけが対象です（遊んだ・コマンドを使った人）")
        await interaction.response.send_message(embed=e, allowed_mentions=discord.AllowedMentions.none())

    # ------------------- 自動で更新されるランキング掲示板 ------------------- #
    def _board_embed(self, guild_id: int) -> discord.Embed:
        ts = int(datetime.datetime.now().timestamp())
        return ranking.build_embed(
            self.eco, self.eco.guild_user_ids(guild_id), "🏆 ペリカランキング",
            header=f"最終更新: <t:{ts}:R>\n\n",
            footer=f"{BOARD_INTERVAL_MINUTES}分ごとに自動で更新されます")

    async def _refresh_board(self, board: dict) -> None:
        """掲示板のメッセージを最新の順位に書き換える。消されていたら投稿し直す。"""
        guild = self.bot.get_guild(board["guild_id"])
        if guild is None:
            return                      # 一時的に見えないだけかもしれないので、記録は残す
        channel = guild.get_channel(board["channel_id"])
        if channel is None:
            self.eco.delete_leaderboard(guild.id)       # チャンネルが消えた
            return
        embed = self._board_embed(guild.id)
        try:
            message = await channel.fetch_message(board["message_id"])
            await message.edit(embed=embed)
        except discord.NotFound:
            try:       # メッセージが消された: 投稿し直して、新しいIDを記録する
                new = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                self.eco.set_leaderboard(guild.id, channel.id, new.id)
            except discord.HTTPException as exc:
                print(f"ランキング掲示板を投稿し直せません: guild={guild.id}: {exc!r}")
        except discord.HTTPException as exc:
            print(f"ランキング掲示板を更新できません: guild={guild.id}: {exc!r}")      # 権限不足など。次の周期で再試行

    async def cog_load(self):
        self.refresh_boards.start()

    def cog_unload(self):
        self.refresh_boards.cancel()

    @tasks.loop(minutes=BOARD_INTERVAL_MINUTES)
    async def refresh_boards(self):
        for board in self.eco.list_leaderboards():
            try:
                await self._refresh_board(board)
            except Exception as exc:       # 1つのサーバーの失敗で、他のサーバーの更新を止めない
                print(f"ランキング掲示板の更新に失敗: {board}: {exc!r}")

    @refresh_boards.before_loop
    async def _before_refresh(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        self.eco.delete_leaderboard(guild.id)

    async def _delete_board_message(self, guild: discord.Guild, board: Optional[dict]) -> None:
        if not board:
            return
        channel = guild.get_channel(board["channel_id"])
        if channel is not None:
            try:
                await (await channel.fetch_message(board["message_id"])).delete()
            except discord.HTTPException:
                pass

    @group.command(name="ランキング掲示", description="ランキングを1つ投稿し、自動で更新し続ける（サーバー管理）")
    @app_commands.rename(channel="チャンネル")
    @app_commands.describe(channel=f"ランキングを置くチャンネル（{BOARD_INTERVAL_MINUTES}分ごとに自動で更新。1サーバーに1つ）")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def board_set(self, interaction: discord.Interaction, channel: discord.TextChannel):
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(embed=ui.error_embed(
                "掲示できません", f"Bot が {channel.mention} に埋め込みを投稿できません。チャンネルの権限を確認してください。"), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        old = self.eco.get_leaderboard(interaction.guild.id)
        message = await channel.send(embed=self._board_embed(interaction.guild.id), allowed_mentions=discord.AllowedMentions.none())
        self.eco.set_leaderboard(interaction.guild.id, channel.id, message.id)
        await self._delete_board_message(interaction.guild, old)        # 前の掲示板があれば、新しいものと入れ替える
        await interaction.followup.send(embed=ui.ok_embed(
            "ランキングを掲示しました",
            f"{message.jump_url}\n{BOARD_INTERVAL_MINUTES}分ごとに、この投稿を自動で書き換えます。止めるときは `/ペリカ ランキング掲示解除` を使ってください。"), ephemeral=True)

    @group.command(name="ランキング掲示解除", description="ランキングの掲示を止めて、投稿を削除する（サーバー管理）")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def board_unset(self, interaction: discord.Interaction):
        board = self.eco.get_leaderboard(interaction.guild.id)
        if board is None:
            return await interaction.response.send_message(embed=ui.warn_embed("掲示していません", "このサーバーには、ランキングの掲示がありません。"), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self._delete_board_message(interaction.guild, board)
        self.eco.delete_leaderboard(interaction.guild.id)
        await interaction.followup.send(embed=ui.ok_embed("掲示を止めました", "自動の更新をやめて、ランキングの投稿を削除しました。"), ephemeral=True)

    @group.command(name="送金", description="ほかの人にペリカを送る（手数料10%・1日の上限あり・3回遊んでから）")
    @app_commands.rename(member="相手", amount="金額")
    @app_commands.describe(member="送る相手", amount=f"送る金額（手数料は別に{int(TRANSFER_FEE_RATE * 100)}%かかります）")
    async def transfer(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 1_000_000]):
        if member.bot:
            return await interaction.response.send_message(embed=ui.error_embed("送れません", "Bot には送金できません。"), ephemeral=True)
        # 送金の間は「プレイ中」にする。ゲーム中に残高を逃がして負けや倍払いを避ける／確認待ちの間にゲームを始める、を防ぐ
        uid = interaction.user.id
        reason = self.bot.sessions.try_start(uid)
        if reason:
            return await interaction.response.send_message(embed=ui.error_embed("いまは送れません", reason), ephemeral=True)
        try:
            fee = math.ceil(round(amount * TRANSFER_FEE_RATE, 6))
            # 確認ボタンを出す前に、残高を先に調べる（足りないのに確認だけさせない）
            if self.eco.balance(uid) < amount + fee:
                return await interaction.response.send_message(embed=ui.error_embed(
                    "送金できません", f"残高が足りません（送金額 {amount:,} + 手数料 {fee:,} が必要です）。"), ephemeral=True)
            if amount >= TRANSFER_CONFIRM_FROM:
                ok = await confirm_action(interaction, "送金しますか？",
                                          f"{member.mention} に **{ui.pelica(amount)}** を送ります。\n手数料: {ui.pelica(fee)}（合計 {ui.pelica(amount + fee)} が引かれます）", "送金する")
                if ok is not True:
                    return
                reply = interaction.followup.send
            else:
                reply = interaction.response.send_message
            now = local_now(self.bot)
            day_start = datetime.datetime.combine(now.date(), datetime.time(), tzinfo=now.tzinfo).timestamp()
            res = self.eco.transfer(uid, member.id, amount, TRANSFER_FEE_RATE, TRANSFER_DAILY_LIMIT, day_start, TRANSFER_MIN_GAMES)
            if not res.ok:
                return await reply(embed=ui.error_embed("送金できません", res.reason), ephemeral=True)
            await reply(embed=ui.ok_embed("送金しました", f"{member.mention} に **{ui.pelica(res.received)}** を送りました。\n"
                                          f"手数料: {ui.pelica(res.fee)}　あなたの残高: **{ui.pelica(res.sender_balance)}**"),
                        allowed_mentions=discord.AllowedMentions(users=[member]))
        finally:
            self.bot.sessions.end(uid)

    @group.command(name="履歴", description="最近の増減を表示（自分だけに見えます）")
    async def history(self, interaction: discord.Interaction):
        rows = self.eco.history(interaction.user.id, 12)
        lines = [f"<t:{int(r['ts'])}:t> `{r['delta']:+,}` → {r['balance_after']:,}　{r['reason']}" for r in rows]
        await interaction.response.send_message(embed=ui.embed("🧾 最近の増減", "\n".join(lines) or "まだ記録がありません。", ui.INFO), ephemeral=True)

    @group.command(name="付与", description="（持ち主のみ）ペリカを付与する")
    @app_commands.rename(member="対象", amount="金額")
    @app_commands.describe(member="付与する相手", amount="付与する金額")
    async def grant(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 10_000_000]):
        if interaction.user.id not in set(self.bot.config.get("owner_ids") or []):
            return await interaction.response.send_message(embed=ui.error_embed("権限がありません", "このコマンドは Bot の持ち主だけが使えます。"), ephemeral=True)
        balance = self.eco.credit(member.id, amount, f"付与（{interaction.user.id}）")
        await interaction.response.send_message(embed=ui.ok_embed("付与しました", f"{member.mention} に {ui.pelica(amount)} を付与しました（残高 {ui.pelica(balance)}）。"),
                                                allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(EconomyCog(bot))
