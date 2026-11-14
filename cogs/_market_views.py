"""`/証券` の保有・市況・オプション画面（View / Modal）。"""

import time

import discord

from core import market, market_events, ui
from core import options as opt


class HoldingsView(ui.OwnerView):
    """/証券 保有 の、ボタンで決済する画面（開いた本人だけが操作できる）。"""

    def __init__(self, cog, user_id: int, guild_id: int):
        super().__init__(user_id, timeout=300, owner_message="自分の `/証券 保有` で操作してください。")
        self.cog, self.user_id = cog, user_id
        for code, label in cog.positions(user_id, guild_id)[:20]:
            button = discord.ui.Button(label=label[:80], style=discord.ButtonStyle.secondary)
            button.callback = self._make(code)
            self.add_item(button)

    def _make(self, code: str):
        async def callback(interaction: discord.Interaction):
            await self.cog.settle_position(interaction, code)

        return callback


class OptionAmountModal(discord.ui.Modal):
    """イベントのボタンから、コール・プットの掛け金を入れる画面。"""

    def __init__(self, cog, kind: str, symbol: str):
        name = opt.KINDS[kind][0]
        super().__init__(title=f"{market.BY_KEY[symbol].name}の{name}")
        self.cog, self.kind, self.symbol = cog, kind, symbol
        self.amount = discord.ui.TextInput(
            label=f"掛け金（{opt.MIN_PREMIUM:,}ペリカ以上）", default="1000", max_length=9
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.amount.value.replace(",", "").strip()
        if not raw.isdigit() or int(raw) < opt.MIN_PREMIUM:
            await interaction.response.send_message(
                embed=ui.error_embed(
                    "掛け金が正しくありません", f"{opt.MIN_PREMIUM:,}ペリカ以上の数字で入れてください。"
                ),
                ephemeral=True,
            )
            return
        await self.cog._option_buy(interaction, self.kind, self.symbol, int(raw))


class MarketView(ui.OwnerView):
    """/証券 市況 の、銘柄と期間のチャートを切り替える画面（自動更新はしない。開いた本人だけが操作できる）。"""

    def __init__(self, cog, user_id: int, guild_id: int, period: str = "24h"):
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
        now = time.time()
        ev = market_events.current(cog.bot.economy.market_events(now), now)
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

    def _quick_buy(self, kind: str, symbol: str):
        async def callback(interaction: discord.Interaction):
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
