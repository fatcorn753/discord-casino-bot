"""ペリカの増減の履歴を、構造化エントリにまとめる。Discord と DB に依存しない純粋なロジック。

1回のゲームは、台帳では「賭け」「払い戻し」などの複数の行になる。画面では1ゲームを1行にまとめる。
"""

import re
from dataclasses import dataclass
from typing import Any

_GAME_ROWS = ("追加の賭け:", "払い戻し:", "保険券で半額返金:", "追加の支払い:", "返金:")
_TRANSFER = re.compile(r"^送金（(送|受)）[→←](\d+)$")


@dataclass(frozen=True)
class HistoryEntry:
    ts: float
    kind: str  # "game" | "transfer" | "other"
    delta: int
    balance_after: int
    game: str | None = None
    bet: int = 0
    direction: str = ""  # "送" | "受"
    target_user_id: int | None = None
    reason: str = ""


def _transfer_of(r: dict[str, Any]) -> tuple[str, int | None] | None:
    """送金の行なら (direction, target_user_id) を返す。構造化列 kind を優先し、未設定なら reason の正規表現へフォールバック。"""
    kind = r.get("kind") or ""
    if kind == "transfer_out":
        direction = "送"
    elif kind == "transfer_in":
        direction = "受"
    else:
        m = _TRANSFER.match(r["reason"])
        if m is None:
            return None
        return m.group(1), int(m.group(2))
    target: int | None = r.get("counterparty_id")
    if target is None:
        m = _TRANSFER.match(r["reason"])
        target = int(m.group(2)) if m else None
    return direction, target


def _is_transfer_fee_row(r: dict[str, Any]) -> bool:
    """送金手数料の行か（構造化列 kind 優先、旧 reason 形式へフォールバック）。"""
    return r.get("kind") == "transfer_fee" or r["reason"] == "送金手数料"


def _entries_oldest_first(rows: list[dict[str, Any]]) -> list[HistoryEntry]:
    """台帳の行（古い順）を HistoryEntry の並びにする。"""
    rows = [dict(r) for r in rows]  # sqlite3.Row なども dict にそろえ、.get で構造化列を読めるようにする
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
                bet += -rows[j]["delta"] if rows[j]["reason"].startswith("追加の賭け:") else 0
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
        t = _transfer_of(r)
        if t is not None:
            direction, target = t
            delta, bal = r["delta"], r["balance_after"]
            if direction == "送" and i + 1 < len(rows) and _is_transfer_fee_row(rows[i + 1]):
                delta, bal = delta + rows[i + 1]["delta"], rows[i + 1]["balance_after"]
                i += 1
            out.append(
                HistoryEntry(
                    ts=r["ts"],
                    kind="transfer",
                    delta=delta,
                    balance_after=bal,
                    direction=direction,
                    target_user_id=target,
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


def summarize_history(rows_newest_first: list[dict[str, Any]], limit: int = 12) -> list[HistoryEntry]:
    """新しい順の台帳の行から、1ゲーム1件にまとめたエントリ（新しい順）を返す。"""
    grouped = _entries_oldest_first(list(reversed(rows_newest_first)))
    return list(reversed(grouped))[:limit]


def game_results(rows_oldest_first: list[dict[str, Any]]) -> list[tuple[float, str, int]]:
    """台帳の行（古い順）から、1ゲームごとの結果 [(時刻, ゲーム名, 損益), ...] を取り出す（過去の分の集計用）。"""
    return [(e.ts, e.game, e.delta) for e in _entries_oldest_first(rows_oldest_first) if e.game]
