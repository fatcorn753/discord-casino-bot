"""帝愛社債の購入と売り（キャンセル）。"""

from typing import TYPE_CHECKING

import discord

from cogs.market.embeds import bond_buy_embed
from cogs.shared import ui
from cogs.shared.messages import format_failure
from domain import bonds
from domain.errors import Failure

from .common import RNG, run_trade

if TYPE_CHECKING:
    from cogs.market.cog import MarketCog


async def bond_buy(cog: "MarketCog", interaction: discord.Interaction, kind_key: str, amount: int) -> None:
    async def run(uid: int) -> None:
        gid = ui.guild_id_of(interaction)
        cog._market_svc.bond_settle(
            gid, cog._market_svc.clock.now_ts(), RNG
        )  # 満期の分を先に入金して、買える額を正しくする
        if amount < bonds.MIN_AMOUNT:
            await ui.reply_error(
                interaction, f"債券は {bonds.MIN_AMOUNT:,}ペリカ以上から買えます。", title="買えません"
            )
            return
        now = cog._market_svc.clock.now_ts()
        quote = cog._market_svc.bond_quote(
            gid, kind_key, amount, now
        )  # 買う直前の金利（買うときに、この利率で固定される）
        ok, reason = cog._market_svc.bond_buy(gid, uid, kind_key, amount, now)
        if not ok:
            await ui.reply_error(interaction, format_failure(reason), title="買えません")
            return
        await interaction.response.send_message(embed=bond_buy_embed(kind_key, amount, now, quote))

    await run_trade(cog._sessions, interaction, run)


async def bond_cancel(cog: "MarketCog", interaction: discord.Interaction, bond_id: int) -> None:
    async def run(uid: int) -> None:
        ok, result = cog._market_svc.bond_cancel(ui.guild_id_of(interaction), uid, bond_id)
        if not ok:
            await ui.reply_error(interaction, format_failure(result), title="売れません")
            return
        assert not isinstance(result, Failure)
        await interaction.response.send_message(  # 公開（ほかの売買の結果と同じ）
            embed=ui.ok_embed(
                f"{interaction.user.display_name} が債券を売りました", f"{ui.pelica(result)}を残高に戻しました。"
            )
        )

    await run_trade(cog._sessions, interaction, run)
