"""ゲームごとの還元率レベルの保存（Economy に混ぜ込む部品）。サーバーごとに、ゲームごとに1つ。

表の定義は core/economy.py の _init_schema にある。レベルの意味と計算は games/rtplevel.py。
"""

from typing import Optional

from games import rtplevel


class RtpMixin:
    """還元率レベルの保存と取得。"""

    def rtp_level_get(self, game: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT level FROM rtp_level WHERE guild_id=? AND game=?", (self._g(), game)
            ).fetchone()
        return row["level"] if row else rtplevel.DEFAULT_LEVEL

    def rtp_levels(self) -> dict:
        """{ゲーム: レベル}（設定していないゲームはレベル3）。"""
        with self._lock:
            saved = {
                r["game"]: r["level"]
                for r in self._conn.execute("SELECT game, level FROM rtp_level WHERE guild_id=?", (self._g(),))
            }
        return {g: saved.get(g, rtplevel.DEFAULT_LEVEL) for g in rtplevel.GAMES}

    def rtp_level_set(self, game: Optional[str], level: int) -> list:
        """レベルを決める。game が None なら、すべてのゲーム。変えたゲームの名前を返す。"""
        if level not in rtplevel.LEVELS:
            raise ValueError("レベルは 1〜6 です")
        games = list(rtplevel.GAMES) if game is None else [game]
        if any(g not in rtplevel.GAMES for g in games):
            raise ValueError(f"ゲーム名が不正です: {game!r}")
        with self._lock, self._conn:
            for g in games:
                if level == rtplevel.DEFAULT_LEVEL:
                    self._conn.execute(
                        "DELETE FROM rtp_level WHERE guild_id=? AND game=?", (self._g(), g)
                    )  # 3 は「いまの設定」＝何も保存しない
                else:
                    self._conn.execute(
                        "INSERT INTO rtp_level (guild_id, game, level) VALUES (?, ?, ?) ON CONFLICT(guild_id, game) DO UPDATE SET level=excluded.level",
                        (self._g(), g, level),
                    )
        return games
