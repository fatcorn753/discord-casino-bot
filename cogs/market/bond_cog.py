"""債券の満期の入金と倒産抽選（1分ごと。あわせて、破産猶予の時間切れの強制破産もここで行う）と、表示の部品。通常債（元本保証）とジャンク債（高利回り・倒産あり）。

買う・売る（満期前の DCF 売却）は `/証券 注文 債券`・`/証券 決済`（cogs/market/cog.py）。種類ごとの利率・期間・上限・倒産の強さは domain/bonds.py。
ジャンク債の倒産抽選は、ここの1分ごとのループ（bond_settle）で引く。
決着の結果は `/証券 保有` の「債券の最近の決着」で見られる（通知は出さない）。
ミッション・連勝・週間ランキング・突発イベントには数えない。
"""

import logging
import random
from typing import TYPE_CHECKING, Any

from discord.ext import commands, tasks

from cogs.market.bond_embeds import KIND_CHOICES, describe_done, due_text  # noqa: F401
from cogs.shared.tasks import run_sync_for_allowed_guilds
from domain.ports.repos import IGuildRepo
from services.market import MarketService
from services.teiai import TeiaiService

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot

RNG = random.SystemRandom()


class BondCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        config: dict[str, Any],
        guild_repo: IGuildRepo,
        market_svc: MarketService,
        teiai_svc: TeiaiService,
    ):
        self.bot = bot
        self._config = config
        self._guild_repo = guild_repo
        self._market_svc = market_svc
        self._teiai = teiai_svc

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
            _log.warning(f"債券の満期確認に失敗: {exc!r}")

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    def run_once(self) -> None:
        now = self._market_svc.clock.now_ts()

        def handle_guild(guild) -> None:
            self._market_svc.settle_maturities(guild.id, now, RNG)  # 満期の債券と、満期のオプションの決済
            self._teiai.trust_accrue(guild.id, now)  # 帝愛信託の利息（1時間ごと）
            for user_id, result in self._teiai.grace_expire(
                guild.id, now
            ):  # 破産猶予の時間切れ（強制破産と、CDS の配当）
                _log.warning(
                    f"強制破産: guild={guild.id} user={user_id} 差し押さえ={result['seized']:,} CDS配当={result['cds_paid']:,}"
                )

        run_sync_for_allowed_guilds(
            self.bot,
            self._guild_repo,
            self._config,
            handle_guild,
            error_label="債券の決着・破産猶予の確認に失敗",
        )


async def setup(bot: "CasinoBot"):
    await bot.add_cog(
        BondCog(
            bot,
            config=bot.config,
            guild_repo=bot.repos.guild,
            market_svc=bot.services.market,
            teiai_svc=bot.services.teiai,
        )
    )
