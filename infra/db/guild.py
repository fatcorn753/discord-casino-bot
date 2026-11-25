"""サーバーの承認・ランキング掲示・ニックネーム設定。

表の定義は infra/db/schema/tables.py にある。
"""

from collections.abc import Iterable

from domain.ports.bridges import WealthyReader, noop_wealthy_reader
from domain.ports.contracts import GuildStatusRow, LeaderboardRow, ProfileRow
from domain.ports.repos import IGuildRepo
from infra.db.base import EcoDb, ScopedRepo
from infra.db.profile import ProfileCore


class GuildRepo(ScopedRepo, IGuildRepo):
    """サーバーの承認・ランキング掲示・ニックネーム設定。"""

    def __init__(
        self,
        db: EcoDb,
        wealthy_reader: WealthyReader | None = None,
        guild_id: int = 0,
        strict: bool = False,
        *,
        profile: ProfileCore | None = None,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._wealthy_reader: WealthyReader = wealthy_reader or noop_wealthy_reader
        self._profile = profile or ProfileCore(db)

    # ------------------------------------------------------------------ #
    # 利用できるサーバーの承認                                            #
    # ------------------------------------------------------------------ #
    def set_guild_status(self, guild_id: int, status: str, name: str | None = None, by: int | None = None) -> None:
        with self._db.tx():
            self._conn.execute(
                "INSERT INTO guild_status (guild_id, status, name, decided_by, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET status=excluded.status, "
                "name=COALESCE(excluded.name, guild_status.name), decided_by=excluded.decided_by, updated_at=excluded.updated_at",
                (guild_id, status, name, by, self._now()),
            )

    def get_guild_status(self, guild_id: int) -> GuildStatusRow | None:
        with self._lock:
            return self._one("SELECT * FROM guild_status WHERE guild_id=?", (guild_id,))

    def list_guild_status(self) -> list[GuildStatusRow]:
        with self._lock:
            return self._all("SELECT * FROM guild_status ORDER BY updated_at DESC")

    def mark_guild_notified(self, guild_id: int) -> None:
        with self._db.tx():
            self._conn.execute("UPDATE guild_status SET notified_at=? WHERE guild_id=?", (self._now(), guild_id))

    # ------------------------------------------------------------------ #
    # ランキング掲示板（自動で更新されるメッセージ。1サーバーに1つ）       #
    # ------------------------------------------------------------------ #
    def set_leaderboard(self, guild_id: int, channel_id: int, message_id: int) -> None:
        with self._db.tx():
            self._conn.execute(
                "INSERT INTO leaderboards (guild_id, channel_id, message_id) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, message_id=excluded.message_id",
                (guild_id, channel_id, message_id),
            )

    def get_leaderboard(self, guild_id: int) -> LeaderboardRow | None:
        with self._lock:
            return self._one("SELECT * FROM leaderboards WHERE guild_id=?", (guild_id,))

    def list_leaderboards(self) -> list[LeaderboardRow]:
        with self._lock:
            return self._all("SELECT * FROM leaderboards")

    def delete_leaderboard(self, guild_id: int) -> None:
        with self._db.tx():
            self._conn.execute("DELETE FROM leaderboards WHERE guild_id=?", (guild_id,))

    # ------------------------------------------------------------------ #
    # 機能のオン・オフ                                                    #
    # ------------------------------------------------------------------ #
    def features_off(self, guild_id: int) -> set[str]:
        with self._lock:
            return {r["feature"] for r in self._all("SELECT feature FROM features_off WHERE guild_id=?", (guild_id,))}

    def set_feature(self, guild_id: int, feature: str, on: bool) -> None:
        with self._db.tx():
            if on:
                self._conn.execute("DELETE FROM features_off WHERE guild_id=? AND feature=?", (guild_id, feature))
            else:
                self._conn.execute(
                    "INSERT OR IGNORE INTO features_off (guild_id, feature) VALUES (?, ?)", (guild_id, feature)
                )

    def set_nick_pref(self, guild_id: int, user_id: int, enabled: bool) -> None:
        """称号をそのサーバーでのニックネームに付けるか（付けると選んだ人だけ）。"""
        with self._db.tx():
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
        with self._db.tx():
            self._conn.execute(
                "INSERT OR IGNORE INTO nick_prefs (guild_id, user_id, enabled) VALUES (?, ?, 0)", (guild_id, user_id)
            )

    def _drop_idle_nick_row(self, guild_id: int, user_id: int) -> None:
        self._conn.execute(
            "DELETE FROM nick_prefs WHERE guild_id=? AND user_id=? AND enabled=0 AND applied_nick IS NULL",
            (guild_id, user_id),
        )

    def nick_managed(self, guild_id: int, user_id: int) -> bool:
        with self._lock:
            return self._one("SELECT 1 FROM nick_prefs WHERE guild_id=? AND user_id=?", (guild_id, user_id)) is not None

    def status_nick_targets(self) -> list[int]:
        """身分のニックネームを、付ける・外す必要があるかもしれない人（このサーバー）。"""
        with self._lock:
            gid = self._g()
            rows = [
                r["user_id"]
                for r in self._all(
                    "SELECT user_id FROM teiai_debts WHERE guild_id=? AND debt > 0 "
                    "UNION SELECT borrower_id AS user_id FROM loans WHERE guild_id=? AND status='overdue' "
                    "UNION SELECT user_id FROM nick_prefs WHERE guild_id=? AND enabled=0",
                    (gid, gid, gid),
                )
            ]
        return rows + sorted(self._wealthy_reader(gid))

    def get_nick_state(self, guild_id: int, user_id: int) -> tuple[str | None, str] | None:
        with self._lock:
            row = self._one(
                "SELECT original_nick, applied_nick FROM nick_prefs WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            )
        return None if row is None or row["applied_nick"] is None else (row["original_nick"], row["applied_nick"])

    def set_nick_state(self, guild_id: int, user_id: int, original: str | None, applied: str | None) -> None:
        with self._db.tx():
            self._conn.execute(
                "UPDATE nick_prefs SET original_nick=?, applied_nick=? WHERE guild_id=? AND user_id=?",
                (original if applied is not None else None, applied, guild_id, user_id),
            )
            self._drop_idle_nick_row(guild_id, user_id)

    def delete_guild_nick_prefs(self, guild_id: int) -> None:
        with self._db.tx():
            self._conn.execute("DELETE FROM nick_prefs WHERE guild_id=?", (guild_id,))

    def nick_pref_guilds(self, user_id: int) -> list[int]:
        with self._lock:
            return [r["guild_id"] for r in self._all("SELECT guild_id FROM nick_prefs WHERE user_id=?", (user_id,))]

    def nick_pref(self, guild_id: int, user_id: int) -> bool:
        with self._lock:
            return (
                self._one("SELECT 1 FROM nick_prefs WHERE guild_id=? AND user_id=? AND enabled=1", (guild_id, user_id))
                is not None
            )

    def profiles_of(self, user_ids: Iterable[int]) -> dict[int, ProfileRow]:
        """{ユーザーID: profile} をまとめて取る（ランキングの称号表示用）。"""
        ids = list(user_ids)
        if not ids:
            return {}
        with self._lock:
            gid = self._g()
            equip_rows = self._profile.equipment_for_users(gid, ids)
            buff_rows = self._profile.buffs_for_users(gid, ids)
        out: dict[int, ProfileRow] = {}
        for user_id, slot, item_id in equip_rows:
            row = out.setdefault(user_id, self._empty_profile_row(gid, user_id))
            if slot in ("title", "card_color", "card_icon", "skin_dice", "skin_table"):
                row[slot] = item_id
        for user_id, buff, charges in buff_rows:
            row = out.setdefault(user_id, self._empty_profile_row(gid, user_id))
            if buff in ("daily_boost", "insurance"):
                row[buff] = charges
        return out

    def _empty_profile_row(self, guild_id: int, user_id: int) -> ProfileRow:
        return {
            "guild_id": guild_id,
            "user_id": user_id,
            "title": None,
            "card_color": None,
            "card_icon": None,
            "skin_dice": None,
            "skin_table": None,
            "daily_boost": 0,
            "insurance": 0,
        }
