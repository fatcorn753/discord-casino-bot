"""帝愛共済（#34）— `/共済`。帝愛銀行の最後の損を負う代わりに、余剰から配当を受け取る持分。

- 総口数はサーバーごとに 10,000口で固定（#93）。新しい人は、譲渡か帝愛の競り（`/共済 入札`）で手に入れる
- 破産・休眠で帝愛が回収した口は、入札の競りに出す（値段は入札で決まる）
- 口は、値段をつけてほかの人へ譲れる（`譲渡` → 相手が `受諾`。手数料と、同じ相手との1日の上限は P2P と同じ）。
  準備率が低いとき・破産猶予中の人がいるときは止まる
- 動議（`提案`）で、帝愛信託の上乗せ金利を変えられる。いくつも並行でき、告知チャンネルにボタンつきで出る（#94）。
  賛成が反対より多く、かつ流通口数の一定割合以上なら可決し、締めた順に後勝ちで決まる
- 配当・動議の締め・競りは、1分ごとの見回りで行う
"""

import logging
import math
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.shared import approval_guard, logthrottle, ui
from cogs.shared.announce import announce_channel
from cogs.shared.bridges import BotBridges, IKyosaiVoteBridge, NoOpKyosaiVoteBridge
from cogs.shared.messages import format_failure
from cogs.shared.tasks import run_for_allowed_guilds
from domain import kyosai, p2p
from domain.errors import Failure
from domain.ports.contracts import KyosaiOfferRow, KyosaiStatus
from domain.ports.repos import IGuildRepo
from services.kyosai import KyosaiService

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot


def _need_yes(circulating: int) -> int:
    """可決に要る賛成の口数の下限（流通口数 × 最低賛成率。切り上げ）。"""
    return math.ceil(circulating * kyosai.MOTION_MIN_YES_SHARE)


def status_embed(s: KyosaiStatus, offers: Sequence[KyosaiOfferRow], user_id: int) -> discord.Embed:
    share = s["units"] / s["total"] if s["total"] else 0.0
    lines = [
        f"あなたの口: **{s['units']:,}口**（全 {s['total']:,}口 の {share:.2%}）　持っている人 {s['holders']}人",
        f"次の配当の見込み: 全体で {ui.pelica(s['pool'])}"
        + (f"（<t:{int(s['next_dividend'])}:R>）" if s["next_dividend"] else "")
        + f"　これまでの配当 {ui.pelica(int(s['paid']))}　これまでの負担 {ui.pelica(int(s['charged']))}",
    ]
    a = s["auction"]
    if a is not None:
        top = f"いまの最高値 {ui.pelica(a['top_bid'])}（<@{a['top_bidder']}>）" if a["top_bidder"] else "まだ入札なし"
        lines.append(
            f"🔨 帝愛の競り: **{a['units']:,}口**　{top}　締切 <t:{int(a['ends'])}:R>（`/共済 入札`）"
            f"　帝愛の持ち口 {s['house']:,}口"
        )
    lines += [
        "口の譲渡: " + ("🔒 **停止中**（準備率が低いか、破産猶予中の人がいます）" if s["locked"] else "🟢 できます"),
        f"帝愛信託の上乗せ金利: **1日 {s['premium']:.2%}**",
    ]
    for m in s["motions"]:
        lines.append(
            f"🗳️ 動議 #{m['id']}: 上乗せ金利を **1日 {m['premium']:.2%}** にする　賛成 {m['yes']:,}口 / 反対 {m['no']:,}口"
            f"（可決には賛成 {_need_yes(s['circulating']):,}口 以上も必要。締切 <t:{int(m['closes'])}:R>）"
        )
    for o in offers:
        if o["seller"] == user_id:
            lines.append(f"📤 申し出 #{o['id']}: <@{o['buyer']}> へ {o['units']:,}口 を {ui.pelica(o['price'])} で")
        else:
            lines.append(
                f"📥 申し出 #{o['id']}: <@{o['seller']}> から {o['units']:,}口 を {ui.pelica(o['price'])} で（`/共済 受諾`）"
            )
    embed = ui.embed("🏛️ 帝愛共済", "\n".join(lines))
    embed.set_footer(
        text="共済は帝愛銀行の最後の砦です。平時は余剰から配当を受け、信託でも払えない損失が出たときは口数に応じて負担します（足りなければ借金）。"
    )
    return embed


