"""抽選: 参加費の預かり、当選者への支払い、返金、再起動、持ち主だけが開催できること。"""

import asyncio
import random
import time
from types import SimpleNamespace

import pytest

from cogs.raffle_cog import RaffleCog, raffle_embed, result_embed
from core.economy import INITIAL_BALANCE, Economy

NOW = 1_000_000.0


@pytest.fixture
def eco(tmp_path):
    return Economy(str(tmp_path / "t.db"))


def open_raffle(eco, fee=1000, closes=NOW + 3600):
    return eco.raffle_open(99, fee, closes, 555)


def test_winner_gets_the_whole_pool_and_fees_are_taken_up_front(eco):
    assert open_raffle(eco) == (True, "")
    for uid in (1, 2, 3):
        assert eco.raffle_join(uid, NOW) == (True, "")
        assert eco.balance(uid) == INITIAL_BALANCE - 1000  # 参加費は先に引かれる
    done = eco.raffle_close(random.Random(1))
    assert done["winner"] in (1, 2, 3) and done["prize"] == 3000 and not done["refunded"]
    assert eco.balance(done["winner"]) == INITIAL_BALANCE - 1000 + 3000
    assert sum(eco.balance(u) for u in (1, 2, 3)) == 3 * INITIAL_BALANCE  # 手数料なし。ペリカの総量は変わらない
    assert eco.raffle_get() is None and eco.raffle_close() is None  # 二重には締められない


def test_fewer_than_two_entrants_get_a_full_refund(eco):
    open_raffle(eco)
    eco.raffle_join(1, NOW)
    done = eco.raffle_close()
    assert done["winner"] is None and done["refunded"] and eco.balance(1) == INITIAL_BALANCE


def test_cancel_refunds_everyone(eco):
    open_raffle(eco)
    for uid in (1, 2, 3):
        eco.raffle_join(uid, NOW)
    done = eco.raffle_close(cancel=True)
    assert done["refunded"] and all(eco.balance(u) == INITIAL_BALANCE for u in (1, 2, 3))


def test_join_guards(eco):
    assert eco.raffle_join(1, NOW)[0] is False  # 募集がない
    open_raffle(eco, fee=2000)
    assert eco.raffle_join(1, NOW) == (True, "")
    assert not eco.raffle_join(1, NOW)[0]  # 二重参加
    assert not eco.raffle_join(2, NOW + 3601)[0]  # 締切後
    eco.stats(3)
    eco._conn.execute("UPDATE users SET debt=500 WHERE user_id=3")
    assert not eco.raffle_join(3, NOW)[0]  # 借金中
    eco.stats(4)
    eco._conn.execute("UPDATE users SET balance=1999 WHERE user_id=4")
    assert not eco.raffle_join(4, NOW)[0]  # 残高不足
    assert eco.raffle_get()["entries"] == [1]
    assert not open_raffle(eco)[0]  # 募集中は1つだけ


def test_open_raffle_survives_a_restart(tmp_path):
    path = str(tmp_path / "t.db")
    first = Economy(path)
    open_raffle(first)
    first.raffle_join(1, NOW)
    first.raffle_set_message(first.raffle_get()["id"], 4242)
    second = Economy(path)
    r = second.raffle_get()
    assert (r["fee"], r["entries"], r["message_id"]) == (1000, [1], 4242)
    assert second.balance(1) == INITIAL_BALANCE - 1000


def test_raffle_does_not_touch_stats(eco):
    open_raffle(eco)
    eco.raffle_join(1, NOW)
    eco.raffle_join(2, NOW)
    eco.raffle_close(random.Random(0))
    s = eco.stats(1)
    assert s["games_played"] == 0 and s["total_wagered"] == 0
    assert eco._conn.execute("SELECT COUNT(*) FROM weekly_stats").fetchone()[0] == 0


def test_embeds_show_entrants_and_prize(eco):
    open_raffle(eco)
    eco.raffle_join(1, NOW)
    eco.raffle_join(2, NOW)
    assert "2人" in raffle_embed(eco.raffle_get()).description and "2,000" in raffle_embed(eco.raffle_get()).description
    done = eco.raffle_close(random.Random(2))
    assert f"<@{done['winner']}>" in result_embed(done).description


class Msg:
    def __init__(self):
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)


class Channel:
    def __init__(self):
        self.sent, self.msg = [], Msg()

    def get_partial_message(self, mid):
        return self.msg

    async def send(self, content=None, **kw):
        self.sent.append((content, kw["embed"].title))


def make_cog(eco, owners=(99,)):
    ch = Channel()
    guild = SimpleNamespace(id=1, get_channel=lambda cid: ch)
    bot = SimpleNamespace(
        guilds=[guild],
        config={"allowed_guild_ids": [1], "owner_ids": list(owners)},
        economy=SimpleNamespace(for_guild=lambda gid: eco, is_approved=lambda *a: True),
    )
    return RaffleCog(bot), ch


