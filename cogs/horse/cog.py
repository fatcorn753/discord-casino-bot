"""/競馬 — 毎日2回の定時レース（と、管理者の臨時レース）。馬を育てて走らせ、馬券を買う。

- 馬の購入券とにんじんは、ショップで買う。`/競馬 厩舎` で、名前と見た目を決めて馬を迎え、ステータスを配分して、レースに申し込む。
- 申し込みの締切で、帝愛の馬で埋めて出走馬を決め、オッズが出る。馬券を買えるのは、発走の2分前まで。
- 発走で、結果を先に決めて精算し、そのあとアニメーション（メッセージの書き換え）で見せる。
数字とシミュレーションは domain/horse.py、処理は infra/db/horse.py・race.py、画面の部品は cogs/horse/views.py。
"""

import asyncio
import datetime
import logging
from typing import TYPE_CHECKING, Any, Optional, cast

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.horse.betting import BetPanel
from cogs.horse.cards import RaceButton, card_embed, card_view, race_title, runner_icon
from cogs.horse.stable import Stable
from cogs.shared import approval_guard, logthrottle, ui
from cogs.shared.announce import announce_channel
from cogs.shared.bridges import BotBridges, IHorseAdminBridge, IHorseButtonBridge
from cogs.shared.tasks import run_for_allowed_guilds
from domain import horse as horse_mod
from domain.ports.contracts import RaceRow, RaceRunResult
from domain.ports.repos import IGuildRepo
from services.horse import HorseDesk, HorseService

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot

TRACK_W = 22  # アニメーションのコースの長さ（文字数）
STEP = 1.4  # メッセージの書き換えの間隔（秒。レート制限に収まる）
ANIM_FRAMES = 24  # 1800m のアニメーションのコマ数（約34秒）。距離に比例して増減する
NONE = discord.AllowedMentions.none()


GAP_ZOOM = 3.0  # 先頭との差を、見やすく広げる倍率（そのままだと、全馬が団子に見える）


def display_frames(frames: list, winner: int, finish: Optional[list] = None) -> list:
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


def anim_frames(distance: int) -> int:
    """距離に応じたコマ数（長いレースほど、長く見せる）。1000m で13コマ・3000m で40コマ。"""
    return max(8, round(ANIM_FRAMES * distance / 1800))


def pick_frames(frames: list, count: int = ANIM_FRAMES) -> list:
    """全ティックの進みから、アニメーションに使うコマを等間隔に選ぶ（最初と最後を含む）。"""
    last = len(frames) - 1
    idx = sorted({round(i * last / (count - 1)) for i in range(count)})
    return [frames[i] for i in idx]


