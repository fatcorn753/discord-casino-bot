"""抽選 — Bot の持ち主が（`/運営 抽選 …` から）開催する。参加費を先に預かり、締切に参加者から1人へ全額渡す。参加はボタン。

- 参加費は全員同じ額（掛金）。集めた参加費の全額（手数料なし）が当選者へ渡る。
- 参加者が2人に満たないとき、中止したときは、全員に全額を返す。
- 開催状況と参加者は DB に保存する。Bot が再起動しても、ボタンは動き、締切を過ぎていれば起動後に締める。
- ミッション・連勝・週間ランキング・突発イベントには数えない。
"""

import random
import time

import discord
from discord.ext import commands, tasks

from cogs.weekly_cog import announce_channel
from core import approval, logthrottle, ui

MIN_FEE = 500  # 参加費の最小。上限はない
RANDOM_FEE_RANGE = (500, 3_000)  # 参加費を省略したときの、ランダムの範囲
DEFAULT_MINUTES = 60
MAX_MINUTES = 24 * 60
JOIN_ID = "raffle:join"
SHOWN_ENTRANTS = 20  # 掲示に並べる参加者の数（それ以上は人数だけ）


def raffle_embed(raffle: dict, now: float = None) -> discord.Embed:
    entries = raffle["entries"]
    prize = raffle["fee"] * len(entries)
    shown = " ".join(f"<@{u}>" for u in entries[:SHOWN_ENTRANTS]) + (
        f" ほか{len(entries) - SHOWN_ENTRANTS}人" if len(entries) > SHOWN_ENTRANTS else ""
    )
    lines = [
        f"参加費: **{ui.pelica(raffle['fee'])}**（全員同じ額。押した時点で先に引かれます）",
        f"締切: <t:{int(raffle['closes'])}:R>（<t:{int(raffle['closes'])}:t>）",
        f"参加者: **{len(entries)}人**" + (f"\n{shown}" if entries else ""),
        f"いまの賞金: **{ui.pelica(prize)}**（集めた参加費の全額。当選者は1人）",
        "",
        "参加者が2人に満たないときは、全員に全額を返します。",
    ]
    return ui.embed("🎟️ 抽選 参加者募集中！", "\n".join(lines), ui.GOLD)


def result_embed(raffle: dict) -> discord.Embed:
    n = len(raffle["entries"])
    if raffle["winner"] is not None:
        lines = [
            f"🎉 当選: <@{raffle['winner']}>",
            f"賞金: **{ui.pelica(raffle['prize'])}**（参加 {n}人 × {ui.pelica(raffle['fee'])}）",
        ]
        return ui.embed("🎟️ 抽選 結果発表！", "\n".join(lines), ui.GOLD)
    if n == 0:
        return ui.embed("🎟️ 抽選 終了", "参加者がいなかったので、抽選は行いませんでした。", ui.INFO)
    return ui.embed(
        "🎟️ 抽選 終了", f"参加者が{n}人だったので、抽選は行わず、全員に{ui.pelica(raffle['fee'])}を返しました。", ui.INFO
    )


