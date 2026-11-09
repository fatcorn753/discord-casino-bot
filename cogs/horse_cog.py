"""/競馬 — 毎日2回の定時レース（と、管理者の臨時レース）。馬を育てて走らせ、馬券を買う。

- 馬の購入券とにんじんは、ショップで買う。`/競馬 厩舎` で、名前と見た目を決めて馬を迎え、ステータスを配分して、レースに申し込む。
- 申し込みの締切で、帝愛の馬で埋めて出走馬を決め、オッズが出る。馬券を買えるのは、発走の2分前まで。
- 発走で、結果を先に決めて精算し、そのあとアニメーション（メッセージの書き換え）で見せる。
数字とシミュレーションは core/horse.py、処理は core/eco_horse.py、画面の部品は cogs/_horse_views.py。
"""

import asyncio
import random
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs._horse_views import RaceButton, Stable, card_embed, card_view, race_title
from cogs.weekly_cog import announce_channel
from core import approval, logthrottle, ui
from core import horse as horse_mod

TRACK_W = 22  # アニメーションのコースの長さ（文字数）
STEP = 1.4  # メッセージの書き換えの間隔（秒。レート制限に収まる）
ANIM_FRAMES = 13  # アニメーションのコマ数（約18秒）
NONE = discord.AllowedMentions.none()


GAP_ZOOM = 3.0  # 先頭との差を、見やすく広げる倍率（そのままだと、全馬が団子に見える）


def display_frames(frames: list, winner: int, finish: list = None) -> list:
    """アニメーション用に、先頭がゴールした瞬間までに切り、先頭との差を GAP_ZOOM 倍に広げる（後ろに下がって見えないようにする）。

    最後のコマは、1着の馬がゴールに着いた瞬間。ほかの馬は、ゴールの時刻の差（finish）だけ手前にいる
    （ティックの粒が粗く、同じコマで何頭もゴールに着くので、時刻の差で並べ直す）。
    """
    end = next((i for i, row in enumerate(frames) if row[winner] >= 1.0), len(frames) - 1)
    out, shown = [], [0.0] * len(frames[0])
    for row in frames[: end + 1]:
        lead = max(row)
        cur = [max(0.0, lead - (lead - p) * GAP_ZOOM) for p in row]
        shown = [max(a, b) for a, b in zip(shown, cur)]  # 前のコマより後ろには下がらない
        out.append(list(shown))
    if finish is not None:
        last = [max(0.0, 1.0 - (f - finish[winner]) / horse_mod.TICKS * GAP_ZOOM) for f in finish]
        before = out[-2] if len(out) > 1 else [0.0] * len(last)
        out[-1] = [min(1.0, max(a, b)) for a, b in zip(before, last)]  # 前のコマより後ろには下がらない
    out[-1][winner] = 1.0
    return out


def pick_frames(frames: list, count: int = ANIM_FRAMES) -> list:
    """全ティックの進みから、アニメーションに使うコマを等間隔に選ぶ（最初と最後を含む）。"""
    last = len(frames) - 1
    idx = sorted({round(i * last / (count - 1)) for i in range(count)})
    return [frames[i] for i in idx]


def lane_text(entries: list, positions: list) -> str:
    rows = []
    for e, p in zip(entries, positions):
        k = min(TRACK_W, int(p * TRACK_W))
        rows.append(f"{e['lane']} {e['silks']}{'─' * k}🐎{'·' * (TRACK_W - k)}🏁 {e['name'][:5]}")
    return "```\n" + "\n".join(rows) + "\n```"


def comment(entries: list, positions: list, previous_leader, final: bool, winner=None):
    """実況の一言と、いまの先頭（馬の添字）。final のときは、winner（1着の馬の添字）を名指しする。"""
    leader = max(range(len(positions)), key=lambda i: positions[i])
    top = positions[leader]
    name = entries[leader]["name"]
    if final:
        w = leader if winner is None else winner
        return f"🏁 **ゴール！！** 1着は **{entries[w]['name']}**！", w
    if top < 0.06:
        return "🔔 ゲートが開いた！ **スタート！**", leader
    if previous_leader is not None and leader != previous_leader:
        return f"🔥 **{name}** が先頭に立った！", leader
    if top >= 0.85:
        return f"📣 ゴール前の叩き合い！ **{name}** 先頭！", leader
    if top >= 0.68:
        return f"📣 **最後の直線！** 先頭は {name}！", leader
    if top >= 0.4:
        return f"向こう正面。先頭は {name}。", leader
    return f"先頭は {name}。", leader