def motion_embed(m: Mapping[str, Any]) -> discord.Embed:
    """動議の投稿（提案のときに告知チャンネルへ出す。ボタンで投票すると書き換わり、締めると結果になる）。"""
    closed = m.get("status", "open") != "open"
    lines = [
        f"<@{m['proposer']}> が、帝愛信託の上乗せ金利を **1日 {m['premium']:.2%}** にする動議を出しました。",
        f"賛成 **{m.get('yes', 0):,}口** / 反対 **{m.get('no', 0):,}口**"
        + ("（締切のときの口数）" if closed else "（締切のときの口数で数え直します）"),
        f"可決の条件: 賛成が反対より多く、かつ賛成が **{_need_yes(m.get('circulating', 0)):,}口** 以上"
        f"（流通口数の {kyosai.MOTION_MIN_YES_SHARE:.0%}）",
    ]
    if closed:
        lines.append(f"締め切りました: **{'✅ 可決' if m['status'] == 'passed' else '❌ 否決'}**")
    else:
        lines.append(
            f"締切 <t:{int(m['closes'])}:R>。口を持つ人は、下のボタンか `/共済 投票 番号:{m['id']}` で投票できます（あとから変えられます）。"
        )
    return ui.embed(f"🗳️ 帝愛共済の動議 #{m['id']}", "\n".join(lines))


