"""ゲームごとの還元率レベルの保存。サーバーごとに、ゲームごとに1つ。

表の定義は infra/db/schema/tables.py にある。レベルの意味と計算は domain/games/rtplevel.py。
"""


from domain.games import rtplevel
from domain.ports.repos import IRtpRepo
from infra.db.base import EcoDb, ScopedRepo


class RtpRepo(ScopedRepo, IRtpRepo):
    """還元率レベルの保存と取得。"""

    def __init__(self, db: EcoDb, guild_id: int = 0, strict: bool = False) -> None:
        super().__init__(db, guild_id, strict)

    def rtp_level_get(self, game: str) -> int:
        with self._lock:
            v = self._scalar("SELECT level FROM rtp_level WHERE guild_id=? AND game=?", (self._g(), game))
        return v if v is not None else rtplevel.DEFAULT_LEVEL

    def rtp_levels(self) -> dict[str, int]:
        """{ゲーム: レベル}（設定していないゲームはレベル3）。"""
        with self._lock:
            saved = {
                r["game"]: r["level"]
                for r in self._all("SELECT game, level FROM rtp_level WHERE guild_id=?", (self._g(),))
            }
        return {g: saved.get(g, rtplevel.DEFAULT_LEVEL) for g in rtplevel.GAMES}

    def rtp_level_set(self, game: str | None, level: int) -> list[str]:
        """レベルを決める。game が None なら、すべてのゲーム。変えたゲームの名前を返す。"""
        if level not in rtplevel.LEVELS:
            raise ValueError("レベルは 1〜6 です")
        games = list(rtplevel.GAMES) if game is None else [game]
        if any(g not in rtplevel.GAMES for g in games):
            raise ValueError(f"ゲーム名が不正です: {game!r}")
        with self._db.tx():
            gid = self._g()
            for g in games:
                if level == rtplevel.DEFAULT_LEVEL:
                    self._conn.execute(
                        "DELETE FROM rtp_level WHERE guild_id=? AND game=?", (gid, g)
                    )  # 3 は「いまの設定」＝何も保存しない
                else:
                    self._conn.execute(
                        "INSERT INTO rtp_level (guild_id, game, level) VALUES (?, ?, ?) ON CONFLICT(guild_id, game) DO UPDATE SET level=excluded.level",
                        (gid, g, level),
                    )
        return games
