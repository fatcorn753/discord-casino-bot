"""ペリカの増減の履歴を、構造化エントリにまとめる。Discord と DB に依存しない純粋なロジック。

1回のゲームは、台帳では「賭け」「払い戻し」などの複数の行になる。画面では1ゲームを1行にまとめる。
"""

import re
from dataclasses import dataclass
from typing import Optional

_GAME_ROWS = ("追加の賭け:", "払い戻し:", "保険券で半額返金:", "追加の支払い:", "返金:")
_TRANSFER = re.compile(r"^送金（(送|受)）[→←](\d+)$")


@dataclass(frozen=True)
class HistoryEntry:
    ts: float
    kind: str  # "game" | "transfer" | "other"
    delta: int
    balance_after: int
    game: Optional[str] = None
    bet: int = 0
    direction: str = ""  # "送" | "受"
    target_user_id: Optional[int] = None
    reason: str = ""


def _entries_oldest_first(rows: list) -> list[HistoryEntry]:
    """台帳の行（古い順）を HistoryEntry の並びにする。"""
    out: list[HistoryEntry] = []
    i = 0
    while i < len(rows):
        r = rows[i]
        reason = r["reason"]
        if reason.startswith("賭け:"):
            game = reason.split(":", 1)[1]
            bet, net, last, j = -r["delta"], r["delta"], r, i + 1
            while (
                j < len(rows)
                and rows[j]["reason"].endswith(":" + game)
                and any(rows[j]["reason"].startswith(p) for p in _GAME_ROWS)
            ):
                if rows[j]["reason"].startswith("追加の賭け:"):
                    bet += -rows[j]["delta"]
                net += rows[j]["delta"]
                last = rows[j]
                j += 1
            out.append(
                HistoryEntry(
                    ts=last["ts"],
                    kind="game",
                    delta=net,
                    balance_after=last["balance_after"],
                    game=game,
                    bet=bet,
                )
            )
            i = j
            continue
        m = _TRANSFER.match(reason)
        if m:
            delta, bal = r["delta"], r["balance_after"]
            if m.group(1) == "送" and i + 1 < len(rows) and rows[i + 1]["reason"] == "送金手数料":
                delta, bal = delta + rows[i + 1]["delta"], rows[i + 1]["balance_after"]
                i += 1
            out.append(
                HistoryEntry(
                    ts=r["ts"],
                    kind="transfer",
                    delta=delta,
                    balance_after=bal,
                    direction=m.group(1),
                    target_user_id=int(m.group(2)),
                )
            )
        else:
            out.append(
                HistoryEntry(
                    ts=r["ts"],
                    kind="other",
                    delta=r["delta"],
                    balance_after=r["balance_after"],
                    reason=reason,
                )
            )
        i += 1
    return out


def summarize_history(rows_newest_first: list, limit: int = 12) -> list[HistoryEntry]:
    """新しい順の台帳の行から、1ゲーム1件にまとめたエントリ（新しい順）を返す。"""
    grouped = _entries_oldest_first(list(reversed(rows_newest_first)))
    return list(reversed(grouped))[:limit]


def game_results(rows_oldest_first: list) -> list:
    """台帳の行（古い順）から、1ゲームごとの結果 [(時刻, ゲーム名, 損益), ...] を取り出す（過去の分の集計用）。"""
    return [(e.ts, e.game, e.delta) for e in _entries_oldest_first(rows_oldest_first) if e.game]
