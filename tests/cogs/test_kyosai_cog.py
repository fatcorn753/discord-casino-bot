"""帝愛共済の動議の投稿（/共済 提案）: 投稿を覚え、締めたら結果に書き換えてボタンを外す。"""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from cogs.kyosai.cog import KyosaiCog, motion_embed
from infra.db.repositories import open_repositories
from tests.fakes import as_bot, as_choice, as_guild, as_interaction, invoke, must
from tests.support import give_test_kyosai_units, make_test_bot

G, A, B = 10, 1, 2
CH, MSG = 555, 4242

pytestmark = pytest.mark.real_scope


class Response:
    def __init__(self):
        self.sent, self.edited = [], []

    async def send_message(self, *args, **kw):
        self.sent.append(kw)

    async def edit_message(self, **kw):
        self.edited.append(kw)


class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kw):
        self.sent.append((args, kw))


class Msg:
    def __init__(self):
        self.id, self.channel, self.edits, self.pins, self.unpins = MSG, SimpleNamespace(id=CH), [], 0, 0

    async def edit(self, **kw):
        self.edits.append(kw)

    async def pin(self):
        self.pins += 1

    async def unpin(self):
        self.unpins += 1


class Channel:
    def __init__(self):
        self.id, self.sent, self.msg = CH, [], Msg()

    def get_partial_message(self, mid):
        return self.msg if mid == MSG else None

    async def send(self, content=None, **kw):
        self.sent.append(kw["embed"])


def setup_cog(tmp_path):
    ch = Channel()
    guild = SimpleNamespace(id=G, get_channel=lambda cid: ch if cid == CH else None, system_channel=None)
    bot = make_test_bot(repos=open_repositories(str(tmp_path / "t.db")), config={"allowed_guild_ids": [G]})
    bot.guilds = [guild]
    cog = KyosaiCog(as_bot(bot), bot.config, bot.services.kyosai, bot.repos.guild)
    return cog, ch, guild, bot


def interaction(uid, guild, ch):
    async def original_response():
        return ch.msg

    return SimpleNamespace(
        guild_id=G,
        guild=guild,
        channel_id=CH,
        message=ch.msg,
        user=SimpleNamespace(id=uid, mention=f"<@{uid}>"),
        response=Response(),
        followup=Followup(),
        original_response=original_response,
    )


def give(bot, uid, n):
    """帝愛の持ち口から uid へ n 口移す（競りで落札したのと同じ状態）。"""
    eco = bot.repos.for_guild(G)
    eco.wallet.balance(uid)
    with eco.db.tx():
        give_test_kyosai_units(eco, uid, n, guild_id=G, now=0.0)


def close_now(bot, motion_id):
    with bot.repos.db.tx():
        bot.repos.db.conn.execute("UPDATE kyosai_motions SET closes=0 WHERE id=?", (motion_id,))


def propose(cog, ch, guild, uid=A):
    it = interaction(uid, guild, ch)
    asyncio.run(cog.propose.callback(cog, as_interaction(it), 0.5))
    assert it.response.sent and "view" in it.response.sent[0]
    return cog._kyosai.status(G, uid)["motions"][-1]["id"]


def test_propose_remembers_the_post_and_pins_it(tmp_path):
    cog, ch, guild, bot = setup_cog(tmp_path)
    give(bot, A, 5000)
    mid = propose(cog, ch, guild)
    m = must(cog._kyosai.motion(G, mid))
    assert (m["channel_id"], m["message_id"]) == (CH, MSG) and ch.msg.pins == 1


def test_tick_rewrites_the_post_with_the_result_removes_buttons_and_unpins(tmp_path, monkeypatch):
    monkeypatch.setattr("domain.approval.is_allowed", lambda *a: True)
    cog, ch, guild, bot = setup_cog(tmp_path)
    give(bot, A, 5000)
    mid = propose(cog, ch, guild)
    close_now(bot, mid)
    asyncio.run(cog._tick_guild(as_guild(guild)))
    edit = ch.msg.edits[-1]
    assert edit["view"] is None and "可決" in must(edit["embed"].description) and ch.msg.unpins == 1


def test_pin_failure_does_not_break_propose_or_tick(tmp_path, monkeypatch):
    monkeypatch.setattr("domain.approval.is_allowed", lambda *a: True)
    cog, ch, guild, bot = setup_cog(tmp_path)
    give(bot, A, 5000)

    async def fail_pin():
        raise discord.HTTPException(cast(Any, SimpleNamespace(status=403, reason="Forbidden")), "Missing Permissions")

    ch.msg.pin = fail_pin
    ch.msg.unpin = fail_pin
    mid = propose(cog, ch, guild)
    close_now(bot, mid)
    asyncio.run(cog._tick_guild(as_guild(guild)))
    assert ch.msg.edits[-1]["view"] is None


def test_button_on_a_closed_motion_shows_the_result_and_removes_the_buttons(tmp_path):
    cog, ch, guild, bot = setup_cog(tmp_path)
    give(bot, A, 5000)
    give(bot, B, 100)
    mid = propose(cog, ch, guild)
    it = interaction(B, guild, ch)
    asyncio.run(cog.handle_vote(as_interaction(it), False, mid))
    assert "view" not in it.response.edited[0] and "反対 **100口**" in must(it.response.edited[0]["embed"].description)
    close_now(bot, mid)
    cog._kyosai.tick(G)
    it = interaction(B, guild, ch)
    asyncio.run(cog.handle_vote(as_interaction(it), True, mid))
    assert it.response.edited[0]["view"] is None and "締め切りました" in must(
        it.response.edited[0]["embed"].description
    )
    assert ch.msg.unpins == 1 and "締め切られています" in it.followup.sent[0][0][0]


def test_slash_vote_also_updates_the_announcement_post_and_keeps_the_buttons(tmp_path):
    """`/共済 投票` で投票したときも、告知の投稿が最新の票数の Embed に書き換わり、ボタン（view）は渡されない。"""
    cog, ch, guild, bot = setup_cog(tmp_path)
    give(bot, A, 5000)
    give(bot, B, 100)
    mid = propose(cog, ch, guild)
    it = interaction(B, guild, ch)
    asyncio.run(invoke(cog.vote, cog, as_interaction(it), as_choice(SimpleNamespace(name="反対", value="no")), mid))
    assert it.response.sent  # コマンド応答はそのまま
    edit = ch.msg.edits[-1]
    assert "view" not in edit  # ボタンは残す
    assert "反対 **100口**" in must(edit["embed"].description)  # 最新の票数に書き換わる


def test_embed_of_an_open_motion_still_shows_the_deadline():
    m = {"id": 1, "proposer": A, "premium": 0.005, "closes": 100, "status": "open", "yes": 1, "no": 0}
    assert "締切 <t:100:R>" in must(motion_embed(m).description)
