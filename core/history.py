"""ペリカの増減の履歴を、読みやすい行にまとめる。Discord と DB に依存しない純粋なロジック。

1回のゲームは、台帳では「賭け」「払い戻し」などの複数の行になる。画面では1ゲームを1行にまとめる。
"""

import re

_GAME_ROWS = ("追加の賭け:", "払い戻し:", "保険券で半額返金:", "追加の支払い:", "返金:")
_TRANSFER = re.compile(r"^送金（(送|受)）[→←](\d+)$")


def _lines_oldest_first(rows: list) -> list:
    """台帳の行（古い順）を、(時刻, 本文, 増減, 残高, ゲーム名 or None) のまとまりにする。"""
    out, i = [], 0
    while i < len(rows):
        r = rows[i]
        reason = r["reason"]
        if reason.startswith("賭け:"):
            game = reason.split(":", 1)[1]
            bet, net, last, j = -r["delta"], r["delta"], r, i + 1
            while j < len(rows) and rows[j]["reason"].endswith(":" + game) and any(rows[j]["reason"].startswith(p) for p in _GAME_ROWS):
                if rows[j]["reason"].startswith("追加の賭け:"):
                    bet += -rows[j]["delta"]
                net += rows[j]["delta"]
                last = rows[j]
                j += 1
            out.append((last["ts"], f"{game} 賭{bet:,}", net, last["balance_after"], game))
            i = j
            continue
        m = _TRANSFER.match(reason)
        if m:
            out.append((r["ts"], f"送金 {'→' if m.group(1) == '送' else '←'} <@{m.group(2)}>", r["delta"], r["balance_after"], None))
        else:
            out.append((r["ts"], reason, r["delta"], r["balance_after"], None))
        i += 1
    return out


def format_history(rows_newest_first: list, limit: int = 12) -> list:
    """新しい順の台帳の行から、1ゲーム1行にまとめた表示行（新しい順）を作る。"""
    grouped = _lines_oldest_first(list(reversed(rows_newest_first)))
    lines = [f"<t:{int(ts)}:t> {text}　**{delta:+,}**　→ {bal:,}ペリカ" for ts, text, delta, bal, _game in reversed(grouped)]
    return lines[:limit]


def game_results(rows_oldest_first: list) -> list:
    """台帳の行（古い順）から、1ゲームごとの結果 [(時刻, ゲーム名, 損益), ...] を取り出す（過去の分の集計用）。"""
    return [(ts, game, net) for ts, _text, net, _bal, game in _lines_oldest_first(rows_oldest_first) if game]