def test_run_once_closes_only_overdue_raffles_and_announces(eco, monkeypatch):
    monkeypatch.setattr("cogs.raffle_cog.approval.is_allowed", lambda *a: True)
    cog, ch = make_cog(eco)
    open_raffle(eco, closes=9e12)  # まだ先
    eco.raffle_set_message(eco.raffle_get()["id"], 4242)
    asyncio.run(cog.run_once())
    assert eco.raffle_get() is not None and not ch.sent
    eco._conn.execute("UPDATE raffle SET closes=1")  # 締切を過ぎた（再起動のあとなど）
    eco.raffle_join(1, 0.5)
    eco.raffle_join(2, 0.5)
    asyncio.run(cog.run_once())
    assert eco.raffle_get() is None
    assert ch.sent[0][1] == "🎟️ 抽選 結果発表！" and ch.sent[0][0].startswith("🎉 <@")
    assert ch.msg.edits and ch.msg.edits[0]["view"] is None  # ボタンを外す


def make_host_cog(eco, monkeypatch, channel=True):
    cog, ch = make_cog(eco)
    sent = []

    class Chan:
        id = 888
        mention = "#告知"

        async def send(self, **kw):
            sent.append((kw["embed"].title, len(kw["view"].children)))
            return SimpleNamespace(id=7777)

    chan = Chan() if channel else None
    monkeypatch.setattr("cogs.raffle_cog.announce_channel", lambda guild, e: chan)
    guild = SimpleNamespace(id=1, name="テスト鯖", get_channel=lambda cid: ch)
    return cog, guild, sent, ch


def test_host_posts_in_the_announce_channel_and_saves_the_message(eco, monkeypatch):
    cog, guild, sent, _ = make_host_cog(eco, monkeypatch)
    ok, text = asyncio.run(cog.host(guild, 99, 1500, 30))
    r = eco.raffle_get()
    assert (
        ok
        and (r["fee"], r["message_id"], r["channel_id"]) == (1500, 7777, 888)
        and sent == [("🎟️ 抽選 参加者募集中！", 1)]
    )
    assert abs(r["closes"] - time.time() - 30 * 60) < 5
    ok, text = asyncio.run(cog.host(guild, 99, 1500, 30))  # 募集中は1つだけ
    assert not ok and "すでに" in text and len(sent) == 1


def test_host_random_fee_is_in_range_and_needs_a_channel(eco, monkeypatch):
    cog, guild, sent, _ = make_host_cog(eco, monkeypatch)
    asyncio.run(cog.host(guild, 99))
    assert 500 <= eco.raffle_get()["fee"] <= 3000 and eco.raffle_get()["fee"] % 100 == 0
    eco2 = eco
    cog2, guild2, sent2, _ = make_host_cog(eco2, monkeypatch, channel=False)
    eco2.raffle_close(cancel=True)
    ok, text = asyncio.run(cog2.host(guild2, 99, 1000))
    assert not ok and "チャンネル" in text and eco2.raffle_get() is None


def test_cancel_refunds_and_reports(eco, monkeypatch):
    cog, guild, sent, ch = make_host_cog(eco, monkeypatch)
    asyncio.run(cog.host(guild, 99, 1000, 30))
    eco.raffle_join(1, time.time())
    eco.raffle_join(2, time.time())
    ok, text = asyncio.run(cog.cancel(guild))
    assert ok and "2人に返金" in text and eco.raffle_get() is None and eco.balance(1) == INITIAL_BALANCE
    assert not asyncio.run(cog.cancel(guild))[0]


def test_join_button_charges_updates_the_post_and_rejects_repeats(eco, monkeypatch):
    monkeypatch.setattr("cogs.raffle_cog.approval.is_allowed", lambda *a: True)
    cog, _ = make_cog(eco)
    open_raffle(eco, closes=9e12)
    eco.raffle_set_message(eco.raffle_get()["id"], 4242)
    log = []

    class Resp:
        async def edit_message(self, **kw):
            log.append(("edit", kw["embed"].description))

        async def send_message(self, **kw):
            log.append(("send", kw["embed"].title))

    class Follow:
        async def send(self, **kw):
            log.append(("follow", kw["embed"].title))

    def click(uid, mid=4242):
        return SimpleNamespace(
            user=SimpleNamespace(id=uid),
            guild_id=1,
            message=SimpleNamespace(id=mid),
            response=Resp(),
            followup=Follow(),
        )

    asyncio.run(cog.on_join(click(1)))
    assert [k for k, _ in log] == ["edit", "follow"] and "1人" in log[0][1] and eco.balance(1) == INITIAL_BALANCE - 1000
    log.clear()
    asyncio.run(cog.on_join(click(1)))  # 二重参加
    asyncio.run(cog.on_join(click(2, mid=1)))  # 別の投稿のボタン
    assert (
        log == [("send", "❌ 参加できません"), ("send", "❌ 募集は終わりました")] and eco.balance(2) == INITIAL_BALANCE
    )


def test_repost_moves_the_post_and_deletes_the_old_one(eco, monkeypatch):
    cog, guild, sent, ch = make_host_cog(eco, monkeypatch)
    open_raffle(eco, closes=9e12)
    eco.raffle_join(1, NOW)
    eco.raffle_set_message(eco.raffle_get()["id"], 4242)
    deleted = []

    class Old(Msg):
        async def delete(self):
            deleted.append(True)

    ch.msg = Old()
    ok, text = asyncio.run(cog.repost(guild))
    r = eco.raffle_get()
    assert ok and (r["message_id"], r["channel_id"], r["entries"]) == (7777, 888, [1]) and deleted == [True]
    assert sent == [("🎟️ 抽選 参加者募集中！", 1)]
    eco.raffle_close(cancel=True)
    assert not asyncio.run(cog.repost(guild))[0]  # 募集中がなければ出し直せない
