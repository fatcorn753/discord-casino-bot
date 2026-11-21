"""Bot の持ち主が使う運営の操作（ペリカの付与・残高の設定・還元率・パチンコの集計）のアプリケーションサービス。"""

from typing import Any, Optional

from domain.ports.repos import IPachinkoRepo, IRtpRepo, IWalletRepo


class AdminService:
    """`/運営` の操作。台帳に理由を残して残高を動かす。"""

    def __init__(self, wallet: IWalletRepo, rtp: IRtpRepo, pachinko: IPachinkoRepo) -> None:
        self._wallet = wallet
        self._rtp = rtp
        self._pachinko = pachinko

    def grant(self, guild_id: int, user_id: int, amount: int, reason: str) -> int:
        """ペリカを付与して、付与後の残高を返す。"""
        return self._wallet.for_guild(guild_id).credit(user_id, amount, reason)

    def set_balance(self, guild_id: int, user_id: int, amount: int, reason: str) -> tuple[int, int, int, int]:
        """残高を設定する。(前の残高, 後の残高, 前の借金, 後の借金)。"""
        return self._wallet.for_guild(guild_id).set_balance(user_id, amount, reason)

    def rtp_levels(self, guild_id: int) -> dict[str, int]:
        return self._rtp.for_guild(guild_id).rtp_levels()

    def rtp_set(self, guild_id: int, game: Optional[str], level: int) -> list[str]:
        """還元率のレベルを変える（game が None なら全ゲーム）。変えたゲームの一覧を返す。"""
        return self._rtp.for_guild(guild_id).rtp_level_set(game, level)

    def pachinko_stats(self, guild_id: Optional[int], since: Optional[float]) -> dict[str, Any]:
        return self._pachinko.pachinko_stats(guild_id, since)
