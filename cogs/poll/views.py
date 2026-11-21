"""投票の投稿（Embed）・ボタン・票の書き換えフォーム。"""

import re
from typing import TYPE_CHECKING, Any, cast

import discord

from cogs.shared import ui
from cogs.shared.bridges import IPollButtonBridge, NoOpPollButtonBridge
from domain import poll
from domain.ports.contracts import PollView

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot


def verdict(p: PollView) -> str:
    if p["yes"] > p["no"]:
        return "✅ 賛成多数"
    if p["no"] > p["yes"]:
        return "❌ 反対多数"
    return "⚖️ 同数"


def poll_embed(p: PollView) -> discord.Embed:
    """投票の投稿。受付中は締切までの時間、締切後は結果を出す。"""
    closed = p["status"] != "open"
    lines = [
        f"**{p['title']}**",
        "",
        f"👍 賛成 **{p['yes']:,}票**　👎 反対 **{p['no']:,}票**",
    ]
    if closed:
        lines.append(f"結果: **{verdict(p)}**")
    else:
        lines.append(f"締切 <t:{int(p['closes'])}:R>（1人1票。あとから変えられます）")
    if p["rigs"]:
        lines.append(f"⚠️ この投票の票数は **{p['rigs']}回** 書き換えられています")
    lines.append(f"作った人: <@{p['creator']}>")
    title = f"🗳️ 投票 #{p['id']}" + ("（締切）" if closed else "")
    embed = ui.embed(title, "\n".join(lines), ui.OK if closed else ui.INFO)
    if not closed:
        embed.set_footer(
            text=f"💰 作った人は {poll.RIG_COST:,}ペリカ払うと、票数を好きな数に書き換えられます（全員に告知されます）"
        )
    return embed


class PollButton(discord.ui.DynamicItem[discord.ui.Button], template=r"poll:(?P<act>yes|no|rig):(?P<id>\d+)"):
    """投票の投稿のボタン。再起動しても動くよう、投票の番号を custom_id に入れている。"""

    LABEL = {
        "yes": ("賛成", "👍", discord.ButtonStyle.success),
        "no": ("反対", "👎", discord.ButtonStyle.danger),
        "rig": (f"票をいじる（{poll.RIG_COST:,}）", "💰", discord.ButtonStyle.secondary),
    }

    def __init__(self, act: str, poll_id: int):
        label, emoji, style = self.LABEL[act]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"poll:{act}:{poll_id}"))
        self.act, self.poll_id = act, poll_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match):
        return cls(match["act"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        bot = cast("CasinoBot", interaction.client)
        bridge = bot.bridges.resolve_or(IPollButtonBridge, NoOpPollButtonBridge())
        await bridge.handle_button(interaction, self.act, self.poll_id)


def poll_view(poll_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for act in ("yes", "no", "rig"):
        view.add_item(PollButton(act, poll_id))
    return view


def parse_count(text: str) -> int:
    """票数の入力を読む。読めなければ ValueError（文面はそのまま利用者に見せる）。"""
    t = text.strip().replace(",", "")
    if not t.isdigit():
        raise ValueError("票数は 0 以上の整数で書いてください。")
    return int(t)


class RigModal(discord.ui.Modal, title="票数を書き換える"):
    """書き換えたあとの票数を入れるフォーム。送ると代金を払い、全員に告知される。"""

    def __init__(self, cog: Any, poll_id: int, yes: int, no: int):
        super().__init__()
        self._cog, self.poll_id = cog, poll_id
        self.yes = discord.ui.TextInput(label="賛成の票数", default=str(yes), max_length=9)
        self.no = discord.ui.TextInput(label="反対の票数", default=str(no), max_length=9)
        self.add_item(self.yes)
        self.add_item(self.no)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            yes, no = parse_count(self.yes.value), parse_count(self.no.value)
        except ValueError as exc:
            await ui.reply_error(interaction, str(exc), title="できません")
            return
        await self._cog.rig(interaction, self.poll_id, yes, no)
