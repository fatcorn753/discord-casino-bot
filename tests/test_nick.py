"""称号をニックネームの後ろに付ける機能。"""

import asyncio
from types import SimpleNamespace

import discord
import pytest

from cogs import shop_cog
from core import nick, shop
from core.economy import Economy
from core.session import Sessions

TITLES = [i.name for i in shop.of_kind(shop.TITLE)]


# ------------------------------ 文字列処理 ------------------------------ #
def test_build_puts_title_after_the_name():
    assert nick.build("たつ", "ざわ…ざわ…") == "たつ【ざわ…ざわ…】"


def test_build_never_exceeds_the_limit_and_keeps_the_title():
    for title in TITLES:
        for base in ("あ" * 100, "😀" * 40, "a" * 100):
            built = nick.build(base, title)
            assert nick.ulen(built) <= nick.NICK_LIMIT and built.endswith(nick.suffix(title))
    assert nick.build("あ" * 100, "常連").startswith("あ")


def test_strip_only_removes_known_titles():
    assert nick.strip_title("たつ【ざわ…ざわ…】", TITLES) == "たつ"
    assert nick.strip_title("たつ【自作の飾り】", TITLES) == "たつ【自作の飾り】"      # 利用者が自分で付けた【…】は消さない
    assert nick.strip_title("たつ【常連】【常連】", TITLES) == "たつ【常連】"          # 末尾の1つだけ
    assert nick.strip_title("", TITLES) == "" and nick.strip_title(None, TITLES) == ""


def plan(cur, state, title):
    return nick.plan(cur, state, "tatu", TITLES, title)


def test_plan_adds_and_replaces_titles():
    new, st = plan(None, None, "常連")
    assert new == "tatu【常連】" and st == nick.NickState(None, "tatu【常連】")      # ニックネームなし → 表示名の後ろ
    new, st = plan("あだ名", None, "常連")
    assert new == "あだ名【常連】" and st.original == "あだ名"
    new, st = plan("あだ名【常連】", st, "地下の帝王")
    assert new == "あだ名【地下の帝王】" and st.original == "あだ名"                 # 付け替えても元の名前は変わらない


def test_plan_restores_exactly_what_was_there_before():
    _, st = plan(None, None, "常連")
    assert plan("tatu【常連】", st, None) == (None, None)                           # もともとニックネームなし → なしに戻る
    _, st = plan("あだ名", None, "常連")
    assert plan("あだ名【常連】", st, None) == ("あだ名", None)


def test_long_nickname_is_restored_in_full_after_truncation():
    """長い名前は称号のために切り詰めるが、記録があるので外せば完全に元へ戻る（切り詰めた名前は残らない）。"""
    long_nick = "あ" * 30
    new, st = plan(long_nick, None, "常連")
    assert nick.ulen(new) <= nick.NICK_LIMIT and new != long_nick + "【常連】"
    assert plan(new, st, None) == (long_nick, None)
    # 称号を付け替えても、元の長い名前から作り直す（「…」が積み重ならない）
    new2, st2 = plan(new, st, "地下の帝王")
    assert st2.original == long_nick and new2.count("…") <= 1
    assert plan(new2, st2, None) == (long_nick, None)


def test_plan_respects_a_nickname_the_user_changed_afterwards():
    _, st = plan("あだ名", None, "常連")
    new, st2 = plan("新しい名前", st, "常連")                                       # 途中で自分で変えた
    assert new == "新しい名前【常連】" and st2.original == "新しい名前"
    target, _ = plan("新しい名前【常連】", st2, None)
    assert target == "新しい名前"
    target, _ = plan("別の名前【常連】", st2, None)                                  # 外す前にまた変えられていた
    assert target == "別の名前"


def test_plan_without_state_strips_known_titles_but_not_custom_brackets():
    assert plan("ボス【常連】", None, None) == ("ボス", None)
    assert plan("ボス【自作】", None, None) == ("ボス【自作】", None)
    assert plan("tatu【常連】", None, None) == (None, None)                         # 表示名と同じなら、ニックネームを消す


def test_plan_with_only_a_title_as_nickname_never_returns_empty_string():
    target, _ = plan("【常連】", None, None)
    assert target is None                                                           # 空文字を edit に渡さない
    new, st = plan("【常連】", None, "常連")
    assert new == "tatu【常連】" and st.original is None