def result_embed(eco, result: dict) -> discord.Embed:
    e = card_embed(eco, {**result["race"], "status": "done"})
    bets = result["bets"]
    if bets:
        net_by_user = {}
        for b in bets:
            net_by_user[b["user_id"]] = net_by_user.get(b["user_id"], 0) + b["payout"] - b["stake"]
        winners = sorted((n, u) for u, n in net_by_user.items() if n > 0)[::-1][:8]
        total_stake = sum(b["stake"] for b in bets)
        total_pay = sum(b["payout"] for b in bets)
        lines = [f"馬券 {len(bets)}枚（賭け金 {ui.pelica(total_stake)}・払い戻し {ui.pelica(total_pay)}）"]
        lines += [f"💰 <@{u}> **+{n:,}**" for n, u in winners] or ["的中者はいませんでした。"]
        e.add_field(name="払い戻し", value="\n".join(lines), inline=False)
    if result["prizes"]:
        e.add_field(
            name="賞金（馬主）",
            value="\n".join(f"🏆 <@{u}> +{ui.pelica(p)}" for u, p in result["prizes"].items()),
            inline=False,
        )
    return e


class HorseCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._busy = set()  # いま処理しているサーバー（二重に走らせない）

    async def cog_load(self):
        self.bot.add_dynamic_items(RaceButton)
        self.tick.start()

    def cog_unload(self):
        self.tick.cancel()

    group = app_commands.Group(
        name="競馬", description="🏇 競走馬を育てて走らせる・馬券を買う（毎日2回）", guild_only=True
    )

    def g(self, interaction: discord.Interaction):
        return self.bot.economy.for_guild(interaction.guild_id)

    # ------------------------------ コマンド ------------------------------ #
    @group.command(name="厩舎", description="自分の馬・ステータスの配分・出走の申し込み")
    async def stable(self, interaction: discord.Interaction):
        view = Stable(self.bot, interaction.user.id, interaction.guild_id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @group.command(name="出馬表", description="いまのレースの出馬表・オッズ（馬券もここから）")
    async def card(self, interaction: discord.Interaction):
        eco = self.g(interaction)
        races = eco.race_open()
        if not races:
            slots = eco.race_slots(time.time(), float(self.bot.config.get("utc_offset_hours", 9)))
            return await interaction.response.send_message(
                embed=ui.embed(
                    "🏇 競馬",
                    f"いま、開催中のレースはありません。次の発走は <t:{int(slots[0])}:t>（<t:{int(slots[0])}:R>）です。",
                    ui.INFO,
                ),
                ephemeral=True,
            )
        race = races[0]
        await interaction.response.send_message(
            embed=card_embed(eco, race),
            view=card_view(race["id"], race["status"] == "betting"),
        )

    # ------------------------------ 開催 ------------------------------ #
    async def start_special(
        self, guild: discord.Guild, entry_min: int, bet_min: int, channel: discord.abc.Messageable = None
    ) -> int:
        """臨時レースを開催する（管理者用）。channel を指定すれば、掲示・馬券・レースはそこに出す。作ったレースの番号を返す。"""
        eco = self.bot.economy.for_guild(guild.id)
        channel = channel or announce_channel(guild, eco)
        race_id = eco.race_create_special(time.time(), entry_min, bet_min, channel.id if channel else 0)
        await self._announce_entry(guild, eco, race_id)
        return race_id

    async def _channel(self, guild: discord.Guild, eco, race: dict):
        channel = guild.get_channel(race["channel_id"]) if race["channel_id"] else None
        return channel or announce_channel(guild, eco)

    async def _post(self, channel, **kw):
        try:
            return await channel.send(allowed_mentions=NONE, **kw)
        except discord.HTTPException as exc:
            logthrottle.warn(("horse-post", channel.id), f"競馬の投稿に失敗: channel={channel.id}: {exc!r}")
            return None

    async def _announce_entry(self, guild: discord.Guild, eco, race_id: int) -> None:
        race = eco.race_get(race_id)
        channel = await self._channel(guild, eco, race)
        if channel is None:
            return
        msg = await self._post(
            channel,
            content="🏇 **競馬** の出走申し込みが始まりました！",
            embed=card_embed(eco, race),
            view=card_view(race_id, False),
        )
        if msg is not None:
            eco.race_set_message(race_id, channel.id, msg.id)

    async def _retire(self, guild: discord.Guild, eco, race: dict) -> None:
        """締切後の古い投稿を、ボタンなしで「締め切りました」の表示に書き換える（残ると、締切の「◯秒前」が増え続ける）。"""
        channel = guild.get_channel(race["channel_id"]) if race["channel_id"] else None
        if channel is None or not race["message_id"]:
            return
        try:
            await channel.get_partial_message(race["message_id"]).edit(
                embed=card_embed(eco, race, closed=True), view=None
            )
        except discord.HTTPException as exc:  # 消されていても、進行には関係ない
            logthrottle.warn(("horse-retire", channel.id), f"競馬の古い投稿を書き換えられません: {exc!r}")

    async def _close_entries(self, guild: discord.Guild, eco, race: dict) -> None:
        field = eco.race_close_entries(race["id"])
        if field is None:
            return
        await self._retire(guild, eco, race)  # 申し込みの投稿（race は、まだ entry のときの値）
        seed = random.SystemRandom().randrange(2**31)
        odds = await asyncio.to_thread(
            horse_mod.make_all_odds, field["runners"], field["track"], horse_mod.ODDS_SIMS, seed
        )
        eco.race_set_odds(race["id"], odds)  # シミュレーションは重いので、別スレッドで走らせる
        race = eco.race_get(race["id"])
        channel = await self._channel(guild, eco, race)
        if channel is not None:
            msg = await self._post(
                channel,
                content="🏇 **出馬表とオッズが出ました！** 馬券は、発走の2分前まで買えます。",
                embed=card_embed(eco, race),
                view=card_view(race["id"], True),
            )
            if msg is not None:
                eco.race_set_message(race["id"], channel.id, msg.id)  # 発走で書き換えるために、こちらを覚える

    async def _run(self, guild: discord.Guild, eco, race: dict) -> None:
        result = eco.race_run(race["id"])
        if result is None:
            return
        await self._retire(guild, eco, race)  # 発売中の投稿（race は、まだ betting のときの値）
        entries = result["entries"]
        winner = result["order"][0]
        frames = pick_frames(display_frames(result["frames"], winner, result["finish"]))
        channel = await self._channel(guild, eco, race)
        if channel is None:
            return
        title = f"{race_title(eco, race)}　発走！　{result['track'].label}"

        def frame_embed(i: int, leader):
            positions = frames[i]
            final = i == len(frames) - 1
            text, new_leader = comment(entries, positions, leader, final, winner)
            return ui.embed(
                title, f"{text}\n{lane_text(entries, positions)}", ui.GOLD if final else ui.INFO
            ), new_leader

        embed, leader = frame_embed(0, None)
        msg = await self._post(channel, embed=embed)
        if msg is not None:
            try:
                for i in range(1, len(frames)):
                    await asyncio.sleep(STEP)
                    embed, leader = frame_embed(i, leader)
                    await msg.edit(embed=embed)
            except discord.HTTPException as exc:  # 消されても、結果はもう精算済み
                logthrottle.warn(("horse-anim", guild.id), f"競馬の演出を表示できません: {exc!r}")
        await asyncio.sleep(STEP)
        await self._post(channel, embed=result_embed(eco, result))

    # ------------------------------ 定時の確認 ------------------------------ #
    @tasks.loop(seconds=20)
    async def tick(self):
        try:
            await self.run_once()
        except Exception as exc:
            print(f"競馬の確認に失敗: {exc!r}")

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def run_once(self) -> None:
        now = time.time()
        tz = float(self.bot.config.get("utc_offset_hours", 9))
        for guild in list(self.bot.guilds):
            if guild.id in self._busy or not approval.is_allowed(self.bot.economy, self.bot.config, guild.id, 0):
                continue
            self._busy.add(guild.id)
            try:
                eco = self.bot.economy.for_guild(guild.id)
                channel = announce_channel(guild, eco)
                race_id = eco.race_ensure_scheduled(now, tz, channel.id if channel else 0)
                if race_id:
                    await self._announce_entry(guild, eco, race_id)
                for race in eco.race_open():
                    if race["status"] == "entry" and now >= race["entry_close"]:
                        await self._close_entries(guild, eco, race)
                    elif race["status"] == "betting" and now >= race["start_at"]:
                        await self._run(guild, eco, race)
            except Exception as exc:  # 1サーバーの失敗で、ほかを止めない
                print(f"競馬を処理できません: guild={guild.id}: {exc!r}")
            finally:
                self._busy.discard(guild.id)


async def setup(bot):
    await bot.add_cog(HorseCog(bot))
