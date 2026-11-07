"""サーバーの承認・ランキング掲示・ニックネーム設定（Economy に混ぜ込む部品）。

表の定義は core/economy.py の _init_schema にある。
"""

import time
from typing import Optional


class GuildMixin:
    """サーバーの承認・ランキング掲示・ニックネーム設定（Economy に混ぜ込む部品）。"""

    # ------------------------------------------------------------------ #
    # 利用できるサーバーの承認                                            #
    # ------------------------------------------------------------------ #
    def set_guild_status(
        self, guild_id: int, status: str, name: Optional[str] = None, by: Optional[int] = None
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO guild_status (guild_id, status, name, decided_by, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET status=excluded.status, "
                "name=COALESCE(excluded.name, guild_status.name), decided_by=excluded.decided_by, updated_at=excluded.updated_at",
                (guild_id, status, name, by, time.time()),
            )

    def get_guild_status(self, guild_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM guild_status WHERE guild_id=?", (guild_id,)).fetchone()
        return dict(row) if row else None

    def list_guild_status(self) -> list:
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM guild_status ORDER BY updated_at DESC")]

    def mark_guild_notified(self, guild_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE guild_status SET notified_at=? WHERE guild_id=?", (time.time(), guild_id))

    # ------------------------------------------------------------------ #
    # ランキング掲示板（自動で更新されるメッセージ。1サーバーに1つ）       #
    # ------------------------------------------------------------------ #
    def set_leaderboard(self, guild_id: int, channel_id: int, message_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO leaderboards (guild_id, channel_id, message_id) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, message_id=excluded.message_id",
                (guild_id, channel_id, message_id),
            )

    def get_leaderboard(self, guild_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM leaderboards WHERE guild_id=?", (guild_id,)).fetchone()
        return dict(row) if row else None

    def list_leaderboards(self) -> list:
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM leaderboards")]

    def delete_leaderboard(self, guild_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM leaderboards WHERE guild_id=?", (guild_id,))

    def set_nick_pref(self, guild_id: int, user_id: int, enabled: bool) -> None:
        """称号をそのサーバーでのニックネームに付けるか（付けると選んだ人だけ）。

        外しても、身分の【地下労働者】などが付いている間は、その記録だけ残す（元のニックネームへ戻すため）。
        """
        with self._lock, self._conn:
            if enabled:
                self._conn.execute(
                    "INSERT INTO nick_prefs (guild_id, user_id, enabled) VALUES (?, ?, 1) "
                    "ON CONFLICT(guild_id, user_id) DO UPDATE SET enabled=1",
                    (guild_id, user_id),
                )
            else:
                self._conn.execute(
                    "UPDATE nick_prefs SET enabled=0 WHERE guild_id=? AND user_id=?", (guild_id, user_id)
                )
                self._drop_idle_nick_row(guild_id, user_id)

    def ensure_nick_row(self, guild_id: int, user_id: int) -> None:
        """身分のニックネームを付ける人の記録を、なければ作る（称号の表示は、選んだことにしない）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO nick_prefs (guild_id, user_id, enabled) VALUES (?, ?, 0)", (guild_id, user_id)
            )

    def _drop_idle_nick_row(self, guild_id: int, user_id: int) -> None:
        """称号の表示も選んでいなくて、Bot が付けたニックネームもなければ、記録を消す。"""
        self._conn.execute(
            "DELETE FROM nick_prefs WHERE guild_id=? AND user_id=? AND enabled=0 AND applied_nick IS NULL",
            (guild_id, user_id),
        )

    def nick_managed(self, guild_id: int, user_id: int) -> bool:
        """Bot がこの人のニックネームを扱っているか（称号の表示を選んだ人、または身分が付いている人）。"""
        with self._lock:
            return (
                self._conn.execute(
                    "SELECT 1 FROM nick_prefs WHERE guild_id=? AND user_id=?", (guild_id, user_id)
                ).fetchone()
                is not None
            )

    def status_nick_targets(self) -> list:
        """身分のニックネームを、付ける・外す必要があるかもしれない人（このサーバー）。借金・延滞・破産者の人と、身分だけで付けている人。"""
        now = time.time()
        with self._lock:
            return [
                r[0]
                for r in self._conn.execute(
                    "SELECT user_id FROM users WHERE guild_id=? AND (debt > 0 OR (bankrupt_at IS NOT NULL AND "
                    "(blacklist_until IS NULL OR blacklist_until > ?)) OR EXISTS (SELECT 1 FROM loans l WHERE "
                    "l.guild_id=users.guild_id AND l.borrower_id=users.user_id AND l.status='overdue')) "
                    "UNION SELECT user_id FROM nick_prefs WHERE guild_id=? AND enabled=0",
                    (self._g(), now, self._g()),
                )
            ]

    def get_nick_state(self, guild_id: int, user_id: int):
        """称号を付けたときの記録 (元のニックネーム, 設定したニックネーム)。未適用なら None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT original_nick, applied_nick FROM nick_prefs WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            ).fetchone()
        return None if row is None or row["applied_nick"] is None else (row["original_nick"], row["applied_nick"])

    def set_nick_state(self, guild_id: int, user_id: int, original, applied) -> None:
        """記録を保存する（applied=None で記録を消す）。設定が ON の人にだけ記録できる。"""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE nick_prefs SET original_nick=?, applied_nick=? WHERE guild_id=? AND user_id=?",
                (original if applied is not None else None, applied, guild_id, user_id),
            )
            self._drop_idle_nick_row(guild_id, user_id)

    def delete_guild_nick_prefs(self, guild_id: int) -> None:
        """Bot がサーバーから外れたときに、そのサーバーの設定を消す。"""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM nick_prefs WHERE guild_id=?", (guild_id,))

    def nick_pref_guilds(self, user_id: int) -> list:
        """称号をニックネームに付ける設定にしているサーバーのID一覧。"""
        with self._lock:
            return [
                r["guild_id"] for r in self._conn.execute("SELECT guild_id FROM nick_prefs WHERE user_id=?", (user_id,))
            ]

    def nick_pref(self, guild_id: int, user_id: int) -> bool:
        """称号をニックネームに付ける設定を、自分で選んでいるか。"""
        with self._lock:
            return (
                self._conn.execute(
                    "SELECT 1 FROM nick_prefs WHERE guild_id=? AND user_id=? AND enabled=1", (guild_id, user_id)
                ).fetchone()
                is not None
            )

    def profiles_of(self, user_ids) -> dict:
        """{ユーザーID: profile} をまとめて取る（ランキングの称号表示用）。"""
        ids = list(user_ids)
        if not ids:
            return {}
        with self._lock:
            marks = ",".join("?" * len(ids))
            return {
                r["user_id"]: dict(r)
                for r in self._conn.execute(
                    f"SELECT * FROM profile WHERE guild_id=? AND user_id IN ({marks})", [self._g(), *ids]
                )
            }
