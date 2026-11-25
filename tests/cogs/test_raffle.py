"""抽選: 参加費の預かり、当選者への支払い、返金、再起動、持ち主だけが開催できること。"""

import asyncio
import random
import time
from types import SimpleNamespace

import pytest

from cogs.raffle.cog import raffle_embed, result_embed
from domain.types import INITIAL_BALANCE
from infra.db.repositories import open_repositories
from tests.fakes import as_guild, as_interaction, must
from tests.support import make_raffle_cog, make_test_bot, set_test_balance, set_test_debt

NOW = 1_000_000.0


@pytest.fixture
def eco(tmp_path):
    return open_repositories(str(tmp_path / "t.db"))


def open_raffle(eco, fee=1000, closes=NOW + 3600):
    return eco.raffle.raffle_open(99, fee, closes, 555)


def test_winner_gets_the_whole_pool_and_fees_are_taken_up_front(eco):
    assert open_raffle(eco) == (True, "")
    for uid in (1, 2, 3):
        assert eco.raffle.raffle_join(uid, NOW) == (True, "")
        assert eco.wallet.balance(uid) == INITIAL_BALANCE - 1000  # 参加費は先に引かれる
    done = eco.raffle.raffle_close(random.Random(1))
    assert done["winner"] in (1, 2, 3) and done["prize"] == 3000 and not done["refunded"]
    assert eco.wallet.balance(done["winner"]) == INITIAL_BALANCE - 1000 + 3000
    assert sum(eco.wallet.balance(u) for u in (1, 2, 3)) == 3 * INITIAL_BALANCE  # 手数料なし。ペリカの総量は変わらない
    assert eco.raffle.raffle_get() is None and eco.raffle.raffle_close() is None  # 二重には締められない


def test_fewer_than_two_entrants_get_a_full_refund(eco):
    open_raffle(eco)
    eco.raffle.raffle_join(1, NOW)
    done = eco.raffle.raffle_close()
    assert done["winner"] is None and done["refunded"] and eco.wallet.balance(1) == INITIAL_BALANCE


def test_cancel_refunds_everyone(eco):
    open_raffle(eco)
    for uid in (1, 2, 3):
        eco.raffle.raffle_join(uid, NOW)
    done = eco.raffle.raffle_close(cancel=True)
    assert done["refunded"] and all(eco.wallet.balance(u) == INITIAL_BALANCE for u in (1, 2, 3))


def test_join_guards(eco):
    assert eco.raffle.raffle_join(1, NOW)[0] is False  # 募集がない
    open_raffle(eco, fee=2000)
    assert eco.raffle.raffle_join(1, NOW) == (True, "")
    assert not eco.raffle.raffle_join(1, NOW)[0]  # 二重参加
    assert not eco.raffle.raffle_join(2, NOW + 3601)[0]  # 締切後
    eco.wallet.stats(3)
    with eco.db.tx():
        set_test_debt(eco, 3, 500)
    assert not eco.raffle.raffle_join(3, NOW)[0]  # 借金中
    eco.wallet.stats(4)
    with eco.db.tx():
        set_test_balance(eco, 4, 1999)
    assert not eco.raffle.raffle_join(4, NOW)[0]  # 残高不足
    assert eco.raffle.raffle_get()["entries"] == [1]
    assert not open_raffle(eco)[0]  # 募集中は1つだけ


def test_open_raffle_survives_a_restart(tmp_path):
    path = str(tmp_path / "t.db")
    first = open_repositories(path)
    open_raffle(first)
    first.raffle.raffle_join(1, NOW)
    first.raffle.raffle_set_message(must(first.raffle.raffle_get())["id"], 4242)
    second = open_repositories(path)
    r = second.raffle.raffle_get()
    assert (must(r)["fee"], must(r)["entries"], must(r)["message_id"]) == (1000, [1], 4242)
    assert second.wallet.balance(1) == INITIAL_BALANCE - 1000


