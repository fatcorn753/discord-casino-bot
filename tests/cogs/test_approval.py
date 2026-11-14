"""サーバーへの追加の承認（許可リスト・持ち主の承認・DM のボタン）。"""

import asyncio
import time
from types import SimpleNamespace

import discord
import pytest

from cogs.admin.cog import RENOTIFY_AFTER, ApprovalButton, ApprovalCog
from cogs.shared import approval_guard
from domain import approval
from infra.db.economy import Economy
from services.session import Sessions

OWNER, FRIEND = 1, 2


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def eco(tmp_path):
    return Economy(str(tmp_path / "t.db"))


class Guild:
    def __init__(self, gid, name="友達の鯖"):
        self.id, self.name, self.owner_id, self.member_count, self.icon = gid, name, 99, 12, None
        self.system_channel, self.text_channels, self.left = None, [], False
        self.me = SimpleNamespace()

    async def leave(self):
        self.left = True


class Owner:
    """DM を受け取る持ち主。"""

    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send(self, **kw):
        if self.fail:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="x"), "DM 不可")
        self.sent.append(kw)


def make(eco, config=None, guilds=(), owner=None):
    owner = owner or Owner()
    cfg = {"owner_ids": [OWNER], "require_approval": True, "allowed_guild_ids": [10], **(config or {})}
    by_id = {g.id: g for g in guilds}
    bot = SimpleNamespace(
        economy=eco,
        config=cfg,
        sessions=Sessions(cooldown=0),
        guilds=list(guilds),
        get_guild=lambda gid: by_id.get(gid),
        get_user=lambda uid: owner,
        fetch_user=None,
    )
    cog = ApprovalCog(bot)
    bot.get_cog = lambda name: cog if name == "ApprovalCog" else None
    return bot, cog, owner


class Interaction:
    def __init__(self, bot, uid, guild):
        self.client, self.user, self.guild, self.guild_id = bot, SimpleNamespace(id=uid), guild, guild.id
        self.sent, self.edited = [], []
        self.response = SimpleNamespace(send_message=self._send, edit_message=self._edit)

    async def _send(self, *a, **kw):
        self.sent.append(kw if not a else {**kw, "content": a[0]})

    async def _edit(self, **kw):
        self.edited.append(kw)


# ----------------------------- 判定 ----------------------------- #
def test_is_allowed_matrix(eco):
    cfg = {"owner_ids": [OWNER], "allowed_guild_ids": [10], "require_approval": True}
    assert approval.is_allowed(eco, cfg, 10, FRIEND)  # 設定ファイルで許可
    assert not approval.is_allowed(eco, cfg, 20, FRIEND)  # 未承認
    assert approval.is_allowed(eco, cfg, 20, OWNER)  # 持ち主は、どのサーバーでも使える
    eco.set_guild_status(20, approval.APPROVED)
    assert approval.is_allowed(eco, cfg, 20, FRIEND)
    eco.set_guild_status(20, approval.DENIED)
    assert not approval.is_allowed(eco, cfg, 20, FRIEND)
    eco.set_guild_status(30, approval.PENDING)
    assert not approval.is_allowed(eco, cfg, 30, FRIEND)


def test_without_approval_mode_old_behaviour_is_kept(eco):
    assert approval.is_allowed(eco, {"allowed_guild_ids": []}, 99, FRIEND)  # リストが空 = どこでも
    assert not approval.is_allowed(eco, {"allowed_guild_ids": [10]}, 99, FRIEND)  # リストあり = その中だけ
    assert approval.is_allowed(eco, {"allowed_guild_ids": [10]}, 10, FRIEND)
    assert not approval.is_allowed(eco, {"allowed_guild_ids": [10], "require_approval": False}, 99, FRIEND)


# ----------------------------- 入口の検査 ----------------------------- #
def test_guard_lets_allowed_servers_through_silently(eco):
    bot, cog, owner = make(eco)
    it = Interaction(bot, FRIEND, Guild(10))
    assert run(approval_guard.guard(it)) is True and it.sent == [] and owner.sent == []


def test_unapproved_server_is_blocked_and_owner_is_asked_once(eco):
    g = Guild(20)
    bot, cog, owner = make(eco, guilds=[g])
    it = Interaction(bot, FRIEND, g)
    assert run(approval_guard.guard(it)) is False
    assert "承認待ち" in it.sent[-1]["embed"].title and it.sent[-1]["ephemeral"]
    assert eco.get_guild_status(20)["status"] == approval.PENDING and len(owner.sent) == 1
    run(approval_guard.guard(Interaction(bot, FRIEND, g)))  # すぐもう一度使っても、DM は増えない
    assert len(owner.sent) == 1


def test_waiting_server_gets_a_reminder_after_an_hour(eco):
    g = Guild(20)
    bot, cog, owner = make(eco, guilds=[g])
    run(approval_guard.guard(Interaction(bot, FRIEND, g)))
    eco._conn.execute("UPDATE guild_status SET notified_at=? WHERE guild_id=20", (time.time() - RENOTIFY_AFTER - 5,))
    eco._conn.commit()
    run(approval_guard.guard(Interaction(bot, FRIEND, g)))
    assert len(owner.sent) == 2