class MotionButton(discord.ui.DynamicItem[discord.ui.Button], template=r"kyosai:(?P<act>yes|no):(?P<id>\d+)"):
    """動議の投稿の賛成・反対ボタン。再起動しても動くよう、動議の番号を custom_id に入れている。"""

    def __init__(self, yes: bool, motion_id: int):
        act = "yes" if yes else "no"
        super().__init__(
            discord.ui.Button(
                label="賛成" if yes else "反対",
                emoji="👍" if yes else "👎",
                style=discord.ButtonStyle.success if yes else discord.ButtonStyle.danger,
                custom_id=f"kyosai:{act}:{motion_id}",
            )
        )
        self.yes, self.motion_id = yes, motion_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match):
        return cls(match["act"] == "yes", int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        bot = cast("CasinoBot", interaction.client)
        bridge = bot.bridges.resolve_or(IKyosaiVoteBridge, NoOpKyosaiVoteBridge())
        await bridge.handle_vote(interaction, self.yes, self.motion_id)


def motion_view(motion_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(MotionButton(True, motion_id))
    view.add_item(MotionButton(False, motion_id))
    return view


class KyosaiCog(commands.Cog, IKyosaiVoteBridge):
    def __init__(self, bot: commands.Bot, config: dict[str, Any], kyosai_svc: KyosaiService, guild_repo: IGuildRepo):
        self.bot = bot
        self._config = config
        self._kyosai = kyosai_svc
        self._guild_repo = guild_repo

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(MotionButton)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        bridges = getattr(self.bot, "bridges", None)
        if isinstance(bridges, BotBridges):
            bridges.unregister(IKyosaiVoteBridge, self)

    group = app_commands.Group(
        name="共済", description="🏛️ 帝愛共済（配当と、最後の損の負担・口の競りと譲渡・動議）", guild_only=True
    )

    async def _fail(self, interaction: discord.Interaction, reason: Any) -> None:
        await ui.reply_error(interaction, format_failure(reason), title="できません")

    @group.command(name="状況", description="自分の口・配当・帝愛の競り・動議・申し出を見る")
    async def status(self, interaction: discord.Interaction):
        gid = ui.guild_id_of(interaction)
        uid = interaction.user.id
        s = self._kyosai.status(gid, uid)
        await interaction.response.send_message(
            embed=status_embed(s, self._kyosai.offers(gid, uid), uid), ephemeral=True
        )

    @group.command(name="入札", description="帝愛が競りに出している口に入札する（入札額は預かり、負けたら返す）")
    @app_commands.rename(amount="金額")
    @app_commands.describe(amount="入札額（ペリカ。いまの最高値より 5%・最低100 以上高く）")
    async def bid(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1_000_000_000]):
        ok, result = self._kyosai.bid(ui.guild_id_of(interaction), interaction.user.id, amount)
        if not ok:
            return await self._fail(interaction, result)
        assert not isinstance(result, Failure)
        await ui.reply_ok(
            interaction,
            f"共済の口 **{result['units']:,}口** に {ui.pelica(amount)} で入札しました（締切 <t:{int(result['ends'])}:R>）。\n"
            "入札額は預かります。ほかの人に上回られたら、手元に戻ります。",
            title="🔨 入札しました",
        )

    @group.command(name="譲渡", description="自分の口を、値段をつけて相手へ譲る申し出をする（相手が受けると成立）")
    @app_commands.rename(member="相手", units="口数", price="値段")
    @app_commands.describe(
        member="譲る相手",
        units="譲る口数（1以上）",
        price=f"全部でいくら（ペリカ。0なら無償。同じ相手とは1日 {p2p.PAIR_DAILY_LIMIT:,} まで）",
    )
    async def offer(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        units: app_commands.Range[int, 1, kyosai.TOTAL_UNITS],
        price: app_commands.Range[int, 0, p2p.PAIR_DAILY_LIMIT],
    ):
        if member.bot:
            return await ui.reply_error(interaction, "Bot には譲れません。", title="できません")
        ok, result = self._kyosai.offer(ui.guild_id_of(interaction), interaction.user.id, member.id, units, price)
        if not ok:
            return await self._fail(interaction, result)
        await interaction.response.send_message(
            content=member.mention,
            embed=ui.embed(
                "🏛️ 共済の口の譲渡の申し出",
                f"{interaction.user.mention} が {member.mention} に、共済の口 **{units:,}口** を **{ui.pelica(price)}** で譲ると申し出ました。\n"
                f"受けるなら `/共済 受諾 番号:{result}`（{kyosai.OFFER_HOURS}時間以内）。"
                f"成立すると、売り手の受け取りから手数料 {p2p.FEE_RATE:.0%} を引きます。",
            ),
            allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False),
        )

    @group.command(name="受諾", description="自分あての口の譲渡の申し出を受ける（代金を払って口を受け取る）")
    @app_commands.rename(offer_id="番号")
    @app_commands.describe(offer_id="申し出の番号（`/共済 状況` に出ます）")
    async def accept(self, interaction: discord.Interaction, offer_id: int):
        ok, result = self._kyosai.accept(ui.guild_id_of(interaction), interaction.user.id, offer_id)
        if not ok:
            return await self._fail(interaction, result)
        assert not isinstance(result, Failure)
        await interaction.response.send_message(
            embed=ui.ok_embed(
                "🏛️ 譲渡が成立しました",
                f"<@{result['seller']}> から {result['units']:,}口 を {ui.pelica(result['price'])} で受け取りました"
                f"（売り手の手数料 {ui.pelica(result['fee'])}）。",
            )
        )

    @group.command(name="取消", description="自分が出した（または自分あての）申し出を取り消す")
    @app_commands.rename(offer_id="番号")
    @app_commands.describe(offer_id="申し出の番号（`/共済 状況` に出ます）")
    async def cancel(self, interaction: discord.Interaction, offer_id: int):
        if not self._kyosai.cancel(ui.guild_id_of(interaction), interaction.user.id, offer_id):
            return await ui.reply_error(interaction, "その申し出は見つかりません。", title="できません")
        await ui.reply_ok(interaction, f"申し出 #{offer_id}", title="取り消しました")

    @group.command(
        name="提案", description="帝愛信託の上乗せ金利を変える動議を出す（告知チャンネルに出て、口数で決まる）"
    )
    @app_commands.rename(premium="上乗せ金利")
    @app_commands.describe(
        premium=f"1日あたりの上乗せ金利（％。0より大きく。出せるのは口を {kyosai.MOTION_MIN_UNITS:,}口 以上持つ人）"
    )
    async def propose(
        self,
        interaction: discord.Interaction,
        premium: app_commands.Range[float, 0.001, kyosai.PREMIUM_LIMIT * 100],
    ):
        gid = ui.guild_id_of(interaction)
        ok, result = self._kyosai.propose(gid, interaction.user.id, premium / 100)
        if not ok:
            return await self._fail(interaction, result)
        assert not isinstance(result, Failure)
        motion_id = result["id"]
        m = self._kyosai.motion(gid, motion_id) or result
        channel = announce_channel(interaction.guild, self._guild_repo) if interaction.guild else None
        if channel is not None and channel.id != interaction.channel_id:
            await ui.reply_ok(
                interaction, f"動議 #{motion_id} を {channel.mention} に告知しました。", title="🗳️ 動議を出しました"
            )
            try:
                msg = await ui.send_channel(channel, embed=motion_embed(m), view=motion_view(motion_id))
            except discord.HTTPException as exc:
                logthrottle.warn(("kyosai", gid), f"共済の動議を告知できません: guild={gid}: {exc!r}")
                msg = await interaction.followup.send(embed=motion_embed(m), view=motion_view(motion_id), wait=True)
        else:
            await interaction.response.send_message(embed=motion_embed(m), view=motion_view(motion_id))
            msg = await interaction.original_response()
        if msg is not None and getattr(msg, "id", None):
            self._kyosai.set_motion_message(gid, motion_id, msg.channel.id, msg.id)
            await ui.pin_message(msg, log_key=("kyosai-pin", gid), label=f"共済の動議 #{motion_id}")

    @group.command(name="投票", description="投票中の動議に、賛成か反対を入れる（あとから変えられる。口数で数える）")
    @app_commands.rename(choice="賛否", motion_id="番号")
    @app_commands.describe(choice="賛成か反対", motion_id="動議の番号（投票中が1件だけなら省略できます）")
    @app_commands.choices(
        choice=[app_commands.Choice(name="賛成", value="yes"), app_commands.Choice(name="反対", value="no")]
    )
    async def vote(
        self, interaction: discord.Interaction, choice: app_commands.Choice[str], motion_id: int | None = None
    ):
        gid = ui.guild_id_of(interaction)
        ok, result = self._kyosai.vote(gid, interaction.user.id, choice.value == "yes", motion_id)
        if not ok:
            return await self._fail(interaction, result)
        assert not isinstance(result, Failure)
        m = self._kyosai.motion(gid, result)
        if m is not None and interaction.guild is not None:
            await _edit_open_post(interaction.guild, m)  # 告知の投稿も最新の票数に書き換える（ボタンは残す）
        await ui.reply_ok(
            interaction, f"動議 #{result} に **{choice.name}**（締切のときの口数で数えます）", title="投票しました"
        )

    # ------------------------------ ボタン ------------------------------ #
    async def handle_vote(self, interaction: discord.Interaction, yes: bool, motion_id: int) -> None:
        if not await approval_guard.feature_guard(interaction, self._guild_repo, "kyosai"):
            return
        gid = ui.guild_id_of(interaction)

        ok, result = self._kyosai.vote(gid, interaction.user.id, yes, motion_id)
        if not ok:
            closed = self._kyosai.motion(gid, motion_id)
            if closed is not None and closed["status"] != "open":
                # 締めたあとのボタン（再起動の前に出た投稿など）: 投稿を結果に書き換えて、ボタンとピン留めを外す
                await interaction.response.edit_message(embed=motion_embed(closed), view=None)
                if getattr(interaction, "message", None) is not None:
                    await ui.unpin_message(
                        interaction.message, log_key=("kyosai-unpin", gid), label=f"共済の動議 #{motion_id}"
                    )
                await interaction.followup.send(f"動議 #{motion_id} は締め切られています。", ephemeral=True)
                return
            await ui.reply_error(interaction, format_failure(result), title="できません")
            return
        m = self._kyosai.motion(gid, motion_id)
        if m is not None:
            await interaction.response.edit_message(embed=motion_embed(m))
            await interaction.followup.send(
                f"動議 #{motion_id} に **{'賛成' if yes else '反対'}** を入れました。", ephemeral=True
            )
        else:
            await interaction.response.send_message(f"**{'賛成' if yes else '反対'}** を入れました。", ephemeral=True)

    # ------------------------------ 見回り（毎分） ------------------------------ #
    @tasks.loop(minutes=1)
    async def tick(self):
        await run_for_allowed_guilds(
            self.bot, self._guild_repo, self._config, self._tick_guild, error_label="帝愛共済の見回りに失敗"
        )

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _tick_guild(self, guild: discord.Guild) -> None:
        dividend, motions, auctions = self._kyosai.tick(guild.id)
        if dividend:
            _log.info(f"共済の配当: guild={guild.id} {dividend}")
        for m in motions:
            await _edit_closed_post(guild, m)
        posts = [_motion_result(m) for m in motions] + [p for p in map(_auction_post, auctions) if p is not None]
        if not posts:
            return
        channel = announce_channel(guild, self._guild_repo)
        if channel is None:
            return
        for title, text in posts:
            try:
                await ui.send_channel(channel, embed=ui.embed(title, text))
            except discord.HTTPException as exc:
                logthrottle.warn(("kyosai", guild.id), f"共済の告知を投稿できません: guild={guild.id}: {exc!r}")