def test_raffle_does_not_touch_stats(eco):
    open_raffle(eco)
    eco.raffle.raffle_join(1, NOW)
    eco.raffle.raffle_join(2, NOW)
    eco.raffle.raffle_close(random.Random(0))
    s = eco.wallet.stats(1)
    assert s["games_played"] == 0 and s["total_wagered"] == 0
    assert eco.db.conn.execute("SELECT COUNT(*) FROM weekly_stats").fetchone()[0] == 0


def test_embeds_show_entrants_and_prize(eco):
    open_raffle(eco)
    eco.raffle.raffle_join(1, NOW)
    eco.raffle.raffle_join(2, NOW)
    assert "2人" in must(raffle_embed(eco.raffle.raffle_get()).description) and "2,000" in must(
        raffle_embed(eco.raffle.raffle_get()).description
    )
    done = eco.raffle.raffle_close(random.Random(2))
    assert f"<@{done['winner']}>" in must(result_embed(done).description)


class Msg:
    def __init__(self, mid=4242):
        self.id = mid
        self.edits = []
        self.pins = 0
        self.unpins = 0

    async def edit(self, **kw):
        self.edits.append(kw)

    async def pin(self):
        self.pins += 1

    async def unpin(self):
        self.unpins += 1


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
    bot = make_test_bot(repos=eco, config={"allowed_guild_ids": [1], "owner_ids": list(owners)})
    bot.guilds = [guild]
    return make_raffle_cog(bot), ch


def test_run_once_closes_only_overdue_raffles_and_announces(eco, monkeypatch):
    monkeypatch.setattr("domain.approval.is_allowed", lambda *a: True)
    cog, ch = make_cog(eco)
    open_raffle(eco, closes=9e12)  # まだ先
    eco.raffle.raffle_set_message(eco.raffle.raffle_get()["id"], 4242)
    asyncio.run(cog.run_once())
    assert eco.raffle.raffle_get() is not None and not ch.sent
    with eco.db.tx():
        eco.db.conn.execute("UPDATE raffle SET closes=1")  # 締切を過ぎた（再起動のあとなど）
    eco.raffle.raffle_join(1, 0.5)
    eco.raffle.raffle_join(2, 0.5)
    asyncio.run(cog.run_once())
    assert eco.raffle.raffle_get() is None
    assert ch.sent[0][1] == "🎟️ 抽選 結果発表！" and ch.sent[0][0].startswith("🎉 <@")
    assert ch.msg.edits and ch.msg.edits[0]["view"] is None and ch.msg.unpins == 1  # ボタンとピン留めを外す


def make_host_cog(eco, monkeypatch, channel=True):
    cog, ch = make_cog(eco)
    sent = []

    class Chan:
        id = 888
        mention = "#告知"

        async def send(self, **kw):
            m = Msg(7777)
            sent.append((kw["embed"].title, len(kw["view"].children), m))
            return m

    chan = Chan() if channel else None
    monkeypatch.setattr("cogs.raffle.cog.announce_channel", lambda guild, e: chan)
    guild = SimpleNamespace(id=1, name="テスト鯖", get_channel=lambda cid: ch)
    return cog, guild, sent, ch


def test_host_posts_in_the_announce_channel_and_saves_the_message(eco, monkeypatch):
    cog, guild, sent, _ = make_host_cog(eco, monkeypatch)
    ok, text = asyncio.run(cog.host(as_guild(guild), 99, 1500, 30))
    r = eco.raffle.raffle_get()
    assert (
        ok
        and (r["fee"], r["message_id"], r["channel_id"]) == (1500, 7777, 888)
        and [(t, n) for t, n, _ in sent] == [("🎟️ 抽選 参加者募集中！", 1)]
        and sent[0][2].pins == 1
    )
    assert abs(r["closes"] - time.time() - 30 * 60) < 5
    ok, text = asyncio.run(cog.host(as_guild(guild), 99, 1500, 30))  # 募集中は1つだけ
    assert not ok and "すでに" in text and len(sent) == 1