# ------------------------------ Discord 側 ------------------------------ #
class Role:
    def __init__(self, pos):
        self.position = pos

    def __ge__(self, other):
        return self.position >= other.position


class Member:
    def __init__(self, uid=1, nick_=None, role_pos=1):
        self.id, self.nick, self.global_name, self.name = uid, nick_, "たつ", "tatu"
        self.top_role = Role(role_pos)
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)
        self.nick = kw["nick"]


def guild_for(can=True, owner_id=999, bot_pos=10, gid=10):
    return SimpleNamespace(id=gid, owner_id=owner_id,
                           me=SimpleNamespace(top_role=Role(bot_pos), guild_permissions=SimpleNamespace(manage_nicknames=can)))


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def eco(tmp_path):
    e = Economy(str(tmp_path / "n.db"))
    e.set_nick_pref(10, 1, True)
    return e


def test_apply_sets_and_restores_nickname(eco):
    g, m = guild_for(), Member()
    run(shop_cog.apply_title_nick(g, m, "常連", eco))
    assert m.nick == "たつ【常連】"
    run(shop_cog.apply_title_nick(g, m, "地下の帝王", eco))
    assert m.nick == "たつ【地下の帝王】"
    run(shop_cog.apply_title_nick(g, m, None, eco))
    assert m.nick is None                                                      # 元の「ニックネームなし」に戻る
    assert eco.get_nick_state(10, 1) is None                                   # 記録も消える


def test_long_nickname_round_trip_through_the_bot(eco):
    g, m = guild_for(), Member(nick_="あ" * 30)
    run(shop_cog.apply_title_nick(g, m, "常連", eco))
    assert m.nick != "あ" * 30 and nick.ulen(m.nick) <= 32
    run(shop_cog.apply_title_nick(g, m, None, eco))
    assert m.nick == "あ" * 30                                                  # 切り詰めた名前ではなく、完全な元の名前


def test_apply_does_nothing_when_nothing_changes(eco):
    g, m = guild_for(), Member(nick_="あだ名")
    run(shop_cog.apply_title_nick(g, m, None, eco))
    assert m.edits == []                                                       # 無駄な API 呼び出しをしない


@pytest.mark.parametrize("case,expect", [
    ("no_permission", "ニックネームの管理"),
    ("owner", "持ち主"),
    ("higher_role", "Bot のロールより上"),
])
def test_apply_explains_why_it_cannot_change_the_nickname(case, expect):
    g = {"no_permission": guild_for(can=False), "owner": guild_for(owner_id=1), "higher_role": guild_for(bot_pos=5)}[case]
    m = Member(role_pos=5 if case == "higher_role" else 1)
    with pytest.raises(shop_cog.ShopError) as exc:
        run(shop_cog.apply_title_nick(g, m, "常連", Economy(":memory:")))
    assert expect in str(exc.value) and m.edits == []


@pytest.mark.parametrize("status", [403, 400, 429, 503])
def test_apply_reports_any_discord_failure_instead_of_pretending_success(eco, status):
    g, m = guild_for(), Member()

    async def fail(**kw):
        cls = discord.Forbidden if status == 403 else discord.HTTPException
        raise cls(SimpleNamespace(status=status, reason="x"), "err")
    m.edit = fail
    with pytest.raises(shop_cog.ShopError):
        run(shop_cog.apply_title_nick(g, m, "常連", eco))
    assert eco.get_nick_state(10, 1) is None                                   # 失敗したときは、記録を残さない


# ------------------------------ コマンドの流れ ------------------------------ #
class FakeInteraction:
    def __init__(self, member, guild):
        self.user, self.guild = member, guild
        self.sent = []
        self.response = SimpleNamespace(send_message=self._send, defer=self._defer)
        self.followup = SimpleNamespace(send=self._send)

    async def _send(self, *a, **kw):
        self.sent.append(kw)

    async def _defer(self, **kw):
        return None


@pytest.fixture
def setup(tmp_path):
    eco = Economy(str(tmp_path / "t.db"))
    eco.credit(1, 50_000, "テスト")
    cog = shop_cog.ShopCog(SimpleNamespace(economy=eco, sessions=Sessions(cooldown=0), get_guild=lambda gid: None))
    return eco, cog


def buy_title(eco, item_id):
    it = shop.BY_ID[item_id]
    assert eco.purchase(1, it.id, shop.label(it), it.price, True).ok


