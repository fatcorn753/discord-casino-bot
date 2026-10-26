"""自動で更新されるランキング掲示板。"""

import asyncio
from types import SimpleNamespace

import discord
import pytest

from cogs.economy_cog import BOARD_INTERVAL_MINUTES, EconomyCog
from core import ranking
from core.economy import Economy
from core.session import Sessions


@pytest.fixture
def eco(tmp_path):
    return Economy(str(tmp_path / "t.db"))


def run(coro):
    return asyncio.run(coro)


def http_error(status):
    cls = discord.NotFound if status == 404 else (discord.Forbidden if status == 403 else discord.HTTPException)
    return cls(SimpleNamespace(status=status, reason="x"), "err")


class FakeMessage:
    def __init__(self, mid, channel):
        self.id, self.channel = mid, channel
        self.jump_url = f"https://discord.com/channels/10/{channel.id}/{mid}"
        self.embeds, self.deleted = [], False

    async def edit(self, **kw):
        if self.channel.edit_error:
            raise self.channel.edit_error
        self.embeds.append(kw["embed"])

    async def delete(self):
        self.deleted = True


class FakeChannel:
    def __init__(self, cid=500, perms=(True, True, True)):
        self.id, self.mention = cid, f"<#{cid}>"
        self.messages, self.next_id, self.edit_error = {}, 1000, None
        self._perms = SimpleNamespace(view_channel=perms[0], send_messages=perms[1], embed_links=perms[2])

    def permissions_for(self, _member):
        return self._perms

    async def fetch_message(self, mid):
        if mid not in self.messages:
            raise http_error(404)
        return self.messages[mid]

    async def send(self, **kw):
        self.next_id += 1
        msg = FakeMessage(self.next_id, self)
        msg.embeds.append(kw["embed"])
        self.messages[msg.id] = msg
        return msg


def make(eco, channel=None):
    channel = channel or FakeChannel()
    guild = SimpleNamespace(id=10, me=object(), get_channel=lambda cid: channel if cid == channel.id else None)
    bot = SimpleNamespace(economy=eco, sessions=Sessions(cooldown=0), config={}, get_guild=lambda gid: guild if gid == 10 else None)
    return EconomyCog(bot), guild, channel


class FakeInteraction:
    def __init__(self, guild):
        self.guild, self.user = guild, SimpleNamespace(id=1)
        self.sent = []
        self.response = SimpleNamespace(send_message=self._send, defer=self._defer)
        self.followup = SimpleNamespace(send=self._send)

    async def _send(self, *a, **kw):
        self.sent.append(kw)

    async def _defer(self, **kw):
        return None


def play_in_guild(eco, uid, guild_id=10):
    eco.touch_guild_user(guild_id, uid)
    eco.balance(uid)


# ----------------------------- 表示 ----------------------------- #
def test_interval_is_30_minutes():
    assert BOARD_INTERVAL_MINUTES == 30


def test_build_embed_ranks_only_the_servers_members(eco):
    eco.credit(1, 9_000, "テスト")
    eco.credit(2, 100, "テスト")
    eco.credit(3, 99_999, "テスト")                     # 別サーバーの人。このサーバーの掲示板には載らない
    play_in_guild(eco, 1)
    play_in_guild(eco, 2)
    e = ranking.build_embed(eco, eco.guild_user_ids(10), "🏆 ペリカランキング", header="最終更新: x\n\n", footer="30分ごと")
    assert e.description.index("<@1>") < e.description.index("<@2>") and "<@3>" not in e.description
    assert e.description.startswith("最終更新: x") and e.footer.text == "30分ごと"


def test_build_embed_with_nobody_playing(eco):
    e = ranking.build_embed(eco, set(), "🏆 ペリカランキング")
    assert "まだ誰も遊んでいません" in e.description


# ----------------------------- 設定 ----------------------------- #
def test_set_posts_one_message_and_remembers_it(eco):
    cog, guild, ch = make(eco)
    play_in_guild(eco, 1)
    it = FakeInteraction(guild)
    run(EconomyCog.board_set.callback(cog, it, ch))
    board = eco.get_leaderboard(10)
    assert board["channel_id"] == ch.id and board["message_id"] in ch.messages
    assert "ランキングを掲示しました" in it.sent[-1]["embed"].title and "30分" in it.sent[-1]["embed"].description


def test_setting_again_replaces_the_old_board_so_there_is_only_one(eco):
    cog, guild, ch = make(eco)
    run(EconomyCog.board_set.callback(cog, FakeInteraction(guild), ch))
    first = eco.get_leaderboard(10)["message_id"]
    run(EconomyCog.board_set.callback(cog, FakeInteraction(guild), ch))
    second = eco.get_leaderboard(10)["message_id"]
    assert first != second and ch.messages[first].deleted and not ch.messages[second].deleted
    assert len(eco.list_leaderboards()) == 1                                    # 1サーバーに1つ


