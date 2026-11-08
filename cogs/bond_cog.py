"""債券の満期の入金（1分ごと）と、表示の部品。通常債（元本保証）とジャンク債（高利回り・倒産あり）。

買う・売る（満期前の DCF 売却）は `/証券 注文`・`/証券 決済`（cogs/market_cog.py）。種類ごとの利率・期間・上限・倒産の強さは core/bonds.py。
ジャンク債の倒産抽選は、ここの1分ごとのループ（bond_settle）で引く。
決着の結果は `/証券 保有` の「債券の最近の決着」で見られる（通知は出さない）。
ミッション・連勝・週間ランキング・突発イベントには数えない。
"""

import random
import time

from discord import app_commands
from discord.ext import commands, tasks

from core import approval, bonds, ui

RNG = random.SystemRandom()
KIND_CHOICES = [
    app_commands.Choice(
        name=f"{k.emoji} {k.name}（{k.hours // 24}日・利息+{k.rate}%"
        + (f"・満期までに{k.default_pct}%倒産）" if k.default_pct else "・元本保証）"),
        value=k.key,
    )
    for k in bonds.KINDS.values()
]


def due_text(due: float) -> str:
    return f"<t:{int(due)}:R>"


def describe_done(row: dict) -> str:
    kind = bonds.KINDS[row["kind"]]
    p = row["principal"]
    if row["status"] == "paid":
        return f"{kind.emoji} {kind.name} {ui.pelica(p)} → 満期 **{ui.pelica(row['paid'])}**（+{row['paid'] - p:,}）"
    if row["status"] == "defaulted":
        return f"{kind.emoji} {kind.name} {ui.pelica(p)} → 💥 **倒産**（元本は戻りません）"
    return f"{kind.emoji} {kind.name} {ui.pelica(p)} → 売却 {ui.pelica(row['paid'])}（元本との差 {row['paid'] - p:+,}）"


class BondCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.tick.start()

    def cog_unload(self):
        self.tick.cancel()

    # ------------------------------ 満期の入金 ------------------------------ #
    @tasks.loop(minutes=1)
    async def tick(self):
        try:
            self.run_once()
        except Exception as exc:
            print(f"債券の満期確認に失敗: {exc!r}")

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    def run_once(self) -> None:
        now = time.time()
        for guild in list(self.bot.guilds):
            if not approval.is_allowed(self.bot.economy, self.bot.config, guild.id, 0):
                continue
            try:
                self.bot.economy.for_guild(guild.id).bond_settle(now, RNG)
            except Exception as exc:  # 1サーバーの失敗で、ほかを止めない
                print(f"債券を決着できません: guild={guild.id}: {exc!r}")


async def setup(bot):
    await bot.add_cog(BondCog(bot))