class JoinView(discord.ui.View):
    """参加ボタン。再起動しても動くように、タイムアウトなし・固定の custom_id にしている。"""

    def __init__(self, cog: "RaffleCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="参加する", emoji="🎟️", style=discord.ButtonStyle.success, custom_id=JOIN_ID)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.on_join(interaction)


class RaffleCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(JoinView(self))
        self.tick.start()

    def cog_unload(self):
        self.tick.cancel()

    def g(self, interaction: discord.Interaction):
        return self.bot.economy.for_guild(interaction.guild_id)

    # ------------------------------ 運営の操作（`/運営 抽選 …` から呼ぶ。サーバーは持ち主が指定する） ------------------------------ #
    async def host(
        self, guild: discord.Guild, host_id: int, fee: int = None, minutes: int = DEFAULT_MINUTES, channel=None
    ) -> tuple:
        """そのサーバーの告知チャンネル（channel を渡せば、そのチャンネル）に、抽選の募集を出す。(成功か, 説明) を返す。"""
        eco = self.bot.economy.for_guild(guild.id)
        channel = channel or announce_channel(guild, eco)
        if channel is None:
            return (
                False,
                "投稿できるチャンネルがありません（ランキング掲示のチャンネル、またはシステムチャンネルに、投稿の権限が必要です）。",
            )
        fee = fee or random.randint(RANDOM_FEE_RANGE[0] // 100, RANDOM_FEE_RANGE[1] // 100) * 100
        ok, reason = eco.raffle_open(host_id, fee, time.time() + minutes * 60, channel.id)
        if not ok:
            return False, reason
        raffle = eco.raffle_get()
        try:
            message = await channel.send(
                embed=raffle_embed(raffle), view=JoinView(self), allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException as exc:
            eco.raffle_close(cancel=True)  # 投稿できなかったので、募集はなかったことにする
            return False, f"投稿できませんでした: {exc}"
        eco.raffle_set_message(raffle["id"], message.id)
        return True, f"{guild.name} の {channel.mention} に投稿しました（参加費 {ui.pelica(fee)}・{minutes}分）。"

    async def repost(self, guild: discord.Guild, channel=None) -> tuple:
        """募集中の抽選の投稿を、告知チャンネル（channel を渡せば、そのチャンネル）に出し直す（古い投稿は消す）。"""
        eco = self.bot.economy.for_guild(guild.id)
        raffle = eco.raffle_get()
        if raffle is None:
            return False, "募集中の抽選がありません。"
        channel = channel or announce_channel(guild, eco)
        if channel is None:
            return False, "投稿できるチャンネルがありません。"
        old_channel, old_id = guild.get_channel(raffle["channel_id"]), raffle["message_id"]
        try:
            message = await channel.send(
                embed=raffle_embed(raffle), view=JoinView(self), allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException as exc:
            return False, f"投稿できませんでした: {exc}"
        eco.raffle_set_message(raffle["id"], message.id, channel.id)  # 古い投稿のボタンは、これで無効になる
        if old_channel is not None and old_id:
            try:
                await old_channel.get_partial_message(old_id).delete()
            except discord.HTTPException:
                pass  # すでに消えている・消せない場合は、そのままにする
        return True, f"{guild.name} の {channel.mention} に出し直しました。"

    async def cancel(self, guild: discord.Guild) -> tuple:
        """募集中の抽選をやめて、参加費を全員に返す。"""
        raffle = self.bot.economy.for_guild(guild.id).raffle_close(cancel=True)
        if raffle is None:
            return False, "募集中の抽選がありません。"
        await self._finish_message(
            guild,
            raffle,
            ui.embed(
                "🎟️ 抽選 中止",
                f"抽選は中止されました。参加した{len(raffle['entries'])}人に参加費を返しました。",
                ui.WARN,
            ),
        )
        return True, f"{guild.name} の抽選を中止し、{len(raffle['entries'])}人に返金しました。"

    # ------------------------------ 参加ボタン ------------------------------ #
    async def on_join(self, interaction: discord.Interaction):
        if interaction.guild_id is None or not approval.is_allowed(
            self.bot.economy, self.bot.config, interaction.guild_id, interaction.user.id
        ):
            return await interaction.response.send_message(embed=ui.error_embed("利用できません"), ephemeral=True)
        eco = self.g(interaction)
        raffle = eco.raffle_get()
        if raffle is None or raffle["message_id"] != interaction.message.id:
            return await interaction.response.send_message(embed=ui.error_embed("募集は終わりました"), ephemeral=True)
        ok, reason = eco.raffle_join(interaction.user.id, time.time())
        if not ok:
            return await interaction.response.send_message(
                embed=ui.error_embed("参加できません", reason), ephemeral=True
            )
        raffle = eco.raffle_get()
        await interaction.response.edit_message(embed=raffle_embed(raffle))
        await interaction.followup.send(
            embed=ui.ok_embed(
                "参加しました",
                f"参加費 {ui.pelica(raffle['fee'])} を預かりました。結果は締切にこのメッセージの下へ出ます。",
            ),
            ephemeral=True,
        )

    # ------------------------------ 締切 ------------------------------ #
    @tasks.loop(seconds=20)
    async def tick(self):
        try:
            await self.run_once()
        except Exception as exc:
            print(f"抽選の確認に失敗: {exc!r}")

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def run_once(self) -> None:
        now = time.time()
        for guild in list(self.bot.guilds):
            if not approval.is_allowed(self.bot.economy, self.bot.config, guild.id, 0):
                continue
            eco = self.bot.economy.for_guild(guild.id)
            raffle = eco.raffle_get()
            if raffle is None or now < raffle["closes"]:
                continue
            try:
                done = eco.raffle_close()
                if done is not None:
                    await self._finish_message(guild, done, result_embed(done))
            except Exception as exc:  # 1サーバーの失敗で、ほかを止めない
                print(f"抽選を締められません: guild={guild.id}: {exc!r}")

    async def _finish_message(self, guild: discord.Guild, raffle: dict, embed: discord.Embed) -> None:
        """募集の投稿からボタンを外し、結果を同じチャンネルへ出す（当選者にはメンションする）。"""
        channel = guild.get_channel(raffle["channel_id"]) if guild else None
        if channel is None:
            return
        winner = raffle.get("winner")
        mentions = discord.AllowedMentions(
            users=[discord.Object(id=winner)] if winner else [], roles=False, everyone=False
        )
        try:
            if raffle["message_id"]:
                closed = raffle_embed(raffle)
                closed.title = "🎟️ 抽選（募集終了）"
                await channel.get_partial_message(raffle["message_id"]).edit(embed=closed, view=None)
            await channel.send(content=f"🎉 <@{winner}>" if winner else None, embed=embed, allowed_mentions=mentions)
        except discord.HTTPException as exc:
            logthrottle.warn(("raffle", guild.id), f"抽選の結果を投稿できません: guild={guild.id}: {exc!r}")


async def setup(bot):
    await bot.add_cog(RaffleCog(bot))
