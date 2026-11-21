"""`/証券` の保有・市況・オプション画面（View / Modal）。"""

from typing import TYPE_CHECKING, Optional

import discord

from cogs.market import orders
from cogs.shared import approval_guard, ui
from cogs.shared.amount import typed_amount
from domain import market, market_events
from domain import options as opt

if TYPE_CHECKING:
    from cogs.market.cog import MarketCog


PAGE_SIZE = 25  # Select 1つに載せられる選択肢の上限（Discord の制限）
PANEL_TIMEOUT = 600


class PercentModal(discord.ui.Modal):
    """保有パネルの「カスタム%」で、手放す割合を入れる画面。"""

    def __init__(self, panel: "HoldingsView", code: str):
        super().__init__(title="手放す割合")
        self.panel, self.code = panel, code
        self.percent = discord.ui.TextInput(label="割合（1〜100の数字。100で全部）", default="50", max_length=3)
        self.add_item(self.percent)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        pct = typed_amount(self.percent.value)
        if pct is None or not 1 <= pct <= 100:
            await ui.reply_error(interaction, "1〜100の数字で入れてください。", title="割合が正しくありません")
            return
        await self.panel.settle(interaction, self.code, pct)


class HoldingsView(ui.OwnerView):
    """/証券 保有 のパネル（開いた本人だけが操作できる）。

    持ち物を Select で選び、割合のボタンで決済する。25件を超えたらページを切り替える。
    決済のあとは、元のパネル（origin の応答）を最新の持ち物で描き直す。
    """

    def __init__(
        self, cog: "MarketCog", user_id: int, guild_id: int, origin: Optional[discord.Interaction] = None
    ) -> None:
        super().__init__(user_id, timeout=PANEL_TIMEOUT, owner_message="自分の `/証券 保有` で操作してください。")
        self.cog, self.user_id, self.guild_id, self.origin = cog, user_id, guild_id, origin
        self.page, self.selected = 0, None
        self._build()

    def embed(self, note: str = "") -> discord.Embed:
        embed = self.cog.holdings_embed(self.user_id, self.guild_id)
        if note:
            embed.description = f"{embed.description}\n{note}"
        return embed

    def _build(self) -> None:
        self.clear_items()
        found = self.cog.positions(self.user_id, self.guild_id)
        codes = [code for code, _ in found]
        if self.selected not in codes:
            self.selected = None
        pages = max(1, -(-len(found) // PAGE_SIZE))
        self.page = min(self.page, pages - 1)
        shown = found[self.page * PAGE_SIZE : (self.page + 1) * PAGE_SIZE]
        if shown:
            select = discord.ui.Select(
                placeholder="決済する持ち物を選ぶ" + (f"（{self.page + 1}/{pages}ページ）" if pages > 1 else ""),
                options=[
                    discord.SelectOption(label=label[:100], value=code, default=code == self.selected)
                    for code, label in shown
                ],
                row=0,
            )
            select.callback = self._on_select
            self.add_item(select)
            # 一部だけ決済できるのは、現物と信用取引だけ（債券・オプションは全部だけ）
            partial = self.selected is not None and self.selected.split(":")[0] in ("spot", "short", "long")
            self._button(
                "全部決済",
                self._settle_button(100),
                row=1,
                style=discord.ButtonStyle.danger,
                disabled=self.selected is None,
            )
            for pct in (50, 25):
                self._button(f"{pct}%", self._settle_button(pct), row=1, disabled=not partial)
            self._button("カスタム%", self._on_custom, row=1, disabled=not partial)
        if pages > 1:
            self._button("◀ 前", self._turn(-1), row=2, disabled=self.page == 0)
            self._button("次 ▶", self._turn(1), row=2, disabled=self.page >= pages - 1)
        self._button("🔄 更新", self._on_refresh, row=2)
        self._button("📈 市況を見る", self._on_market, row=2)

    def _button(self, label, callback, *, row: int, style=discord.ButtonStyle.secondary, disabled: bool = False):
        button = discord.ui.Button(label=label, style=style, row=row, disabled=disabled)
        button.callback = callback
        self.add_item(button)
        return button

    async def redraw(self, interaction: discord.Interaction, note: str = "") -> None:
        self._build()
        await interaction.response.edit_message(embed=self.embed(note), view=self)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        values = ui.selected_values(interaction)
        self.selected = values[0] if values else None
        await self.redraw(interaction)

    def _turn(self, step: int):
        async def callback(interaction: discord.Interaction) -> None:
            self.page += step
            await self.redraw(interaction)

        return callback

    def _settle_button(self, pct: int):
        async def callback(interaction: discord.Interaction) -> None:
            if self.selected is None:
                return await self.redraw(interaction, "決済する持ち物を選んでください。")
            await self.settle(interaction, self.selected, pct)

        return callback

    async def _on_custom(self, interaction: discord.Interaction) -> None:
        if self.selected is None:
            return await self.redraw(interaction, "決済する持ち物を選んでください。")
        await interaction.response.send_modal(PercentModal(self, self.selected))

    async def _on_refresh(self, interaction: discord.Interaction) -> None:
        await self.redraw(interaction)

    async def _on_market(self, interaction: discord.Interaction) -> None:
        await self.cog.open_market(interaction)

    async def settle(self, interaction: discord.Interaction, code: str, pct: int) -> None:
        """決済は別のメッセージで答え、そのあと元のパネルを描き直す（古い評価額を残さない）。"""
        await self.cog.settle_position(interaction, code, pct)
        await self.refresh_origin()

    async def refresh_origin(self) -> None:
        if self.origin is None:
            return
        self._build()
        try:
            await self.origin.edit_original_response(embed=self.embed(), view=self)
        except discord.HTTPException:
            pass  # パネルが消された・時間切れでも、決済そのものは済んでいる


class OptionAmountModal(discord.ui.Modal):
    """イベントのボタンから、コール・プットの掛け金を入れる画面。"""

    def __init__(self, cog: "MarketCog", kind: str, symbol: str):
        name = opt.KINDS[kind][0]
        super().__init__(title=f"{market.BY_KEY[symbol].name}の{name}")
        self.cog, self.kind, self.symbol = cog, kind, symbol
        self.amount = discord.ui.TextInput(
            label=f"掛け金（{opt.MIN_PREMIUM:,}ペリカ以上）", default="1000", max_length=9
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await approval_guard.client_feature_guard(interaction, "market"):
            return
        premium = typed_amount(self.amount.value)
        if premium is None or premium < opt.MIN_PREMIUM:
            await ui.reply_error(
                interaction, f"{opt.MIN_PREMIUM:,}ペリカ以上の数字で入れてください。", title="掛け金が正しくありません"
            )
            return
        await orders.option_buy(self.cog, interaction, self.kind, self.symbol, premium)


class MarketView(ui.OwnerView):
    """/証券 市況 の、銘柄と期間のチャートを切り替える画面（自動更新はしない。開いた本人だけが操作できる）。"""

    def __init__(self, cog: "MarketCog", user_id: int, guild_id: int, period: str = "24h"):
        super().__init__(user_id, timeout=300, owner_message="自分の `/証券 市況` で操作してください。")
        self.cog, self.user_id, self.guild_id = cog, user_id, guild_id
        self.symbol, self.period = None, period
        self.symbol_select = discord.ui.Select(
            placeholder="銘柄を選ぶ",
            options=[discord.SelectOption(label="📋 全銘柄の一覧", value="all")]
            + [discord.SelectOption(label=f"{s.emoji} {s.name}", value=s.key) for s in market.SYMBOLS],
        )
        self.period_select = discord.ui.Select(
            placeholder="期間を選ぶ",
            options=[discord.SelectOption(label=label, value=key) for key, (label, _) in market.PERIODS.items()],
        )
        self.symbol_select.callback = self._on_symbol
        self.period_select.callback = self._on_period
        self.add_item(self.symbol_select)
        self.add_item(self.period_select)
        now = cog._market_svc.clock.now_ts()
        ev = market_events.current(cog._market_svc.market_events(now), now)
        if ev is not None:  # イベントがあるとき、その銘柄のコール・プットを、すぐ買えるボタンを出す
            for kind in (opt.CALL, opt.PUT):
                name, emoji, _ = opt.KINDS[kind]
                button = discord.ui.Button(
                    label=f"⚡ {market.BY_KEY[ev['symbol']].name}の{name}を買う",
                    style=discord.ButtonStyle.danger if kind == opt.CALL else discord.ButtonStyle.primary,
                    emoji=emoji,
                )
                button.callback = self._quick_buy(kind, ev["symbol"])
                self.add_item(button)
        holdings = discord.ui.Button(label="💼 保有を見る", style=discord.ButtonStyle.secondary)
        holdings.callback = self._on_holdings
        self.add_item(holdings)

    async def _on_holdings(self, interaction: discord.Interaction) -> None:
        await self.cog.open_holdings(interaction)

    def _quick_buy(self, kind: str, symbol: str):
        async def callback(interaction: discord.Interaction):
            if not await approval_guard.client_feature_guard(interaction, "market"):
                return
            await interaction.response.send_modal(OptionAmountModal(self.cog, kind, symbol))

        return callback

    def _embed(self) -> discord.Embed:
        if self.symbol is None:
            return self.cog.summary_embed(self.guild_id)
        return self.cog.chart_embed(self.period, guild_id=self.guild_id, symbol=self.symbol)

    async def _on_symbol(self, interaction: discord.Interaction):
        value = self.symbol_select.values[0]
        self.symbol = None if value == "all" else value
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _on_period(self, interaction: discord.Interaction):
        self.period = self.period_select.values[0]
        await interaction.response.edit_message(embed=self._embed(), view=self)