def test_set_is_refused_when_the_bot_cannot_post_there(eco):
    cog, guild, _ = make(eco)
    bad = FakeChannel(cid=777, perms=(True, False, True))
    it = FakeInteraction(guild)
    run(EconomyCog.board_set.callback(cog, it, bad))
    assert "掲示できません" in it.sent[-1]["embed"].title and eco.get_leaderboard(10) is None


def test_unset_stops_and_removes_the_message(eco):
    cog, guild, ch = make(eco)
    run(EconomyCog.board_set.callback(cog, FakeInteraction(guild), ch))
    mid = eco.get_leaderboard(10)["message_id"]
    run(EconomyCog.board_unset.callback(cog, FakeInteraction(guild)))
    assert eco.get_leaderboard(10) is None and ch.messages[mid].deleted
    it = FakeInteraction(guild)
    run(EconomyCog.board_unset.callback(cog, it))                               # 掲示がないときは案内する
    assert "掲示していません" in it.sent[-1]["embed"].title


def test_set_and_unset_require_manage_guild(eco):
    """コマンドに「サーバー管理」権限の検査がついている。"""
    from discord import app_commands
    for cmd in (EconomyCog.board_set, EconomyCog.board_unset):
        assert any("has_permissions" in repr(c) for c in cmd.checks), cmd.name
    assert app_commands  # noqa


# ----------------------------- 更新 ----------------------------- #
def test_refresh_edits_the_same_message_with_latest_ranking(eco):
    cog, guild, ch = make(eco)
    play_in_guild(eco, 1)
    run(EconomyCog.board_set.callback(cog, FakeInteraction(guild), ch))
    mid = eco.get_leaderboard(10)["message_id"]
    eco.credit(1, 50_000, "テスト")
    run(cog._refresh_board(eco.get_leaderboard(10)))
    msg = ch.messages[mid]
    assert len(msg.embeds) == 2 and "53,000ペリカ" in msg.embeds[-1].description         # 同じメッセージが、最新の金額に書き換わる
    assert len(ch.messages) == 1                                                         # メッセージは増えない


def test_refresh_reposts_when_the_message_was_deleted(eco):
    cog, guild, ch = make(eco)
    run(EconomyCog.board_set.callback(cog, FakeInteraction(guild), ch))
    old = eco.get_leaderboard(10)["message_id"]
    del ch.messages[old]                                                                  # 誰かが消した
    run(cog._refresh_board(eco.get_leaderboard(10)))
    new = eco.get_leaderboard(10)["message_id"]
    assert new != old and new in ch.messages                                              # 投稿し直して、IDを更新する


def test_refresh_forgets_the_board_when_the_channel_is_gone(eco):
    cog, guild, ch = make(eco)
    run(EconomyCog.board_set.callback(cog, FakeInteraction(guild), ch))
    guild.get_channel = lambda cid: None
    run(cog._refresh_board(eco.get_leaderboard(10)))
    assert eco.get_leaderboard(10) is None


def test_refresh_keeps_the_board_when_permissions_are_temporarily_missing(eco):
    cog, guild, ch = make(eco)
    run(EconomyCog.board_set.callback(cog, FakeInteraction(guild), ch))
    board = eco.get_leaderboard(10)
    ch.edit_error = http_error(403)
    run(cog._refresh_board(board))                                                        # 例外を出さず、次の周期で再試行する
    assert eco.get_leaderboard(10) == board


def test_refresh_does_nothing_when_the_bot_is_not_in_the_guild_right_now(eco):
    cog, guild, ch = make(eco)
    run(EconomyCog.board_set.callback(cog, FakeInteraction(guild), ch))
    cog.bot.get_guild = lambda gid: None
    run(cog._refresh_board(eco.get_leaderboard(10)))
    assert eco.get_leaderboard(10) is not None


def test_one_failing_server_does_not_stop_the_others(eco):
    cog, guild, ch = make(eco)
    eco.set_leaderboard(99, 1, 1)
    eco.set_leaderboard(10, ch.id, 5)
    calls = []

    async def fake_refresh(board):
        calls.append(board["guild_id"])
        if board["guild_id"] == 99:
            raise RuntimeError("壊れた掲示板")
    cog._refresh_board = fake_refresh
    run(EconomyCog.refresh_boards.coro(cog))
    assert sorted(calls) == [10, 99]


def test_leaving_a_server_removes_its_board(eco):
    cog, guild, ch = make(eco)
    eco.set_leaderboard(10, ch.id, 5)
    run(cog.on_guild_remove(SimpleNamespace(id=10)))
    assert eco.get_leaderboard(10) is None


def test_board_storage_roundtrip_and_replace(eco):
    eco.set_leaderboard(10, 1, 2)
    eco.set_leaderboard(10, 3, 4)                    # 同じサーバーは上書き（1つだけ）
    assert eco.get_leaderboard(10) == {"guild_id": 10, "channel_id": 3, "message_id": 4}
    assert len(eco.list_leaderboards()) == 1
