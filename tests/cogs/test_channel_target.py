"""告知のサーバー・チャンネル指定: 候補、検証、お知らせ・抽選・イベント・クイズが指定先に出ること。"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from cogs.admin.cog import AnnounceModal, postable
from infra.db.repositories import open_repositories
from tests.fakes import as_channel, as_guild, as_interaction, invoke, must
from tests.support import make_approval_cog, make_event_cog, make_quiz_cog, make_raffle_cog, make_test_bot

OWNER, FRIEND = 1, 2


class Chan:
    def __init__(self, cid, name, ok=True):
        self.id, self.name, self.sent, self._ok, self.mention = cid, name, [], ok, f"#{name}"
        self.deleted: list[int] = []

    def permissions_for(self, _me):
        return SimpleNamespace(view_channel=self._ok, send_messages=self._ok, embed_links=self._ok)

    async def send(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(id=9000 + len(self.sent))

    def get_partial_message(self, mid):
        async def delete():
            self.deleted = self.deleted + [mid]

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
    from cogs.shared.bridges import IEventAdminBridge, IQuizAdminBridge, IRaffleAdminBridge

    by_id = {g.id: g for g in guilds}
    bot = make_test_bot(db_path=str(tmp_path / "t.db"), config={"owner_ids": [OWNER]})
    bot.guilds = list(guilds)
    bot.get_guild = lambda gid: by_id.get(gid)
    if cogs and "QuizCog" in cogs:
        bot.bridges.register(IQuizAdminBridge, cogs["QuizCog"])
    if cogs and "EventCog" in cogs:
        bot.bridges.register(IEventAdminBridge, cogs["EventCog"])
    if cogs and "RaffleCog" in cogs:
        bot.bridges.register(IRaffleAdminBridge, cogs["RaffleCog"])
    return make_approval_cog(bot), bot.repos, bot


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
    assert postable(chans[10], as_guild(guild)) and not postable(chans[12], as_guild(guild))


def test_channel_autocomplete_lists_only_postable_channels_of_the_chosen_guild_for_the_owner(tmp_path):
    guild, chans = make_guild()
    cog, eco, bot = make_admin(tmp_path, [guild])
    inter = dm(OWNER, [], SimpleNamespace(サーバー="5"))
    names = lambda current: [c.name for c in asyncio.run(cog.channel_autocomplete(as_interaction(inter), current))]  # noqa: E731
    assert names("") == ["#general", "#クイズ部屋"]  # 投稿できないチャンネルは出ない
    assert names("クイズ") == ["#クイズ部屋"] and names("zzz") == []
    assert [c.value for c in asyncio.run(cog.channel_autocomplete(as_interaction(inter), "クイズ"))] == ["11"]
    assert (
        asyncio.run(cog.channel_autocomplete(as_interaction(dm(FRIEND, [], SimpleNamespace(サーバー="5"))), "")) == []
    )  # 持ち主以外には出さない
    assert asyncio.run(cog.channel_autocomplete(as_interaction(dm(OWNER, [])), "")) == []  # サーバー未選択
    assert (
        asyncio.run(cog.channel_autocomplete(as_interaction(dm(OWNER, [], SimpleNamespace(guild_id="5"))), "")) != []
    )  # 引数名の形でも読める


def test_pick_channel_validates(tmp_path):
    from cogs.admin.targets import pick_channel

    guild, chans = make_guild()
    cog, eco, bot = make_admin(tmp_path, [guild])
    log = []
    pick = lambda guild_, cid: asyncio.run(pick_channel(as_interaction(dm(OWNER, log)), guild_, cid))  # noqa: E731
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
    asyncio.run(modal.on_submit(as_interaction(inter)))
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
    monkeypatch.setattr("domain.approval.is_allowed", lambda *a: True)
    log = []
    asyncio.run(invoke(cog.announce, cog, dm(OWNER, log), None, None, None, "11"))  # サーバーなしでチャンネルだけ
    assert log[-1][0] == "send" and "チャンネルを使えません" in log[-1][1]
    asyncio.run(invoke(cog.announce, cog, dm(OWNER, log), "5", None, None, "11"))
    kind, modal = log[-1]
    assert kind == "modal" and modal.channel is chans[11] and modal.guilds == [guild]
    asyncio.run(invoke(cog.announce, cog, dm(OWNER, log), "5", None, None, "12"))  # 投稿できないチャンネル
    assert "権限" in log[-1][2]


# ----------------------------- 各機能が指定先に出す ----------------------------- #
def test_raffle_host_and_repost_use_the_chosen_channel(tmp_path):
    guild, chans = make_guild()
    bot = make_test_bot(db_path=str(tmp_path / "t.db"))
    bot.guilds = [guild]
    eco = bot.repos
    cog = make_raffle_cog(bot)
    ok, text = asyncio.run(cog.host(as_guild(guild), OWNER, 1000, 30, as_channel(chans[11])))
    r = eco.raffle.raffle_get()
    assert ok and r["channel_id"] == 11 and len(chans[11].sent) == 1 and chans[10].sent == [] and "#クイズ部屋" in text
    ok, text = asyncio.run(cog.repost(as_guild(guild), as_channel(chans[10])))  # 別のチャンネルへ出し直す
    assert ok and eco.raffle.raffle_get()["channel_id"] == 10 and len(chans[10].sent) == 1
    assert chans[11].deleted == [9001]  # 古い投稿は元のチャンネルから消える


def test_manual_event_announces_start_and_end_in_the_chosen_channel(tmp_path, monkeypatch):
    guild, chans = make_guild()
    bot = make_test_bot(db_path=str(tmp_path / "t.db"))
    bot.guilds = [guild]
    eco = bot.repos
    monkeypatch.setattr("domain.approval.is_allowed", lambda *a: True)
    cog = make_event_cog(bot)
    asyncio.run(cog.start_manual(as_guild(guild), "スロット", 80, 45, as_channel(chans[11])))
    assert (
        len(chans[11].sent) == 1
        and chans[10].sent == []
        and eco.events.manual_event_get(time.time())["channel_id"] == 11
    )
    asyncio.run(cog.stop_manual(as_guild(guild)))
    assert (
        len(chans[11].sent) == 2 and "終了" in chans[11].sent[1]["embed"].title and chans[10].sent == []
    )  # 終了も同じチャンネル
    asyncio.run(cog.start_manual(as_guild(guild), "ルーレット", 50, 30))  # 指定なしは告知チャンネル（system_channel）
    assert len(chans[10].sent) == 1 and eco.events.manual_event_get(time.time())["channel_id"] == 0


def test_old_manual_event_table_gets_channel_id(tmp_path):
    import sqlite3

    path = str(tmp_path / "t.db")
    open_repositories(path)
    c = sqlite3.connect(path)
    c.executescript("""DROP TABLE manual_event; CREATE TABLE manual_event (guild_id INTEGER PRIMARY KEY, game TEXT NOT NULL, boost INTEGER NOT NULL,
        starts REAL NOT NULL, ends REAL NOT NULL, ended INTEGER NOT NULL DEFAULT 0); INSERT INTO manual_event VALUES (0, 'ルーレット', 50, 0, 9e12, 0);""")
    c.execute("PRAGMA user_version = 0")
    c.commit()
    c.close()
    assert must(open_repositories(path).events.manual_event_get(100))["channel_id"] == 0


@pytest.mark.real_scope
def test_quiz_broadcast_can_target_one_guild_and_channel(tmp_path, monkeypatch):
    g1, c1 = make_guild(5, "鯖5")
    g2, c2 = make_guild(6, "鯖6")
    bot = make_test_bot(db_path=str(tmp_path / "t.db"))
    bot.guilds = [g1, g2]
    qid = bot.repos.quiz.quiz_add("問", ["a", "b", "c", "d"], "", 100, None, 0.0)
    cog = make_quiz_cog(bot)
    monkeypatch.setattr("domain.approval.is_allowed", lambda e, c, gid, uid: gid == 5)  # 6 は許可されていない
    monkeypatch.setattr("cogs.quiz.cog.announce_channel", lambda g, e: (c1 if g.id == 5 else c2)[10])
    assert (
        asyncio.run(cog.broadcast(qid)) == 1 and len(c1[10].sent) == 1 and c2[10].sent == []
    )  # 既定: 許可されたサーバーの告知チャンネル
    assert (
        asyncio.run(cog.broadcast(qid, as_guild(g1), as_channel(c1[11]))) == 1 and len(c1[11].sent) == 1
    )  # サーバー + チャンネル
    assert (
        asyncio.run(cog.broadcast(qid, as_guild(g2))) == 1 and len(c2[10].sent) == 1
    )  # 指定したサーバーには、許可外でも出す
    assert asyncio.run(cog.broadcast(qid, as_guild(g2), as_channel(c2[11]))) == 1 and len(c2[11].sent) == 1
    modal = cog.add_modal(True, as_guild(g1), as_channel(c1[11]))
    assert modal.guild is g1 and modal.channel is c1[11] and cog.add_modal().guild is None


def test_bulk_register_announces_to_the_chosen_target(tmp_path):
    from cogs.shared.bridges import IQuizAdminBridge
    from tests.domain.test_quizimport import ONE, FakeFile

    guild, chans = make_guild()
    calls = []

    class StubQuiz(IQuizAdminBridge):
        async def broadcast(self, quiz_id, guild=None, channel=None):
            calls.append((quiz_id, guild, channel))
            return 1

    cog, eco, bot = make_admin(tmp_path, [guild], {"QuizCog": StubQuiz()})
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
    asyncio.run(invoke(cog.quiz_bulk, cog, inter, FakeFile(ONE), SimpleNamespace(value=1), "5", "11"))
    assert "鯖5 の #クイズ部屋に出します" in shown["embeds"][0].description
    asyncio.run(shown["view"].register.callback(inter))
    assert calls == [(1, guild, chans[11])] and "鯖5 の #クイズ部屋に、1問" in edits[0]
