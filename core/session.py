"""同じ人が同時に複数のゲームを始めないようにする（連打・二重プレイ対策）。"""

import time
from typing import Optional


class Sessions:
    def __init__(self, cooldown: float = 2.0):
        self._active = set()
        self._last_end = {}
        self.cooldown = cooldown

    def try_start(self, user_id: int, now: Optional[float] = None) -> Optional[str]:
        """開始できれば None を返して「プレイ中」にする。できなければ理由を返す。"""
        now = time.monotonic() if now is None else now
        if user_id in self._active:
            return "いま遊んでいるゲームがあります。終わってから次を始めてください。"
        wait = self.cooldown - (now - self._last_end.get(user_id, -1e9))
        if wait > 0:
            return f"少し待ってください（あと {wait:.1f} 秒）。"
        self._active.add(user_id)
        return None

    def end(self, user_id: int, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        self._active.discard(user_id)
        self._last_end[user_id] = now
        if len(self._last_end) > 1000:     # 古い記録を捨てて、メモリが増え続けないようにする
            self._last_end = {u: t for u, t in self._last_end.items() if now - t < self.cooldown}

    def is_active(self, user_id: int) -> bool:
        return user_id in self._active
