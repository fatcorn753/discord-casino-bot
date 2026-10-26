"""サーバーへの追加の承認（許可リスト・持ち主の承認・DM のボタン）。"""

import asyncio
import time
from types import SimpleNamespace

import discord
import pytest

from cogs.approval_cog import RENOTIFY_AFTER, ApprovalButton, ApprovalCog
from cogs.economy_cog import EconomyCog
from core import approval
from core.economy import Economy
from core.session import Sessions

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
    bot = SimpleNamespace(economy=eco, config=cfg, sessions=Sessions(cooldown=0), guilds=list(guilds),
                          get_guild=lambda gid: by_id.get(gid), get_user=lambda uid: owner, fetch_user=None)
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
    assert approval.is_allowed(eco, cfg, 10, FRIEND)                        # 設定ファイルで許可
    assert not approval.is_allowed(eco, cfg, 20, FRIEND)                    # 未承認
    assert approval.is_allowed(eco, cfg, 20, OWNER)                         # 持ち主は、どのサーバーでも使える
    eco.set_guild_status(20, approval.APPROVED)
    assert approval.is_allowed(eco, cfg, 20, FRIEND)
    eco.set_guild_status(20, approval.DENIED)
    assert not approval.is_allowed(eco, cfg, 20, FRIEND)
    eco.set_guild_status(30, approval.PENDING)
    assert not approval.is_allowed(eco, cfg, 30, FRIEND)


def test_without_approval_mode_old_behaviour_is_kept(eco):
    assert approval.is_allowed(eco, {"allowed_guild_ids": []}, 99, FRIEND)               # リストが空 = どこでも
    assert not approval.is_allowed(eco, {"allowed_guild_ids": [10]}, 99, FRIEND)         # リストあり = その中だけ
    assert approval.is_allowed(eco, {"allowed_guild_ids": [10]}, 10, FRIEND)
    assert not approval.is_allowed(eco, {"allowed_guild_ids": [10], "require_approval": False}, 99, FRIEND)


# ----------------------------- 入口の検査 ----------------------------- #
def test_guard_lets_allowed_servers_through_silently(eco):
    bot, cog, owner = make(eco)
    it = Interaction(bot, FRIEND, Guild(10))
    assert run(approval.guard(it)) is True and it.sent == [] and owner.sent == []


def test_unapproved_server_is_blocked_and_owner_is_asked_once(eco):
    g = Guild(20)
    bot, cog, owner = make(eco, guilds=[g])
    it = Interaction(bot, FRIEND, g)
    assert run(approval.guard(it)) is False
    assert "承認待ち" in it.sent[-1]["embed"].title and it.sent[-1]["ephemeral"]
    assert eco.get_guild_status(20)["status"] == approval.PENDING and len(owner.sent) == 1
    run(approval.guard(Interaction(bot, FRIEND, g)))                        # すぐもう一度使っても、DM は増えない
    assert len(owner.sent) == 1


def test_waiting_server_gets_a_reminder_after_an_hour(eco):
    g = Guild(20)
    bot, cog, owner = make(eco, guilds=[g])
    run(approval.guard(Interaction(bot, FRIEND, g)))
    eco._conn.execute("UPDATE guild_status SET notified_at=? WHERE guild_id=20", (time.time() - RENOTIFY_AFTER - 5,))
    eco._conn.commit()
    run(approval.guard(Interaction(bot, FRIEND, g)))
    assert len(owner.sent) == 2


def test_denied_server_is_not_asked_about_again(eco):
    g = Guild(20)
    bot, cog, owner = make(eco, guilds=[g])
    eco.set_guild_status(20, approval.DENIED, "x", OWNER)
    it = Interaction(bot, FRIEND, g)
    assert run(approval.guard(it)) is False
    assert "許可されていません" in it.sent[-1]["embed"].description and owner.sent == []


def test_owner_can_use_commands_in_a_pending_server_to_approve_it(eco):
    g = Guild(20)
    bot, cog, owner = make(eco, guilds=[g])
    assert run(approval.guard(Interaction(bot, OWNER, g))) is True


