"""自動で更新されるランキング掲示板。"""

import asyncio
import logging
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

from cogs.economy import boards
from cogs.economy.cog import BOARD_INTERVAL_MINUTES, EconomyCog
from cogs.shared import ranking
from infra.db.repositories import open_repositories
from tests.fakes import as_guild, fake_response, invoke, must
from tests.support import make_economy_cog, make_ranking_repos, make_test_bot


@pytest.fixture
def eco(tmp_path):
    return open_repositories(str(tmp_path / "t.db"))


def run(coro):
    return asyncio.run(coro)


def http_error(status):
    cls = discord.NotFound if status == 404 else (discord.Forbidden if status == 403 else discord.HTTPException)
    return cls(fake_response(status), "err")


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
        self.messages, self.next_id = {}, 1000
        self.edit_error: discord.HTTPException | None = None
        self._perms = SimpleNamespace(view_channel=perms[0], send_messages=perms[1], embed_links=perms[2])

    def permissions_for(self, _member):
        return self._perms

    async def fetch_message(self, mid):
        raise http_error(403)  # 「メッセージ履歴を読む」権限がない状態。掲示板の更新はこれに頼らない

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
    bot = make_test_bot(repos=eco)
    bot.get_guild = lambda gid: guild if gid == 10 else None
    return make_economy_cog(bot), guild, channel


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
    eco.wallet.balance(uid)


# ----------------------------- 表示 ----------------------------- #
def test_interval_is_30_minutes():
    assert BOARD_INTERVAL_MINUTES == 1


def test_build_embed_ranks_only_the_servers_members(eco):
    eco.wallet.credit(1, 9_000, "テスト")
    eco.wallet.credit(2, 100, "テスト")
    eco.wallet.credit(3, 99_999, "テスト")  # 別サーバーの人。このサーバーの掲示板には載らない
    play_in_guild(eco, 1)
    play_in_guild(eco, 2)
    e = ranking.build_embed(
        make_ranking_repos(eco),
        {1, 2},
        "🏆 ペリカランキング",
        header="最終更新: x\n\n",
        footer="1分ごと",
    )
    text = must(e.description)
    assert text.index("<@1>") < text.index("<@2>") and "<@3>" not in text
    assert text.startswith("最終更新: x") and e.footer.text == "1分ごと"


def test_build_embed_with_nobody_playing(eco):
    e = ranking.build_embed(make_ranking_repos(eco), set(), "🏆 ペリカランキング")
    assert "まだ誰も遊んでいません" in must(e.description)


# ----------------------------- 設定 ----------------------------- #
def test_set_posts_one_message_and_remembers_it(eco):
    cog, guild, ch = make(eco)
    play_in_guild(eco, 1)
    it = FakeInteraction(guild)
    run(invoke(EconomyCog.board_set, cog, it, ch))
    board = eco.guild.get_leaderboard(10)
    assert board["channel_id"] == ch.id and board["message_id"] in ch.messages
    assert "ランキングを掲示しました" in it.sent[-1]["embed"].title and "1分" in it.sent[-1]["embed"].description


def test_setting_again_replaces_the_old_board_so_there_is_only_one(eco):
    cog, guild, ch = make(eco)
    run(invoke(EconomyCog.board_set, cog, FakeInteraction(guild), ch))
    first = eco.guild.get_leaderboard(10)["message_id"]
    run(invoke(EconomyCog.board_set, cog, FakeInteraction(guild), ch))
    second = eco.guild.get_leaderboard(10)["message_id"]
    assert first != second and ch.messages[first].deleted and not ch.messages[second].deleted
    assert len(eco.guild.list_leaderboards()) == 1  # 1サーバーに1つ


def test_set_is_refused_when_the_bot_cannot_post_there(eco):
    cog, guild, _ = make(eco)
    bad = FakeChannel(cid=777, perms=(True, False, True))
    it = FakeInteraction(guild)
    run(invoke(EconomyCog.board_set, cog, it, bad))
    assert "掲示できません" in it.sent[-1]["embed"].title and eco.guild.get_leaderboard(10) is None


def test_unset_stops_and_removes_the_message(eco):
    cog, guild, ch = make(eco)
    run(invoke(EconomyCog.board_set, cog, FakeInteraction(guild), ch))
    mid = eco.guild.get_leaderboard(10)["message_id"]
    run(invoke(EconomyCog.board_set, cog, FakeInteraction(guild), None, True))
    assert eco.guild.get_leaderboard(10) is None and ch.messages[mid].deleted
    it = FakeInteraction(guild)
    run(invoke(EconomyCog.board_set, cog, it, None, True))  # 掲示がないときは案内する
    assert "掲示していません" in it.sent[-1]["embed"].title


# ----------------------------- 更新 ----------------------------- #
def test_refresh_edits_the_same_message_with_latest_ranking(eco):
    cog, guild, ch = make(eco)
    play_in_guild(eco, 1)
    run(invoke(EconomyCog.board_set, cog, FakeInteraction(guild), ch))
    mid = eco.guild.get_leaderboard(10)["message_id"]
    eco.wallet.credit(1, 50_000, "テスト")
    run(
        boards.refresh_board(
            cog.bot, cog._ranking, cog._guild_repo, eco.guild.get_leaderboard(10), BOARD_INTERVAL_MINUTES
        )
    )
    msg = ch.messages[mid]
    assert (
        len(msg.embeds) == 2 and "53,000ペリカ" in msg.embeds[-1].description
    )  # 同じメッセージが、最新の金額に書き換わる
    assert len(ch.messages) == 1  # メッセージは増えない