def test_denied_server_is_not_asked_about_again(eco):
    g = Guild(20)
    bot, cog, owner = make(eco, guilds=[g])
    eco.set_guild_status(20, approval.DENIED, "x", OWNER)
    it = Interaction(bot, FRIEND, g)
    assert run(approval_guard.guard(it)) is False
    assert "許可されていません" in it.sent[-1]["embed"].description and owner.sent == []


def test_owner_can_use_commands_in_a_pending_server_to_approve_it(eco):
    g = Guild(20)
    bot, cog, owner = make(eco, guilds=[g])
    assert run(approval_guard.guard(Interaction(bot, OWNER, g))) is True


def test_server_error_is_clear_when_not_in_approval_mode(eco):
    bot, cog, owner = make(eco, config={"require_approval": False})
    it = Interaction(bot, FRIEND, Guild(20))
    assert run(approval_guard.guard(it)) is False and "使えません" in it.sent[-1]["embed"].title


# ----------------------------- 追加された時 ----------------------------- #
def test_joining_a_new_server_asks_the_owner_with_buttons(eco):
    g = Guild(20)
    bot, cog, owner = make(eco, guilds=[g])
    run(cog.on_guild_join(g))
    assert eco.get_guild_status(20)["status"] == approval.PENDING
    msg = owner.sent[0]
    assert "新しいサーバー" in msg["embed"].title and "友達の鯖" in msg["embed"].fields[0].value
    ids = [item.custom_id for item in msg["view"].children]
    assert ids == ["guild-approve:allow:20", "guild-approve:deny:20"]


def test_preapproved_and_already_approved_servers_are_not_asked(eco):
    pre, ok = Guild(10), Guild(30)
    bot, cog, owner = make(eco, guilds=[pre, ok])
    eco.set_guild_status(30, approval.APPROVED)
    run(cog.on_guild_join(pre))
    run(cog.on_guild_join(ok))  # 承認済みのサーバーへ追加し直されても、聞き直さない
    assert owner.sent == []


def test_readding_after_a_refusal_asks_again(eco):
    g = Guild(20)
    bot, cog, owner = make(eco, guilds=[g])
    eco.set_guild_status(20, approval.DENIED, "x", OWNER)
    run(cog.on_guild_join(g))
    assert eco.get_guild_status(20)["status"] == approval.PENDING and len(owner.sent) == 1


def test_dm_failure_does_not_break_anything(eco):
    g = Guild(20)
    bot, cog, owner = make(eco, guilds=[g], owner=Owner(fail=True))
    run(cog.on_guild_join(g))
    assert eco.get_guild_status(20)["status"] == approval.PENDING  # DM が届かなくても、承認待ちとして記録される


def test_startup_picks_up_servers_added_while_the_bot_was_down(eco):
    g1, g2 = Guild(10), Guild(40)
    bot, cog, owner = make(eco, guilds=[g1, g2])
    run(cog.on_ready())
    assert eco.get_guild_status(40)["status"] == approval.PENDING and len(owner.sent) == 1


def test_nothing_happens_when_approval_mode_is_off(eco):
    g = Guild(20)
    bot, cog, owner = make(eco, config={"require_approval": False}, guilds=[g])
    run(cog.on_guild_join(g))
    run(cog.on_ready())
    assert owner.sent == [] and eco.get_guild_status(20) is None


# ----------------------------- ボタン ----------------------------- #
def test_only_owners_can_press_the_buttons(eco):
    bot, cog, owner = make(eco)
    it = Interaction(bot, FRIEND, Guild(20))
    assert run(ApprovalButton("allow", 20).interaction_check(it)) is False and it.sent[0]["ephemeral"]
    assert run(ApprovalButton("allow", 20).interaction_check(Interaction(bot, OWNER, Guild(20)))) is True


def test_allow_button_approves_without_restart(eco):
    g = Guild(20)
    bot, cog, owner = make(eco, guilds=[g])
    eco.set_guild_status(20, approval.PENDING, "友達の鯖")
    it = Interaction(bot, OWNER, g)
    it.client = bot
    run(ApprovalButton("allow", 20).callback(it))
    assert eco.get_guild_status(20)["status"] == approval.APPROVED and eco.get_guild_status(20)["decided_by"] == OWNER
    assert "許可しました" in it.edited[-1]["embed"].title and it.edited[-1]["view"] is None  # ボタンは消える
    assert approval.is_allowed(eco, bot.config, 20, FRIEND)  # すぐ使える


def test_deny_button_refuses_and_leaves_the_server(eco):
    g = Guild(20)
    bot, cog, owner = make(eco, guilds=[g])
    eco.set_guild_status(20, approval.PENDING, "友達の鯖")
    it = Interaction(bot, OWNER, g)
    run(ApprovalButton("deny", 20).callback(it))
    assert g.left and eco.get_guild_status(20)["status"] == approval.DENIED
    assert not approval.is_allowed(eco, bot.config, 20, FRIEND)


def test_deny_works_even_if_the_bot_already_left(eco):
    bot, cog, owner = make(eco, guilds=[])
    eco.set_guild_status(20, approval.PENDING, "友達の鯖")
    run(ApprovalButton("deny", 20).callback(Interaction(bot, OWNER, Guild(20))))
    assert eco.get_guild_status(20)["status"] == approval.DENIED