def test_server_error_is_clear_when_not_in_approval_mode(eco):
    bot, cog, owner = make(eco, config={"require_approval": False})
    it = Interaction(bot, FRIEND, Guild(20))
    assert run(approval.guard(it)) is False and "使えません" in it.sent[-1]["embed"].title


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
    run(cog.on_guild_join(ok))                                              # 承認済みのサーバーへ追加し直されても、聞き直さない
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
    assert eco.get_guild_status(20)["status"] == approval.PENDING            # DM が届かなくても、承認待ちとして記録される


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
    assert "許可しました" in it.edited[-1]["embed"].title and it.edited[-1]["view"] is None       # ボタンは消える
    assert approval.is_allowed(eco, bot.config, 20, FRIEND)                                         # すぐ使える


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
    assert re.fullmatch(r"guild-approve:(?P<action>allow|deny):(?P<gid>[0-9]+)", ApprovalButton("deny", 9).item.custom_id)
    restored = run(ApprovalButton.from_custom_id(None, None, {"action": "deny", "gid": "77"}))
    assert (restored.action, restored.guild_id) == ("deny", 77)


# ----------------------------- 持ち主用コマンド ----------------------------- #
def eco_cog(bot):
    return EconomyCog(bot)


def test_server_commands_are_owner_only(eco):
    g = Guild(20)
    bot, cog, owner = make(eco, guilds=[g])
    c = eco_cog(bot)
    for cmd, args in ((EconomyCog.server_list, ()), (EconomyCog.server_allow, ("20",)), (EconomyCog.server_revoke, ("20",))):
        it = Interaction(bot, FRIEND, g)
        run(cmd.callback(c, it, *args))
        assert "権限がありません" in it.sent[-1]["embed"].title
    assert eco.get_guild_status(20) is None


def test_owner_can_allow_list_and_revoke(eco):
    g = Guild(20, "友達の鯖")
    bot, cog, owner = make(eco, guilds=[g])
    c = eco_cog(bot)
    run(EconomyCog.server_allow.callback(c, Interaction(bot, OWNER, g), "20"))
    assert eco.get_guild_status(20)["status"] == approval.APPROVED
    it = Interaction(bot, OWNER, g)
    run(EconomyCog.server_list.callback(c, it))
    text = "\n".join(f.value for f in it.sent[-1]["embed"].fields)
    assert "友達の鯖" in text and "10" in text                              # 許可済みの一覧に出る（設定ファイル分も）
    run(EconomyCog.server_revoke.callback(c, Interaction(bot, OWNER, g), "20"))
    assert eco.get_guild_status(20)["status"] == approval.DENIED and g.left


def test_revoke_refuses_preapproved_servers_and_bad_ids(eco):
    g = Guild(10)
    bot, cog, owner = make(eco, guilds=[g])
    c = eco_cog(bot)
    it = Interaction(bot, OWNER, g)
    run(EconomyCog.server_revoke.callback(c, it, "10"))
    assert "取り消せません" in it.sent[-1]["embed"].title and not g.left
    for bad in ("abc", "１２３", "12 3", ""):
        it = Interaction(bot, OWNER, g)
        run(EconomyCog.server_allow.callback(c, it, bad))
        assert "入力エラー" in it.sent[-1]["embed"].title
    assert eco.list_guild_status() == []


def test_allow_can_preapprove_a_server_the_bot_has_not_joined_yet(eco):
    bot, cog, owner = make(eco)
    run(EconomyCog.server_allow.callback(eco_cog(bot), Interaction(bot, OWNER, Guild(10)), "555"))
    assert approval.is_allowed(eco, bot.config, 555, FRIEND)


def test_guild_status_roundtrip_keeps_name(eco):
    eco.set_guild_status(5, approval.PENDING, "名前")
    eco.set_guild_status(5, approval.APPROVED, None, OWNER)               # 名前を渡さなくても、前の名前は残る
    row = eco.get_guild_status(5)
    assert row["name"] == "名前" and row["status"] == approval.APPROVED and row["decided_by"] == OWNER