def _motion_post(guild: discord.Guild, m: Mapping[str, Any]) -> Any | None:
    """動議の告知の投稿（覚えているときだけ）。"""
    channel = guild.get_channel(m.get("channel_id") or 0)
    return ui.partial_message(channel, m["message_id"]) if channel is not None and m.get("message_id") else None


async def _edit_open_post(guild: discord.Guild, m: Mapping[str, Any]) -> None:
    """投票中の動議の告知の投稿を、最新の票数の Embed だけで書き換える（ボタンは残す。投稿を覚えていないときは何もしない）。"""
    msg = _motion_post(guild, m)
    if msg is None:
        return
    try:
        await msg.edit(embed=motion_embed(m))
    except discord.HTTPException as exc:
        logthrottle.warn(("kyosai", m["id"]), f"共済の動議の投稿を直せません: motion={m['id']}: {exc!r}")


async def _edit_closed_post(guild: discord.Guild, m: Mapping[str, Any]) -> None:
    """締めた動議の告知の投稿を、結果に書き換えてボタンとピン留めを外す（投稿を覚えていないときは何もしない）。"""
    msg = _motion_post(guild, m)
    if msg is None:
        return
    try:
        await msg.edit(embed=motion_embed(m), view=None)
    except discord.HTTPException as exc:
        logthrottle.warn(("kyosai", m["id"]), f"共済の動議の投稿を直せません: motion={m['id']}: {exc!r}")
    await ui.unpin_message(msg, log_key=("kyosai-unpin", guild.id), label=f"共済の動議 #{m['id']}")


