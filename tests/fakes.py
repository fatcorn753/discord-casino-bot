"""discord の型を受け取る関数へ、テスト用のフェイクを渡すための境界。

テストのフェイク（SimpleNamespace や小さなクラス）は、discord の型を名前のうえで継承していない。
そのまま渡すと Pyright が型の不一致として報告するため、型の検査はここで明示的に切り、
テスト本体ではフェイクの属性（送ったメッセージなど）を型付きのまま読めるようにする。
"""

import datetime
from collections.abc import Callable, Coroutine
from types import SimpleNamespace
from typing import Any, TypeVar, cast

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from domain.ports.clock import IClock

Coro = Coroutine[Any, Any, Any]


def invoke(command: "app_commands.Command[Any, ..., Any] | discord.ui.Item[Any]", *args: object) -> Coro:
    """スラッシュコマンド・ボタンの callback を、フェイクの引数で呼ぶ。"""
    callback = cast(Callable[..., Coro], command.callback)
    return callback(*args)


def as_interaction(fake: object) -> "discord.Interaction[Any]":
    return cast("discord.Interaction[Any]", fake)


def as_guild(fake: object) -> discord.Guild:
    return cast(discord.Guild, fake)


def as_member(fake: object) -> discord.Member:
    return cast(discord.Member, fake)


def as_user(fake: object) -> discord.User:
    return cast(discord.User, fake)


def as_channel(fake: object) -> discord.TextChannel:
    return cast(discord.TextChannel, fake)


def as_message(fake: object) -> discord.Message:
    return cast(discord.Message, fake)


def as_choice(fake: object) -> "app_commands.Choice[Any]":
    return cast("app_commands.Choice[Any]", fake)


def as_bot(fake: object) -> commands.Bot:
    return cast(commands.Bot, fake)


_T = TypeVar("_T")


def must(value: _T | None) -> _T:
    """discord の Optional な値（埋め込みの本文など）が、テストの場面では必ずあることを確かめて返す。"""
    assert value is not None
    return value


def fake_response(status: int, reason: str = "x") -> aiohttp.ClientResponse:
    """discord.HTTPException などに渡す、HTTP の応答のフェイク（status と reason だけを持つ）。"""
    return cast(aiohttp.ClientResponse, SimpleNamespace(status=status, reason=reason))


def button(item: object) -> "discord.ui.Button[Any]":
    assert isinstance(item, discord.ui.Button)
    return item


def buttons(view: "discord.ui.View") -> "list[discord.ui.Button[Any]]":
    """画面のボタンだけ（セレクトなどを除く）を、並び順のまま返す。"""
    return [c for c in view.children if isinstance(c, discord.ui.Button)]


def selects(view: "discord.ui.View") -> "list[discord.ui.Select[Any]]":
    return [c for c in view.children if isinstance(c, discord.ui.Select)]


def call(handler: Callable[..., Coro], *args: object) -> Coro:
    """デコレーターで型が合成された非同期ハンドラ（autocomplete など）を、フェイクの引数で呼ぶ。"""
    return handler(*args)


def as_item(fake: object) -> "discord.ui.Item[Any]":
    return cast("discord.ui.Item[Any]", fake)


def freeze_time(monkeypatch: Any, clock: IClock, now: "float | Callable[[], float]") -> None:
    """clock の現在時刻を now（UNIX 秒）に固定する。時差は clock.offset_hours() のまま。

    now に関数を渡すと、呼ばれるたびにその値を使う（テストの途中で時刻を進めるとき）。
    Service・Cog は同じ IClock を共有するので、bot.services.clock を渡せば全体の時刻が変わる。
    リポジトリの内部処理の時刻は、eco.db.clock（make_test_bot では services と同じ時計）を渡す。
    """
    tz = datetime.timezone(datetime.timedelta(hours=clock.offset_hours()))
    read = now if callable(now) else (lambda: now)
    monkeypatch.setattr(clock, "now", lambda: datetime.datetime.fromtimestamp(read(), tz))
