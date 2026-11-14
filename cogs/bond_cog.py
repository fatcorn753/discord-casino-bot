"""債券の満期の入金と倒産抽選（1分ごと。あわせて、破産猶予の時間切れの強制破産もここで行う）と、表示の部品。通常債（元本保証）とジャンク債（高利回り・倒産あり）。

買う・売る（満期前の DCF 売却）は `/証券 注文`・`/証券 決済`（cogs/market_cog.py）。種類ごとの利率・期間・上限・倒産の強さは core/bonds.py。
ジャンク債の倒産抽選は、ここの1分ごとのループ（bond_settle）で引く。
決着の結果は `/証券 保有` の「債券の最近の決着」で見られる（通知は出さない）。
ミッション・連勝・週間ランキング・突発イベントには数えない。
"""

import random
import time

from discord.ext import commands, tasks

from cogs._bond_embeds import KIND_CHOICES, describe_done, due_text  # noqa: F401
from core import approval

RNG = random.SystemRandom()


class BondCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.tick.start()

    async def cog_unload(self) -> None:
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
                eco = self.bot.economy.for_guild(guild.id)
                eco.bond_settle(now, RNG)
                eco.option_settle(now)  # 満期のオプションの決済
                for user_id, result in eco.grace_expire(now):  # 破産猶予の時間切れ（強制破産と、CDS の配当）
                    print(
                        f"強制破産: guild={guild.id} user={user_id} 差し押さえ={result['seized']:,} CDS配当={result['cds_paid']:,}"
                    )
            except Exception as exc:  # 1サーバーの失敗で、ほかを止めない
                print(f"債券の決着・破産猶予の確認に失敗: guild={guild.id}: {exc!r}")


async def setup(bot):
    await bot.add_cog(BondCog(bot))
