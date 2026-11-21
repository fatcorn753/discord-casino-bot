"""/闘技場 — 地下闘技場。1日1回、1on1とバトロワの両方を同時開催する（それぞれ別の大会）。

戦士への賭け総額に応じて武器で強くなるが、必ず勝てるとは限らない。決勝は準決勝よりオッズが高い。
数字とシミュレーションは domain/arena.py、処理は infra/db/arena.py、画面の部品は cogs/arena/*.py。

既知の制限: 掲示パネルの賭けボタンは、Bot再起動をまたぐと動かなくなる（再起動後は `/闘技場 大会` で出し直す）。
"""

import asyncio
import logging
import random
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.arena.embeds import FORMAT_NAME, STAGE_LABEL, battle_embed, final_result_embed, semis_result_embed
from cogs.arena.views import BetPanel
from cogs.shared import approval_guard, logthrottle, ui
from cogs.shared.announce import announce_channel
from cogs.shared.bridges import BotBridges, IArenaAdminBridge
from cogs.shared.tasks import run_for_allowed_guilds
from domain import arena as arena_mod
from domain.ports.contracts import ArenaFighterRow, ArenaFinalResult, ArenaSemiResult, ArenaTournamentDetailRow
from domain.ports.repos import IGuildRepo
from services.arena import ArenaService

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot

_log = logging.getLogger(__name__)
NONE = discord.AllowedMentions.none()
ANIM_FRAMES = 6  # 演出のコマ数
ANIM_STEP = 1.2  # 演出のコマ間隔（秒）

DayKey = tuple[int, str, str]  # (guild_id, day, format)


