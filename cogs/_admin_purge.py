"""Bot 自身の発言を、チャンネルからまとめて消す処理（`/運営 発言削除` から呼ぶ）。

Bot は自分の発言を消すのに「メッセージの管理」権限は要らない。履歴をたどるのに「メッセージ履歴を読む」権限が要る。
自分の発言は一括削除できないので、1件ずつ消す（Discord の制限に合わせて、discord.py が自動で待つ）。
"""

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import discord

PROGRESS_EVERY = 25  # この件数ごとに、進み具合を知らせる


@dataclass
class PurgeResult:
    scanned: int = 0  # 見た発言の数（Bot 以外の発言も含む）
    deleted: int = 0  # 消せた数
    failed: int = 0  # 消せなかった数（権限や通信のエラー）


def can_read_history(channel, guild: discord.Guild) -> bool:
    """Bot がそのチャンネルの履歴を読めるか（チャンネルを見る・メッセージ履歴を読む）。"""
    perms = channel.permissions_for(guild.me)
    return bool(perms.view_channel and perms.read_message_history)


async def delete_own_messages(
    channel, bot_user_id: int, on_progress: Optional[Callable[[PurgeResult], Awaitable[None]]] = None
) -> PurgeResult:
    """channel の履歴を新しい順にたどり、Bot 自身の発言だけを消す。履歴を読めないとき（discord.Forbidden）は、そのまま例外にする。"""
    result = PurgeResult()
    async for message in channel.history(limit=None):
        result.scanned += 1
        if message.author.id != bot_user_id:
            continue
        try:
            await message.delete()
            result.deleted += 1
        except discord.NotFound:
            continue  # 先に消えていた
        except discord.HTTPException as exc:
            result.failed += 1
            print(f"発言を消せません: channel={getattr(channel, 'id', '?')} message={message.id}: {exc!r}")
        if on_progress is not None and (result.deleted + result.failed) % PROGRESS_EVERY == 0:
            await on_progress(result)
    return result