def test_button_ids_survive_restart_format():
    import re

    assert ApprovalButton("allow", 123).item.custom_id == "guild-approve:allow:123"
    assert re.fullmatch(
        r"guild-approve:(?P<action>allow|deny):(?P<gid>[0-9]+)", ApprovalButton("deny", 9).item.custom_id
    )
    restored = run(ApprovalButton.from_custom_id(None, None, {"action": "deny", "gid": "77"}))
    assert (restored.action, restored.guild_id) == ("deny", 77)


# ----------------------------- 持ち主用コマンド ----------------------------- #
def eco_cog(bot):
    return ApprovalCog(bot)


def test_server_commands_are_owner_only(eco):
    g = Guild(20)
    bot, cog, owner = make(eco, guilds=[g])
    c = eco_cog(bot)
    for cmd, args in (
        (ApprovalCog.server_list, ()),
        (ApprovalCog.server_allow, ("20",)),
        (ApprovalCog.server_revoke, ("20",)),
    ):
        it = Interaction(bot, FRIEND, g)
        run(cmd.callback(c, it, *args))
        assert "権限がありません" in it.sent[-1]["embed"].title
    assert eco.get_guild_status(20) is None


def test_owner_can_allow_list_and_revoke(eco):
    g = Guild(20, "友達の鯖")
    bot, cog, owner = make(eco, guilds=[g])
    c = eco_cog(bot)
    run(ApprovalCog.server_allow.callback(c, Interaction(bot, OWNER, g), "20"))
    assert eco.get_guild_status(20)["status"] == approval.APPROVED
    it = Interaction(bot, OWNER, g)
    run(ApprovalCog.server_list.callback(c, it))
    text = "\n".join(f.value for f in it.sent[-1]["embed"].fields)
    assert "友達の鯖" in text and "10" in text  # 許可済みの一覧に出る（設定ファイル分も）
    run(ApprovalCog.server_revoke.callback(c, Interaction(bot, OWNER, g), "20"))
    assert eco.get_guild_status(20)["status"] == approval.DENIED and g.left


def test_revoke_refuses_preapproved_servers_and_bad_ids(eco):
    g = Guild(10)
    bot, cog, owner = make(eco, guilds=[g])
    c = eco_cog(bot)
    it = Interaction(bot, OWNER, g)
    run(ApprovalCog.server_revoke.callback(c, it, "10"))
    assert "取り消せません" in it.sent[-1]["embed"].title and not g.left
    for bad in ("abc", "１２３", "12 3", ""):
        it = Interaction(bot, OWNER, g)
        run(ApprovalCog.server_allow.callback(c, it, bad))
        assert "入力エラー" in it.sent[-1]["embed"].title
    assert eco.list_guild_status() == []


def test_allow_can_preapprove_a_server_the_bot_has_not_joined_yet(eco):
    bot, cog, owner = make(eco)
    run(ApprovalCog.server_allow.callback(eco_cog(bot), Interaction(bot, OWNER, Guild(10)), "555"))
    assert approval.is_allowed(eco, bot.config, 555, FRIEND)


def test_guild_status_roundtrip_keeps_name(eco):
    eco.set_guild_status(5, approval.PENDING, "名前")
    eco.set_guild_status(5, approval.APPROVED, None, OWNER)  # 名前を渡さなくても、前の名前は残る
    row = eco.get_guild_status(5)
    assert row["name"] == "名前" and row["status"] == approval.APPROVED and row["decided_by"] == OWNER


def test_concurrent_commands_send_only_one_request_dm():
    """未承認サーバーから同時にコマンドが来ても、承認依頼の DM は1通だけ。"""
    import asyncio
    import os
    import tempfile
    from types import SimpleNamespace

    from cogs.admin.cog import ApprovalCog
    from infra.db.economy import Economy

    eco = Economy(os.path.join(tempfile.mkdtemp(), "t.db"), strict=True)
    sent = []

    class User:
        async def send(self, **kw):
            await asyncio.sleep(0.01)
            sent.append(kw)

    bot = SimpleNamespace(
        economy=eco, config={"require_approval": True, "owner_ids": [7]}, get_user=lambda uid: User(), fetch_user=None
    )
    cog = ApprovalCog(bot)
    guild = SimpleNamespace(id=55, name="g", owner_id=1, member_count=3, icon=None)

    async def go():
        await asyncio.gather(*(cog.ensure_requested(guild) for _ in range(5)))

    asyncio.run(go())
    assert len(sent) == 1


# ----------------------------- DMでだけ使える「運営」と、管理ツール ----------------------------- #
def test_owner_commands_are_dm_only_and_not_in_other_groups():
    from discord import app_commands

    assert ApprovalCog.group.name == "運営"
    ctx = ApprovalCog.group.allowed_contexts
    assert ctx.dm_channel and ctx.private_channel and not ctx.guild  # サーバーのコマンド候補には出ない
    from cogs.economy.cog import EconomyCog

    assert not any(
        getattr(EconomyCog, n, None) for n in ("server_list", "server_allow", "server_revoke", "grant")
    )  # 「ペリカ」グループからは消えた
    assert {c.name for c in ApprovalCog.group.commands} == {
        "サーバー一覧",
        "サーバー許可",
        "サーバー取消",
        "付与",
        "残高設定",
        "競馬開催",
        "お知らせ",
        "発言削除",
        "抽選",
        "クイズ",
        "イベント",
        "還元率",
        "パチンコ統計",
        "クリック履歴",
    }
    assert app_commands  # noqa