def mode(value):
    return SimpleNamespace(value=value, name=value)


def test_turning_on_applies_the_equipped_title(setup):
    eco, cog = setup
    buy_title(eco, "title_regular")
    eco.equip(1, "title", "title_regular")
    g, m = guild_for(), Member()
    cog.bot.get_guild = lambda gid: g if gid == g.id else None
    it = FakeInteraction(m, g)
    run(shop_cog.ShopCog.title_display.callback(cog, it, mode("on")))
    assert m.nick == "たつ【常連】" and eco.nick_pref(g.id, 1)


def test_turning_on_is_refused_when_the_bot_cannot_change_the_nickname(setup):
    eco, cog = setup
    g, m = guild_for(owner_id=1), Member()
    it = FakeInteraction(m, g)
    run(shop_cog.ShopCog.title_display.callback(cog, it, mode("on")))
    assert "持ち主" in it.sent[-1]["embed"].description and not eco.nick_pref(g.id, 1)      # 設定は有効にならない


def test_equipping_a_title_updates_the_nickname_only_when_enabled(setup):
    eco, cog = setup
    buy_title(eco, "title_regular")
    g, m = guild_for(), Member()
    cog.bot.get_guild = lambda gid: g if gid == g.id else None
    it = FakeInteraction(m, g)
    run(shop_cog.ShopCog.use.callback(cog, it, "title_regular"))
    assert m.nick is None and eco.get_profile(1)["title"] == "title_regular"           # 設定がOFFなら名前は変えない
    eco.set_nick_pref(g.id, 1, True)
    run(shop_cog.ShopCog.use.callback(cog, FakeInteraction(m, g), "title_regular"))
    assert m.nick == "たつ【常連】"


def test_unequipping_the_title_restores_the_nickname(setup):
    eco, cog = setup
    buy_title(eco, "title_regular")
    eco.equip(1, "title", "title_regular")
    eco.set_nick_pref(10, 1, True)
    g, m = guild_for(), Member(nick_="あだ名【常連】")
    cog.bot.get_guild = lambda gid: g if gid == g.id else None
    run(shop_cog.ShopCog.unequip.callback(cog, FakeInteraction(m, g), SimpleNamespace(value="title", name="称号")))
    assert m.nick == "あだ名" and eco.get_profile(1)["title"] is None


def test_turning_off_restores_the_nickname_and_the_setting(setup):
    eco, cog = setup
    eco.set_nick_pref(10, 1, True)
    g, m = guild_for(), Member(nick_="あだ名【地下の帝王】")
    run(shop_cog.ShopCog.title_display.callback(cog, FakeInteraction(m, g), mode("off")))
    assert m.nick == "あだ名" and not eco.nick_pref(10, 1)


def test_title_change_is_applied_only_to_the_server_where_it_was_made(setup):
    eco, cog = setup
    buy_title(eco, "title_regular")
    eco.set_nick_pref(10, 1, True)
    eco.set_nick_pref(20, 1, True)
    g1, g2 = guild_for(gid=10), guild_for(gid=20)
    m1, m2 = Member(), Member()
    guilds = {10: (g1, m1), 20: (g2, m2)}

    def get_guild(gid):
        g = guilds[gid][0]
        g.get_member = lambda uid, gid=gid: guilds[gid][1]
        return g
    cog.bot.get_guild = get_guild
    run(shop_cog.ShopCog.use.callback(cog, FakeInteraction(m1, g1), "title_regular"))
    assert m1.nick == "たつ【常連】" and m2.nick != "たつ【常連】"      # 称号はサーバーごとに別。他のサーバーには触れない


def test_nick_prefs_are_stored_per_server(setup):
    eco, _ = setup
    eco.set_nick_pref(10, 1, True)
    assert eco.nick_pref(10, 1) and not eco.nick_pref(20, 1) and eco.nick_pref_guilds(1) == [10]
    eco.set_nick_pref(10, 1, False)
    assert eco.nick_pref_guilds(1) == []


# ----------------- レビュー指摘（状態の整合・失敗の扱い）の回帰テスト ----------------- #
def test_turning_off_never_touches_nicknames_of_people_who_never_enabled_it(setup):
    eco, cog = setup
    g, m = guild_for(), Member(nick_="ボス【常連】")                 # 自作の【常連】。設定は一度もONにしていない
    run(shop_cog.ShopCog.title_display.callback(cog, FakeInteraction(m, g), mode("off")))
    assert m.nick == "ボス【常連】" and m.edits == []


