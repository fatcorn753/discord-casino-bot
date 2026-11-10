"""/取引 — プレイヤー間の市場。競走馬と、消耗品・見た目の品を、定価で売ったり、競りに出したりする。

- 出品: `/取引 出品`（品・馬を選び、値段・方式を決める）。出した品・馬は、出品の間、預かる。
- 買う・入札: `/取引 一覧` から、出品を選んで、「買う」「入札する」。入札のお金は、残高から預かる（抜かれたら返る）。
- 売れると、売り手の受け取りから 5% を引く（帝愛銀行の収入）。同じ2人の取引は、24時間で合計 10万ペリカまで。
- 締切は、Bot が定期的に確認して精算する（競りの落札は、告知チャンネルに知らせる）。
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs._horse_views import stat_lines
from cogs.weekly_cog import announce_channel
from core import approval, logthrottle, p2p, ui
from core import shop as shop_mod
from core.confirm import confirm_action

CONFIRM_FROM = 10_000  # この金額以上の購入・入札は、確認ボタンを挟む
SHOWN = 25
NONE = discord.AllowedMentions.none()


def when(ts: float) -> str:
    return f"<t:{int(ts)}:R>（<t:{int(ts)}:t>）"


def price_text(r: dict) -> str:
    if r["mode"] == "fixed":
        return f"定価 **{ui.pelica(r['price'])}**"
    now = f"現在 **{ui.pelica(r['bid'])}**" if r["bid"] else f"開始 **{ui.pelica(r['price'])}**"
    return f"競り {now}"


def line_of(eco, r: dict) -> str:
    return f"`#{r['id']}` {eco.p2p_label(r)}　{price_text(r)}　{'締切' if r['mode'] == 'auction' else '期限'} {when(r['end_at'])}"


def detail_embed(eco, r: dict) -> discord.Embed:
    lines = [
        f"出品者: <@{r['seller_id']}>",
        price_text(r),
        f"{'締切' if r['mode'] == 'auction' else '期限'}: {when(r['end_at'])}",
    ]
    if r["mode"] == "auction":
        need = p2p.min_next_bid(r["price"], r["bid"])
        lines.append(
            f"最高入札: {'<@' + str(r['bidder_id']) + '>' if r['bidder_id'] else 'まだありません'}　次の入札は **{ui.pelica(need)}** 以上"
        )
        lines.append(
            f"（締切の{p2p.SNIPE_SECONDS // 60}分前を切ってからの入札は、締切が{p2p.SNIPE_SECONDS // 60}分延びます）"
        )
    if r["kind"] == "horse":
        horse = eco.horse_get(int(r["ref"]))
        if horse:
            lines += [
                "",
                stat_lines(horse),
                f"戦績 {horse['races']}戦{horse['wins']}勝（3着以内 {horse['places']}）　賞金 {ui.pelica(horse['earnings'])}",
            ]
    else:
        item = shop_mod.BY_ID.get(r["ref"])
        if item and item.desc:
            lines += ["", item.desc]
    lines.append(f"\n売れると、売り手の受け取りから {p2p.FEE_RATE:.0%} を引きます。")
    return ui.embed(f"🤝 {eco.p2p_label(r)}", "\n".join(lines), ui.INFO)


class BidModal(discord.ui.Modal):
    def __init__(self, view: "DetailView", need: int):
        super().__init__(title="入札")
        self.view = view
        self.amount = discord.ui.TextInput(label=f"入札額（{need:,}ペリカ以上）", max_length=9, default=str(need))
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.amount.value.replace(",", "").strip())
        except ValueError:
            return await interaction.response.send_message(
                embed=ui.error_embed("入札できません", "数字で入れてください。"), ephemeral=True
            )
        await self.view.cog.do_bid(interaction, self.view.listing_id, amount)


class DetailView(discord.ui.View):
    """出品の詳細。「買う」（定価）か「入札する」（競り）。"""

    def __init__(self, cog: "P2PCog", user_id: int, listing_id: int, mode: str):
        super().__init__(timeout=300)
        self.cog, self.user_id, self.listing_id = cog, user_id, listing_id
        label = "🛒 買う" if mode == "fixed" else "🔨 入札する"
        go = discord.ui.Button(label=label, style=discord.ButtonStyle.success)
        go.callback = self._on_go
        self.add_item(go)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def _on_go(self, interaction: discord.Interaction):
        eco = self.cog.eco_of(interaction)
        row = eco.p2p_get(self.listing_id)
        if row is None or row["status"] != "active":
            return await interaction.response.send_message(
                embed=ui.error_embed("もうありません", "その出品は、終わりました。"), ephemeral=True
            )
        if row["mode"] == "fixed":
            return await self.cog.do_buy(interaction, self.listing_id)
        await interaction.response.send_modal(BidModal(self, p2p.min_next_bid(row["price"], row["bid"])))


class ListView(discord.ui.View):
    """出品の一覧。選ぶと、詳細が出る。"""

    def __init__(self, cog: "P2PCog", user_id: int, guild_id: int, kind=None):
        super().__init__(timeout=300)
        self.cog, self.user_id, self.guild_id, self.kind = cog, user_id, guild_id, kind
        self.rows = cog.bot.economy.for_guild(guild_id).p2p_active(kind, limit=SHOWN)
        eco = cog.bot.economy.for_guild(guild_id)
        if self.rows:
            select = discord.ui.Select(
                placeholder="見る出品を選ぶ",
                options=[
                    discord.SelectOption(
                        label=eco.p2p_label(r)[:80],
                        value=str(r["id"]),
                        description=(
                            f"定価 {r['price']:,}" if r["mode"] == "fixed" else f"競り {(r['bid'] or r['price']):,}"
                        )
                        + "ペリカ",
                    )
                    for r in self.rows
                ],
            )
            select.callback = self._on_pick
            self.add_item(select)

    def embed(self) -> discord.Embed:
        eco = self.cog.bot.economy.for_guild(self.guild_id)
        title = "🤝 取引の一覧" + {"horse": "（競走馬）", "item": "（品物）"}.get(self.kind, "")
        body = (
            "\n".join(line_of(eco, r) for r in self.rows)
            or "いま、出ているものはありません。`/取引 出品` で、自分の品や馬を出せます。"
        )
        return ui.embed(title, body, ui.INFO)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def _on_pick(self, interaction: discord.Interaction):
        eco = self.cog.eco_of(interaction)
        row = eco.p2p_get(int(interaction.data["values"][0]))
        if row is None or row["status"] != "active":
            return await interaction.response.send_message(
                embed=ui.error_embed("もうありません", "その出品は、終わりました。"), ephemeral=True
            )
        view = DetailView(self.cog, self.user_id, row["id"], row["mode"])
        if row["seller_id"] == self.user_id:
            view.clear_items()  # 自分の出品は、買えない
        await interaction.response.send_message(embed=detail_embed(eco, row), view=view, ephemeral=True)


class P2PCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.tick.start()

    def cog_unload(self):
        self.tick.cancel()

    def eco_of(self, interaction: discord.Interaction):
        return self.bot.economy.for_guild(interaction.guild_id)

    group = app_commands.Group(
        name="取引", description="🤝 プレイヤー間の市場（競走馬・品物を、定価で売る／競りに出す）", guild_only=True
    )

    # ------------------------------ 買う・入札 ------------------------------ #
    async def do_buy(self, interaction: discord.Interaction, listing_id: int) -> None:
        uid = interaction.user.id
        eco = self.eco_of(interaction)
        row = eco.p2p_get(listing_id)
        if row is None or row["status"] != "active":
            return await interaction.response.send_message(
                embed=ui.error_embed("買えません", "その出品は、もうありません。"), ephemeral=True
            )
        reason = self.bot.sessions.try_start(uid)
        if reason:
            return await interaction.response.send_message(
                embed=ui.error_embed("いまは買えません", reason), ephemeral=True
            )
        try:
            reply = interaction.response.send_message
            if row["price"] >= CONFIRM_FROM:
                ok = await confirm_action(
                    interaction,
                    "購入しますか？",
                    f"**{eco.p2p_label(row)}**\n{ui.pelica(row['price'])} かかります。",
                    "買う",
                )
                if ok is not True:
                    return
                reply = interaction.followup.send
            done, result = eco.p2p_buy(uid, listing_id)
            if not done:
                return await reply(embed=ui.error_embed("買えませんでした", result), ephemeral=True)
            await reply(
                embed=ui.ok_embed(
                    "購入しました",
                    f"**{eco.p2p_label(row)}** を {ui.pelica(result)} で買いました。\n残高: **{ui.pelica(eco.balance(uid))}**",
                ),
                ephemeral=True,
            )
        finally:
            self.bot.sessions.end(uid)

    async def do_bid(self, interaction: discord.Interaction, listing_id: int, amount: int) -> None:
        uid = interaction.user.id
        eco = self.eco_of(interaction)
        row = eco.p2p_get(listing_id)
        if row is None or row["status"] != "active":
            return await interaction.response.send_message(
                embed=ui.error_embed("入札できません", "その出品は、もうありません。"), ephemeral=True
            )
        reason = self.bot.sessions.try_start(uid)
        if reason:
            return await interaction.response.send_message(
                embed=ui.error_embed("いまは入札できません", reason), ephemeral=True
            )
        try:
            reply = interaction.response.send_message
            if amount >= CONFIRM_FROM:
                ok = await confirm_action(
                    interaction,
                    "入札しますか？",
                    f"**{eco.p2p_label(row)}**\n{ui.pelica(amount)} を、残高から預かります（抜かれたら返ります）。",
                    "入札する",
                )
                if ok is not True:
                    return
                reply = interaction.followup.send
            done, result = eco.p2p_bid(uid, listing_id, amount)
            if not done:
                return await reply(embed=ui.error_embed("入札できませんでした", result), ephemeral=True)
            await reply(
                embed=ui.ok_embed(
                    "入札しました",
                    f"**{eco.p2p_label(row)}** に **{ui.pelica(amount)}** で入札しました。締切 {when(result)}\n残高: **{ui.pelica(eco.balance(uid))}**",
                ),
                ephemeral=True,
            )
        finally:
            self.bot.sessions.end(uid)

    # ------------------------------ コマンド ------------------------------ #
    @group.command(name="一覧", description="出ている品・馬を見る（選ぶと、買う・入札できる）")
    @app_commands.rename(kind="種類")
    @app_commands.choices(
        kind=[app_commands.Choice(name="競走馬", value="horse"), app_commands.Choice(name="品物", value="item")]
    )
    async def listing(self, interaction: discord.Interaction, kind: app_commands.Choice[str] = None):
        view = ListView(self, interaction.user.id, interaction.guild_id, kind.value if kind else None)
        kwargs = {"view": view} if view.rows else {}  # 出品がなければ、選ぶ部品は出さない（view=None は渡さない）
        await interaction.response.send_message(embed=view.embed(), ephemeral=True, **kwargs)

    async def _sell_autocomplete(self, interaction: discord.Interaction, current: str):
        eco = self.eco_of(interaction)
        uid = interaction.user.id
        out = []
        for item_id, qty in eco.inventory(uid).items():
            if p2p.tradable(item_id):
                name = f"{shop_mod.label(shop_mod.BY_ID[item_id])}（持っている {qty}）"
                out.append(app_commands.Choice(name=name[:100], value=f"item:{item_id}"))
        for horse in eco.horse_list(uid):
            out.append(app_commands.Choice(name=f"🐴 {horse['name']}（馬）"[:100], value=f"horse:{horse['id']}"))
        return [c for c in out if current in c.name][:25]

    @group.command(name="出品", description="自分の品・馬を、定価で売る／競りに出す（出した間は、預かります）")
    @app_commands.rename(target="品", mode="方式", price="値段", qty="個数", hours="競りの長さ")
    @app_commands.describe(
        target="出す品・馬（候補から選ぶ）",
        mode="定価（すぐ買える）か、競り（入札で上がる）",
        price="定価、または競りの開始価格（まとめて1つの値段）",
        qty="消耗品の個数（省略で1）",
        hours="競りの長さ（競りのとき。省略で24時間）",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="定価（すぐ買える）", value="fixed"),
            app_commands.Choice(name="競り（入札で上がる）", value="auction"),
        ],
        hours=[app_commands.Choice(name=f"{h}時間", value=h) for h in p2p.AUCTION_HOURS],
    )
    async def sell(
        self,
        interaction: discord.Interaction,
        target: str,
        mode: app_commands.Choice[str],
        price: app_commands.Range[int, 1, 10_000_000],
        qty: app_commands.Range[int, 1, 99] = 1,
        hours: app_commands.Choice[int] = None,
    ):
        kind, _, ref = target.partition(":")
        if kind not in ("item", "horse") or not ref:
            return await interaction.response.send_message(
                embed=ui.error_embed("出品できません", "「品」は、候補から選んでください。"), ephemeral=True
            )
        eco = self.eco_of(interaction)
        ok, result = eco.p2p_list(
            interaction.user.id,
            kind,
            ref,
            qty,
            mode.value,
            price,
            hours.value if hours else 24 if mode.value == "auction" else None,
        )
        if not ok:
            return await interaction.response.send_message(
                embed=ui.error_embed("出品できません", result), ephemeral=True
            )
        row = eco.p2p_get(result)
        await interaction.response.send_message(
            embed=ui.ok_embed(
                "出品しました",
                f"{line_of(eco, row)}\n売れる（落札される）と、{p2p.FEE_RATE:.0%} の手数料を引いた額が入ります。取り消しは `/取引 取消`。",
            ),
            ephemeral=True,
        )

    sell.autocomplete("target")(_sell_autocomplete)

    @group.command(name="自分", description="自分の出品と、入札中の競りを見る")
    async def mine(self, interaction: discord.Interaction):
        eco = self.eco_of(interaction)
        data = eco.p2p_mine(interaction.user.id)
        sell = "\n".join(line_of(eco, r) for r in data["selling"]) or "出していません。"
        bid = "\n".join(line_of(eco, r) for r in data["bidding"]) or "入札中のものは、ありません。"
        e = ui.embed("🤝 自分の取引", "", ui.INFO)
        e.add_field(name="出している", value=sell[:1024], inline=False)
        e.add_field(name="入札中（あなたが最高入札者）", value=bid[:1024], inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    async def _cancel_autocomplete(self, interaction: discord.Interaction, current: str):
        eco = self.eco_of(interaction)
        rows = eco.p2p_mine(interaction.user.id)["selling"]
        return [
            app_commands.Choice(name=f"#{r['id']} {eco.p2p_label(r)}"[:100], value=r["id"])
            for r in rows
            if current in f"#{r['id']} {eco.p2p_label(r)}"
        ][:25]

    @group.command(
        name="取消", description="自分の出品を取り消して、品・馬を戻す（入札が入った競りは、取り消せません）"
    )
    @app_commands.rename(listing_id="出品")
    async def cancel(self, interaction: discord.Interaction, listing_id: int):
        ok, why = self.eco_of(interaction).p2p_cancel(interaction.user.id, listing_id)
        if not ok:
            return await interaction.response.send_message(embed=ui.error_embed("取り消せません", why), ephemeral=True)
        await interaction.response.send_message(
            embed=ui.ok_embed("取り消しました", "出品を取り消して、品・馬を戻しました。"), ephemeral=True
        )

    cancel.autocomplete("listing_id")(_cancel_autocomplete)

    # ------------------------------ 締切の精算 ------------------------------ #
    @tasks.loop(seconds=30)
    async def tick(self):
        for guild in list(self.bot.guilds):
            if not approval.is_allowed(self.bot.economy, self.bot.config, guild.id, 0):
                continue
            try:
                await self._settle_guild(guild)
            except Exception as exc:  # 1サーバーの失敗で、ほかを止めない
                print(f"取引の精算に失敗: guild={guild.id}: {exc!r}")

    @tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()

    async def _settle_guild(self, guild: discord.Guild) -> None:
        eco = self.bot.economy.for_guild(guild.id)
        results = eco.p2p_settle()
        channel = (
            announce_channel(guild, eco)
            if any(res == "sold" and r["mode"] == "auction" for r, res in results)
            else None
        )
        for row, res in results:
            if res != "sold" or row["mode"] != "auction" or channel is None:
                continue  # 落札だけ、告知チャンネルに知らせる
            done = eco.p2p_get(row["id"])
            try:
                await channel.send(
                    embed=ui.embed(
                        "🔨 競りの落札",
                        f"**{eco.p2p_label(row)}** を、<@{done['buyer_id']}> が **{ui.pelica(done['sold_price'])}** で落札しました（出品者 <@{row['seller_id']}>）。",
                        ui.GOLD,
                    ),
                    allowed_mentions=NONE,
                )
            except discord.HTTPException as exc:
                logthrottle.warn(("p2p-post", guild.id), f"落札の告知に失敗: guild={guild.id}: {exc!r}")


async def setup(bot):
    await bot.add_cog(P2PCog(bot))
