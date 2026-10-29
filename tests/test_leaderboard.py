"""自動で更新されるランキング掲示板。"""

import asyncio
from types import SimpleNamespace

import discord
from discord import app_commands
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
        raise http_error(403)       # 「メッセージ履歴を読む」権限がない状態。掲示板の更新はこれに頼らない

    def get_partial_message(self, mid):
        channel = self

        class Partial:
            async def edit(self, **kw):
                if mid not in channel.messages:
                    raise http_error(404)
                await channel.messages[mid].edit(**kw)

            async def delete(self):
                if mid not in channel.messages:
                    raise http_error(404)
                await channel.messages[mid].delete()
        return Partial()

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
    assert BOARD_INTERVAL_MINUTES == 1


def test_build_embed_ranks_only_the_servers_members(eco):
    eco.credit(1, 9_000, "テスト")
    eco.credit(2, 100, "テスト")
    eco.credit(3, 99_999, "テスト")                     # 別サーバーの人。このサーバーの掲示板には載らない
    play_in_guild(eco, 1)
    play_in_guild(eco, 2)
    e = ranking.build_embed(eco, eco.guild_user_ids(10), "🏆 ペリカランキング", header="最終更新: x\n\n", footer="1分ごと")
    assert e.description.index("<@1>") < e.description.index("<@2>") and "<@3>" not in e.description
    assert e.description.startswith("最終更新: x") and e.footer.text == "1分ごと"


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
    assert "ランキングを掲示しました" in it.sent[-1]["embed"].title and "1分" in it.sent[-1]["embed"].description


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


def test_ranking_shows_the_title_after_the_name(eco):
    """称号は名前の後ろ（ニックネームの「名前【称号】」と同じ順）。"""
    from core import shop
    play_in_guild(eco, 1)
    it = shop.BY_ID["title_zawa"]
    eco.credit(1, 20_000, "テスト")
    assert eco.purchase(1, it.id, shop.label(it), it.price, True).ok
    eco.equip(1, "title", it.id)
    e = ranking.build_embed(eco, eco.guild_user_ids(10), "🏆 テスト")
    assert "<@1>『ざわ…ざわ…』" in e.description and "『ざわ…ざわ…』<@1>" not in e.description


# ----------------------------- 掲示の権限 ----------------------------- #
def _check(command, user_id, manage_guild, owners):
    it = SimpleNamespace(user=SimpleNamespace(id=user_id), permissions=SimpleNamespace(manage_guild=manage_guild),
                         client=SimpleNamespace(config={"owner_ids": owners}))
    return command.checks[0](it)


@pytest.mark.parametrize("command", [EconomyCog.board_set, EconomyCog.board_unset])
def test_board_commands_allow_server_managers_and_bot_owners_only(command):
    assert _check(command, 5, True, [])                 # サーバー管理権限がある
    assert _check(command, 7, False, [7])               # 権限がなくても、Bot の持ち主はどのサーバーでも使える
    with pytest.raises(app_commands.MissingPermissions):
        _check(command, 5, False, [7])                  # どちらでもない


def test_failure_log_is_throttled(capsys):
    from core import logthrottle
    logthrottle._last.clear()
    for _ in range(5):
        logthrottle.warn(("k", 1), "失敗しました")
    logthrottle.warn(("k", 2), "別のサーバーの失敗")
    out = capsys.readouterr().out
    assert out.count("失敗しました") == 1 and "別のサーバーの失敗" in out
