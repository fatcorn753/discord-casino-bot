"""/ペリカ — 残高・デイリー・ランキング・送金・履歴。"""

import datetime
import math
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import approval, logthrottle, ranking, shop, ui
from core import history as history_mod
from core import missions as missions_mod
from core.confirm import confirm_action
from core.economy import Economy

TRANSFER_FEE_RATE = 0.10        # 送る側が、送金額の10%を手数料として払う
TRANSFER_DAILY_LIMIT = 20_000   # 1日に送れる合計（手数料を含む）
TRANSFER_CONFIRM_FROM = 5_000   # この額以上は確認ボタンを挟む
BOARD_INTERVAL_MINUTES = 1       # ランキング掲示板を更新する間隔
TRANSFER_MIN_GAMES = 3          # 送金できるのは、ゲームで3回以上遊んだ人（複数アカウントで稼ぐのを防ぐ）


def reset_label(bot) -> str:
    """日付が切り替わる（デイリーやミッションがリセットされる）時刻の表示。"""
    offset = float(bot.config.get("utc_offset_hours", 9))
    return "日本時間 0:00" if offset == 9 else f"UTC{offset:+g} の 0:00"


DAILY_SLOT_HOURS = 6            # デイリーボーナスを受け取れる間隔（0・6・12・18時に切り替わる）


def tz_label(bot) -> str:
    offset = float(bot.config.get("utc_offset_hours", 9))
    return "日本時間" if offset == 9 else f"UTC{offset:+g}"


