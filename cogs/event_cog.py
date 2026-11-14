"""突発イベントの告知。1日1回、1つのゲームで30分だけ勝ち分が増える（中身は core/events.py）。

開始と終了を、ランキング掲示のチャンネル（無ければシステムチャンネル）に投稿する。メンションは付けない。
Bot が止まっていて開始を逃しても、告知はやり直さない（終了後に起動したときは、何も投稿しない）。
"""

import datetime
import time

import discord
from discord.ext import commands, tasks

from cogs._shared import announce_channel
from core import approval, events, logthrottle, ui
from core.clock import local_now


def _event_channel(guild: discord.Guild, m: dict):
    """特別イベントの開始を告知したチャンネル（消えていれば None → 告知チャンネルに出す）。"""
    return guild.get_channel(m["channel_id"]) if m.get("channel_id") else None


class EventCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.tick.start()

    def cog_unload(self):
        self.tick.cancel()

    @tasks.loop(minutes=1)
    async def tick(self):
        try:
            await self.run_once()
        except Exception as exc:
            print(f"突発イベントの確認に失敗: {exc!r}")

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    def build_embed(self, ev: events.Event, ended: bool) -> discord.Embed:
        if ended:
            return ui.embed(f"{ev.title} は終了しました", "また次のイベントをお楽しみに！", ui.INFO)
        text = (
            f"{ev.blurb}\n\n**{ev.clock(ev.start_min)} 〜 {ev.clock(ev.end_min)}**（{events.DURATION_MIN}分間）の間、"
            f"`/カジノ {ev.game}` で勝つと、**勝ち分が{1 + events.BOOST:g}倍**になります。\n"
            f"（上乗せは1回につき最大 {ui.pelica(events.BONUS_CAP)} まで）"
        )
        return ui.embed(f"🔥 突発イベント発生！ {ev.title}", text, ui.GOLD)

    def manual_embed(self, m: dict, ended: bool) -> discord.Embed:
        title, blurb = events.GAMES.get(m["game"], (m["game"], ""))  # 廃止したゲームのイベントが残っていても落ちない
        if ended:
            return ui.embed(f"{title} は終了しました", "また次のイベントをお楽しみに！", ui.INFO)
        text = (
            f"{blurb}\n\n<t:{int(m['starts'])}:t> 〜 <t:{int(m['ends'])}:t>（<t:{int(m['ends'])}:R>に終了）の間、"
            f"`/カジノ {m['game']}` で勝つと、**勝ち分が{1 + m['boost'] / 100:g}倍**になります。\n"
            f"（上乗せは1回につき最大 {ui.pelica(events.BONUS_CAP)} まで）"
        )
        return ui.embed(f"🔥 特別イベント発生！ {title}", text, ui.GOLD)

    async def start_manual(self, guild: discord.Guild, game: str, boost_pct: int, minutes: int, channel=None) -> tuple:
        """持ち主が開く特別イベント。いま開催中のものがあれば置き換える。channel を渡すと、そこに告知する（終了の告知も同じ所）。"""
        eco = self.bot.economy.for_guild(guild.id)
        now = time.time()
        replaced = eco.manual_event_get(now) is not None
        eco.manual_event_start(game, boost_pct, now, now + minutes * 60, channel.id if channel is not None else 0)
        await self._post(guild, eco, self.manual_embed(eco.manual_event_get(now), False), channel)
        return True, f"{guild.name}: {events.GAMES[game][0]}を{minutes}分間、勝ち分+{boost_pct}%で開きました" + (
            "（前のイベントは置き換えました）" if replaced else ""
        )

    async def stop_manual(self, guild: discord.Guild) -> tuple:
        eco = self.bot.economy.for_guild(guild.id)
        m = eco.manual_event_stop(time.time())
        if m is None:
            return False, f"{guild.name}: 開催中の特別イベントはありません"
        await self._post(guild, eco, self.manual_embed(m, True), _event_channel(guild, m))
        return True, f"{guild.name}: {events.GAMES.get(m['game'], (m['game'],))[0]}を終わらせました"

    async def run_once(self) -> None:
        now = local_now(self.bot)
        for guild in list(self.bot.guilds):
            if not approval.is_allowed(self.bot.economy, self.bot.config, guild.id, 0):
                continue
            try:
                await self._run_guild(guild, now)
            except Exception as exc:  # 1サーバーの失敗で、ほかのサーバーを止めない
                print(f"イベントの進行に失敗: guild={guild.id}: {exc!r}")

    async def _run_guild(self, guild: discord.Guild, now: datetime.datetime) -> None:
        minutes = now.hour * 60 + now.minute
        eco = self.bot.economy.for_guild(guild.id)
        done = eco.manual_event_finish_due(time.time())  # 時間が来た特別イベントの終了を、1回だけ告知する
        if done is not None:
            await self._post(guild, eco, self.manual_embed(done, True), _event_channel(guild, done))
        ev = events.schedule(guild.id, now.date())
        phase = eco.event_phase(now.date())
        if phase == 0 and ev.start_min <= minutes < ev.end_min:
            eco.set_event_phase(now.date(), 1)
            await self._post(guild, eco, self.build_embed(ev, False))
        elif phase < 2 and minutes >= ev.end_min:
            eco.set_event_phase(now.date(), 2)
            if phase == 1:  # 開始を告知した分だけ、終了も告知する
                await self._post(guild, eco, self.build_embed(ev, True))

    async def _post(self, guild: discord.Guild, eco, embed: discord.Embed, channel=None) -> None:
        channel = channel or announce_channel(guild, eco)
        if channel is None:
            return
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            logthrottle.warn(("event", guild.id), f"突発イベントを投稿できません: guild={guild.id}: {exc!r}")


async def setup(bot):
    await bot.add_cog(EventCog(bot))
