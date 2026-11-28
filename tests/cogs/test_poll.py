"""投票（/投票）: 作成・1人1票・作った人だけの有料の票の書き換えと告知・締切。"""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from cogs.poll.cog import PollCog
from cogs.poll.views import RigModal, parse_count, poll_embed, verdict
from domain import poll
from domain.errors import Failure
from domain.types import INITIAL_BALANCE
from infra.db.repositories import open_repositories
from tests.fakes import as_bot, as_guild, as_interaction, must
from tests.support import make_test_bot

G, OWNER, A, B = 1, 10, 11, 12
NOW = 1_000_000.0


@pytest.fixture
def eco(tmp_path):
    return open_repositories(str(tmp_path / "t.db")).for_guild(G)


def create(eco, minutes=60, creator=OWNER, title="カレーは飲み物"):
    ok, poll_id = eco.polls.poll_create(creator, title, NOW + minutes * 60, 555, NOW)
    assert not isinstance(poll_id, Failure)
    return poll_id


# ----------------------------- 記録 ----------------------------- #
def test_one_vote_per_person_and_it_can_be_changed(eco):
    pid = create(eco)
    eco.polls.poll_vote(A, pid, True, NOW)
    eco.polls.poll_vote(B, pid, True, NOW)
    ok, p = eco.polls.poll_vote(A, pid, False, NOW)  # 変えた
    assert ok and (p["yes"], p["no"]) == (1, 1)
    ok, p = eco.polls.poll_vote(A, pid, False, NOW)  # 同じ票を入れ直しても 1票
    assert (p["yes"], p["no"]) == (1, 1)


def test_create_guards(eco):
    assert eco.polls.poll_create(A, "  ", NOW + 60, 1, NOW)[1].code == "poll_bad_title"
    assert eco.polls.poll_create(A, "x" * (poll.TITLE_MAX + 1), NOW + 60, 1, NOW)[1].code == "poll_bad_title"
    assert eco.polls.poll_create(A, "x", NOW, 1, NOW)[1].code == "poll_bad_duration"
    assert eco.polls.poll_create(A, "x", NOW + poll.MAX_MINUTES * 60 + 1, 1, NOW)[1].code == "poll_bad_duration"
    for _ in range(poll.MAX_OPEN_PER_USER):
        create(eco, creator=A)
    assert eco.polls.poll_create(A, "x", NOW + 60, 1, NOW)[1].code == "poll_too_many"


def test_only_the_creator_can_rig_and_it_costs_and_goes_to_the_bank(eco):
    pid = create(eco)
    eco.polls.poll_vote(A, pid, False, NOW)
    eco.wallet.balance(B)
    ok, why = eco.polls.poll_rig(B, pid, 100, 0, NOW)
    assert not ok and why.code == "poll_not_creator" and eco.wallet.balance(B) == INITIAL_BALANCE
    cash0 = eco.bank.bank_status(NOW)["cash"]
    ok, res = eco.polls.poll_rig(OWNER, pid, 100, 0, NOW)
    assert ok and res["before"] == (0, 1) and (res["poll"]["yes"], res["poll"]["no"]) == (100, 0)
    assert res["poll"]["rigs"] == 1
    assert eco.wallet.balance(OWNER) == INITIAL_BALANCE - poll.RIG_COST
    assert eco.bank.bank_status(NOW)["cash"] > cash0  # 代金は帝愛銀行の収入（新しいペリカは作らない）


def test_real_votes_after_rigging_add_to_the_rigged_counts(eco):
    pid = create(eco)
    eco.polls.poll_vote(A, pid, True, NOW)
    eco.polls.poll_rig(OWNER, pid, 0, 50, NOW)
    _, p = eco.polls.poll_vote(B, pid, True, NOW)
    assert (p["yes"], p["no"]) == (1, 50)
    _, p = eco.polls.poll_vote(A, pid, False, NOW)  # 書き換えで 0 にした賛成から抜けても、マイナスにはならない
    assert (p["yes"], p["no"]) == (0, 51)


def test_rig_guards(eco):
    pid = create(eco)
    assert eco.polls.poll_rig(OWNER, pid, -1, 0, NOW)[1].code == "poll_bad_count"
    assert eco.polls.poll_rig(OWNER, pid, poll.MAX_COUNT + 1, 0, NOW)[1].code == "poll_bad_count"
    eco.wallet.balance(OWNER)
    with eco.db.tx():
        eco.db.conn.execute("UPDATE accounts SET balance=? WHERE user_id=?", (poll.RIG_COST - 1, OWNER))
    assert eco.polls.poll_rig(OWNER, pid, 1, 0, NOW)[1].code == "insufficient_balance"


def test_no_votes_or_rigs_after_the_deadline_and_close_once(eco):
    pid = create(eco, minutes=1)
    eco.polls.poll_vote(A, pid, True, NOW)
    later = NOW + 61
    assert eco.polls.poll_vote(B, pid, True, later)[1].code == "poll_closed"
    assert eco.polls.poll_rig(OWNER, pid, 9, 9, later)[1].code == "poll_closed"
    assert eco.polls.poll_close_due(NOW + 30) == []
    done = eco.polls.poll_close_due(later)
    assert [d["id"] for d in done] == [pid] and done[0]["status"] == "closed" and done[0]["yes"] == 1
    assert eco.polls.poll_close_due(later) == []


