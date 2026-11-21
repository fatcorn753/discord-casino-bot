"""`/ペリカ` の画面の組み立て（残高カード・デイリー・返済・自己破産・ミッション・履歴）。値は services/ から受け取る。"""

from typing import Any, Optional

import discord

from cogs.shared import ui
from cogs.shared.messages import format_history
from domain import missions as missions_mod
from domain import shop, skins, teiai
from domain.types import DAILY_BASE, DAILY_STREAK_BONUS, DAILY_STREAK_CAP, DailyResult
from services.economy import BalanceCard, MissionBoard


def no_account_embed(name: str) -> discord.Embed:
    return ui.embed(f"💴 {name} の残高", "まだ遊んでいません（口座がありません）。", ui.INFO)


def balance_embed(card: BalanceCard, name: str, avatar_url: str) -> discord.Embed:
    """残高カード。ショップで買ったカードの色・アイコン・称号を反映する。"""
    s, prof = card.stats, card.profile
    color_item, icon_item, title_item = (shop.BY_ID.get(prof[k]) for k in ("card_color", "card_icon", "title"))
    icon = icon_item.value if icon_item else "💴"
    title_line = f"## 🏷️ {title_item.name}\n" if title_item else ""
    e = ui.embed(
        f"{icon} {name} の残高",
        f"{title_line}# {ui.pelica(s['balance'])}",
        color_item.value if color_item and isinstance(color_item.value, int) else ui.GOLD,
    )
    e.set_thumbnail(url=avatar_url)
    e.add_field(name="身分", value=teiai.STATUS_LABEL[card.display_status], inline=True)
    if s.get("debt"):
        e.add_field(
            name="帝愛への借金",
            value=f"{ui.pelica(s['debt'])}（収入の{int(teiai.REPAY_RATE * 100)}%が天引きで返済されます。`/ペリカ 返済` で自分から返せます）\n"
            f"⏳ 帝愛の利息（トイチ）: 1日ごとに借金の{teiai.DEBT_RATE_PER_DAY:.0%}が加わります（{teiai.DEBT_INTEREST_CEILING:,}ペリカまで）",
            inline=False,
        )
    if card.rank:
        e.add_field(name="このサーバーでの順位（純資産）", value=f"{card.rank[0]}位 / {card.rank[2]}人", inline=True)
    owned, collection_total = shop.collection_value(card.inventory)
    if owned and prof["frozen"]:  # 借金中・延滞中・破産者は、ショップの品が差し押さえ中（完済してカタギに戻ると返る）
        e.add_field(
            name=f"🏆 コレクション（資産価値 {ui.pelica(collection_total)}）",
            value="🔒 帝愛により差し押さえ中（借金を完済して、カタギに戻ると返されます）",
            inline=False,
        )
    elif owned:  # ショップのコレクション。持っているものが、絵文字で並ぶ
        e.add_field(
            name=f"🏆 コレクション（資産価値 {ui.pelica(collection_total)}）",
            value=" ".join(str(i.value) for i in owned),
            inline=False,
        )
    if card.has_frozen_equipment:
        e.add_field(
            name="🔒 称号・カード・演出",
            value="差し押さえ中です（借金を完済して、カタギに戻ると使えます）",
            inline=False,
        )
    if card.crypto_held:
        e.add_field(name="証券（現物）の評価額", value=ui.pelica(card.crypto_held), inline=True)
    if card.bonds_held:
        e.add_field(name="債券（預けている元本）", value=ui.pelica(card.bonds_held), inline=True)
    e.add_field(name="遊んだ回数", value=f"{s['games_played']:,}回", inline=True)
    e.add_field(name="最大の勝ち", value=ui.pelica(s["biggest_win"]), inline=True)
    e.add_field(
        name="連続ログイン",
        value=f"{s['daily_streak']}日" if s["daily_streak"] else "まだ受け取っていません",
        inline=True,
    )
    play = card.play
    if play["plays"]:
        e.add_field(name="勝率", value=f"{play['wins'] * 100 // play['plays']}%（{play['plays']:,}戦）", inline=True)
        e.add_field(name="ゲームの損益", value=f"{play['net_total']:+,}ペリカ", inline=True)
    if play["best_streak"] >= 2:
        e.add_field(name="最大連勝", value=f"{play['best_streak']}連勝", inline=True)
    e.set_footer(text="勝率・損益は、集計を始めてからのゲームの分です")
    return e


def daily_wait_embed(streak: int, next_label: str, next_ts: int) -> discord.Embed:
    return ui.warn_embed(
        "まだ受け取れません",
        f"次は **{next_label}**（<t:{next_ts}:R>）から受け取れます。\n"
        f"ボーナスは6時間ごと（0・6・12・18時）です。\n連続ログイン: **{streak}日**",
    )