def test_host_random_fee_is_in_range_and_needs_a_channel(eco, monkeypatch):
    cog, guild, sent, _ = make_host_cog(eco, monkeypatch)
    asyncio.run(cog.host(as_guild(guild), 99))
    assert 500 <= eco.raffle.raffle_get()["fee"] <= 3000 and eco.raffle.raffle_get()["fee"] % 100 == 0
    eco2 = eco
    cog2, guild2, sent2, _ = make_host_cog(eco2, monkeypatch, channel=False)
    eco2.raffle.raffle_close(cancel=True)
    ok, text = asyncio.run(cog2.host(as_guild(guild2), 99, 1000))
    assert not ok and "チャンネル" in text and eco2.raffle.raffle_get() is None


def test_host_random_fee_is_deterministic_with_injected_rng(eco, monkeypatch):
    cog, guild, sent, _ = make_host_cog(eco, monkeypatch)
    asyncio.run(cog.host(as_guild(guild), 99, rng=random.Random(42)))
    first = eco.raffle.raffle_get()["fee"]
    eco.raffle.raffle_close(cancel=True)
    asyncio.run(cog.host(as_guild(guild), 99, rng=random.Random(42)))
    second = eco.raffle.raffle_get()["fee"]
    assert first == second and 500 <= first <= 3000 and first % 100 == 0


def test_cancel_refunds_and_reports(eco, monkeypatch):
    cog, guild, sent, ch = make_host_cog(eco, monkeypatch)
    asyncio.run(cog.host(as_guild(guild), 99, 1000, 30))
    eco.raffle.raffle_join(1, time.time())
    eco.raffle.raffle_join(2, time.time())
    ok, text = asyncio.run(cog.cancel(as_guild(guild)))
    assert (
        ok
        and "2人に返金" in text
        and eco.raffle.raffle_get() is None
        and eco.wallet.balance(1) == INITIAL_BALANCE
        and ch.msg.unpins == 1
    )
    assert not asyncio.run(cog.cancel(as_guild(guild)))[0]


def test_join_button_charges_updates_the_post_and_rejects_repeats(eco, monkeypatch):
    monkeypatch.setattr("domain.approval.is_allowed", lambda *a: True)
    cog, _ = make_cog(eco)
    open_raffle(eco, closes=9e12)
    eco.raffle.raffle_set_message(eco.raffle.raffle_get()["id"], 4242)
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

    asyncio.run(cog.on_join(as_interaction(click(1))))
    assert (
        [k for k, _ in log] == ["edit", "follow"]
        and "1人" in log[0][1]
        and eco.wallet.balance(1) == INITIAL_BALANCE - 1000
    )
    log.clear()
    asyncio.run(cog.on_join(as_interaction(click(1))))  # 二重参加
    asyncio.run(cog.on_join(as_interaction(click(2, mid=1))))  # 別の投稿のボタン
    assert (
        log == [("send", "❌ 参加できません"), ("send", "❌ 募集は終わりました")]
        and eco.wallet.balance(2) == INITIAL_BALANCE
    )


def test_repost_moves_the_post_and_deletes_the_old_one(eco, monkeypatch):
    cog, guild, sent, ch = make_host_cog(eco, monkeypatch)
    open_raffle(eco, closes=9e12)
    eco.raffle.raffle_join(1, NOW)
    eco.raffle.raffle_set_message(eco.raffle.raffle_get()["id"], 4242)
    deleted = []

    class Old(Msg):
        async def delete(self):
            deleted.append(True)

    ch.msg = Old()
    ok, text = asyncio.run(cog.repost(as_guild(guild)))
    r = eco.raffle.raffle_get()
    assert ok and (r["message_id"], r["channel_id"], r["entries"]) == (7777, 888, [1]) and deleted == [True]
    assert [(t, n) for t, n, _ in sent] == [("🎟️ 抽選 参加者募集中！", 1)] and sent[0][2].pins == 1
    eco.raffle.raffle_close(cancel=True)
    assert not asyncio.run(cog.repost(as_guild(guild)))[0]  # 募集中がなければ出し直せない