def fake_dm_interaction(root_name, uid, owner_ids=(OWNER,), guild=None):
    sub = SimpleNamespace(name="サーバー一覧", root_parent=SimpleNamespace(name=root_name))
    return SimpleNamespace(
        command=sub,
        guild=guild,
        user=SimpleNamespace(id=uid),
        client=SimpleNamespace(config={"owner_ids": list(owner_ids)}),
    )


def test_only_the_owner_may_use_the_dm_group_outside_a_server():
    assert approval_guard.is_owner_dm_command(fake_dm_interaction("運営", OWNER))
    assert not approval_guard.is_owner_dm_command(fake_dm_interaction("運営", FRIEND))  # 他の人はDMでも使えない
    assert not approval_guard.is_owner_dm_command(fake_dm_interaction("ペリカ", OWNER))  # 他のコマンドはDMで使えない
    assert not approval_guard.is_owner_dm_command(
        fake_dm_interaction("運営", OWNER, guild=SimpleNamespace(id=1))
    )  # サーバー内の扱いは、いつもどおり
    assert not approval_guard.is_owner_dm_command(
        SimpleNamespace(
            command=None,
            guild=None,
            user=SimpleNamespace(id=OWNER),
            client=SimpleNamespace(config={"owner_ids": [OWNER]}),
        )
    )


@pytest.mark.real_scope
def test_manage_tool_grants_to_the_right_server_and_lists_servers(tmp_path, capsys, monkeypatch):
    import manage

    path = str(tmp_path / "casino.db")
    monkeypatch.setattr(manage, "DB_PATH", path)
    Economy(path, strict=True).set_guild_status(10, approval.APPROVED, "友達の鯖")
    assert manage.main(["grant", "10", "5", "777", "テスト"]) == 0
    assert (
        Economy(path, strict=True).for_guild(10).balance(5) == 3000 + 777
        and Economy(path, strict=True).for_guild(20).peek_stats(5) is None
    )
    assert manage.main(["servers"]) == 0
    out = capsys.readouterr().out
    assert "777" in out and "友達の鯖" in out
    assert (
        manage.main(["grant", "10", "5", "0"]) == 2 and manage.main(["grant", "10", "5", "99999999"]) == 2
    )  # 範囲外は断る
    assert Economy(path, strict=True).for_guild(10).balance(5) == 3000 + 777


def make_admin(tmp_path, guilds=None, extra_cogs=None):
    eco = Economy(str(tmp_path / "t.db"))
    cogs = dict(extra_cogs or {})
    guild = SimpleNamespace(id=5, name="友達鯖", members=[], fetch_member=None)
    by_id = {5: guild} if guilds is None else guilds
    bot = SimpleNamespace(
        config={"owner_ids": [OWNER]},
        economy=eco,
        guilds=list(by_id.values()),
        get_guild=lambda gid: by_id.get(gid),
        get_cog=lambda name: cogs.get(name),
    )
    return ApprovalCog(bot), eco, guild, bot


class Resp2:
    def __init__(self, log):
        self.log = log

    async def send_message(self, **kw):
        self.log.append(("send", kw["embed"].title))

    async def defer(self, **kw):
        self.log.append(("defer",))

    async def send_modal(self, modal):
        self.log.append(("modal", type(modal).__name__))


class Follow2:
    def __init__(self, log):
        self.log = log

    async def send(self, **kw):
        self.log.append(("follow", kw["embed"].title))


def dm(uid, log):
    return SimpleNamespace(
        user=SimpleNamespace(id=uid),
        guild_id=None,
        response=Resp2(log),
        followup=Follow2(log),
        namespace=SimpleNamespace(),
    )


def test_grant_in_dm_pays_the_chosen_member_in_the_chosen_guild(tmp_path):
    import asyncio

    from infra.db.economy import INITIAL_BALANCE

    cog, eco, guild, bot = make_admin(tmp_path)

    async def fetch(uid):
        if uid == 42:
            return SimpleNamespace(id=42, bot=False, mention="<@42>", display_name="友達")
        if uid == 43:
            return SimpleNamespace(id=43, bot=True, mention="<@43>", display_name="bot")
        raise discord.NotFound(SimpleNamespace(status=404, reason="x"), "unknown member")

    guild.fetch_member = fetch
    log = []
    asyncio.run(cog.grant.callback(cog, dm(FRIEND, log), "5", "42", 1000))
    assert log == [("send", "❌ 権限がありません")] and eco.balance(42) == INITIAL_BALANCE
    log.clear()
    asyncio.run(cog.grant.callback(cog, dm(OWNER, log), "5", "<@42>", 1000, "テスト"))  # メンションの形でも受け付ける
    assert log == [("send", "✅ 付与しました")] and eco.balance(42) == INITIAL_BALANCE + 1000
    log.clear()
    for args in (("999", "42"), ("5", "abc"), ("5", "43"), ("5", "44")):  # 不明なサーバー・ID・Bot・サーバーにいない人
        asyncio.run(cog.grant.callback(cog, dm(OWNER, log), *args, 1000))
    assert [t for _, t in log] == [
        "❌ サーバーが見つかりません",
        "❌ ユーザーを指定してください",
        "❌ Bot には付与できません",
        "❌ そのサーバーにいません",
    ]
    assert eco.balance(42) == INITIAL_BALANCE + 1000 and eco.balance(44) == INITIAL_BALANCE


