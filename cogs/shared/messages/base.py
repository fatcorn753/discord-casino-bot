"""ドメイン・永続化層のエラー・通知・表示データを Discord UI 向けに整形する基底契約（Bridge / §8.1）。"""

from collections.abc import Callable, Mapping
from typing import Any, Optional

from domain.earnings import MemoInfo
from domain.errors import Failure, Notice
from domain.history import summarize_history

MessageEntry = str | Callable[[dict[str, Any]], str]


class IMessageFormatter:
    """UI メッセージ整形の抽象契約（Bridge ターゲット）。"""

    def format_failure(self, reason: Any) -> str:
        raise NotImplementedError

    def format_notice(self, item: Any) -> str:
        raise NotImplementedError

    def format_notices(self, items: list[Any]) -> list[str]:
        raise NotImplementedError

    def format_memo(self, info: MemoInfo) -> str:
        raise NotImplementedError

    def format_history(self, rows_newest_first: list[Any], limit: int = 12) -> list[str]:
        raise NotImplementedError

    def format_standings_lines(
        self, rows: list[Any], medals: dict[int, str], viewer_id: Optional[int] = None
    ) -> list[str]:
        raise NotImplementedError


class MessageFormatter(IMessageFormatter):
    """失敗コード・通知コード別の整形テーブルを段階拡張する基底実装。"""

    failures: dict[str, MessageEntry]
    notices: dict[str, MessageEntry]

    def __init__(
        self,
        *,
        extra_failures: Optional[Mapping[str, MessageEntry]] = None,
        extra_notices: Optional[Mapping[str, MessageEntry]] = None,
    ) -> None:
        self.failures = {}
        self.notices = {}
        self._register_entries()
        if extra_failures:
            self.failures.update(extra_failures)
        if extra_notices:
            self.notices.update(extra_notices)

    def _register_entries(self) -> None:
        pass

    def format_failure(self, reason: Any) -> str:
        """Failure（または互換の str）を日本語 UI 文言へ整形する。"""
        if not isinstance(reason, Failure):
            return str(reason)
        entry = self.failures.get(reason.code)
        if entry is None:
            return str(reason)
        return entry if isinstance(entry, str) else entry(reason.params)

    def format_notice(self, item: Any) -> str:
        """Notice（または互換の str）を Discord UI 向け文言へ整形する。"""
        if not isinstance(item, Notice):
            return str(item)
        entry = self.notices.get(item.code)
        if entry is None:
            return str(item)
        return entry if isinstance(entry, str) else entry(item.params)

    def format_notices(self, items: list[Any]) -> list[str]:
        return [self.format_notice(n) for n in items]

    def format_memo(self, info: MemoInfo) -> str:
        word = "大幅な上方修正の噂" if info.hint > 0 else "大幅な下方修正の噂"
        return (
            f"【極秘メモ】<t:{int(info.release_at)}:F> の {info.symbol_name} の決算、**{word}**あり…？\n"
            f"（確度 {info.truth:.0%} ／ ガセの確率 {1 - info.truth:.0%}。信じるかは、あなた次第。周りには黙っておくか、煽るか…）"
        )

    def format_history(self, rows_newest_first: list[Any], limit: int = 12) -> list[str]:
        entries = summarize_history(rows_newest_first, limit)
        lines: list[str] = []
        for e in entries:
            match e.kind:
                case "game":
                    text = f"{e.game} 賭{e.bet:,}"
                case "transfer":
                    arrow = "→" if e.direction == "送" else "←"
                    text = f"送金 {arrow} <@{e.target_user_id}>"
                case _:
                    text = e.reason
            lines.append(f"<t:{int(e.ts)}:t> {text}　**{e.delta:+,}**　→ {e.balance_after:,}ペリカ")
        return lines

    def format_standings_lines(
        self, rows: list[Any], medals: dict[int, str], viewer_id: Optional[int] = None
    ) -> list[str]:
        lines: list[str] = []
        for rank, (uid, net, plays) in enumerate(rows, 1):
            mark = medals.get(rank, f"`{rank:>2}`")
            me = "　◀ あなた" if viewer_id == uid else ""
            lines.append(f"{mark} <@{uid}>　**{net:+,}ペリカ**（{plays}戦）{me}")
        return lines