def laps_for(distance: int) -> int:
    """コースを何周する見せ方にするか（長いレースは、同じコースを回る）。2000m で2周・3000m で3周。"""
    return max(1, distance // 1000)


def lane_text(entries: list, positions: list, laps: int = 1, now: Optional[datetime.datetime] = None) -> str:
    """各馬の位置。laps>1 のときは、コースの1周が全体の 1/laps で、何周目かを頭に書く（馬ごとに、いまの周の位置を出す）。

    now を渡すと、季節演出の期間中は、帝愛の馬が季節の絵（例: 🦌）で走る。
    """
    rows = []
    for e, p in zip(entries, positions):
        lap = min(laps - 1, int(p * laps))
        frac = 1.0 if p >= 1.0 else p * laps - lap
        k = min(TRACK_W, int(frac * TRACK_W))
        mark = f" {lap + 1}周目" if laps > 1 and p < 1.0 else ""
        icon = runner_icon(e, now) if now is not None else "🐎"
        rows.append(f"{e['lane']} {e['silks']}{'─' * k}{icon}{'·' * (TRACK_W - k)}🏁 {e['name'][:5]}{mark}")
    head = ""
    if laps > 1:
        lead = min(laps, int(max(positions) * laps) + 1) if max(positions) < 1.0 else laps
        head = f"🔁 先頭は {lead}周目 / 全{laps}周\n"
    return "```\n" + head + "\n".join(rows) + "\n```"


def comment(entries: list, positions: list, previous_leader, final: bool, winner=None, distance: Optional[int] = None):
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
        e = entries[leader]
        is_spurt = distance is not None and e["guts"] > 20 and top > horse_mod.reach_for(e["stamina"], distance)
        if is_spurt:
            return f"💪 **{name}** が根性のラストスパートで逆転！！", leader
        return f"🔥 **{name}** が先頭に立った！", leader
    if top >= 0.85:
        return f"📣 ゴール前の叩き合い！ **{name}** 先頭！", leader
    if top >= 0.68:
        return f"📣 **最後の直線！** 先頭は {name}！", leader
    if top >= 0.4:
        return f"向こう正面。先頭は {name}。", leader
    return f"先頭は {name}。", leader


def result_embed(desk: HorseDesk, result: RaceRunResult) -> discord.Embed:
    e = card_embed(desk, cast("RaceRow", {**result["race"], "status": "done"}))
    bets = result["bets"]
    if bets:
        # 的中した人は、払い戻しの額だけを載せる（収支は出さない。何頭もまとめて買うと、当たっても収支は赤字になりやすいので）
        paid = {}
        for b in bets:
            paid[b["user_id"]] = paid.get(b["user_id"], 0) + b["payout"]
        winners = sorted(((pay, u) for u, pay in paid.items() if pay > 0), reverse=True)[:8]
        total_stake = sum(b["stake"] for b in bets)
        total_pay = sum(b["payout"] for b in bets)
        lines = [f"馬券 {len(bets)}枚（賭け金 {ui.pelica(total_stake)}・払い戻し {ui.pelica(total_pay)}）"]
        lines += [f"💰 <@{u}> 払い戻し **{pay:,}**" for pay, u in winners] or ["的中者はいませんでした。"]
        e.add_field(name="払い戻し", value="\n".join(lines), inline=False)
    if result["prizes"]:
        e.add_field(
            name="賞金（馬主）",
            value="\n".join(f"🏆 <@{u}> +{ui.pelica(p)}" for u, p in result["prizes"].items()),
            inline=False,
        )
    if result.get("retired"):
        lines = [
            f"💐 **{h['silks']} {h['name']}** が定年（{horse_mod.MAX_RACES}戦）で名誉引退"
            + (
                f"（<@{h['owner_id']}> に引退{'金' if h['value'] >= 0 else '費用'} {abs(h['value']):,}）"
                if h["value"]
                else ""
            )
            for h in result["retired"]
        ]
        e.add_field(name="🐴 定年引退", value="\n".join(lines), inline=False)
    return e


class HorseCog(commands.Cog, IHorseAdminBridge, IHorseButtonBridge):
    def __init__(
        self,
        bot: commands.Bot,
        config: dict[str, Any],
        horse_svc: HorseService,
        guild_repo: IGuildRepo,
    ) -> None:
        self.bot = bot
        self._config = config
        self._horse_svc = horse_svc
        self._guild_repo = guild_repo
        self._busy = set()  # いま処理しているサーバー（二重に走らせない）
        self._posts = {}  # レースの番号 → `/競馬 出馬表` で出した投稿 [(チャンネル, メッセージ)]（締切で書き換える）
        self._bet_closed = set()  # 馬券の締切で、投稿を書き換え済みのレース

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(RaceButton)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        bridges = getattr(self.bot, "bridges", None)
        if isinstance(bridges, BotBridges):
            bridges.unregister(IHorseAdminBridge, self)
            bridges.unregister(IHorseButtonBridge, self)

    group = app_commands.Group(
        name="競馬", description="🏇 競走馬を育てて走らせる・馬券を買う（毎日2回）", guild_only=True
    )

    def _desk_for(self, interaction: discord.Interaction) -> HorseDesk:
        return self._horse_svc.desk(ui.guild_id_of(interaction))

    async def handle_button(self, interaction: discord.Interaction, action: str, race_id: int) -> None:
        gid = ui.guild_id_of(interaction)
        desk = self._horse_svc.desk(gid)
        race = desk.race_get(race_id)
        if race is None:
            await ui.reply_error(interaction, "そのレースは、もうありません。", title="見つかりません")
            return
        if action == "card":
            await interaction.response.send_message(embed=card_embed(desk, race), ephemeral=True)
            return
        if not await approval_guard.feature_guard(interaction, self._guild_repo, "horse"):
            return
        if race["status"] != "betting" or desk.clock.now_ts() >= race["bet_close"]:
            await ui.reply_error(interaction, "このレースの馬券は、まだ（またはもう）買えません。", title="買えません")
            return
        panel = BetPanel(self._horse_svc, interaction.user.id, race, self.post_bet_announcement)
        await interaction.response.send_message(embed=panel.embed(desk), view=panel, ephemeral=True)

    # ------------------------------ コマンド ------------------------------ #
    @group.command(name="厩舎", description="自分の馬・ステータスの配分・出走の申し込み")
    async def stable(self, interaction: discord.Interaction):
        view = Stable(
            self._horse_svc,
            interaction.user.id,
            ui.guild_id_of(interaction),
        )
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @group.command(name="出馬表", description="いまのレースの出馬表・オッズ（馬券もここから）")
    async def card(self, interaction: discord.Interaction):
        desk = self._desk_for(interaction)
        races = desk.race_open()
        if not races:
            slots = self._horse_svc.next_slots(ui.guild_id_of(interaction))
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
            embed=card_embed(desk, race),
            view=card_view(race["id"], race["status"] == "betting"),
        )
        try:  # 締切や発走で書き換えられるように、覚えておく（再起動すると忘れる）
            msg = await interaction.original_response()
            self._posts.setdefault(race["id"], []).append((msg.channel.id, msg.id))
        except discord.HTTPException:
            pass

    # ------------------------------ 開催 ------------------------------ #
    async def start_special(
        self, guild: discord.Guild, entry_min: int, bet_min: int, channel: Optional[discord.abc.Messageable] = None
    ) -> int:
        """臨時レースを開催する（管理者用）。channel を指定すれば、掲示・馬券・レースはそこに出す。作ったレースの番号を返す。"""
        desk = self._horse_svc.desk(guild.id)
        target = channel or announce_channel(guild, self._guild_repo)
        target_id = getattr(target, "id", 0)
        race_id = desk.race_create_special(desk.clock.now_ts(), entry_min, bet_min, target_id)
        await self._announce_entry(guild, desk, race_id)
        return race_id

    async def _channel(self, guild: discord.Guild, race: RaceRow):
        channel = guild.get_channel(race["channel_id"]) if race["channel_id"] else None
        return channel or announce_channel(guild, self._guild_repo)

    async def _post(self, channel, **kw):
        try:
            return await channel.send(allowed_mentions=NONE, **kw)
        except discord.HTTPException as exc:
            logthrottle.warn(("horse-post", channel.id), f"競馬の投稿に失敗: channel={channel.id}: {exc!r}")
            return None

    async def post_bet_announcement(self, guild: discord.Guild, race: RaceRow, content: str) -> None:
        channel = await self._channel(guild, race)
        if channel is not None:
            await self._post(channel, content=content)

    async def _announce_entry(self, guild: discord.Guild, desk: HorseDesk, race_id: int) -> None:
        race = desk.race_get(race_id)
        if race is None:
            return
        channel = await self._channel(guild, race)
        if channel is None:
            return
        msg = await self._post(
            channel,
            content="🏇 **競馬** の出走申し込みが始まりました！",
            embed=card_embed(desk, race),
            view=card_view(race_id, False),
        )
        if msg is not None:
            desk.race_set_message(race_id, channel.id, msg.id)

    async def _retire(self, guild: discord.Guild, desk: HorseDesk, race: RaceRow) -> None:
        """締切後の古い投稿を、ボタンなしで「締め切りました」の表示に書き換える（残ると、締切の「◯秒前」が増え続ける）。

        チャンネルへの告知のほか、`/競馬 出馬表` で出した投稿（self._posts）も同じように書き換える。
        """
        targets = [(race["channel_id"], race["message_id"])] if race["message_id"] else []
        targets += self._posts.pop(race["id"], [])
        embed = card_embed(desk, race, closed=True)
        for channel_id, message_id in targets:
            channel = guild.get_channel(channel_id) if channel_id else None
            if channel is None:
                continue
            try:
                await cast(discord.TextChannel, channel).get_partial_message(message_id).edit(embed=embed, view=None)
            except discord.HTTPException as exc:  # 消されていても、進行には関係ない
                logthrottle.warn(("horse-retire", channel.id), f"競馬の古い投稿を書き換えられません: {exc!r}")

    async def _close_entries(self, guild: discord.Guild, desk: HorseDesk, race: RaceRow) -> None:
        updated = self._horse_svc.close_entries_and_set_odds(guild.id, race["id"])
        if updated is None:
            return
        await self._retire(guild, desk, race)  # 申し込みの投稿（race は、まだ entry のときの値）
        race = updated
        channel = await self._channel(guild, race)
        if channel is not None:
            msg = await self._post(
                channel,
                content="🏇 **出馬表とオッズが出ました！** 馬券は、発走の2分前まで買えます。",
                embed=card_embed(desk, race),
                view=card_view(race["id"], True),
            )
            if msg is not None:
                desk.race_set_message(race["id"], channel.id, msg.id)  # 発走で書き換えるために、こちらを覚える

    async def _run(self, guild: discord.Guild, desk: HorseDesk, race: RaceRow) -> None:
        result = self._horse_svc.run_race(guild.id, race["id"])
        if result is None:
            return
        await self._retire(guild, desk, race)  # 発売中の投稿（race は、まだ betting のときの値）
        entries = result["entries"]
        winner = result["order"][0]
        frames = pick_frames(
            display_frames(result["frames"], winner, result["finish"]), anim_frames(result["track"].distance)
        )
        channel = await self._channel(guild, race)
        if channel is None:
            return
        title = f"{race_title(desk, race)}　発走！　{result['track'].label}"

        laps = laps_for(result["track"].distance)
        today = desk.clock.now()

        def frame_embed(i: int, leader):
            positions = frames[i]
            final = i == len(frames) - 1
            text, new_leader = comment(entries, positions, leader, final, winner, result["track"].distance)
            return ui.embed(
                title, f"{text}\n{lane_text(entries, positions, laps, today)}", ui.GOLD if final else ui.INFO
            ), new_leader

        embed, leader = frame_embed(0, None)
        msg = await self._post(channel, embed=embed)
        if msg is not None:
            try:
                for i in range(1, len(frames)):
                    await asyncio.sleep(STEP)
                    embed, leader = frame_embed(i, leader)
                    await msg.edit(embed=embed)
            except discord.HTTPException as exc:  # 消されていても、結果はもう精算済み
                logthrottle.warn(("horse-anim", guild.id), f"競馬の演出を表示できません: {exc!r}")
        await asyncio.sleep(STEP)
        await self._post(channel, embed=result_embed(desk, result))

    # ------------------------------ 定時の確認 ------------------------------ #
    @tasks.loop(seconds=5)  # 締切・発走の表示を、すぐ書き換えるために短くする
    async def tick(self):
        try:
            await self.run_once()
        except Exception as exc:
            _log.warning(f"競馬の確認に失敗: {exc!r}")

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def run_once(self) -> None:
        now = self._horse_svc.clock.now_ts()

        async def _handle(guild: discord.Guild) -> None:
            desk = self._horse_svc.desk(guild.id)
            channel = announce_channel(guild, self._guild_repo)
            # 止めているあいだは、新しいレースを組まない（組んだレースは、最後まで走らせて精算する）
            off = approval_guard.feature_off(self._guild_repo, guild.id, "horse")
            race_id = None if off else self._horse_svc.ensure_scheduled(guild.id, now, channel.id if channel else 0)

            if race_id:
                await self._announce_entry(guild, desk, race_id)
            for race in desk.race_open():
                match race["status"]:
                    case "entry" if now >= race["entry_close"]:
                        await self._close_entries(guild, desk, race)
                    case "betting" if now >= race["start_at"]:
                        await self._run(guild, desk, race)
                    case "betting" if now >= race["bet_close"] and race["id"] not in self._bet_closed:
                        # 馬券の締切で、投稿を「締め切りました」に書き換える（発走まで、「◯秒前」が増えるのを防ぐ）
                        self._bet_closed.add(race["id"])
                        await self._retire(guild, desk, race)

        await run_for_allowed_guilds(
            self.bot,
            self._guild_repo,
            self._config,
            _handle,
            error_label="競馬を処理できません",
            busy=self._busy,
        )


async def setup(bot: "CasinoBot") -> None:
    cog = HorseCog(
        bot,
        bot.config,
        bot.services.horse,
        bot.repos.guild,
    )
    bot.bridges.register(IHorseAdminBridge, cog)
    bot.bridges.register(IHorseButtonBridge, cog)
    await bot.add_cog(cog)
