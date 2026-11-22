"""ドメイン・永続化層が返す構造化エラー・通知イベント・表示データの Discord 向け整形。"""

from typing import Any

from cogs.shared.messages.base import IMessageFormatter, MessageEntry
from cogs.shared.messages.notices import MessageFormatter
from domain.earnings import MemoInfo

DEFAULT_MESSAGES = MessageFormatter()
FAILURE_FORMATTERS: dict[str, MessageEntry] = DEFAULT_MESSAGES.failures
NOTICE_FORMATTERS: dict[str, MessageEntry] = DEFAULT_MESSAGES.notices


def format_failure(reason: Any) -> str:
    """Failure（または互換の str）を日本語 UI 文言へ整形する。"""
    return DEFAULT_MESSAGES.format_failure(reason)


def format_notice(item: Any) -> str:
    """Notice（または互換の str）を Discord UI 向け文言へ整形する。"""
    return DEFAULT_MESSAGES.format_notice(item)


def format_notices(items: list[Any]) -> list[str]:
    return DEFAULT_MESSAGES.format_notices(items)


def format_memo(info: MemoInfo) -> str:
    return DEFAULT_MESSAGES.format_memo(info)


def format_history(rows_newest_first: list[Any], limit: int = 12) -> list[str]:
    return DEFAULT_MESSAGES.format_history(rows_newest_first, limit)


def format_standings_lines(rows: list[Any], medals: dict[int, str], viewer_id: int | None = None) -> list[str]:
    return DEFAULT_MESSAGES.format_standings_lines(rows, medals, viewer_id)


__all__ = [
    "DEFAULT_MESSAGES",
    "FAILURE_FORMATTERS",
    "IMessageFormatter",
    "MessageFormatter",
    "NOTICE_FORMATTERS",
    "format_failure",
    "format_history",
    "format_memo",
    "format_notice",
    "format_notices",
    "format_standings_lines",
]