def slot_of(now: datetime.datetime) -> int:
    """いまが、何番目の「受け取りの時間帯」か（6時間ごとに1つ進む）。"""
    return now.date().toordinal() * (24 // DAILY_SLOT_HOURS) + now.hour // DAILY_SLOT_HOURS


def next_slot(bot) -> datetime.datetime:
    """次にデイリーボーナスを受け取れる時刻。"""
    now = local_now(bot)
    start = datetime.datetime.combine(now.date(), datetime.time(), tzinfo=now.tzinfo)
    return start + datetime.timedelta(hours=(now.hour // DAILY_SLOT_HOURS + 1) * DAILY_SLOT_HOURS)


def next_slot_label(bot) -> str:
    t = next_slot(bot)
    return f"{t.hour}:00（{tz_label(bot)}）"


def next_reset(bot) -> datetime.datetime:
    now = local_now(bot)
    return datetime.datetime.combine(now.date() + datetime.timedelta(days=1), datetime.time(), tzinfo=now.tzinfo)


def local_now(bot) -> datetime.datetime:
    tz = datetime.timezone(datetime.timedelta(hours=float(bot.config.get("utc_offset_hours", 9))))
    return datetime.datetime.now(tz)


def manage_guild_or_owner():
    """サーバー管理権限を持つ人、または Bot の持ち主（owner_ids）だけが使える。"""
    def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id in approval.owner_ids(interaction.client.config):
            return True
        perms = interaction.permissions
        if perms.manage_guild:
            return True
        raise app_commands.MissingPermissions(["manage_guild"])
    return app_commands.check(predicate)


class EconomyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def g(self, interaction: discord.Interaction):
        """このサーバーのペリカの記録（ペリカはサーバーごとに別々）。"""
        return self.bot.economy.for_guild(interaction.guild.id)

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
        s = self.g(interaction).stats(member.id) if member.id == interaction.user.id else self.g(interaction).peek_stats(member.id)
        if s is None:
            return await interaction.response.send_message(embed=ui.embed(
                f"💴 {member.display_name} の残高", "まだ遊んでいません（口座がありません）。", ui.INFO))
        rank = self.g(interaction).rank_of(member.id)
        # ショップで買ったカードの色・アイコン・称号を反映する
        prof = self.g(interaction).get_profile(member.id)
        color_item, icon_item, title_item = (shop.BY_ID.get(prof[k]) for k in ("card_color", "card_icon", "title"))
        icon = icon_item.value if icon_item else "💴"
        title_line = f"🏷️ **{title_item.name}**\n" if title_item else ""
        e = ui.embed(f"{icon} {member.display_name} の残高", f"{title_line}# {ui.pelica(s['balance'])}", color_item.value if color_item else ui.GOLD)
        e.set_thumbnail(url=member.display_avatar.url)
        if rank:
            e.add_field(name="このサーバーでの順位（総資産）", value=f"{rank[0]}位 / {rank[2]}人", inline=True)
        held = self.g(interaction).crypto_values().get(member.id, 0)
        if held:
            e.add_field(name="CSトークン評価額", value=ui.pelica(held), inline=True)
        e.add_field(name="遊んだ回数", value=f"{s['games_played']:,}回", inline=True)
        e.add_field(name="最大の勝ち", value=ui.pelica(s["biggest_win"]), inline=True)
        e.add_field(name="連続ログイン", value=f"{s['daily_streak']}日" if s["daily_streak"] else "まだ受け取っていません", inline=True)
        play = self.g(interaction).play_summary(member.id)
        if play["plays"]:
            e.add_field(name="勝率", value=f"{play['wins'] * 100 // play['plays']}%（{play['plays']:,}戦）", inline=True)
            e.add_field(name="ゲームの損益", value=f"{play['net_total']:+,}ペリカ", inline=True)
        if play["best_streak"] >= 2:
            e.add_field(name="最大連勝", value=f"{play['best_streak']}連勝", inline=True)
        e.set_footer(text="勝率・損益は、集計を始めてからのゲームの分です")
        await interaction.response.send_message(embed=e)

    @group.command(name="デイリー", description="6時間ごとの無料ボーナス（日本時間 0・6・12・18時に切り替わる）")
    async def daily(self, interaction: discord.Interaction):
        now = local_now(self.bot)
        result = self.g(interaction).claim_daily(interaction.user.id, now.date(), slot_of(now))
        if not result.ok:
            return await interaction.response.send_message(embed=ui.warn_embed(
                "まだ受け取れません",
                f"次は **{next_slot_label(self.bot)}**（<t:{int(next_slot(self.bot).timestamp())}:R>）から受け取れます。\n"
                f"ボーナスは6時間ごと（0・6・12・18時）です。\n連続ログイン: **{result.streak}日**"), ephemeral=True)
        next_bonus = Economy.DAILY_BASE + Economy.DAILY_STREAK_BONUS * min(result.streak, Economy.DAILY_STREAK_CAP)
        boost = "🎟️ デイリー2倍券を使った！\n" if result.boosted else ""
        await interaction.response.send_message(embed=ui.ok_embed(
            "デイリーボーナス",
            f"{boost}**+{ui.pelica(result.amount)}**\n連続ログイン: **{result.streak}日**（明日は {ui.pelica(next_bonus)}）\n残高: **{ui.pelica(result.balance)}**\n\n"
            f"🕛 次のボーナスは **{next_slot_label(self.bot)}**（<t:{int(next_slot(self.bot).timestamp())}:R>）から。6時間ごと（0・6・12・18時）に受け取れます。"))

    @group.command(name="ミッション", description="今日のミッションと進み具合を見る（達成すると自動でペリカがもらえる）")
    async def missions(self, interaction: discord.Interaction):
        now = local_now(self.bot)
        status = self.g(interaction).mission_status(interaction.user.id, now.date())
        lines = []
        for m, progress, done in status:
            bar = "🟩" * (10 * min(progress, m.target) // m.target) + "⬜" * (10 - 10 * min(progress, m.target) // m.target)
            mark = "✅" if done else "🔸"
            shown = m.target if done else progress
            lines.append(f"{mark} **{m.name}**　報酬 {ui.pelica(m.reward)}\n{bar} {shown:,} / {m.target:,}")
        tomorrow = next_reset(self.bot)
        streak, best = self.g(interaction).win_streak(interaction.user.id)
        lines.append(f"\n※ 賭け金 {ui.pelica(missions_mod.MIN_BET)} 以上のゲームだけが数えられます。連勝はその日の分だけです。")
        lines.append(f"🎁 3つ全部で、さらに **+{ui.pelica(missions_mod.COMPLETE_BONUS)}**")
        lines.append(f"🔥 いまの連勝: **{streak}** ／ 自己ベスト: **{best}**")
        lines.append(f"🕛 次の更新: **{reset_label(self.bot)}**（<t:{int(tomorrow.timestamp())}:R>）")
        await interaction.response.send_message(embed=ui.embed("📜 今日のミッション", "\n".join(lines), ui.GOLD), ephemeral=True)

    @group.command(name="ランキング", description="このサーバーで、ペリカの多い順に並べる")
    async def ranking(self, interaction: discord.Interaction):
        e = ranking.build_embed(self.g(interaction), None, "🏆 このサーバーのランキング（総資産）", interaction.user.id,
                                footer="総資産 = 残高 + CSトークンの評価額 ｜ ペリカはサーバーごとに別々です")
        await interaction.response.send_message(embed=e, allowed_mentions=discord.AllowedMentions.none())

    # ------------------- 自動で更新されるランキング掲示板 ------------------- #
    def _board_embed(self, guild_id: int) -> discord.Embed:
        ts = int(datetime.datetime.now().timestamp())
        return ranking.build_embed(
            self.bot.economy.for_guild(guild_id), None, "🏆 ペリカランキング（総資産）",
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
            # fetch_message は「メッセージ履歴を読む」権限が要る。自分の投稿の書き換えには要らないので、読まずに直接書き換える
            await channel.get_partial_message(board["message_id"]).edit(embed=embed)
        except discord.NotFound:
            try:       # メッセージが消された: 投稿し直して、新しいIDを記録する
                new = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                self.eco.set_leaderboard(guild.id, channel.id, new.id)
            except discord.HTTPException as exc:
                logthrottle.warn(("board-repost", guild.id), f"ランキング掲示板を投稿し直せません: guild={guild.id}: {exc!r}")
        except discord.HTTPException as exc:
            logthrottle.warn(("board", guild.id), f"ランキング掲示板を更新できません: guild={guild.id}: {exc!r}")      # 権限不足など。次の周期で再試行（ログは30分に1回）

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
                await channel.get_partial_message(board["message_id"]).delete()
            except discord.HTTPException:
                pass

    @group.command(name="ランキング掲示", description="ランキングを1つ投稿し、自動で更新し続ける（サーバー管理）")
    @app_commands.rename(channel="チャンネル")
    @app_commands.describe(channel=f"ランキングを置くチャンネル（{BOARD_INTERVAL_MINUTES}分ごとに自動で更新。1サーバーに1つ）")
    @manage_guild_or_owner()
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
    @manage_guild_or_owner()
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
            if self.g(interaction).balance(uid) < amount + fee:
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
            res = self.g(interaction).transfer(uid, member.id, amount, TRANSFER_FEE_RATE, TRANSFER_DAILY_LIMIT, day_start, TRANSFER_MIN_GAMES)
            if not res.ok:
                return await reply(embed=ui.error_embed("送金できません", res.reason), ephemeral=True)
            await reply(embed=ui.ok_embed("送金しました", f"{member.mention} に **{ui.pelica(res.received)}** を送りました。\n"
                                          f"手数料: {ui.pelica(res.fee)}　あなたの残高: **{ui.pelica(res.sender_balance)}**"),
                        allowed_mentions=discord.AllowedMentions(users=[member]))
        finally:
            self.bot.sessions.end(uid)

    @group.command(name="履歴", description="最近の増減を表示（自分だけに見えます）")
    async def history(self, interaction: discord.Interaction):
        rows = self.g(interaction).history(interaction.user.id, 60)       # 1ゲームは複数の行なので、多めに読んでまとめる
        lines = history_mod.format_history(rows, 12)
        await interaction.response.send_message(embed=ui.embed("🧾 最近の増減", "\n".join(lines) or "まだ記録がありません。", ui.INFO),
                                                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(EconomyCog(bot))