def test_parse_user_id_and_autocomplete(tmp_path):
    import asyncio

    from cogs.admin.cog import parse_user_id

    assert parse_user_id(" 123 ") == 123 and parse_user_id("<@123>") == 123 and parse_user_id("<@!123>") == 123
    assert parse_user_id("abc") is None and parse_user_id("") is None and parse_user_id("0") is None
    cog, eco, guild, bot = make_admin(tmp_path)
    guild.members = [
        SimpleNamespace(id=1, name="alice", display_name="ありす", bot=False),
        SimpleNamespace(id=2, name="bot", display_name="bot", bot=True),
    ]
    inter = dm(OWNER, [])
    assert [c.value for c in asyncio.run(cog.guild_autocomplete(inter, "友達"))] == ["5"]
    assert asyncio.run(cog.guild_autocomplete(dm(FRIEND, []), "友達")) == []  # 持ち主以外には候補も出さない
    inter.namespace = SimpleNamespace(**{"サーバー": "5"})
    assert [c.value for c in asyncio.run(cog.user_autocomplete(inter, "ありす"))] == ["1"]  # Bot は出ない
    assert asyncio.run(cog.user_autocomplete(inter, "")) == []


def test_raffle_and_quiz_admin_commands_delegate_and_are_owner_only(tmp_path):
    import asyncio

    calls = []

    class FakeRaffle:
        async def host(self, guild, host_id, fee, minutes, channel=None):
            calls.append(("host", guild.id, fee, minutes))
            return True, "投稿した"

        async def repost(self, guild, channel=None):
            calls.append(("repost", guild.id))
            return False, "募集なし"

        async def cancel(self, guild):
            calls.append(("cancel", guild.id))
            return True, "中止した"

    class FakeQuiz:
        def add_modal(self, announce=True, guild=None, channel=None):
            calls.append(("modal", announce))
            return SimpleNamespace()

        def listing_embed(self):
            return __import__("cogs.shared.ui", fromlist=["ui"]).embed("一覧")

        def remove(self, qid):
            return qid == 1

    cog, eco, guild, bot = make_admin(tmp_path, extra_cogs={"RaffleCog": FakeRaffle(), "QuizCog": FakeQuiz()})
    log = []
    asyncio.run(cog.raffle_start.callback(cog, dm(FRIEND, log), "5", 1000, 30))
    asyncio.run(cog.quiz_delete.callback(cog, dm(FRIEND, log), 1))
    assert [x[1] for x in log] == ["❌ 権限がありません"] * 2 and calls == []
    log.clear()
    asyncio.run(cog.raffle_start.callback(cog, dm(OWNER, log), "5", 1000, 30))
    asyncio.run(cog.raffle_repost.callback(cog, dm(OWNER, log), "5"))
    asyncio.run(cog.raffle_cancel.callback(cog, dm(OWNER, log), "5"))
    asyncio.run(cog.raffle_start.callback(cog, dm(OWNER, log), "77", None, 60))  # 不明なサーバー
    assert calls == [("host", 5, 1000, 30), ("repost", 5), ("cancel", 5)]
    assert [x[1] for x in log if x[0] == "follow"] == [
        "✅ 抽選を始めました",
        "❌ 出し直せません",
        "✅ 中止しました",
    ] and log[-1] == ("send", "❌ サーバーが見つかりません")
    log.clear()
    asyncio.run(cog.quiz_add.callback(cog, dm(OWNER, log), SimpleNamespace(value=0)))
    asyncio.run(cog.quiz_list.callback(cog, dm(OWNER, log)))
    asyncio.run(cog.quiz_delete.callback(cog, dm(OWNER, log), 1))
    asyncio.run(cog.quiz_delete.callback(cog, dm(OWNER, log), 2))
    assert calls[-1] == ("modal", False) and [x[1] for x in log] == [
        "SimpleNamespace",
        "一覧",
        "✅ クイズ #1 を削除しました",
        "❌ その番号の問題はありません",
    ]


# ----------------------------- お知らせ ----------------------------- #
class FakeImage:
    def __init__(self, content_type="image/png", size=1000, filename="a.png", data=b"PNGDATA"):
        self.content_type, self.size, self.filename, self._data = content_type, size, filename, data

    async def read(self):
        return self._data