def daily_embed(
    result: DailyResult, next_label: str, next_ts: int, season: Optional[skins.Season] = None
) -> discord.Embed:
    """デイリーボーナスの結果。季節演出の期間中は、見出しと一言がその季節のものになる（額は変わらない）。"""
    next_bonus = DAILY_BASE + DAILY_STREAK_BONUS * min(result.streak, DAILY_STREAK_CAP)
    boost = "🎟️ デイリー2倍券を使った！\n" if result.boosted else ""
    if result.guarded:
        boost += f"🛡️ 連続守り券を **{result.guarded}枚** 使って、連続ログインをつないだ！\n"
    debt_line = ""
    if result.skimmed:
        debt_line += f"🕴️ 帝愛にピンハネされた: **{ui.pelica(result.skimmed)}**（借金は減りません）\n"
    if result.repaid:
        debt_line += f"💸 借金の返済に天引きされた: **{ui.pelica(result.repaid)}**\n"
    if result.seed:
        debt_line += f"🕴️ 帝愛が種銭を貸し付けた: **{ui.pelica(result.seed)}**（借金に加わります）\n"
    lead = f"{season.daily_line}\n" if season and season.daily_line else ""
    body = (
        f"{lead}{boost}**+{ui.pelica(result.amount)}**\n{debt_line}連続ログイン: **{result.streak}日**（明日は {ui.pelica(next_bonus)}）\n残高: **{ui.pelica(result.balance)}**\n\n"
        f"🕛 次のボーナスは **{next_label}**（<t:{next_ts}:R>）から。6時間ごと（0・6・12・18時）に受け取れます。"
    )
    if season and season.daily_title:  # 季節の見出しは、自前の絵文字で始まるので ✅ を付けない
        return ui.embed(season.daily_title, body, ui.OK)
    return ui.ok_embed("デイリーボーナス", body)


def repaid_embed(paid: int, left: int) -> discord.Embed:
    text = f"帝愛に **{ui.pelica(paid)}** 返しました。" + (
        f"\n残りの借金: **{ui.pelica(left)}**" if left else "\n**借金を完済しました！**"
    )
    return ui.ok_embed("返済しました", text)


BANKRUPT_REFUSALS = {
    "already": (
        "すでに破産者です",
        "借金を完済すると、カタギに戻ります（貸付だけは、完済から7日、借りられません）。",
    ),
    "nothing": ("破産できません", "借金も、貸付の借り入れもありません。"),
}


def bankrupt_warning() -> str:
    return (
        "**全財産（現金・CSトークン・空売り・債券）と、ショップで買った全品（称号・カード・演出・コレクション・消耗品）を差し押さえ**、"
        f"換金（ショップの品は購入額の{int(teiai.SEIZE_RATE * 100)}%）して、帝愛 → 貸付（古い順）の順に配当します。\n"
        "・貸付の残りは**免責**（貸した人の損になります）\n"
        f"・帝愛の借金は、残っていても **{teiai.BANKRUPT_FLOOR:,}ペリカまで圧縮**\n"
        f"・身分が🔴**破産者**になり、**完済して{teiai.BLACKLIST_DAYS}日たつまで、貸付を借りられません**\n"
        "・**元に戻せません。**"
    )


def bankrupt_result_embed(result: dict[str, Any], name: str) -> discord.Embed:
    """自己破産の結果（公開。誰が破産したかも出す）。"""
    lines = [
        f"差し押さえた額: **{ui.pelica(result['seized'])}**（うちショップの品の換金 {ui.pelica(result['shop_value'])}）",
        f"帝愛への配当: **{ui.pelica(result['senior_paid'])}**",
        f"貸付への配当: **{ui.pelica(result['junior_paid'])}**（免責になった貸付の残り: {ui.pelica(result['forgiven'])}）",
        f"帝愛の借金: 圧縮のあと **{ui.pelica(result['debt'])}**",
    ]
    if result["seed"]:
        lines.append(f"🕴️ 帝愛が種銭 **{ui.pelica(result['seed'])}** を貸し付けました（借金に加わります）。")
    return ui.embed(f"🔴 {name} が自己破産しました", "\n".join(lines), ui.ERROR)


def missions_embed(board: MissionBoard, reset_text: str) -> discord.Embed:
    lines = []
    for m, progress, done in board.status:
        bar = "🟩" * (10 * min(progress, m.target) // m.target) + "⬜" * (10 - 10 * min(progress, m.target) // m.target)
        mark = "✅" if done else "🔸"
        shown = m.target if done else progress
        lines.append(f"{mark} **{m.name}**　報酬 {ui.pelica(m.reward)}\n{bar} {shown:,} / {m.target:,}")
    lines.append(
        f"\n※ 賭け金 {ui.pelica(missions_mod.MIN_BET)} 以上のゲームだけが数えられます。連勝はその日の分だけです。"
    )
    lines.append(f"🎁 3つ全部で、さらに **+{ui.pelica(missions_mod.COMPLETE_BONUS)}**")
    lines.append(f"🔥 いまの連勝: **{board.win_streak}** ／ 自己ベスト: **{board.best_streak}**")
    lines.append(f"🕛 次の更新: **{reset_text}**（<t:{int(board.next_reset.timestamp())}:R>）")
    return ui.embed("📜 今日のミッション", "\n".join(lines), ui.GOLD)


def history_embed(rows: list[Any]) -> discord.Embed:
    lines = format_history(rows, 12)
    return ui.embed("🧾 最近の増減", "\n".join(lines) or "まだ記録がありません。", ui.INFO)
