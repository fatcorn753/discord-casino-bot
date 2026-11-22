"""共通部品（infra/clock, cogs/shared/ui.OwnerView, cogs/shared/confirm, cogs/shared）の単体テスト。"""

import asyncio
import datetime
from types import SimpleNamespace
from typing import cast

import discord
import pytest

from cogs.shared import ui
from cogs.shared.announce import announce_channel
from cogs.shared.confirm import confirmed_reply, user_session
from domain.ports.contracts import LeaderboardRow
from domain.ports.repos import ILeaderboardConfigRepo
from infra.clock import ConfigClock
from services.session import Sessions
from tests.fakes import as_guild, as_interaction


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


# ---- infra.clock ---- #
def test_clock_uses_config_offset():
    default = ConfigClock({})
    now_jst = default.now()
    assert now_jst.utcoffset() == datetime.timedelta(hours=9) and default.offset_hours() == 9
    assert default.today() == now_jst.date()

    utc_minus5 = ConfigClock({"utc_offset_hours": -5})
    assert utc_minus5.now().utcoffset() == datetime.timedelta(hours=-5)
    assert abs(utc_minus5.now_ts() - default.now_ts()) < 5  # 時差が違っても、同じ瞬間を指す


# ---- cogs.shared.ui.OwnerView ---- #
def test_owner_view_allows_owner_and_rejects_others():
    async def run():
        view = ui.OwnerView(owner_id=10, owner_message="本人専用です")
        ok_it, ok_sent, _ = make_interaction(10)
        assert await view.interaction_check(as_interaction(ok_it)) is True
        assert ok_sent == []

        bad_it, bad_sent, _ = make_interaction(99)
        assert await view.interaction_check(as_interaction(bad_it)) is False
        assert bad_sent == [{"content": "本人専用です", "embed": None, "ephemeral": True}]

    asyncio.run(run())


def test_owner_view_silent_and_custom_rejection():
    class CustomView(ui.OwnerView):
        async def _reject_non_owner(self, interaction: discord.Interaction) -> None:
            await interaction.response.send_message(embed=ui.error_embed("権限なし"), ephemeral=True)

    async def run():
        silent = ui.OwnerView(owner_id=10)
        it1, sent1, _ = make_interaction(99)
        assert await silent.interaction_check(as_interaction(it1)) is False
        assert sent1 == []

        custom = CustomView(owner_id=10)
        it2, sent2, _ = make_interaction(99)
        assert await custom.interaction_check(as_interaction(it2)) is False
        assert len(sent2) == 1 and "権限なし" in sent2[0]["embed"].title

    asyncio.run(run())


# ---- cogs.shared.ui の応答ヘルパ（reply_embed / reply_error / reply_warn / reply_ok） ----
def test_reply_helpers_send_via_response_when_not_done():
    async def run():
        it, sent, followed = make_interaction(1)  # この Resp は is_done を持たない（未応答として扱う）
        await ui.reply_error(as_interaction(it), "本文です", title="失敗")
        await ui.reply_warn(as_interaction(it), "注意です")
        await ui.reply_ok(as_interaction(it), "できました")
        await ui.reply_embed(as_interaction(it), ui.embed("そのまま", "渡すだけ"), ephemeral=False)
        assert followed == []
        assert [s["embed"].title for s in sent] == ["❌ 失敗", "⚠️ 確認", "✅ 完了", "そのまま"]
        assert sent[0]["embed"].description == "本文です" and sent[0]["ephemeral"] is True
        assert sent[1]["embed"].description == "注意です" and sent[1]["ephemeral"] is True
        assert sent[2]["embed"].description == "できました" and sent[2]["ephemeral"] is True
        assert sent[3]["ephemeral"] is False

    asyncio.run(run())


def test_reply_error_uses_default_title():
    async def run():
        it, sent, _ = make_interaction(1)
        await ui.reply_error(as_interaction(it), "本文")
        assert sent[0]["embed"].title == "❌ できません" and sent[0]["embed"].description == "本文"

    asyncio.run(run())


def test_reply_helpers_followup_when_response_already_done():
    sent_via_response = []

    class Resp:
        def is_done(self):
            return True

        async def send_message(self, *args, **kw):
            sent_via_response.append(kw)

    class Follow:
        def __init__(self):
            self.sent = []

        async def send(self, *args, **kw):
            self.sent.append(kw)

    async def run():
        it = SimpleNamespace(user=SimpleNamespace(id=1), response=Resp(), followup=Follow())
        await ui.reply_error(as_interaction(it), "本文", title="失敗", ephemeral=False)
        assert sent_via_response == []
        assert len(it.followup.sent) == 1
        assert "❌ 失敗" in it.followup.sent[0]["embed"].title
        assert it.followup.sent[0]["embed"].description == "本文"
        assert it.followup.sent[0]["ephemeral"] is False

    asyncio.run(run())