def make_announce(tmp_path, monkeypatch, guild_ids=(5, 6), no_channel=(), fail=()):
    guilds, posts = {}, []

    class Chan:
        def __init__(self, gid):
            self.gid = gid

        async def send(self, **kw):
            if self.gid in fail:
                raise discord.HTTPException(SimpleNamespace(status=403, reason="x"), "forbidden")
            posts.append(
                (
                    self.gid,
                    kw["embed"].title,
                    kw["embed"].description,
                    kw["embed"].image.url,
                    kw["file"] is not discord.utils.MISSING,
                )
            )

    for gid in guild_ids:
        guilds[gid] = SimpleNamespace(id=gid, name=f"鯖{gid}", members=[])
    cog, eco, _, bot = make_admin(tmp_path, guilds=guilds)
    monkeypatch.setattr("cogs.admin.views.announce_channel", lambda g, e: None if g.id in no_channel else Chan(g.id))
    monkeypatch.setattr("cogs.admin.cog.approval.is_allowed", lambda e, c, gid, uid: gid != 6 or uid != 0)
    return cog, guilds, posts


def test_announce_validates_image_and_opens_the_modal_for_the_owner_only(tmp_path, monkeypatch):
    import asyncio

    cog, guilds, posts = make_announce(tmp_path, monkeypatch)
    log = []
    asyncio.run(cog.announce.callback(cog, dm(FRIEND, log), None, None, None))
    assert log == [("send", "❌ 権限がありません")]
    log.clear()
    asyncio.run(cog.announce.callback(cog, dm(OWNER, log), None, FakeImage(content_type="application/pdf"), None))
    asyncio.run(cog.announce.callback(cog, dm(OWNER, log), None, FakeImage(size=9 * 1024 * 1024), None))
    asyncio.run(cog.announce.callback(cog, dm(OWNER, log), "999", None, None))
    assert [x[1] for x in log] == ["❌ 画像を使えません", "❌ 画像を使えません", "❌ サーバーが見つかりません"]
    log.clear()
    asyncio.run(cog.announce.callback(cog, dm(OWNER, log), "5", FakeImage(), "見出し"))
    assert log == [("modal", "AnnounceModal")]


def run_announce(cog, guilds_arg, heading, body, image, clicks):
    """モーダルの送信→確認ボタンまでを動かす。clicks は押すボタンの名前（send / cancel）。"""
    import asyncio

    from cogs.admin.cog import AnnounceModal

    edits, shown = [], {}

    class Follow:
        async def send(self, **kw):
            shown.update(kw)

    class Resp:
        async def defer(self, **kw):
            pass

        async def edit_message(self, **kw):
            assert not ("embed" in kw and "embeds" in kw), (
                "embed と embeds は同時に渡せない（Discord.py が TypeError にする）"
            )
            edits.append(kw["embeds"][0].title)

    inter = SimpleNamespace(user=SimpleNamespace(id=OWNER), response=Resp(), followup=Follow())

    async def edit_original(**kw):
        edits.append(kw["embed"].title + "|" + kw["embed"].description)

    inter.edit_original_response = edit_original
    modal = AnnounceModal(cog, guilds_arg, heading, image)
    modal.body._value = body
    asyncio.run(modal.on_submit(inter))
    view = shown["view"]
    other = SimpleNamespace(user=SimpleNamespace(id=FRIEND))
    assert asyncio.run(view.interaction_check(other)) is False  # 持ち主以外は押せない
    asyncio.run(getattr(view, clicks).callback(inter))
    return shown, edits


def test_announce_preview_then_send_to_all_allowed_guilds_with_image(tmp_path, monkeypatch):
    cog, guilds, posts = make_announce(tmp_path, monkeypatch)
    shown, edits = run_announce(cog, list(guilds.values()), "", "こんにちは\n2行目", FakeImage(), "send")
    assert [e.title for e in shown["embeds"]] == ["📢 送信前の確認", "📢 お知らせ"] and len(
        shown["files"]
    ) == 1  # プレビューが出る
    assert "鯖5" in shown["embeds"][0].description and "鯖6" in shown["embeds"][0].description
    assert posts == [
        (5, "📢 お知らせ", "こんにちは\n2行目", "attachment://a.png", True),
        (6, "📢 お知らせ", "こんにちは\n2行目", "attachment://a.png", True),
    ]
    assert edits[0] == "📢 送信中…" and edits[1].startswith("✅ お知らせを送りました|✅ 2サーバーに送りました")


def test_announce_reports_failures_and_text_only_has_no_file(tmp_path, monkeypatch):
    cog, guilds, posts = make_announce(tmp_path, monkeypatch, guild_ids=(5, 6, 7), no_channel=(6,), fail=(7,))
    shown, edits = run_announce(cog, list(guilds.values()), "重要", "本文のみ", None, "send")
    assert shown["files"] == [] and [x[0] for x in posts] == [
        5
    ]  # 添付なし。鯖6は投稿先なし、鯖7は投稿失敗、鯖5だけ成功
    final = edits[-1]
    assert "1サーバーに送りました" in final and "鯖6（投稿できるチャンネルがありません）" in final and "鯖7" in final


def test_announce_text_only_post_has_no_attachment(tmp_path, monkeypatch):
    cog, guilds, posts = make_announce(tmp_path, monkeypatch, guild_ids=(5,))
    run_announce(cog, [guilds[5]], "重要", "本文のみ", None, "send")
    assert posts == [(5, "重要", "本文のみ", None, False)]


