"""配信パネルの投稿（Embed）・ボタン・フォーム・配信者メニュー。"""

import re
from typing import TYPE_CHECKING, Any, cast

import discord

from cogs.shared import ui
from cogs.shared.amount import typed_amount
from cogs.shared.bridges import IStreamButtonBridge, NoOpStreamButtonBridge
from domain import stream
from domain.ports.contracts import StreamSettleResult, StreamView

if TYPE_CHECKING:
    from cogs.shared.bot import CasinoBot
    from cogs.stream.cog import StreamCog

SIDE_TEXT = {stream.WIN: "🏆 配信者の勝ち", stream.LOSE: "💀 配信者の負け"}
ROUND_STATUS = {"open": "賭けを受付中", "locked": "締切（勝敗を待っています）"}


def _odds_text(win_total: int, lose_total: int, side: str) -> str:
    o = stream.odds(win_total, lose_total, side)
    return f"（当たれば約 {o:.2f}倍）" if o is not None else ""


def panel_embed(s: StreamView) -> discord.Embed:
    """配信パネル。配信中はスパチャの合計と、いまの勝負の賭けの様子を出す。"""
    live = s["status"] == "live"
    lines = [f"配信者: <@{s['streamer']}>"]
    if live and s["voice_channel_id"]:
        lines.append(f"📍 場所: <#{s['voice_channel_id']}>")
    if s["url"]:
        lines.append(f"📺 {s['url']}")
    lines.append(f"💸 スパチャ **{ui.pelica(s['tips'])}**（{s['tip_count']:,}件）")
    r = s["round"]
    if live and r is not None:
        lines += [
            "",
            f"🎲 **{r['label']}** ・ {ROUND_STATUS.get(r['status'], r['status'])}",
            f"🏆 勝ち **{ui.pelica(r['win_total'])}** {_odds_text(r['win_total'], r['lose_total'], stream.WIN)}",
            f"💀 負け **{ui.pelica(r['lose_total'])}** {_odds_text(r['win_total'], r['lose_total'], stream.LOSE)}",
            f"賭けた人 {r['bettors']:,}人",
        ]
    elif live:
        lines += ["", "🎲 いまは勝負がありません（配信者が「🎛️ 配信者メニュー」から始めます）"]
    if live:
        lines.append(f"\nこのパネルは <t:{int(s['ends'])}:R> に自動で閉じます")
    title = ("🔴 配信中: " if live else "⏹️ 配信終了: ") + s["title"]
    embed = ui.embed(title, "\n".join(lines), 0xE62117 if live else ui.INFO)
    if live:
        embed.set_footer(
            text=(
                f"スパチャは配信者への送金です（手数料・1日の上限は送金と同じ）。"
                f"賭けは山分けで、負けた側から帝愛が {stream.RAKE:.0%} を引きます。"
                f"1人 {stream.BET_MAX:,}ペリカ まで。配信者は自分の勝負に賭けられません"
            )
        )
    return embed


def tip_embed(user: discord.abc.User, amount: int, message: str) -> discord.Embed:
    """スパチャの投稿。額が大きいほど目立つ色にする。"""
    e = discord.Embed(description=message or None, color=stream.tip_color(amount))
    e.set_author(name=f"{user.display_name}　💸 {amount:,}ペリカ", icon_url=user.display_avatar.url)
    return e


def result_text(res: StreamSettleResult) -> str:
    """勝負の精算の結果（チャンネルへの投稿の本文）。"""
    if res["winner"] is None:
        head = "↩️ 無効になりました。賭け金は全額返しました。"
    elif res["refunded"]:
        head = f"結果: **{SIDE_TEXT[res['winner']]}**\n片方にしか賭けがなかったので、勝負にならず、全額返しました。"
    else:
        head = f"結果: **{SIDE_TEXT[res['winner']]}**"
    lines = [f"🎲 **{res['label']}**", head]
    if not res["refunded"] and res["payouts"]:
        top = sorted(res["payouts"].items(), key=lambda kv: -kv[1])[:10]
        lines.append("\n".join(f"<@{uid}> **{ui.pelica(paid)}**" for uid, paid in top))
        if len(res["payouts"]) > len(top):
            lines.append(f"ほか {len(res['payouts']) - len(top)}人")
        lines.append(f"帝愛の控除: {ui.pelica(res['rake'])}")
    elif not res["payouts"] and res["winner"] is not None and not res["refunded"]:
        lines.append("当てた人はいませんでした。")
    return "\n".join(lines)


class StreamButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"stream:(?P<act>tip|win|lose|menu):(?P<id>\d+)"
):
    """配信パネルのボタン。再起動しても動くよう、配信の番号を custom_id に入れている。"""

    LABEL = {
        "tip": ("スパチャ", "💸", discord.ButtonStyle.danger),
        "win": ("勝ちに賭ける", "🏆", discord.ButtonStyle.success),
        "lose": ("負けに賭ける", "💀", discord.ButtonStyle.secondary),
        "menu": ("配信者メニュー", "🎛️", discord.ButtonStyle.primary),
    }

    def __init__(self, act: str, stream_id: int):
        label, emoji, style = self.LABEL[act]
        super().__init__(
            discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"stream:{act}:{stream_id}")
        )
        self.act, self.stream_id = act, stream_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match):
        return cls(match["act"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        bot = cast("CasinoBot", interaction.client)
        bridge = bot.bridges.resolve_or(IStreamButtonBridge, NoOpStreamButtonBridge())
        await bridge.handle_button(interaction, self.act, self.stream_id)


def panel_view(stream_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for act in ("tip", "win", "lose", "menu"):
        view.add_item(StreamButton(act, stream_id))
    return view


class TipModal(discord.ui.Modal, title="スパチャを送る"):
    """スパチャの額と、添える文。"""

    def __init__(self, cog: "StreamCog", stream_id: int):
        super().__init__()
        self._cog, self.stream_id = cog, stream_id
        self.amount = discord.ui.TextInput(label=f"金額（{stream.TIP_MIN:,}ペリカ から）", max_length=9)
        self.message = discord.ui.TextInput(
            label="メッセージ（省略できます）",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=stream.TIP_MESSAGE_MAX,
        )
        self.add_item(self.amount)
        self.add_item(self.message)

    async def on_submit(self, interaction: discord.Interaction):
        amount = typed_amount(self.amount.value)
        if amount is None:
            await ui.reply_error(interaction, "数字で入れてください。", title="送れません")
            return
        await self._cog.do_tip(interaction, self.stream_id, amount, (self.message.value or "").strip())


class BetModal(discord.ui.Modal):
    """賭ける額。"""

    def __init__(self, cog: "StreamCog", stream_id: int, side: str):
        super().__init__(title=f"{SIDE_TEXT[side]}に賭ける")
        self._cog, self.stream_id, self.side = cog, stream_id, side
        self.amount = discord.ui.TextInput(
            label=f"金額（{stream.BET_MIN:,}〜{stream.BET_MAX:,}ペリカ）", max_length=9, default=str(stream.BET_MIN)
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        amount = typed_amount(self.amount.value)
        if amount is None:
            await ui.reply_error(interaction, "数字で入れてください。", title="賭けられません")
            return
        await self._cog.do_bet(interaction, self.stream_id, self.side, amount)


class RoundModal(discord.ui.Modal, title="勝負を始める"):
    """勝負の名前（例: 3戦目・ボス戦）。省略すると「次の勝負」。"""

    def __init__(self, cog: "StreamCog", stream_id: int):
        super().__init__()
        self._cog, self.stream_id = cog, stream_id
        self.label = discord.ui.TextInput(
            label="勝負の名前（省略で「次の勝負」）", required=False, max_length=stream.ROUND_LABEL_MAX
        )
        self.add_item(self.label)

    async def on_submit(self, interaction: discord.Interaction):
        await self._cog.do_open_round(interaction, self.stream_id, self.label.value or "")


class StreamerMenu(ui.OwnerView):
    """配信者だけに見せる操作（勝負の開始・締切・勝敗・無効・配信の終了）。"""

    def __init__(self, cog: "StreamCog", streamer: int, s: StreamView):
        super().__init__(streamer, timeout=600, owner_message="これは配信者だけが使えます。")
        self._cog, self.stream_id = cog, s["id"]
        r = s["round"]
        buttons: list[tuple[str, discord.ButtonStyle, str, bool]] = [
            ("🎲 勝負を始める", discord.ButtonStyle.primary, "open", r is not None),
            ("🔒 賭けを締め切る", discord.ButtonStyle.secondary, "lock", r is None or r["status"] != "open"),
            ("🏆 勝った", discord.ButtonStyle.success, "win", r is None),
            ("💀 負けた", discord.ButtonStyle.danger, "lose", r is None),
            ("↩️ 無効にする（全額返す）", discord.ButtonStyle.secondary, "void", r is None),
            ("⏹️ 配信を終える", discord.ButtonStyle.danger, "end", False),
        ]
        for label, style, act, disabled in buttons:
            btn = discord.ui.Button(
                label=label, style=style, disabled=disabled, row=0 if act in ("open", "lock") else 1
            )
            btn.callback = self._make(act)
            self.add_item(btn)

    def _make(self, act: str):
        async def callback(interaction: discord.Interaction):
            await self._cog.do_menu(interaction, self.stream_id, act)

        return callback
