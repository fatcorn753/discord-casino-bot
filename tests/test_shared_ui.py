"""共通部品（core/clock, core/ui.OwnerView, core/confirm ヘルパー, cogs/_shared）の単体テスト。"""

import asyncio
import datetime
from types import SimpleNamespace

import discord
import pytest

from cogs._shared import announce_channel
from core import ui
from core.clock import local_now, local_today
from core.confirm import confirmed_reply, user_session
from core.session import Sessions


def make_interaction(uid: int = 1):
    sent = []
    followed = []

    class Resp:
        async def send_message(self, content=None, *, embed=None, ephemeral=False, **kw):
            sent.append({"content": content, "embed": embed, "ephemeral": ephemeral, **kw})

    class Follow:
        async def send(self, content=None, *, embed=None, ephemeral=False, **kw):
            followed.append({"content": content, "embed": embed, "ephemeral": ephemeral, **kw})

    it = SimpleNamespace(user=SimpleNamespace(id=uid), response=Resp(), followup=Follow())
    return it, sent, followed


# ---- core.clock ---- #
def test_clock_uses_config_offset():
    bot_default = SimpleNamespace(config={})
    now_jst = local_now(bot_default)
    assert now_jst.utcoffset() == datetime.timedelta(hours=9)
    assert local_today(bot_default) == now_jst.date()

    bot_utc_minus5 = SimpleNamespace(config={"utc_offset_hours": -5})
    now_est = local_now(bot_utc_minus5)
    assert now_est.utcoffset() == datetime.timedelta(hours=-5)


# ---- core.ui.OwnerView ---- #
def test_owner_view_allows_owner_and_rejects_others():
    async def run():
        view = ui.OwnerView(owner_id=10, owner_message="本人専用です")
        ok_it, ok_sent, _ = make_interaction(10)
        assert await view.interaction_check(ok_it) is True
        assert ok_sent == []

        bad_it, bad_sent, _ = make_interaction(99)
        assert await view.interaction_check(bad_it) is False
        assert bad_sent == [{"content": "本人専用です", "embed": None, "ephemeral": True}]

    asyncio.run(run())


def test_owner_view_silent_and_custom_rejection():
    class CustomView(ui.OwnerView):
        async def _reject_non_owner(self, interaction: discord.Interaction) -> None:
            await interaction.response.send_message(embed=ui.error_embed("権限なし"), ephemeral=True)

    async def run():
        silent = ui.OwnerView(owner_id=10)
        it1, sent1, _ = make_interaction(99)
        assert await silent.interaction_check(it1) is False
        assert sent1 == []

        custom = CustomView(owner_id=10)
        it2, sent2, _ = make_interaction(99)
        assert await custom.interaction_check(it2) is False
        assert len(sent2) == 1 and "権限なし" in sent2[0]["embed"].title

    asyncio.run(run())


# ---- core.confirm.user_session & confirmed_reply ---- #
def test_user_session_lifecycle_and_exception_cleanup():
    bot = SimpleNamespace(sessions=Sessions(cooldown=0))

    async def run():
        it, sent, _ = make_interaction(1)
        async with user_session(bot, it) as ok:
            assert ok is True
            assert bot.sessions.is_active(1)
        assert not bot.sessions.is_active(1)
        assert sent == []

        with pytest.raises(RuntimeError):
            async with user_session(bot, it) as ok:
                assert ok is True
                raise RuntimeError("boom")
        assert not bot.sessions.is_active(1)

    asyncio.run(run())


def test_user_session_rejects_when_already_busy():
    bot = SimpleNamespace(sessions=Sessions())
    assert bot.sessions.try_start(1) is None

    async def run():
        it, sent, _ = make_interaction(1)
        async with user_session(bot, it, busy_title="いまは買えません") as ok:
            assert ok is False
        assert len(sent) == 1
        assert "いまは買えません" in sent[0]["embed"].title
        assert sent[0]["ephemeral"] is True
        assert bot.sessions.is_active(1)  # 既存のセッションを勝手に閉じない

    asyncio.run(run())


def test_confirmed_reply_branches():
    async def run():
        it, _, _ = make_interaction(1)
        called = []

        async def yes(*args):
            called.append(args)
            return True

        async def no(*args):
            called.append(args)
            return False

        async def timeout(*args):
            called.append(args)
            return None

        r1 = await confirmed_reply(it, False, "t", "d", "ok", confirm_fn=yes)
        assert r1 == it.response.send_message
        assert called == []

        r2 = await confirmed_reply(it, True, "t", "d", "ok", confirm_fn=yes)
        assert r2 == it.followup.send
        assert len(called) == 1

        r3 = await confirmed_reply(it, True, "t", "d", "ok", confirm_fn=no)
        assert r3 is None

        r4 = await confirmed_reply(it, True, "t", "d", "ok", confirm_fn=timeout)
        assert r4 is None

    asyncio.run(run())


# ---- cogs._shared.announce_channel ---- #
def test_announce_channel_preference_and_permissions():
    def chan(cid, view=True, send=True, embed=True):
        return SimpleNamespace(
            id=cid,
            permissions_for=lambda _me: SimpleNamespace(view_channel=view, send_messages=send, embed_links=embed),
        )

    board_ch = chan(100)
    sys_ch = chan(200)
    blocked_ch = chan(300, embed=False)

    channels = {100: board_ch, 200: sys_ch, 300: blocked_ch}
    guild = SimpleNamespace(id=1, me=object(), system_channel=sys_ch, get_channel=lambda cid: channels.get(cid))

    eco_with_board = SimpleNamespace(get_leaderboard=lambda _gid: {"channel_id": 100})
    assert announce_channel(guild, eco_with_board) is board_ch

    eco_blocked_board = SimpleNamespace(get_leaderboard=lambda _gid: {"channel_id": 300})
    assert announce_channel(guild, eco_blocked_board) is sys_ch

    eco_no_board = SimpleNamespace(get_leaderboard=lambda _gid: None)
    assert announce_channel(guild, eco_no_board) is sys_ch

    guild_none = SimpleNamespace(id=1, me=object(), system_channel=blocked_ch, get_channel=lambda _cid: None)
    assert announce_channel(guild_none, eco_no_board) is None