def test_announce_cancel_sends_nothing(tmp_path, monkeypatch):
    cog, guilds, posts = make_announce(tmp_path, monkeypatch)
    shown, edits = run_announce(cog, list(guilds.values()), "", "やめる", None, "cancel")
    assert posts == [] and edits == ["📢 お知らせ"]


def test_event_admin_commands_target_one_or_all_allowed_guilds_and_are_owner_only(tmp_path, monkeypatch):
    import asyncio

    calls = []

    class FakeEvent:
        async def start_manual(self, guild, game, boost, minutes, channel=None):
            calls.append(("start", guild.id, game, boost, minutes))
            return True, f"{guild.name}: 開いた"

        async def stop_manual(self, guild):
            calls.append(("stop", guild.id))
            return True, f"{guild.name}: 終わらせた"

    guilds = {5: SimpleNamespace(id=5, name="鯖5", members=[]), 6: SimpleNamespace(id=6, name="鯖6", members=[])}
    cog, eco, _, bot = make_admin(tmp_path, guilds=guilds, extra_cogs={"EventCog": FakeEvent()})
    monkeypatch.setattr("cogs.admin.cog.approval.is_allowed", lambda e, c, gid, uid: gid != 6)  # 6 は許可されていない
    log = []
    game = SimpleNamespace(value="スロット")
    asyncio.run(cog.event_start.callback(cog, dm(FRIEND, log), game, 30, 50, None))
    asyncio.run(cog.event_stop.callback(cog, dm(FRIEND, log), None))
    assert [x[1] for x in log] == ["❌ 権限がありません"] * 2 and calls == []
    log.clear()
    asyncio.run(cog.event_start.callback(cog, dm(OWNER, log), game, 45, 80, None))  # 全サーバー（許可されている分だけ）
    asyncio.run(
        cog.event_start.callback(cog, dm(OWNER, log), game, 10, 100, "6")
    )  # 指定したサーバーには、許可外でも開ける
    asyncio.run(cog.event_stop.callback(cog, dm(OWNER, log), "5"))
    asyncio.run(cog.event_start.callback(cog, dm(OWNER, log), game, 10, 100, "999"))  # 不明なサーバー
    assert calls == [("start", 5, "スロット", 80, 45), ("start", 6, "スロット", 100, 10), ("stop", 5)]
    assert [x[1] for x in log if x[0] == "follow"] == [
        "✅ 特別イベントを開きました（1サーバー）",
        "✅ 特別イベントを開きました（1サーバー）",
        "特別イベントを終わらせました",
    ]
    assert log[-1] == ("send", "❌ サーバーが見つかりません")


# ----------------------------- パチンコ統計 ----------------------------- #
def make_set(starts=3, hits=1, rounds=((2,),), spins=None):
    """テスト用の SetResult。spins を渡さなければ、starts 回転のうち先頭 hits 回が当たり。"""
    from domain.games import pachinko

    if spins is None:
        spins = tuple(
            pachinko.SpinInfo(
                "gold" if i == 0 else "", "super" if i == 0 else "", i < hits, tuple(rounds[i]) if i < hits else (), 0.1
            )
            for i in range(starts)
        )
    raw = sum(s.raw for s in spins)
    return pachinko.SetResult(len(spins), spins, raw)


@pytest.mark.real_scope
def test_pachinko_records_aggregate_by_omen_tier_guild_and_period(tmp_path):
    eco = Economy(str(tmp_path / "t.db"))
    g1, g2 = eco.for_guild(5), eco.for_guild(6)
    g1.pachinko_record_set(1, 100, 100, make_set(3, 1, ((20, 10),)), now=1000)  # 3回転、先頭が当たり（連チャン2、30倍）
    g1.pachinko_record_set(1, 100, 100, make_set(2, 0), now=2000)
    g2.pachinko_record_set(3, 50, 100, make_set(1, 1, ((2,),)), now=4000)
    data = eco.pachinko_stats()
    sets_n, hit_sets, bet, back, balls, starts, chain_max = data["sets"]
    assert (sets_n, hit_sets, bet, back, balls, starts, chain_max) == (3, 2, 250, 100 * 30 + 50 * 2, 300, 6, 2)
    assert (
        data["spins"] == (6, 2)
        and data["omen"] == {"gold": (3, 2), "": (3, 0)}
        and data["tier"] == {"super": (3, 2), "": (3, 0)}
    )  # 各セットの1回転目が、金・スーパー
    assert eco.pachinko_stats(5)["sets"][0] == 2 and eco.pachinko_stats(6)["sets"][0] == 1
    assert eco.pachinko_stats(5, since=1500)["sets"][:4] == (1, 0, 100, 0)  # 期間で絞れる
    assert eco.pachinko_stats(99)["sets"] == (0, 0, 0, 0, 0, 0, 0) and eco.pachinko_stats(99)["spins"] == (0, 0)


