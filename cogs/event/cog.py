"""突発イベントの告知。1日1回、1つのゲームで30分だけ勝ち分が増える（中身は domain/events.py）。

開始と終了を、ランキング掲示のチャンネル（無ければシステムチャンネル）に投稿する。メンションは付けない。
Bot が止まっていて開始を逃しても、告知はやり直さない（終了後に起動したときは、何も投稿しない）。
"""

import datetime
import logging
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands, tasks

from cogs.event.texts import event_copy
from cogs.shared import logthrottle, ui
from cogs.shared.announce import announce_channel
from cogs.shared.bridges import BotBridges, IEventAdminBridge
from cogs.shared.tasks import run_for_allowed_guilds
from domain import events
from domain.ports.clock import IClock
from domain.ports.contracts import ManualEventRow
from domain.ports.repos import IGuildRepo
from services.event import EventService

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot


def _event_channel(guild: discord.Guild, m: ManualEventRow) -> discord.abc.GuildChannel | None:
    """特別イベントの開始を告知したチャンネル（消えていれば None → 告知チャンネルに出す）。"""
    return guild.get_channel(m["channel_id"]) if m.get("channel_id") else None


class EventCog(commands.Cog, IEventAdminBridge):
    def __init__(
        self,
        bot: commands.Bot,
        config: dict[str, Any],
        event_svc: EventService,
        guild_repo: IGuildRepo,
        clock: IClock,
    ) -> None:
        self.bot = bot
        self._config = config
        self._events = event_svc
        self._guild_repo = guild_repo
        self._clock = clock

    async def cog_load(self) -> None:
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        bridges = getattr(self.bot, "bridges", None)
        if isinstance(bridges, BotBridges):
            bridges.unregister(IEventAdminBridge, self)

    @tasks.loop(minutes=1)
    async def tick(self) -> None:
        try:
            await self.run_once()
        except Exception as exc:
            _log.warning(f"突発イベントの確認に失敗: {exc!r}")

    @tick.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    def build_embed(self, ev: events.Event, ended: bool) -> discord.Embed:
        title, blurb = event_copy(ev.game)
        if ended:
            return ui.embed(f"{title} は終了しました", "また次のイベントをお楽しみに！", ui.INFO)
        text = (
            f"{blurb}\n\n**{ev.clock(ev.start_min)} 〜 {ev.clock(ev.end_min)}**（{events.DURATION_MIN}分間）の間、"
            f"`/カジノ {ev.game}` で勝つと、**勝ち分が{1 + events.BOOST:g}倍**になります。\n"
            f"（上乗せは1回につき最大 {ui.pelica(events.BONUS_CAP)} まで）"
        )
        return ui.embed(f"🔥 突発イベント発生！ {title}", text, ui.GOLD)

    def manual_embed(self, m: ManualEventRow, ended: bool) -> discord.Embed:
        title, blurb = event_copy(m["game"])  # 廃止したゲームのイベントが残っていても落ちない
        if ended:
            return ui.embed(f"{title} は終了しました", "また次のイベントをお楽しみに！", ui.INFO)
        text = (
            f"{blurb}\n\n<t:{int(m['starts'])}:t> 〜 <t:{int(m['ends'])}:t>（<t:{int(m['ends'])}:R>に終了）の間、"
            f"`/カジノ {m['game']}` で勝つと、**勝ち分が{1 + m['boost'] / 100:g}倍**になります。\n"
            f"（上乗せは1回につき最大 {ui.pelica(events.BONUS_CAP)} まで）"
        )
        return ui.embed(f"🔥 特別イベント発生！ {title}", text, ui.GOLD)

    async def start_manual(
        self,
        guild: discord.Guild,
        game: str,
        boost_pct: int,
        minutes: int,
        channel: discord.abc.Messageable | None = None,
    ) -> tuple[bool, str]:
        """持ち主が開く特別イベント。いま開催中のものがあれば置き換える。channel を渡すと、そこに告知する（終了の告知も同じ所）。"""
        replaced, started = self._events.start_manual(
            guild.id, game, boost_pct, minutes, getattr(channel, "id", 0) or 0
        )
        if started is not None:
            await self._post(guild, self.manual_embed(started, False), channel)
        title, _ = event_copy(game)
        return True, f"{guild.name}: {title}を{minutes}分間、勝ち分+{boost_pct}%で開きました" + (
            "（前のイベントは置き換えました）" if replaced else ""
        )

    async def stop_manual(self, guild: discord.Guild) -> tuple[bool, str]:
        m = self._events.stop_manual(guild.id)
        if m is None:
            return False, f"{guild.name}: 開催中の特別イベントはありません"
        await self._post(guild, self.manual_embed(m, True), _event_channel(guild, m))
        title, _ = event_copy(m["game"])
        return True, f"{guild.name}: {title}を終わらせました"

    async def run_once(self) -> None:
        now = self._clock.now()
        await run_for_allowed_guilds(
            self.bot,
            self._guild_repo,
            self._config,
            lambda guild: self._run_guild(guild, now),
            error_label="イベントの進行に失敗",
        )

    async def _run_guild(self, guild: discord.Guild, now: datetime.datetime) -> None:
        minutes = now.hour * 60 + now.minute
        done = self._events.finish_due(guild.id, now.timestamp())  # 時間が来た特別イベントの終了を、1回だけ告知する
        if done is not None:
            await self._post(guild, self.manual_embed(done, True), _event_channel(guild, done))
        ev = events.schedule(guild.id, now.date())
        phase = self._events.phase(guild.id, now.date())
        if phase == 0 and ev.start_min <= minutes < ev.end_min:
            self._events.set_phase(guild.id, now.date(), 1)
            await self._post(guild, self.build_embed(ev, False))
        elif phase < 2 and minutes >= ev.end_min:
            self._events.set_phase(guild.id, now.date(), 2)
            if phase == 1:  # 開始を告知した分だけ、終了も告知する
                await self._post(guild, self.build_embed(ev, True))

    async def _post(
        self,
        guild: discord.Guild,
        embed: discord.Embed,
        channel: object = None,
    ) -> None:
        target = channel or announce_channel(guild, self._guild_repo)
        if target is None:
            return
        try:
            await ui.send_channel(target, embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            logthrottle.warn(("event", guild.id), f"突発イベントを投稿できません: guild={guild.id}: {exc!r}")


async def setup(bot: "CasinoBot") -> None:
    cog = EventCog(bot, bot.config, bot.services.event, bot.repos.guild, bot.services.clock)
    bot.bridges.register(IEventAdminBridge, cog)
    await bot.add_cog(cog)
