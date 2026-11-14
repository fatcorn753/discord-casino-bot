"""告知のサーバー・チャンネル指定: 候補、検証、お知らせ・抽選・イベント・クイズが指定先に出ること。"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from cogs.admin.cog import AnnounceModal, ApprovalCog, postable
from cogs.event import cog as event_cog
from cogs.event.cog import EventCog
from cogs.quiz.cog import QuizCog
from cogs.raffle.cog import RaffleCog
from infra.db.economy import Economy
from services.session import Sessions

OWNER, FRIEND = 1, 2


class Chan:
    def __init__(self, cid, name, ok=True):
        self.id, self.name, self.sent, self._ok, self.mention = cid, name, [], ok, f"#{name}"

    def permissions_for(self, _me):
        return SimpleNamespace(view_channel=self._ok, send_messages=self._ok, embed_links=self._ok)

    async def send(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(id=9000 + len(self.sent))

    def get_partial_message(self, mid):
        async def delete():
            self.deleted = getattr(self, "deleted", []) + [mid]

        return SimpleNamespace(delete=delete)


def make_guild(gid=5, name="鯖5"):
    chans = {10: Chan(10, "general"), 11: Chan(11, "クイズ部屋"), 12: Chan(12, "見えない", ok=False)}
    guild = SimpleNamespace(
        id=gid,
        name=name,
        me=object(),
        text_channels=list(chans.values()),
        system_channel=chans[10],
        get_channel=lambda cid: chans.get(cid),
    )
    return guild, chans


def make_admin(tmp_path, guilds, cogs=None):
    eco = Economy(str(tmp_path / "t.db"))
    by_id = {g.id: g for g in guilds}
    bot = SimpleNamespace(
        config={"owner_ids": [OWNER]},
        economy=eco,
        guilds=list(guilds),
        get_guild=lambda gid: by_id.get(gid),
        get_cog=lambda n: (cogs or {}).get(n),
    )
    return ApprovalCog(bot), eco, bot


def dm(uid, log, namespace=None):
    class R:
        async def send_message(self, **kw):
            log.append(("send", kw["embed"].title, kw["embed"].description))

        async def defer(self, **kw):
            pass

        async def send_modal(self, modal):
            log.append(("modal", modal))

    class F:
        async def send(self, **kw):
            log.append(("follow", (kw.get("embed") or kw["embeds"][0]).title))

    return SimpleNamespace(
        user=SimpleNamespace(id=uid),
        guild_id=None,
        response=R(),
        followup=F(),
        namespace=namespace or SimpleNamespace(),
    )


def test_postable_needs_view_send_and_embed_permissions():
    guild, chans = make_guild()
    assert postable(chans[10], guild) and not postable(chans[12], guild)


def test_channel_autocomplete_lists_only_postable_channels_of_the_chosen_guild_for_the_owner(tmp_path):
    guild, chans = make_guild()
    cog, eco, bot = make_admin(tmp_path, [guild])
    inter = dm(OWNER, [], SimpleNamespace(**{"サーバー": "5"}))
    names = lambda current: [c.name for c in asyncio.run(cog.channel_autocomplete(inter, current))]  # noqa: E731
    assert names("") == ["#general", "#クイズ部屋"]  # 投稿できないチャンネルは出ない
    assert names("クイズ") == ["#クイズ部屋"] and names("zzz") == []
    assert [c.value for c in asyncio.run(cog.channel_autocomplete(inter, "クイズ"))] == ["11"]
    assert (
        asyncio.run(cog.channel_autocomplete(dm(FRIEND, [], SimpleNamespace(**{"サーバー": "5"})), "")) == []
    )  # 持ち主以外には出さない
    assert asyncio.run(cog.channel_autocomplete(dm(OWNER, []), "")) == []  # サーバー未選択
    assert (
        asyncio.run(cog.channel_autocomplete(dm(OWNER, [], SimpleNamespace(guild_id="5")), "")) != []
    )  # 引数名の形でも読める


def test_pick_channel_validates(tmp_path):
    guild, chans = make_guild()
    cog, eco, bot = make_admin(tmp_path, [guild])
    log = []
    pick = lambda guild_, cid: asyncio.run(cog._pick_channel(dm(OWNER, log), guild_, cid))  # noqa: E731
    assert pick(guild, None) == (True, None) and pick(None, None) == (True, None)
    assert pick(guild, "11") == (True, chans[11])
    assert pick(None, "11") == (False, None) and "サーバー" in log[-1][2]  # サーバーなしでチャンネルだけは不可
    assert pick(guild, "999") == (False, None) and "見つかりません" in log[-1][2]
    assert pick(guild, "abc") == (False, None)
    assert pick(guild, "12") == (False, None) and "権限" in log[-1][2]  # 投稿できないチャンネル


def run_announce(cog, guilds, channel, image=None):
    shown = {}

    class F:
        async def send(self, **kw):
            shown.update(kw)

    class R:
        async def defer(self, **kw):
            pass

        async def edit_message(self, **kw):
            pass

    edits = []

    async def edit_original(**kw):
        edits.append(kw["embed"].description)

    inter = SimpleNamespace(
        user=SimpleNamespace(id=OWNER, display_name="持ち主"),
        response=R(),
        followup=F(),
        edit_original_response=edit_original,
    )
    modal = AnnounceModal(cog, guilds, "", image, channel)
    modal.body._value = "本文"
    asyncio.run(modal.on_submit(inter))
    asyncio.run(shown["view"].send.callback(inter))
    return shown, edits


def test_announce_goes_to_the_chosen_channel_only(tmp_path, monkeypatch):
    guild, chans = make_guild()
    other, other_chans = make_guild(6, "鯖6")
    cog, eco, bot = make_admin(tmp_path, [guild, other])
    monkeypatch.setattr("cogs.admin.views.announce_channel", lambda g, e: (chans if g.id == 5 else other_chans)[10])
    shown, edits = run_announce(cog, [guild], chans[11])
    assert "#クイズ部屋" in shown["embeds"][0].description  # プレビューに送り先が出る
    assert len(chans[11].sent) == 1 and chans[10].sent == [] and other_chans[10].sent == []
    run_announce(cog, [guild, other], None)  # 指定なしなら、これまでどおり各サーバーの告知チャンネル
    assert len(chans[10].sent) == 1 and len(other_chans[10].sent) == 1


def test_command_requires_a_guild_with_a_channel_and_passes_it_on(tmp_path, monkeypatch):
    guild, chans = make_guild()
    cog, eco, bot = make_admin(tmp_path, [guild])
    monkeypatch.setattr("cogs.admin.cog.approval.is_allowed", lambda *a: True)
    log = []
    asyncio.run(cog.announce.callback(cog, dm(OWNER, log), None, None, None, "11"))  # サーバーなしでチャンネルだけ
    assert log[-1][0] == "send" and "チャンネルを使えません" in log[-1][1]
    asyncio.run(cog.announce.callback(cog, dm(OWNER, log), "5", None, None, "11"))
    kind, modal = log[-1]
    assert kind == "modal" and modal.channel is chans[11] and modal.guilds == [guild]
    asyncio.run(cog.announce.callback(cog, dm(OWNER, log), "5", None, None, "12"))  # 投稿できないチャンネル
    assert "権限" in log[-1][2]


# ----------------------------- 各機能が指定先に出す ----------------------------- #
def test_raffle_host_and_repost_use_the_chosen_channel(tmp_path):
    guild, chans = make_guild()
    eco = Economy(str(tmp_path / "t.db"))
    bot = SimpleNamespace(guilds=[guild], config={}, economy=eco)
    cog = RaffleCog(bot)
    ok, text = asyncio.run(cog.host(guild, OWNER, 1000, 30, chans[11]))
    r = eco.raffle_get()
    assert ok and r["channel_id"] == 11 and len(chans[11].sent) == 1 and chans[10].sent == [] and "#クイズ部屋" in text
    ok, text = asyncio.run(cog.repost(guild, chans[10]))  # 別のチャンネルへ出し直す
    assert ok and eco.raffle_get()["channel_id"] == 10 and len(chans[10].sent) == 1
    assert chans[11].deleted == [9001]  # 古い投稿は元のチャンネルから消える


def test_manual_event_announces_start_and_end_in_the_chosen_channel(tmp_path, monkeypatch):
    guild, chans = make_guild()
    eco = Economy(str(tmp_path / "t.db"))
    bot = SimpleNamespace(guilds=[guild], config={}, economy=eco, sessions=Sessions(cooldown=0))
    monkeypatch.setattr(event_cog.approval, "is_allowed", lambda *a: True)
    cog = EventCog(bot)
    asyncio.run(cog.start_manual(guild, "スロット", 80, 45, chans[11]))
    assert len(chans[11].sent) == 1 and chans[10].sent == [] and eco.manual_event_get(time.time())["channel_id"] == 11
    asyncio.run(cog.stop_manual(guild))
    assert (
        len(chans[11].sent) == 2 and "終了" in chans[11].sent[1]["embed"].title and chans[10].sent == []
    )  # 終了も同じチャンネル
    asyncio.run(cog.start_manual(guild, "ルーレット", 50, 30))  # 指定なしは告知チャンネル（system_channel）
    assert len(chans[10].sent) == 1 and eco.manual_event_get(time.time())["channel_id"] == 0


def test_old_manual_event_table_gets_channel_id(tmp_path):
    import sqlite3

    path = str(tmp_path / "t.db")
    Economy(path)
    c = sqlite3.connect(path)
    c.executescript("""DROP TABLE manual_event; CREATE TABLE manual_event (guild_id INTEGER PRIMARY KEY, game TEXT NOT NULL, boost INTEGER NOT NULL,
        starts REAL NOT NULL, ends REAL NOT NULL, ended INTEGER NOT NULL DEFAULT 0); INSERT INTO manual_event VALUES (0, 'ルーレット', 50, 0, 9e12, 0);""")
    c.commit()
    c.close()
    assert Economy(path).manual_event_get(100)["channel_id"] == 0


@pytest.mark.real_scope
def test_quiz_broadcast_can_target_one_guild_and_channel(tmp_path, monkeypatch):
    g1, c1 = make_guild(5, "鯖5")
    g2, c2 = make_guild(6, "鯖6")
    eco = Economy(str(tmp_path / "t.db"))
    qid = eco.quiz_add("問", ["a", "b", "c", "d"], "", 100, None, 0.0)
    bot = SimpleNamespace(
        guilds=[g1, g2],
        config={},
        economy=SimpleNamespace(quiz_get=eco.quiz_get, for_guild=lambda gid: eco, is_approved=lambda *a: True),
    )
    cog = QuizCog(bot)
    monkeypatch.setattr("cogs.quiz.cog.approval.is_allowed", lambda e, c, gid, uid: gid == 5)  # 6 は許可されていない
    monkeypatch.setattr("cogs.quiz.cog.announce_channel", lambda g, e: (c1 if g.id == 5 else c2)[10])
    assert (
        asyncio.run(cog.broadcast(qid)) == 1 and len(c1[10].sent) == 1 and c2[10].sent == []
    )  # 既定: 許可されたサーバーの告知チャンネル
    assert asyncio.run(cog.broadcast(qid, g1, c1[11])) == 1 and len(c1[11].sent) == 1  # サーバー + チャンネル
    assert asyncio.run(cog.broadcast(qid, g2)) == 1 and len(c2[10].sent) == 1  # 指定したサーバーには、許可外でも出す
    assert asyncio.run(cog.broadcast(qid, g2, c2[11])) == 1 and len(c2[11].sent) == 1
    modal = cog.add_modal(True, g1, c1[11])
    assert modal.guild is g1 and modal.channel is c1[11] and cog.add_modal().guild is None


def test_bulk_register_announces_to_the_chosen_target(tmp_path):
    from tests.domain.test_quizimport import ONE, FakeFile

    guild, chans = make_guild()
    calls = []
    quiz_cog = SimpleNamespace()

    async def broadcast(qid, g=None, c=None):
        calls.append((qid, g, c))
        return 1

    quiz_cog.broadcast = broadcast
    cog, eco, bot = make_admin(tmp_path, [guild], {"QuizCog": quiz_cog})
    log, shown = [], {}

    class R:
        async def send_message(self, **kw):
            log.append(kw["embed"].title)

        async def defer(self, **kw):
            pass

    class F:
        async def send(self, **kw):
            shown.update(kw)

    edits = []

    async def edit_original(**kw):
        edits.append(kw["embeds"][0].description)

    inter = SimpleNamespace(
        user=SimpleNamespace(id=OWNER, display_name="持ち主"),
        response=R(),
        followup=F(),
        edit_original_response=edit_original,
        namespace=SimpleNamespace(),
    )
    asyncio.run(cog.quiz_bulk.callback(cog, inter, FakeFile(ONE), SimpleNamespace(value=1), "5", "11"))
    assert "鯖5 の #クイズ部屋に出します" in shown["embeds"][0].description
    asyncio.run(shown["view"].register.callback(inter))
    assert calls == [(1, guild, chans[11])] and "鯖5 の #クイズ部屋に、1問" in edits[0]