def test_pachinko_stats_embed_compares_actual_to_design():
    from cogs.admin.cog import pachinko_stats_embed
    from domain.games import pachinko

    empty = pachinko_stats_embed({"omen": {}, "tier": {}, "spins": (0, 0), "sets": (0, 0, 0, 0, 0, 0, 0)}, "全サーバー")
    assert "まだ記録がありません" in empty.description
    data = {
        "omen": {"gold": (200, 110), "": (50, 5)},
        "tier": {"super": (100, 45)},
        "spins": (250, 115),
        "sets": (40, 5, 4000, 3600, 4000, 280, 6),
    }
    e = pachinko_stats_embed(data, "テスト鯖・過去7日")
    assert "テスト鯖・過去7日" in e.title and "40セット（4,000発）" in e.description
    assert "実績 **7.00回**" in e.description and "設計 7.00回" in e.description  # 1セットあたりのスタート入賞
    assert "当たったセット: 実績 **12.5%**" in e.description and f"設計 {pachinko.hit_rate():.1%}" in e.description
    assert "還元率: 実績 **90.0%**" in e.description and "最大連チャン 6連" in e.description
    assert "1回転の当たり率: 実績 **46.00%**" in e.description
    omen, tier = e.fields[0].value, e.fields[1].value
    assert (
        "🟡 金: 200回転　実績 **55.0%**" in omen and "なし: 50回転　実績 **10.0%**" in omen and "（参考）" in omen
    )  # 50回転は参考
    assert "🔵 青: 0回転　実績 **—**" in omen
    assert "スーパー: 100回転　実績 **45.0%** ／ 設計" in tier and "45.0%（参考）" not in tier


def test_pachinko_stats_command_is_owner_only_and_scopes_to_the_chosen_guild(tmp_path):
    import asyncio

    cog, eco, guild, bot = make_admin(tmp_path)
    eco.for_guild(5).pachinko_record_set(1, 100, 100, make_set(1, 0))
    sent = []

    class R:
        async def send_message(self, **kw):
            sent.append(kw["embed"].title)

    inter = lambda uid: SimpleNamespace(user=SimpleNamespace(id=uid), guild_id=None, response=R())  # noqa: E731
    asyncio.run(cog.pachinko_stats.callback(cog, inter(FRIEND), None, None))
    asyncio.run(cog.pachinko_stats.callback(cog, inter(OWNER), "5", 7))
    asyncio.run(cog.pachinko_stats.callback(cog, inter(OWNER), None, None))
    asyncio.run(cog.pachinko_stats.callback(cog, inter(OWNER), "999", None))
    assert sent == [
        "❌ 権限がありません",
        "🎰 パチンコ統計（友達鯖・過去7日）",
        "🎰 パチンコ統計（全サーバー・全期間）",
        "❌ サーバーが見つかりません",
    ]


def test_set_balance_command_sets_exact_amount_for_owner_only(tmp_path):
    from infra.db.economy import INITIAL_BALANCE

    cog, eco, guild, bot = make_admin(tmp_path)

    async def fetch(uid):
        if uid == 42:
            return SimpleNamespace(id=42, bot=False, mention="<@42>", display_name="友達")
        if uid == 43:
            return SimpleNamespace(id=43, bot=True, mention="<@43>", display_name="bot")
        raise discord.NotFound(SimpleNamespace(status=404, reason="x"), "unknown member")

    guild.fetch_member = fetch
    log = []
    asyncio.run(cog.set_balance.callback(cog, dm(FRIEND, log), "5", "42", 100))
    assert log == [("send", "❌ 権限がありません")] and eco.balance(42) == INITIAL_BALANCE
    log.clear()
    asyncio.run(cog.set_balance.callback(cog, dm(OWNER, log), "5", "42", 100_000, "テスト"))
    assert log == [("send", "✅ 残高を設定しました")] and eco.balance(42) == 100_000
    log.clear()
    asyncio.run(cog.set_balance.callback(cog, dm(OWNER, log), "5", "42", 0))  # 減らすこともできる
    assert eco.balance(42) == 0
    asyncio.run(cog.set_balance.callback(cog, dm(OWNER, log), "5", "42", -5000))  # マイナスは借金
    assert eco.balance(42) == 0 and eco.debt(42) == 5000
    log.clear()
    for args in (("5", "43"), ("5", "44")):
        asyncio.run(cog.set_balance.callback(cog, dm(OWNER, log), *args, 1000))
    assert [t for _, t in log] == ["❌ Bot には設定できません", "❌ そのサーバーにいません"]


def test_economy_set_balance_exact_and_negative_means_debt(tmp_path):
    from infra.db.economy import INITIAL_BALANCE

    eco = Economy(str(tmp_path / "t.db"))
    assert eco.set_balance(42, 10_000, "テスト") == (INITIAL_BALANCE, 10_000, 0, 0)
    assert eco.set_balance(42, 0, "テスト") == (10_000, 0, 0, 0) and eco.balance(42) == 0
    assert eco.set_balance(42, 0, "テスト") == (0, 0, 0, 0)  # 同じ額なら、何も変わらない
    eco.credit(42, 700, "テスト")
    assert eco.set_balance(42, -5_000, "テスト") == (700, 0, 0, 5_000)  # 手元は0、借金は5,000ちょうど
    assert eco.balance(42) == 0 and eco.debt(42) == 5_000
    assert eco.set_balance(42, -2_000, "テスト")[2:] == (5_000, 2_000)  # いまの借金は置き換わる
    assert eco.set_balance(42, 1_000, "テスト")[2:] == (2_000, 2_000)  # 0以上なら、借金には触れない
    assert eco.balance(42) == 1_000 and eco.debt(42) == 2_000
