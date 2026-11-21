"""二重プレイ防止セッション管理のインターフェース。"""



class ISessionGuard:
    """ユーザー単位の排他実行・クールダウン制御契約。"""

    cooldown: float

    def try_start(self, user_id: int, now: float | None = None) -> str | None:
        raise NotImplementedError

    def end(self, user_id: int, now: float | None = None) -> None:
        raise NotImplementedError

    def is_active(self, user_id: int) -> bool:
        raise NotImplementedError
