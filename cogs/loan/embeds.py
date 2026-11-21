"""/貸付 の Embed 生成と整形ヘルパー。"""

from collections.abc import Mapping
from typing import Any

import discord

from cogs.shared import ui
from domain import loans, teiai
from domain.ports.contracts import LoanCredit


def who(user_id: int) -> str:
    return f"<@{user_id}>"


def terms_lines(loan: Mapping[str, Any]) -> list[str]:
    p, r, t = loan["principal"], loan["rate_tenths"], loan["term_days"]
    lender = who(loan["lender_id"]) if loan["lender_id"] else "（募集中）"
    return [
        f"貸す人: {lender}　借りる人: {who(loan['borrower_id'])}",
        f"金額: **{ui.pelica(p)}**　日利: **{loans.rate_text(r)}**　期間: **{t}日**",
        f"期間いっぱい借りた場合の返済: **{ui.pelica(loans.full_term_total(p, r, t))}**（利息 {ui.pelica(loans.interest(p, r, t))}）",
        "早く返せば、利息は日割りで減ります（経過日数は切り上げで、最低1日）。",
    ]


def borrower_note(credit: LoanCredit) -> str:
    """貸す人が、貸す相手のリスクを見られるように、借りる人の信用情報を出す（資産の額は見せない）。"""
    lines = [
        f"**借りる人の信用情報**\n身分: {teiai.STATUS_LABEL[credit['status']]}"
        + (f"　帝愛への借金: {ui.pelica(credit['debt'])}" if credit["debt"] else "")
    ]
    if credit["grace_until"] is not None:
        lines.append(f"⚠️ 破産猶予中（<t:{int(credit['grace_until'])}:R> に強制破産）")
    lines.append(
        f"返済の実績: 完済 **{credit['repaid']}件**　延滞中 **{credit['overdue']}件**　踏み倒し（破産で免責） **{credit['discharged']}件**"
    )
    lines.append(f"いま借りている貸付: **{credit['active']}件**（元本の残り {ui.pelica(credit['owed'])}）")
    if credit["bankrupt_at"]:
        lines.append(f"破産の履歴: 最後に <t:{int(credit['bankrupt_at'])}:D>")
    return "\n".join(lines)


def request_embed(loan: Mapping[str, Any], note: str = "") -> discord.Embed:
    lines = (
        terms_lines(loan)
        + ([note] if note else [])
        + [
            f"募集の期限: <t:{int(loan['offer_expires'])}:R>",
            "最初に「貸す」を押した人が、貸し手になります（その場で、貸す人の残高から、借りる人へお金が移ります）。",
        ]
    )
    return ui.embed(f"🙋 貸付の募集 #{loan['id']}", "\n".join(lines), ui.GOLD)


def offer_embed(loan: Mapping[str, Any], note: str = "") -> discord.Embed:
    lines = (
        terms_lines(loan)
        + ([note] if note else [])
        + [
            f"承諾の期限: <t:{int(loan['offer_expires'])}:R>",
            "借りる人が「承諾」を押すと、お金が移ります。",
        ]
    )
    return ui.embed(f"🤝 貸付の提案 #{loan['id']}", "\n".join(lines), ui.GOLD)


def state_embed(loan: Mapping[str, Any], title: str, note: str, color: int = ui.INFO) -> discord.Embed:
    return ui.embed(f"🤝 貸付 #{loan['id']} {title}", "\n".join(terms_lines(loan) + ([note] if note else [])), color)
