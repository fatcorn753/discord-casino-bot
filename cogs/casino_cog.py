"""/カジノ — 遊べるゲームの入口。"""

import discord
from discord import app_commands
from discord.ext import commands

from play.blackjack import play_blackjack
from play.chinchiro import play_chinchiro
from play.common import MAX_BET, MIN_BET
from play.pachinko import play_pachinko
from play.simple import play_coin, play_roulette, play_slot

BET_HELP = f"賭けるペリカ（{MIN_BET:,}〜{MAX_BET:,}）"


class Casino(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    casino = app_commands.Group(name="カジノ", description="🎰 ペリカを賭けて遊ぶ（遊びのゲーム用の仮想通貨です）")

    @casino.command(name="チンチロ", description="🎲 地下チンチロ。親と勝負、目が出なければ3回まで振れる")
    @app_commands.rename(bet="賭け金")
    @app_commands.describe(bet=BET_HELP)
    async def chinchiro(self, interaction: discord.Interaction, bet: app_commands.Range[int, MIN_BET, MAX_BET]):
        await play_chinchiro(interaction, self.bot, bet)

    @casino.command(name="パチンコ", description="🎰 地下パチンコ。玉が落ちて、リールが回る演出つき")
    @app_commands.rename(bet="賭け金")
    @app_commands.describe(bet=BET_HELP)
    async def pachinko(self, interaction: discord.Interaction, bet: app_commands.Range[int, MIN_BET, MAX_BET]):
        await play_pachinko(interaction, self.bot, bet)

    @casino.command(name="スロット", description="🍒 3つのリール。揃えば大きく増える")
    @app_commands.rename(bet="賭け金")
    @app_commands.describe(bet=BET_HELP)
    async def slot(self, interaction: discord.Interaction, bet: app_commands.Range[int, MIN_BET, MAX_BET]):
        await play_slot(interaction, self.bot, bet)

    @casino.command(name="コイン", description="🪙 コイン投げ。表か裏を当てる（当たりで1.95倍）")
    @app_commands.rename(bet="賭け金", side="予想")
    @app_commands.describe(bet=BET_HELP, side="表か裏か")
    @app_commands.choices(side=[app_commands.Choice(name="表", value="表"), app_commands.Choice(name="裏", value="裏")])
    async def coin(self, interaction: discord.Interaction, bet: app_commands.Range[int, MIN_BET, MAX_BET], side: app_commands.Choice[str]):
        await play_coin(interaction, self.bot, bet, side.value)

    @casino.command(name="ブラックジャック", description="🃏 21に近づけてディーラーに勝つ（ヒット・スタンド・ダブル）")
    @app_commands.rename(bet="賭け金")
    @app_commands.describe(bet=BET_HELP)
    async def blackjack(self, interaction: discord.Interaction, bet: app_commands.Range[int, MIN_BET, MAX_BET]):
        await play_blackjack(interaction, self.bot, bet)

    @casino.command(name="ルーレット", description="🎡 色・偶奇・範囲・数字に賭ける")
    @app_commands.rename(bet="賭け金", kind="賭け方", number="数字")
    @app_commands.describe(bet=BET_HELP, kind="赤黒・偶奇などは2倍、範囲(12個)は3倍、数字ぴったりは36倍",
                           number="「ストレート（数字）」のときだけ 0〜36 を指定")
    @app_commands.choices(kind=[
        app_commands.Choice(name="赤（2倍）", value="red"), app_commands.Choice(name="黒（2倍）", value="black"),
        app_commands.Choice(name="偶数（2倍）", value="even"), app_commands.Choice(name="奇数（2倍）", value="odd"),
        app_commands.Choice(name="1〜18（2倍）", value="low"), app_commands.Choice(name="19〜36（2倍）", value="high"),
        app_commands.Choice(name="1〜12（3倍）", value="dozen1"), app_commands.Choice(name="13〜24（3倍）", value="dozen2"),
        app_commands.Choice(name="25〜36（3倍）", value="dozen3"),
        app_commands.Choice(name="ストレート（数字・36倍）", value="number"),
    ])
    async def roulette(self, interaction: discord.Interaction, bet: app_commands.Range[int, MIN_BET, MAX_BET],
                       kind: app_commands.Choice[str], number: app_commands.Range[int, 0, 36] = None):
        await play_roulette(interaction, self.bot, bet, kind.value, number)


async def setup(bot):
    await bot.add_cog(Casino(bot))
