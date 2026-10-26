"""称号をニックネームの後ろに付ける・外すための、純粋な文字列処理（Discord に依存しない）。

見た目: `たつ【ざわ…ざわ…】`。Bot は「付ける前のニックネーム」と「付けた結果」を記録しておき、外すときは
利用者が途中で変えていなければ、記録した元のニックネームへ正確に戻す。変えていた場合は、Bot が知っている
称号の名前に一致する末尾の【…】だけを外す（利用者が自分で付けた【…】は消さない）。
"""

from dataclasses import dataclass
from typing import Optional

NICK_LIMIT = 32          # Discord のニックネームの最大文字数


def ulen(text: str) -> int:
    """UTF-16 の単位での長さ。絵文字などの数え方の違いで上限を超えないよう、多めに見積もる側で数える。"""
    return len(text.encode("utf-16-le")) // 2


def suffix(title: str) -> str:
    return f"【{title}】"


def strip_title(name: Optional[str], titles) -> str:
    """末尾の「【既知の称号】」を外した名前を返す。"""
    name = name or ""
    for title in sorted(titles, key=len, reverse=True):
        s = suffix(title)
        if name.endswith(s):
            return name[: -len(s)].rstrip()
    return name


def build(base: str, title: str) -> str:
    """名前の後ろに称号を付ける。上限を超える場合は、称号を残して名前の方を切り詰める。"""
    s = suffix(title)
    room = max(2, NICK_LIMIT - ulen(s))
    if ulen(base) > room:
        while base and ulen(base) + 1 > room:
            base = base[:-1]
        base += "…"
    return base + s


@dataclass(frozen=True)
class NickState:
    original: Optional[str]      # 付ける前のニックネーム（None はニックネームなし）
    applied: str                 # Bot が実際に設定したニックネーム


def plan(current: Optional[str], state: Optional[NickState], default_name: str, titles,
         title: Optional[str]) -> tuple:
    """(設定すべきニックネーム, 記録しておく状態) を返す。title=None は、称号を外して元に戻す。

    - 前回 Bot が設定したままなら、記録した元のニックネームを使う（長い名前が切り詰められていても戻せる）
    - 利用者が途中で変えていたら、現在の名前から既知の称号を外したものを「元」とみなす
    """
    if state is not None and current == state.applied:
        original = state.original
    else:
        stripped = strip_title(current, titles) if current else ""
        original = stripped or None
    if title:
        new = build(original or default_name, title)
        return new, NickState(original, new)
    target = original if (original and original != default_name) else None      # 表示名と同じなら、ニックネームを消す
    if state is not None and current == state.applied:
        target = state.original
    return target, None
