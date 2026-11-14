"""二重プレイ防止セッション管理のインターフェース。"""

from typing import Optional


class ISessionGuard:
    """ユーザー単位の排他実行・クールダウン制御契約。"""

    cooldown: float

    def try_start(self, user_id: int, now: Optional[float] = None) -> Optional[str]:
        raise NotImplementedError

    def end(self, user_id: int, now: Optional[float] = None) -> None:
        raise NotImplementedError

    def is_active(self, user_id: int) -> bool:
        raise NotImplementedError