def test_refresh_reposts_when_the_message_was_deleted(eco):
    cog, guild, ch = make(eco)
    run(invoke(EconomyCog.board_set, cog, FakeInteraction(guild), ch))
    old = eco.guild.get_leaderboard(10)["message_id"]
    del ch.messages[old]  # 誰かが消した
    run(
        boards.refresh_board(
            cog.bot, cog._ranking, cog._guild_repo, eco.guild.get_leaderboard(10), BOARD_INTERVAL_MINUTES
        )
    )
    new = eco.guild.get_leaderboard(10)["message_id"]
    assert new != old and new in ch.messages  # 投稿し直して、IDを更新する


def test_refresh_forgets_the_board_when_the_channel_is_gone(eco):
    cog, guild, ch = make(eco)
    run(invoke(EconomyCog.board_set, cog, FakeInteraction(guild), ch))
    guild.get_channel = lambda cid: None
    run(
        boards.refresh_board(
            cog.bot, cog._ranking, cog._guild_repo, eco.guild.get_leaderboard(10), BOARD_INTERVAL_MINUTES
        )
    )
    assert eco.guild.get_leaderboard(10) is None


def test_refresh_keeps_the_board_when_permissions_are_temporarily_missing(eco):
    cog, guild, ch = make(eco)
    run(invoke(EconomyCog.board_set, cog, FakeInteraction(guild), ch))
    board = eco.guild.get_leaderboard(10)
    ch.edit_error = http_error(403)
    run(
        boards.refresh_board(cog.bot, cog._ranking, cog._guild_repo, board, BOARD_INTERVAL_MINUTES)
    )  # 例外を出さず、次の周期で再試行する
    assert eco.guild.get_leaderboard(10) == board


def test_refresh_does_nothing_when_the_bot_is_not_in_the_guild_right_now(eco):
    cog, guild, ch = make(eco)
    run(invoke(EconomyCog.board_set, cog, FakeInteraction(guild), ch))
    cog.bot.get_guild = lambda gid: None
    run(
        boards.refresh_board(
            cog.bot, cog._ranking, cog._guild_repo, eco.guild.get_leaderboard(10), BOARD_INTERVAL_MINUTES
        )
    )
    assert eco.guild.get_leaderboard(10) is not None


def test_one_failing_server_does_not_stop_the_others(eco, monkeypatch):
    cog, guild, ch = make(eco)
    eco.guild.set_leaderboard(99, 1, 1)
    eco.guild.set_leaderboard(10, ch.id, 5)
    calls = []

    async def fake_refresh(bot, ranking_repos, guild_repo, board, interval_minutes):
        calls.append(board["guild_id"])
        if board["guild_id"] == 99:
            raise RuntimeError("壊れた掲示板")

    monkeypatch.setattr(boards, "refresh_board", fake_refresh)
    run(EconomyCog.refresh_boards.coro(cog))
    assert sorted(calls) == [10, 99]


def test_leaving_a_server_removes_its_board(eco):
    cog, guild, ch = make(eco)
    eco.guild.set_leaderboard(10, ch.id, 5)
    run(cog.on_guild_remove(as_guild(SimpleNamespace(id=10))))
    assert eco.guild.get_leaderboard(10) is None


def test_board_storage_roundtrip_and_replace(eco):
    eco.guild.set_leaderboard(10, 1, 2)
    eco.guild.set_leaderboard(10, 3, 4)  # 同じサーバーは上書き（1つだけ）
    assert eco.guild.get_leaderboard(10) == {"guild_id": 10, "channel_id": 3, "message_id": 4}
    assert len(eco.guild.list_leaderboards()) == 1


def test_ranking_does_not_show_titles_to_keep_each_row_on_one_line(eco):
    from domain import shop

    play_in_guild(eco, 1)
    it = shop.BY_ID["title_zawa"]
    eco.wallet.credit(1, 20_000, "テスト")
    assert eco.shop.purchase(1, it.id, shop.label(it), it.price, True).ok
    eco.shop.equip(1, "title", it.id)
    e = ranking.build_embed(make_ranking_repos(eco), {1}, "🏆 テスト")
    assert "ざわ…ざわ…" not in must(e.description) and "<@1>　" in must(e.description)


# ----------------------------- 掲示の権限 ----------------------------- #
def _check(command, user_id, manage_guild, owners):
    it = SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        permissions=SimpleNamespace(manage_guild=manage_guild),
        client=SimpleNamespace(config={"owner_ids": owners}),
    )
    return command.checks[0](it)


@pytest.mark.parametrize("command", [EconomyCog.board_set])
def test_board_commands_allow_server_managers_and_bot_owners_only(command):
    assert _check(command, 5, True, [])  # サーバー管理権限がある
    assert _check(command, 7, False, [7])  # 権限がなくても、Bot の持ち主はどのサーバーでも使える
    with pytest.raises(app_commands.MissingPermissions):
        _check(command, 5, False, [7])  # どちらでもない


def test_failure_log_is_throttled(caplog):
    from cogs.shared import logthrottle

    logthrottle._last.clear()
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            logthrottle.warn(("k", 1), "失敗しました")
        logthrottle.warn(("k", 2), "別のサーバーの失敗")
    out = caplog.text
    assert out.count("失敗しました") == 1 and "別のサーバーの失敗" in out