class ArenaCog(commands.Cog, IArenaAdminBridge):
    def __init__(
        self,
        bot: commands.Bot,
        config: dict[str, Any],
        arena_svc: ArenaService,
        guild_repo: IGuildRepo,
    ) -> None:
        self.bot = bot
        self._config = config
        self._arena = arena_svc
        self._guild_repo = guild_repo
        self._busy: set[int] = set()
        # 自動投稿した掲示のBetPanel（段が進むたび、同じメッセージを書き換える）
        self._panels: dict[DayKey, BetPanel] = {}

    async def cog_load(self) -> None:
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        bridges = getattr(self.bot, "bridges", None)
        if isinstance(bridges, BotBridges):
            bridges.unregister(IArenaAdminBridge, self)

    group = app_commands.Group(
        name="闘技場",
        description="🏛️ 地下闘技場（1日1回、1on1・バトロワの両方を開催・NPC戦士に賭ける）",
        guild_only=True,
    )

    @group.command(name="大会", description="今日の大会の状況を見る・賭ける")
    @app_commands.rename(fmt="形式")
    @app_commands.choices(fmt=[app_commands.Choice(name=name, value=key) for key, name in FORMAT_NAME.items()])
    async def tournament(self, interaction: discord.Interaction, fmt: app_commands.Choice[str]) -> None:
        t = self._arena.ensure_tournament(ui.guild_id_of(interaction), fmt.value)
        panel = BetPanel(self._arena, t)
        await interaction.response.send_message(embed=panel.embed(), view=panel)
        try:
            panel.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    # ------------------------------ 定時の進行 ------------------------------ #
    @tasks.loop(seconds=30)
    async def tick(self) -> None:
        try:
            await self.run_once()
        except Exception as exc:
            _log.exception("地下闘技場の確認に失敗: %r", exc)

    @tick.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def _channel(self, guild: discord.Guild) -> discord.abc.GuildChannel | None:
        return announce_channel(guild, self._guild_repo)

    async def _post(self, channel: object, **kw: Any) -> discord.Message | None:
        try:
            return await ui.send_channel(channel, allowed_mentions=NONE, **kw)
        except discord.HTTPException as exc:
            cid = getattr(channel, "id", 0)
            logthrottle.warn(("arena-post", cid), f"地下闘技場の投稿に失敗: channel={cid}: {exc!r}")
            return None

    async def _animate_match(
        self, channel: object, fighters: list[ArenaFighterRow], winner_idx: int, title: str
    ) -> None:
        """戦闘演出（HPバーのメッセージ書き換え）。勝敗はすでに決まっており、見た目だけ。"""
        frames = arena_mod.battle_frames(len(fighters), winner_idx, random.Random(), ANIM_FRAMES)
        msg = await self._post(channel, embed=battle_embed(fighters, frames[0], title))
        if msg is None:
            return
        try:
            for hp in frames[1:]:
                await asyncio.sleep(ANIM_STEP)
                await msg.edit(embed=battle_embed(fighters, hp, title))
        except discord.HTTPException as exc:
            cid = getattr(channel, "id", 0)
            logthrottle.warn(("arena-anim", cid), f"闘技場の演出を表示できません: {exc!r}")

    async def _advance_semis(
        self, guild: discord.Guild, t: ArenaTournamentDetailRow, day_key: DayKey
    ) -> ArenaSemiResult | None:
        """準決勝を決着させ、演出・結果投稿・パネル更新まで行う（時刻は見ない。呼ぶ側が判断する）。"""
        pre_fighters = t["fighters"]
        result = self._arena.run_semis(guild.id, t["id"])
        if result:
            channel = await self._channel(guild)
            if channel is not None:
                for group_no, info in result["groups"].items():
                    group_fighters = [f for f in pre_fighters if f["group_no"] == group_no]
                    winner_idx = next(i for i, f in enumerate(group_fighters) if f["name"] == info["winner"])
                    await self._animate_match(channel, group_fighters, winner_idx, f"⏳ 準決勝 第{group_no}組、開戦！")
                await self._post(channel, embed=semis_result_embed(result))
            if day_key in self._panels:
                await self._panels[day_key].refresh()
        return result

    async def _advance_final(
        self, guild: discord.Guild, t: ArenaTournamentDetailRow, day_key: DayKey
    ) -> ArenaFinalResult | None:
        """決勝を決着させ、演出・結果投稿・パネル更新まで行う（時刻は見ない。呼ぶ側が判断する）。"""
        pre_finalists = [f for f in t["fighters"] if f["stage"] == "final"]
        result = self._arena.run_final(guild.id, t["id"])
        if result:
            channel = await self._channel(guild)
            names = [f["name"] for f in pre_finalists]
            if channel is not None and len(pre_finalists) >= 2 and result["champion"] in names:  # バトロワは4人決勝
                winner_idx = names.index(result["champion"])
                await self._animate_match(channel, pre_finalists, winner_idx, "👑 決勝、開戦！")
            if channel is not None:
                await self._post(channel, embed=final_result_embed(result))
            if day_key in self._panels:
                await self._panels[day_key].refresh()
                del self._panels[day_key]  # 大会は終わったので、以後は書き換えない
        return result

    async def host_now(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable | None,
        advance: bool,
        fmt: str | None = None,
    ) -> str:
        """[管理者用] 今日の大会を、時刻を待たずに出す（無ければ作成）。advance なら、現在の段をすぐ決着させる。

        fmt を省略すると、1on1・バトロワの両方を対象にする。
        """
        now = self._arena.clock.now_ts()
        results = []
        for f in arena_mod.FORMATS if fmt is None else [fmt]:
            t = self._arena.ensure_tournament(guild.id, f, now)
            day_key = (guild.id, t["day"], f)
            target = channel or await self._channel(guild)
            panel = BetPanel(self._arena, t)
            msg = await self._post(
                target,
                content=f"🏛️ **地下闘技場** 今日の{FORMAT_NAME.get(f, f)}の大会です！",
                embed=panel.embed(),
                view=panel,
            )
            panel.message = msg
            self._panels[day_key] = panel
            if advance:
                if t["status"] == "semi_betting":
                    await self._advance_semis(guild, t, day_key)
                    t = self._arena.ensure_tournament(guild.id, f, now)
                if t["status"] == "final_betting":
                    await self._advance_final(guild, t, day_key)
                    t = self._arena.ensure_tournament(guild.id, f, now)
            status: str = t["status"]
            results.append(f"{FORMAT_NAME.get(f, f)}: {STAGE_LABEL.get(status, status)}")
        return "　".join(results)

    async def _process(self, guild: discord.Guild, now: float, fmt: str, tz_hour: int) -> None:
        """1形式ぶんの、告知・準決勝・決勝の定時進行。

        止めているあいだは、新しい大会を作らない（作ってあった大会は、最後まで進めて精算する）。
        """
        if approval_guard.feature_off(self._guild_repo, guild.id, "arena"):
            current = self._arena.current_tournament(guild.id, fmt, now)
            if current is None:
                return
        t = self._arena.ensure_tournament(guild.id, fmt, now)

        day_key = (guild.id, t["day"], fmt)
        if t["created"] == now:  # このBotプロセスで初めてではなく、大会そのものが今できたときだけ告知する
            channel = await self._channel(guild)
            if channel is not None:
                panel = BetPanel(self._arena, t)
                msg = await self._post(
                    channel,
                    content=f"🏛️ **地下闘技場** 今日の{FORMAT_NAME.get(fmt, fmt)}の大会が始まりました！",
                    embed=panel.embed(),
                    view=panel,
                )
                panel.message = msg
                self._panels[day_key] = panel
        if t["status"] == "semi_betting" and tz_hour >= arena_mod.SEMI_HOUR:
            await self._advance_semis(guild, t, day_key)
        t = self._arena.ensure_tournament(guild.id, fmt, now)  # 準決勝の決着を反映して読み直す
        if t["status"] == "final_betting" and tz_hour >= arena_mod.FINAL_HOUR:
            await self._advance_final(guild, t, day_key)

    async def run_once(self) -> None:
        now = self._arena.clock.now_ts()
        tz_hour = self._arena.local_hour(now)

        async def _handle(guild: discord.Guild) -> None:
            for fmt in arena_mod.FORMATS:
                await self._process(guild, now, fmt, tz_hour)

        await run_for_allowed_guilds(
            self.bot,
            self._guild_repo,
            self._config,
            _handle,
            error_label="地下闘技場を処理できません",
            busy=self._busy,
            logger=_log,
        )


async def setup(bot: "CasinoBot") -> None:
    cog = ArenaCog(bot, bot.config, bot.services.arena, bot.repos.guild)
    await bot.add_cog(cog)
    bot.bridges.register(IArenaAdminBridge, cog)
