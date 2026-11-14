"""`/運営 発言削除`: Bot 自身の発言だけを、指定チャンネルからすべて消す。"""

import asyncio
from types import SimpleNamespace

import discord
import pytest

from cogs.admin.cog import ApprovalCog
from cogs.admin.purge import PROGRESS_EVERY, can_read_history, delete_own_messages
from cogs.admin.views import PurgeConfirm
from infra.db.economy import Economy

ME, OWNER, FRIEND, OTHER = 99, 1, 2, 7


def not_found():
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")


def http_error():
    return discord.HTTPException(SimpleNamespace(status=500, reason="Server Error"), "boom")


class Message:
    def __init__(self, mid, author, fail=None):
        self.id, self.author, self.fail, self.deleted = mid, SimpleNamespace(id=author), fail, False

    async def delete(self):
        if self.fail is not None:
            raise self.fail
        self.deleted = True


class Channel:
    """履歴に messages（新しい順）を持つ、偽のチャンネル。"""

    def __init__(self, messages, history_error=None, readable=True, cid=10):
        self.id, self.name, self.messages, self.history_error, self.readable = (
            cid,
            "general",
            messages,
            history_error,
            readable,
        )
        self.mention = "#general"

    def permissions_for(self, _me):
        return SimpleNamespace(
            view_channel=True, send_messages=True, embed_links=True, read_message_history=self.readable
        )

    async def send(self, **kw):  # 投稿先として選べるチャンネルの条件（_pick_channel が見る）
        return SimpleNamespace(id=1)

    async def history(self, limit=None):
        if self.history_error is not None:
            raise self.history_error
        for message in self.messages:
            yield message


def run(coro):
    return asyncio.run(coro)


def test_only_the_bots_own_messages_are_deleted():
    msgs = [Message(1, ME), Message(2, OTHER), Message(3, ME), Message(4, OTHER), Message(5, ME)]
    result = run(delete_own_messages(Channel(msgs), ME))
    assert [m.deleted for m in msgs] == [True, False, True, False, True]  # 他の人の発言には触れない
    assert (result.scanned, result.deleted, result.failed) == (5, 3, 0)


def test_already_gone_messages_are_skipped_and_other_errors_are_counted():
    msgs = [Message(1, ME, fail=not_found()), Message(2, ME, fail=http_error()), Message(3, ME)]
    result = run(delete_own_messages(Channel(msgs), ME))
    assert (result.deleted, result.failed) == (1, 1) and msgs[2].deleted  # 1件の失敗で、残りを止めない


def test_progress_is_reported_every_few_deletions():
    msgs = [Message(i, ME) for i in range(PROGRESS_EVERY * 2 + 3)]
    seen = []

    async def progress(result):
        seen.append(result.deleted)

    run(delete_own_messages(Channel(msgs), ME, progress))
    assert seen == [PROGRESS_EVERY, PROGRESS_EVERY * 2]


