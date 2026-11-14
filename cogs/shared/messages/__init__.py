"""ドメイン・永続化層が返す構造化エラー・通知イベント・表示データの Discord 向け整形。"""

from typing import Any, Callable, Optional

from cogs.shared.messages.economy import ECONOMY_FAILURES
from cogs.shared.messages.games import GAMES_FAILURES
from cogs.shared.messages.market import MARKET_FAILURES
from cogs.shared.messages.notices import NOTICE_FORMATTERS
from domain.earnings import MemoInfo
from domain.errors import Failure, Notice
from domain.history import summarize_history

Formatter = str | Callable[[dict[str, Any]], str]

FAILURE_FORMATTERS: dict[str, Formatter] = {
    **ECONOMY_FAILURES,
    **MARKET_FAILURES,
    **GAMES_FAILURES,
}


def format_failure(reason: Any) -> str:
    """Failure（または互換の str）を日本語 UI 文言へ整形する。"""
    if not isinstance(reason, Failure):
        return str(reason)
    entry = FAILURE_FORMATTERS.get(reason.code)
    if entry is None:
        return str(reason)
    return entry if isinstance(entry, str) else entry(reason.params)


def format_notice(item: Any) -> str:
    """Notice（または互換の str）を Discord UI 向け文言へ整形する。"""
    if not isinstance(item, Notice):
        return str(item)
    entry = NOTICE_FORMATTERS.get(item.code)
    if entry is None:
        return str(item)
    return entry if isinstance(entry, str) else entry(item.params)


def format_notices(items: list[Any]) -> list[str]:
    return [format_notice(n) for n in items]


def format_memo(info: MemoInfo) -> str:
    word = "大幅な上方修正の噂" if info.hint > 0 else "大幅な下方修正の噂"
    return (
        f"【極秘メモ】<t:{int(info.release_at)}:F> の {info.symbol_name} の決算、**{word}**あり…？\n"
        f"（確度 {info.truth:.0%} ／ ガセの確率 {1 - info.truth:.0%}。信じるかは、あなた次第。周りには黙っておくか、煽るか…）"
    )


def format_history(rows_newest_first: list, limit: int = 12) -> list[str]:
    entries = summarize_history(rows_newest_first, limit)
    lines: list[str] = []
    for e in entries:
        if e.kind == "game":
            text = f"{e.game} 賭{e.bet:,}"
        elif e.kind == "transfer":
            arrow = "→" if e.direction == "送" else "←"
            text = f"送金 {arrow} <@{e.target_user_id}>"
        else:
            text = e.reason
        lines.append(f"<t:{int(e.ts)}:t> {text}　**{e.delta:+,}**　→ {e.balance_after:,}ペリカ")
    return lines


def format_standings_lines(rows: list, medals: dict[int, str], viewer_id: Optional[int] = None) -> list[str]:
    lines: list[str] = []
    for rank, (uid, net, plays) in enumerate(rows, 1):
        mark = medals.get(rank, f"`{rank:>2}`")
        me = "　◀ あなた" if viewer_id == uid else ""
        lines.append(f"{mark} <@{uid}>　**{net:+,}ペリカ**（{plays}戦）{me}")
    return lines


__all__ = [
    "FAILURE_FORMATTERS",
    "NOTICE_FORMATTERS",
    "format_failure",
    "format_history",
    "format_memo",
    "format_notice",
    "format_notices",
    "format_standings_lines",
]