# ---- cogs.shared.confirm.user_session & confirmed_reply ---- #
def test_user_session_lifecycle_and_exception_cleanup():
    sessions = Sessions(cooldown=0)

    async def run():
        it, sent, _ = make_interaction(1)
        async with user_session(sessions, as_interaction(it)) as ok:
            assert ok is True
            assert sessions.is_active(1)
        assert not sessions.is_active(1)
        assert sent == []

        with pytest.raises(RuntimeError):
            async with user_session(sessions, as_interaction(it)) as ok:
                assert ok is True
                raise RuntimeError("boom")
        assert not sessions.is_active(1)

    asyncio.run(run())


def test_user_session_rejects_when_already_busy():
    sessions = Sessions()
    assert sessions.try_start(1) is None

    async def run():
        it, sent, _ = make_interaction(1)
        async with user_session(sessions, as_interaction(it), busy_title="いまは買えません") as ok:
            assert ok is False
        assert len(sent) == 1
        assert "いまは買えません" in sent[0]["embed"].title
        assert sent[0]["ephemeral"] is True
        assert sessions.is_active(1)  # 既存のセッションを勝手に閉じない

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

        r1 = await confirmed_reply(as_interaction(it), False, "t", "d", "ok", confirm_fn=yes)
        assert r1 == it.response.send_message
        assert called == []

        r2 = await confirmed_reply(as_interaction(it), True, "t", "d", "ok", confirm_fn=yes)
        assert r2 == it.followup.send
        assert len(called) == 1

        r3 = await confirmed_reply(as_interaction(it), True, "t", "d", "ok", confirm_fn=no)
        assert r3 is None

        r4 = await confirmed_reply(as_interaction(it), True, "t", "d", "ok", confirm_fn=timeout)
        assert r4 is None

    asyncio.run(run())


# ---- cogs.shared.announce.announce_channel ---- #
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

    class Boards(ILeaderboardConfigRepo):
        def __init__(self, channel_id: int | None) -> None:
            self._channel_id = channel_id

        def get_leaderboard(self, guild_id: int) -> LeaderboardRow | None:
            return None if self._channel_id is None else cast("LeaderboardRow", {"channel_id": self._channel_id})

    eco_with_board = Boards(100)
    assert announce_channel(as_guild(guild), eco_with_board) is board_ch

    eco_blocked_board = Boards(300)
    assert announce_channel(as_guild(guild), eco_blocked_board) is sys_ch

    eco_no_board = Boards(None)
    assert announce_channel(as_guild(guild), eco_no_board) is sys_ch

    guild_none = SimpleNamespace(id=1, me=object(), system_channel=blocked_ch, get_channel=lambda _cid: None)
    assert announce_channel(as_guild(guild_none), eco_no_board) is None


def test_messages_format_failures_notices_and_memo():
    from cogs.shared.messages import format_failure, format_memo, format_notice
    from domain.earnings import MemoInfo
    from domain.errors import fail, notice

    assert "1,000ペリカ" in format_failure(fail("insufficient_balance", balance=1000))
    assert "<t:123:R>" in format_failure(fail("escape_cooldown", ready=123))
    assert "<@42>" in format_notice(notice("cds_default_payout", target_id=42, pay=500))
    assert "<t:999:F>" in format_memo(MemoInfo("CSトークン", 1, 999.0, 0.8))


def test_bot_bridges_stores_and_exposes_typed_bridges():
    from cogs.shared.bridges import BotBridges, IApprovalBridge, IShopTitleBridge, NoOpShopTitleBridge

    bridges = BotBridges()
    assert bridges.resolve(IShopTitleBridge) is None
    assert bridges.resolve(IApprovalBridge) is None
    assert not bridges.has(IShopTitleBridge)
    assert isinstance(bridges.resolve_or(IShopTitleBridge, NoOpShopTitleBridge()), NoOpShopTitleBridge)

    class StubShop(IShopTitleBridge):
        async def sync_title_nick(self, user_id, here=None, member=None):
            return ("ok", 1)

        async def after_bankruptcy(self, guild, member):
            return None

    stub = StubShop()
    bridges.register(IShopTitleBridge, stub)
    assert bridges.has(IShopTitleBridge)
    assert bridges.resolve(IShopTitleBridge) is stub
    assert bridges.resolve_or(IShopTitleBridge, NoOpShopTitleBridge()) is stub
    assert isinstance(bridges.resolve(IShopTitleBridge), IShopTitleBridge)
    bridges.unregister(IShopTitleBridge, stub)
    assert not bridges.has(IShopTitleBridge)