def _motion_result(motion: Mapping[str, Any]) -> tuple[str, str]:

    verdict = "✅ 可決" if motion["status"] == "passed" else "❌ 否決"
    text = (
        f"動議 #{motion['id']}（帝愛信託の上乗せ金利を 1日 {motion['premium']:.2%} にする）は **{verdict}** されました。"
        f"\n賛成 {motion['yes']:,}口 / 反対 {motion['no']:,}口（可決には賛成 {_need_yes(motion['circulating']):,}口 以上も必要）"
    )
    return "🗳️ 帝愛共済の動議の結果", text


def _auction_post(event: Mapping[str, Any]) -> tuple[str, str] | None:
    if event["event"] == "sold":
        return (
            "🔨 帝愛共済の競りの結果",
            f"共済の口 **{event['units']:,}口** を <@{event['top_bidder']}> が {ui.pelica(event['top_bid'])} で落札しました。",
        )
    if event["event"] == "listed":
        return (
            "🔨 帝愛共済の口の競り",
            f"帝愛が回収した共済の口 **{event['units']:,}口** を競りに出しました（締切 <t:{int(event['ends'])}:R>）。\n"
            f"`/共済 入札` で入札できます（借金がある人は入札できません）。",
        )
    return None


async def setup(bot: "CasinoBot") -> None:
    cog = KyosaiCog(bot, bot.config, bot.services.kyosai, bot.repos.guild)
    await bot.add_cog(cog)
    bot.bridges.register(IKyosaiVoteBridge, cog)
