"""discord_posts テーブルの単一書込ヘルパー（全 9 ドメインの Discord 投稿メタデータ所有権）。"""

import sqlite3

_VALID_ENTITY_TYPES = frozenset(
    {
        "stream",
        "horse_race",
        "market_listing",
        "kyosai_motion",
        "quiz",
        "raffle",
        "poll",
        "loan",
        "manual_event",
    }
)


def _check_entity(entity_type: str) -> None:
    if entity_type not in _VALID_ENTITY_TYPES:
        raise ValueError(f"不正なエンティティ種別です: {entity_type!r}")


def init_discord_post(conn: sqlite3.Connection, entity_type: str, entity_id: int, channel_id: int) -> None:
    """投稿先の初期チャンネルを記録する（channel_id <= 0 なら行を置かず削除する）。"""
    _check_entity(entity_type)
    if channel_id <= 0:
        conn.execute("DELETE FROM discord_posts WHERE entity_type=? AND entity_id=?", (entity_type, entity_id))
        return
    conn.execute(
        "INSERT INTO discord_posts (entity_type, entity_id, channel_id, message_id, post_done) "
        "VALUES (?, ?, ?, 0, 0) "
        "ON CONFLICT(entity_type, entity_id) DO UPDATE SET "
        "channel_id=excluded.channel_id, message_id=0, post_done=0",
        (entity_type, entity_id, channel_id),
    )


def set_discord_message(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: int,
    message_id: int,
    channel_id: int | None = None,
) -> None:
    """投稿メッセージ ID（および必要に応じてチャンネル ID）を記録・更新する。"""
    _check_entity(entity_type)
    if channel_id is None:
        cur = conn.execute(
            "UPDATE discord_posts SET message_id=? WHERE entity_type=? AND entity_id=?",
            (message_id, entity_type, entity_id),
        )
        if not cur.rowcount:
            conn.execute(
                "INSERT INTO discord_posts (entity_type, entity_id, channel_id, message_id, post_done) "
                "VALUES (?, ?, 0, ?, 0)",
                (entity_type, entity_id, message_id),
            )
        return
    conn.execute(
        "INSERT INTO discord_posts (entity_type, entity_id, channel_id, message_id, post_done) "
        "VALUES (?, ?, ?, ?, 0) "
        "ON CONFLICT(entity_type, entity_id) DO UPDATE SET "
        "channel_id=excluded.channel_id, message_id=excluded.message_id",
        (entity_type, entity_id, channel_id, message_id),
    )


def mark_discord_post_done(conn: sqlite3.Connection, entity_type: str, entity_id: int) -> None:
    """告知の最終書き換えが完了したことを記録する。"""
    _check_entity(entity_type)
    conn.execute(
        "UPDATE discord_posts SET post_done=1 WHERE entity_type=? AND entity_id=?",
        (entity_type, entity_id),
    )


def delete_discord_post(conn: sqlite3.Connection, entity_type: str, entity_id: int) -> None:
    """削除されたエンティティに対応する投稿記録を削除する。"""
    _check_entity(entity_type)
    conn.execute("DELETE FROM discord_posts WHERE entity_type=? AND entity_id=?", (entity_type, entity_id))