def test_enabling_again_after_a_failure_keeps_the_existing_setting(setup):
    eco, cog = setup
    eco.set_nick_pref(10, 1, True)                                  # すでにON
    g = guild_for(can=False)                                        # いまは権限がなく、失敗する
    m = Member()
    it = FakeInteraction(m, g)
    run(shop_cog.ShopCog.title_display.callback(cog, it, mode("on")))
    assert eco.nick_pref(10, 1)                                     # 既存の設定は消えない


def test_failed_first_enable_is_rolled_back(setup):
    eco, cog = setup
    buy_title(eco, "title_regular")
    eco.equip(1, "title", "title_regular")
    g, m = guild_for(), Member()
    cog.bot.get_guild = lambda gid: g

    async def fail(**kw):
        raise discord.HTTPException(SimpleNamespace(status=503, reason="x"), "err")
    m.edit = fail
    it = FakeInteraction(m, g)
    run(shop_cog.ShopCog.title_display.callback(cog, it, mode("on")))
    assert not eco.nick_pref(10, 1)                                 # 付けられなかったので、設定もONにならない
    assert "ニックネームに付けられません" in it.sent[-1]["embed"].title and "503" not in it.sent[-1]["embed"].description          # エラー番号などの開発者向けの表示は出さない


def test_server_error_is_reported_when_equipping_not_shown_as_success(setup):
    eco, cog = setup
    buy_title(eco, "title_regular")
    eco.set_nick_pref(10, 1, True)
    g, m = guild_for(), Member()
    cog.bot.get_guild = lambda gid: g

    async def fail(**kw):
        raise discord.HTTPException(SimpleNamespace(status=400, reason="x"), "bad nick")
    m.edit = fail
    it = FakeInteraction(m, g)
    run(shop_cog.ShopCog.use.callback(cog, it, "title_regular"))
    text = it.sent[-1]["embed"].description
    assert "反映できませんでした" in text and "ニックネームにも反映しました" not in text


def test_failures_in_other_servers_are_counted_not_swallowed(setup):
    eco, cog = setup
    buy_title(eco, "title_regular")
    eco.equip(1, "title", "title_regular")
    eco.set_nick_pref(10, 1, True)
    eco.set_nick_pref(20, 1, True)
    g1, g2 = guild_for(gid=10), guild_for(gid=20, can=False)       # 2つ目は権限がなくて反映できない
    m1, m2 = Member(), Member()
    g1.get_member, g2.get_member = (lambda uid: m1), (lambda uid: m2)
    cog.bot.get_guild = lambda gid: {10: g1, 20: g2}[gid]
    problem, others = run(cog._sync_title_nick(1))      # サーバーを指定しない（全サーバー）
    assert problem == "" and others == 1 and m1.nick == "たつ【常連】"


def test_rapid_repeated_changes_end_in_the_latest_state(setup):
    eco, cog = setup
    for t in ("title_rookie", "title_regular"):
        buy_title(eco, t)
    eco.set_nick_pref(10, 1, True)
    g, m = guild_for(), Member()
    cog.bot.get_guild = lambda gid: g

    async def burst():
        eco.equip(1, "title", "title_rookie")
        a = cog._sync_title_nick(1, g, m)
        eco.equip(1, "title", "title_regular")
        b = cog._sync_title_nick(1, g, m)
        await asyncio.gather(a, b)
    run(burst())
    assert m.nick == "たつ【常連】"                                  # 連打しても、最後に装備した称号になる


def test_leaving_a_server_removes_its_nick_settings(setup):
    eco, cog = setup
    eco.set_nick_pref(10, 1, True)
    eco.set_nick_pref(20, 1, True)
    run(cog.on_guild_remove(SimpleNamespace(id=10)))
    assert eco.nick_pref_guilds(1) == [20]


def test_old_database_without_nick_state_columns_is_migrated(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE nick_prefs (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY (guild_id, user_id))")
    con.execute("INSERT INTO nick_prefs VALUES (10, 1)")
    con.commit()
    con.close()
    eco = Economy(path)                                             # 古い表でも、起動時に列が足される
    assert eco.nick_pref(10, 1)
    eco.set_nick_state(10, 1, None, "tatu【常連】")
    assert eco.get_nick_state(10, 1) == (None, "tatu【常連】")