def test_missing_history_permission_surfaces_as_forbidden():
    channel = Channel([], history_error=discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no"))
    with pytest.raises(discord.Forbidden):
        run(delete_own_messages(channel, ME))


def test_can_read_history_needs_view_and_history_permissions():
    guild = SimpleNamespace(me=object())
    assert can_read_history(Channel([], readable=True), guild) and not can_read_history(
        Channel([], readable=False), guild
    )


# ----------------------------- コマンドと確認ボタン ----------------------------- #
class Interaction:
    def __init__(self, uid=OWNER, namespace=None):
        self.user = SimpleNamespace(id=uid, send=self._dm)
        self.guild_id = None
        self.namespace = namespace or SimpleNamespace()
        self.log, self.edits, self.dms = [], [], []
        outer = self

        class Response:
            async def send_message(self, **kw):
                outer.log.append(("send", (kw.get("embed") or kw["embeds"][0]).title, kw.get("view")))

            async def edit_message(self, **kw):
                outer.edits.append(("edit", kw["embeds"][0].title))

        self.response = Response()

    async def _dm(self, **kw):
        self.dms.append(kw["embed"].title)

    async def edit_original_response(self, **kw):
        self.edits.append(("original", kw["embeds"][0].title))


def make_cog(tmp_path, channel):
    guild = SimpleNamespace(
        id=5, name="鯖5", me=object(), text_channels=[channel], system_channel=channel, get_channel=lambda cid: channel
    )
    bot = SimpleNamespace(
        config={"owner_ids": [OWNER]},
        economy=Economy(str(tmp_path / "t.db")),
        guilds=[guild],
        get_guild=lambda gid: guild if gid == 5 else None,
        get_cog=lambda name: None,
        user=SimpleNamespace(id=ME),
    )
    return ApprovalCog(bot)


def test_the_command_is_owner_only_and_asks_for_confirmation_before_deleting(tmp_path):
    channel = Channel([Message(1, ME)])
    cog = make_cog(tmp_path, channel)
    stranger = Interaction(FRIEND)
    run(cog.purge_own_messages.callback(cog, stranger, "5", "10"))
    assert "権限" in stranger.log[0][1] or "使えません" in stranger.log[0][1]  # 持ち主以外は、断られる
    owner = Interaction(OWNER)
    run(cog.purge_own_messages.callback(cog, owner, "5", "10"))
    assert owner.log[0][1] == "🧹 発言の削除（確認）" and isinstance(owner.log[0][2], PurgeConfirm)
    assert channel.messages[0].deleted is False  # 確認のボタンを押すまで、何も消さない


def test_the_command_refuses_a_channel_where_the_history_cannot_be_read(tmp_path):
    channel = Channel([Message(1, ME)], readable=False)
    cog = make_cog(tmp_path, channel)
    owner = Interaction(OWNER)
    run(cog.purge_own_messages.callback(cog, owner, "5", "10"))
    assert owner.log[0][1] == "❌ 履歴を読めません" and channel.messages[0].deleted is False


def click(view, name):
    button = next(c for c in view.children if c.label == name)
    return button.callback


def test_confirm_deletes_only_after_the_owner_presses_the_button(tmp_path):
    msgs = [Message(1, ME), Message(2, OTHER), Message(3, ME)]
    channel = Channel(msgs)
    cog = make_cog(tmp_path, channel)
    view = PurgeConfirm(cog, OWNER, channel)
    assert run(view.interaction_check(Interaction(FRIEND))) is False and run(view.interaction_check(Interaction(OWNER)))
    it = Interaction(OWNER)
    run(click(view, "すべて消す")(it))
    assert [m.deleted for m in msgs] == [True, False, True]
    assert it.edits[0] == ("edit", "🧹 消しています…") and it.edits[-1] == ("original", "✅ 🧹 発言を消しました")
    assert cog.purging == set()  # 終わったら、実行中の印を外す


def test_cancel_deletes_nothing(tmp_path):
    channel = Channel([Message(1, ME)])
    cog = make_cog(tmp_path, channel)
    it = Interaction(OWNER)
    run(click(PurgeConfirm(cog, OWNER, channel), "やめる")(it))
    assert channel.messages[0].deleted is False and it.edits == [("edit", "🧹 発言の削除")]


def test_the_same_channel_cannot_be_purged_twice_at_once(tmp_path):
    channel = Channel([Message(1, ME)])
    cog = make_cog(tmp_path, channel)
    cog.purging.add(channel.id)
    it = Interaction(OWNER)
    run(click(PurgeConfirm(cog, OWNER, channel), "すべて消す")(it))
    assert it.edits == [("edit", "❌ 実行中です")] and channel.messages[0].deleted is False


def test_forbidden_history_is_explained_and_the_busy_mark_is_cleared(tmp_path):
    channel = Channel([], history_error=discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no"))
    cog = make_cog(tmp_path, channel)
    it = Interaction(OWNER)
    run(click(PurgeConfirm(cog, OWNER, channel), "すべて消す")(it))
    assert it.edits[-1] == ("original", "❌ 履歴を読めません") and cog.purging == set()


def test_when_the_screen_cannot_be_updated_the_result_goes_to_the_owners_dm(tmp_path):
    channel = Channel([Message(1, ME)])
    cog = make_cog(tmp_path, channel)
    it = Interaction(OWNER)

    async def expired(**kw):
        raise http_error()

    it.edit_original_response = expired  # 15分を過ぎて、応答を書き換えられない
    run(click(PurgeConfirm(cog, OWNER, channel), "すべて消す")(it))
    assert it.dms == ["✅ 🧹 発言を消しました"] and channel.messages[0].deleted
