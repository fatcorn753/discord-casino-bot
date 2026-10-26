"""/ヘルプ — 遊び方の一覧。"""

import discord
from discord import app_commands
from discord.ext import commands

from core import ui
from play.common import MAX_BET, MIN_BET


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ヘルプ", description="📖 遊び方とコマンドの一覧")
    async def help(self, interaction: discord.Interaction):
        e = ui.embed("📖 ペリカカジノ", "ゲーム内の仮想通貨「ペリカ」で遊ぶ、お遊び用のカジノです。\n"
                     "**現実のお金とは関係なく、換金もできません。** 遊びすぎにはご注意を。", ui.GOLD)
        e.add_field(name="💴 まずはここから", value="\n".join([
            "`/ペリカ デイリー` — 1日1回のボーナス（連続ログインで増える）",
            "`/ペリカ 残高` — 残高と成績",
            "`/ペリカ ランキング` — ペリカの多い順",
            "`/ペリカ 送金` — 友達に送る（手数料10%・1日の上限あり）",
            "`/ペリカ 履歴` — 最近の増減",
            "`/ペリカ ランキング掲示` — ランキングを自動更新（30分ごと）で掲示（管理者）",
        ]), inline=False)
        e.add_field(name="🛍️ ショップ（`/ショップ`）", value="\n".join([
            "`一覧` — 称号・カードの色/アイコン・消耗品・名前の色を、ペリカで交換",
            "`購入` `持ち物` `使う` `外す` — 買う・確認する・身につける・外す",
            "`称号表示` — 称号をニックネームの後ろに付ける（例: たつ【ざわ…ざわ…】）",
            "※ 景品はゲーム内・サーバー内のものだけです（現実の価値はありません）",
        ]), inline=False)
        e.add_field(name="🎰 ゲーム（`/カジノ`）", value="\n".join([
            "`チンチロ` — 🎲 地下チンチロ。親と勝負。目が出なければ3回まで",
            "`パチンコ` — 🎰 地下パチンコ。玉・リール・リーチの演出つき",
            "`スロット` — 🍒 3つ揃えば大きく増える",
            "`コイン` — 🪙 表裏を当てる",
            "`ブラックジャック` — 🃏 ヒット・スタンド・ダブル",
            "`ルーレット` — 🎡 色・偶奇・範囲・数字に賭ける",
        ]), inline=False)
        e.add_field(name="ルール", value=f"賭け金は {ui.pelica(MIN_BET)} 〜 {ui.pelica(MAX_BET)}。"
                    "同時に遊べるゲームは1つだけです。途中で Bot が止まった場合、賭け金は返ります。", inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Help(bot))
