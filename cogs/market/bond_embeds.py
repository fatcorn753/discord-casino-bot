"""債券の表示文・選択肢（`BondCog` と `MarketCog` の双方から参照する）。"""

from discord import app_commands

from cogs.shared import ui
from domain import bonds

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