@pytest.mark.real_scope
def test_polls_are_separate_per_server(tmp_path):
    repos = open_repositories(str(tmp_path / "t.db"))
    pid = create(repos.for_guild(1))
    assert repos.for_guild(2).polls.poll_get(pid) is None
    res = repos.for_guild(2).polls.poll_vote(A, pid, True, NOW)
    assert not res[0] and isinstance(res[1], Failure) and res[1].code == "poll_closed"


# ----------------------------- 画面 ----------------------------- #
def test_embed_and_verdict(eco):
    pid = create(eco)
    eco.polls.poll_vote(A, pid, True, NOW)
    p = must(eco.polls.poll_get(pid))
    text = must(poll_embed(p).description)
    assert "カレーは飲み物" in text and "賛成 **1票**" in text and "書き換え" not in text
    eco.polls.poll_rig(OWNER, pid, 3, 7, NOW)
    p = must(eco.polls.poll_get(pid))
    assert "1回** 書き換え" in must(poll_embed(p).description) and verdict(p) == "❌ 反対多数"
    assert parse_count("1,000") == 1000
    with pytest.raises(ValueError):
        parse_count("-3")


class Response:
    def __init__(self):
        self.sent, self.edited, self.modal = [], [], None

    async def send_message(self, *args, **kw):
        self.sent.append(kw)

    async def edit_message(self, **kw):
        self.edited.append(kw)

    async def send_modal(self, modal):
        self.modal = modal


class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kw):
        self.sent.append((args, kw))


class Msg:
    def __init__(self):
        self.id = 4242
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
        self.sent.append(kw["embed"])


def setup_cog(eco):
    ch = Channel()
    guild = SimpleNamespace(id=G, get_channel=lambda cid: ch)
    bot = make_test_bot(repos=open_repositories(eco.db.db_path), config={"allowed_guild_ids": [G]})
    bot.guilds = [guild]
    cog = PollCog(as_bot(bot), bot.config, bot.services.poll, bot.repos.guild)
    return cog, ch, guild, bot


def interaction(uid, guild, ch=None):
    async def original_response():
        assert ch is not None
        return ch.msg

    return SimpleNamespace(
        guild_id=G,
        guild=guild,
        channel_id=555,
        user=SimpleNamespace(id=uid, mention=f"<@{uid}>"),
        response=Response(),
        followup=Followup(),
        original_response=original_response,
    )


def test_buttons_vote_and_only_the_creator_gets_the_rig_form(eco):
    cog, ch, guild, bot = setup_cog(eco)
    ok, pid = cog._poll.create(G, OWNER, "議題", 60, 555)
    assert not isinstance(pid, Failure)
    cog._poll.set_message(G, pid, 555, 4242)
    it = interaction(A, guild)
    asyncio.run(cog.handle_button(as_interaction(it), "yes", pid))
    assert "賛成 **1票**" in it.response.edited[0]["embed"].description and it.followup.sent
    it = interaction(A, guild)
    asyncio.run(cog.handle_button(as_interaction(it), "rig", pid))
    assert it.response.modal is None and "作った人だけ" in it.response.sent[0]["embed"].description
    it = interaction(OWNER, guild)
    asyncio.run(cog.handle_button(as_interaction(it), "rig", pid))
    assert isinstance(it.response.modal, RigModal) and it.response.modal.yes.default == "1"


def test_rig_is_announced_to_everyone_and_the_post_is_updated(eco):
    cog, ch, guild, bot = setup_cog(eco)
    ok, pid = cog._poll.create(G, OWNER, "議題", 60, 555)
    assert not isinstance(pid, Failure)
    cog._poll.set_message(G, pid, 555, 4242)
    it = interaction(OWNER, guild)
    asyncio.run(cog.rig(as_interaction(it), pid, 99, 1))
    sent = it.response.sent[0]
    assert "ephemeral" not in sent  # 全員に見える告知
    assert "書き換えました" in sent["embed"].description and "99" in sent["embed"].description
    assert "賛成 **99票**" in ch.msg.edits[-1]["embed"].description


def test_tick_closes_due_polls_removes_buttons_unpins_and_posts_the_result(eco, monkeypatch):
    monkeypatch.setattr("domain.approval.is_allowed", lambda *a: True)
    cog, ch, guild, bot = setup_cog(eco)
    it = interaction(OWNER, guild, ch)
    asyncio.run(cast(Any, cog.create.callback)(cog, as_interaction(it), "議題", "1分"))
    pid = bot.repos.db.conn.execute("SELECT id FROM polls ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert ch.msg.pins == 1
    asyncio.run(cog._tick_guild(as_guild(guild)))
    assert not ch.sent and ch.msg.unpins == 0
    with bot.repos.db.tx():
        bot.repos.db.conn.execute("UPDATE polls SET closes=0 WHERE id=?", (pid,))
    asyncio.run(cog._tick_guild(as_guild(guild)))
    assert ch.msg.edits[-1]["view"] is None and ch.msg.unpins == 1 and "結果" in ch.sent[0].title
    asyncio.run(cog._tick_guild(as_guild(guild)))
    assert len(ch.sent) == 1  # 二重には締めない


def test_poll_pin_error_does_not_fail_create(eco):
    cog, ch, guild, _ = setup_cog(eco)

    async def fail_pin():
        raise discord.HTTPException(cast(Any, SimpleNamespace(status=403, reason="Forbidden")), "Missing Permissions")

    ch.msg.pin = fail_pin
    it = interaction(OWNER, guild, ch)
    asyncio.run(cast(Any, cog.create.callback)(cog, as_interaction(it), "議題", "1時間"))
    assert it.response.sent and "view" in it.response.sent[0]
