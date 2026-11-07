"""/カジノ — 遊べるゲームの入口。"""

import discord
from discord import app_commands
from discord.ext import commands

from cogs._amount import balance_choices
from games import pachinko as pachinko_game
from play.blackjack import play_blackjack
from play.chinchiro import play_chinchiro
from play.common import MIN_BET
from play.pachinko import play_pachinko
from play.simple import play_roulette, play_slot

BET_HELP = f"賭けるペリカ（{MIN_BET:,}以上。上限なし）"


class Casino(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    casino = app_commands.Group(name="カジノ", description="🎰 ペリカを賭けて遊ぶ（遊びのゲーム用の仮想通貨です）")

    async def _bet_autocomplete(self, interaction: discord.Interaction, current: str):
        """賭け金の欄に、残高を見せる。"""
        return balance_choices(self.bot, interaction, current, MIN_BET)

    @casino.command(name="チンチロ", description="🎲 チンチロ。親と勝負、目が出なければ3回まで振れる")
    @app_commands.rename(bet="賭け金")
    @app_commands.describe(bet=BET_HELP)
    @app_commands.autocomplete(bet=_bet_autocomplete)
    async def chinchiro(self, interaction: discord.Interaction, bet: app_commands.Range[int, MIN_BET]):
        await play_chinchiro(interaction, self.bot, bet)

    @casino.command(name="パチンコ", description="🎰 玉を打ち出して、入った数だけ回転。当たれば連チャン")
    @app_commands.rename(bet="賭け金", sets="回数")
    @app_commands.describe(
        bet="1セット（100発）の賭け金",
        sets=f"連続で打つセット数（1〜{pachinko_game.MAX_SETS}。省略で1）。賭け金×回数が上限以内",
    )
    @app_commands.autocomplete(bet=_bet_autocomplete)
    async def pachinko(
        self,
        interaction: discord.Interaction,
        bet: app_commands.Range[int, MIN_BET],
        sets: app_commands.Range[int, 1, pachinko_game.MAX_SETS] = 1,
    ):
        await play_pachinko(interaction, self.bot, bet, sets)

    @casino.command(name="スロット", description="🍒 3つのリール。揃えば大きく増える")
    @app_commands.rename(bet="賭け金")
    @app_commands.describe(bet=BET_HELP)
    @app_commands.autocomplete(bet=_bet_autocomplete)
    async def slot(self, interaction: discord.Interaction, bet: app_commands.Range[int, MIN_BET]):
        await play_slot(interaction, self.bot, bet)

    @casino.command(name="ブラックジャック", description="🃏 21に近づけてディーラーに勝つ（ヒット・スタンド・ダブル）")
    @app_commands.rename(bet="賭け金")
    @app_commands.describe(bet=BET_HELP)
    @app_commands.autocomplete(bet=_bet_autocomplete)
    async def blackjack(self, interaction: discord.Interaction, bet: app_commands.Range[int, MIN_BET]):
        await play_blackjack(interaction, self.bot, bet)

    @casino.command(name="ルーレット", description="🎡 色・偶奇・範囲・数字に賭ける")
    @app_commands.rename(bet="賭け金", kind="賭け方", number="数字")
    @app_commands.describe(
        bet=BET_HELP,
        kind="赤黒・偶奇などは2倍、範囲(12個)は3倍、数字ぴったりは36倍",
        number="「ストレート（数字）」のときだけ 0〜36 を指定",
    )
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="赤（2倍）", value="red"),
            app_commands.Choice(name="黒（2倍）", value="black"),
            app_commands.Choice(name="偶数（2倍）", value="even"),
            app_commands.Choice(name="奇数（2倍）", value="odd"),
            app_commands.Choice(name="1〜18（2倍）", value="low"),
            app_commands.Choice(name="19〜36（2倍）", value="high"),
            app_commands.Choice(name="1〜12（3倍）", value="dozen1"),
            app_commands.Choice(name="13〜24（3倍）", value="dozen2"),
            app_commands.Choice(name="25〜36（3倍）", value="dozen3"),
            app_commands.Choice(name="ストレート（数字・36倍）", value="number"),
        ]
    )
    @app_commands.autocomplete(bet=_bet_autocomplete)
    async def roulette(
        self,
        interaction: discord.Interaction,
        bet: app_commands.Range[int, MIN_BET],
        kind: app_commands.Choice[str],
        number: app_commands.Range[int, 0, 36] = None,
    ):
        await play_roulette(interaction, self.bot, bet, kind.value, number)


async def setup(bot):
    await bot.add_cog(Casino(bot))
