"""配信（/配信）: 山分けの計算・スパチャ（送金と同じ規則）・賭けの預かりと精算・無効と自動終了・パネルのボタン。"""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from cogs.stream.cog import StreamCog
from cogs.stream.views import BetModal, StreamerMenu, TipModal, panel_embed, result_text
from domain import stream
from domain.errors import Failure
from infra.db.repositories import open_repositories
from tests.fakes import as_bot, must
from tests.support import make_test_bot, set_test_debt, set_test_games_played

pytestmark = pytest.mark.real_scope

G, HOST, A, B, C = 1, 10, 11, 12, 13
NOW = 1_000_000.0


@pytest.fixture
def eco(tmp_path):
    e = open_repositories(str(tmp_path / "t.db"), strict=True).for_guild(G)
    for uid in (HOST, A, B, C):
        e.wallet.credit(uid, 50_000, "テスト")
    return e


def start(eco, host=HOST) -> int:
    ok, sid = eco.streams.stream_start(host, "ランク戦", "", 555, NOW)
    assert ok and not isinstance(sid, Failure)
    return sid


def opened(eco, sid, host=HOST):
    ok, view = eco.streams.stream_round_open(host, sid, "1戦目", NOW)
    assert ok, view
    return view


def bal(eco, uid):
    return eco.wallet.balance(uid)


# ----------------------------- 山分けの計算（純粋） ----------------------------- #
def test_settle_shares_the_losing_side_minus_rake_in_proportion_to_stakes():
    bets = {A: (stream.WIN, 1000), B: (stream.WIN, 3000), C: (stream.LOSE, 2000)}
    res = stream.settle(bets, stream.WIN)
    rake = int(2000 * stream.RAKE)
    pot = 2000 - rake
    assert res.payouts == {A: 1000 + pot * 1000 // 4000, B: 3000 + pot * 3000 // 4000}
    assert sum(res.payouts.values()) + res.rake == 6000  # 新しくペリカを作らない
    assert not res.refunded and all(res.payouts[u] >= bets[u][1] for u in res.payouts)  # 当てて損はしない


def test_settle_refunds_everything_when_void_or_one_sided():
    bets = {A: (stream.WIN, 1000), B: (stream.WIN, 500)}
    for winner in (stream.WIN, stream.LOSE, None):
        res = stream.settle(bets, winner)
        assert res.refunded and res.payouts == {A: 1000, B: 500} and res.rake == 0
    res = stream.settle({A: (stream.WIN, 1000), C: (stream.LOSE, 1000)}, None)
    assert res.refunded and res.payouts == {A: 1000, C: 1000}


def test_odds_and_tip_colors():
    assert stream.odds(1000, 0, stream.WIN) is None
    assert stream.odds(1000, 1000, stream.WIN) == pytest.approx(1 + (1000 - 1000 * stream.RAKE) / 1000)
    assert stream.tip_color(100) != stream.tip_color(20000)
    assert stream.tip_color(stream.TIP_MIN) == stream.TIP_TIERS[-1][1]


# ----------------------------- 配信と勝負（infra） ----------------------------- #
def test_one_live_panel_per_streamer_and_title_and_url_are_checked(eco):
    sid = start(eco)
    assert eco.streams.stream_start(HOST, "もう1つ", "", 555, NOW)[1].code == "stream_already_live"
    assert eco.streams.stream_start(A, " ", "", 555, NOW)[1].code == "stream_bad_title"
    assert eco.streams.stream_start(A, "配信", "javascript:x", 555, NOW)[1].code == "stream_bad_url"
    s = must(eco.streams.stream_get(sid))
    assert s["status"] == "live" and s["round"] is None and s["ends"] == NOW + stream.MAX_HOURS * 3600


def test_bets_are_held_and_the_winner_side_shares_the_pot(eco, monkeypatch):
    paid_in = []
    monkeypatch.setattr(
        type(eco.streams._bank), "bank_income_for", lambda self, gid, amount, now, **kw: paid_in.append(amount)
    )
    sid = start(eco)
    before = {u: bal(eco, u) for u in (A, B, C)}
    opened(eco, sid)
    assert eco.streams.stream_bet(A, sid, stream.WIN, 1000, NOW)[0]
    assert eco.streams.stream_bet(A, sid, stream.WIN, 1000, NOW)[0]  # 同じ側なら足せる
    assert eco.streams.stream_bet(B, sid, stream.LOSE, 2000, NOW)[0]
    assert bal(eco, A) == before[A] - 2000  # 賭け金は預かる
    r = must(must(eco.streams.stream_get(sid))["round"])
    assert (r["win_total"], r["lose_total"], r["bettors"]) == (2000, 2000, 2)
    ok, view = eco.streams.stream_round_lock(HOST, sid)
    assert ok
    assert eco.streams.stream_bet(C, sid, stream.WIN, 500, NOW)[1].code == "stream_no_open_round"  # 締切のあと
    ok, res = eco.streams.stream_round_settle(HOST, sid, stream.WIN, NOW)
    assert ok and not isinstance(res, Failure)
    rake = int(2000 * stream.RAKE)
    assert res["payouts"] == {A: 4000 - rake} and res["rake"] == rake
    assert bal(eco, A) == before[A] + 2000 - rake and bal(eco, B) == before[B] - 2000
    assert paid_in == [rake]  # 控除は帝愛銀行の収入
    assert must(eco.streams.stream_get(sid))["round"] is None  # 次の勝負を始められる
    assert "1戦目" in result_text(res) and "配信者の勝ち" in result_text(res)
    opened(eco, sid)


def test_bet_rules_block_the_streamer_switching_sides_limits_and_debt(eco):
    sid = start(eco)
    assert eco.streams.stream_bet(A, sid, stream.WIN, 1000, NOW)[1].code == "stream_no_open_round"
    opened(eco, sid)
    assert eco.streams.stream_round_open(HOST, sid, "2戦目", NOW)[1].code == "stream_round_active"
    assert eco.streams.stream_round_open(A, sid, "乗っ取り", NOW)[1].code == "stream_not_streamer"
    assert eco.streams.stream_bet(HOST, sid, stream.LOSE, 1000, NOW)[1].code == "stream_self_bet"
    assert eco.streams.stream_bet(A, sid, stream.WIN, stream.BET_MIN - 1, NOW)[1].code == "stream_bet_min"
    assert eco.streams.stream_bet(A, sid, stream.WIN, stream.BET_MAX, NOW)[0]
    assert eco.streams.stream_bet(A, sid, stream.WIN, stream.BET_MIN, NOW)[1].code == "stream_bet_max"
    assert eco.streams.stream_bet(A, sid, stream.LOSE, stream.BET_MIN, NOW)[1].code == "stream_other_side"
    with eco.db.tx():
        set_test_debt(eco, B, 5000, guild_id=G)
    assert eco.streams.stream_bet(B, sid, stream.LOSE, 1000, NOW)[1].code == "debt_blocks"
    assert eco.streams.stream_round_settle(A, sid, stream.WIN, NOW)[1].code == "stream_not_streamer"


def test_void_and_ending_and_timeout_refund_every_bet(eco):
    sid = start(eco)
    opened(eco, sid)
    before = {u: bal(eco, u) for u in (A, B)}
    eco.streams.stream_bet(A, sid, stream.WIN, 1000, NOW)
    eco.streams.stream_bet(B, sid, stream.LOSE, 3000, NOW)
    ok, res = eco.streams.stream_round_settle(HOST, sid, None, NOW)
    assert ok and not isinstance(res, Failure) and res["refunded"] and res["winner"] is None
    assert {u: bal(eco, u) for u in (A, B)} == before and "無効" in result_text(res)
    # 配信を終えると、決まっていない勝負は無効になる
    opened(eco, sid)
    eco.streams.stream_bet(A, sid, stream.WIN, 1000, NOW)
    ok, voided = eco.streams.stream_end(HOST, sid, NOW)
    assert ok and voided is not None and voided["refunded"] and bal(eco, A) == before[A]
    assert must(eco.streams.stream_get(sid))["status"] == "ended"
    assert eco.streams.stream_bet(A, sid, stream.WIN, 1000, NOW)[1].code == "stream_no_open_round"
    # 閉じ忘れても、時間切れで閉じて返す
    sid2 = start(eco)
    opened(eco, sid2)
    eco.streams.stream_bet(B, sid2, stream.LOSE, 1000, NOW)
    assert eco.streams.stream_close_due(NOW + 60) == []
    [(view, voided2)] = eco.streams.stream_close_due(NOW + stream.MAX_HOURS * 3600)
    assert view["status"] == "ended" and voided2 is not None and bal(eco, B) == before[B]


def test_streams_are_separate_per_server(tmp_path):
    repos = open_repositories(str(tmp_path / "t.db"), strict=True)
    a, b = repos.for_guild(1), repos.for_guild(2)
    ok, sid = a.streams.stream_start(HOST, "配信", "", 1, NOW)
    assert ok and not isinstance(sid, Failure)
    assert b.streams.stream_get(sid) is None
    assert b.streams.stream_start(HOST, "別のサーバー", "", 1, NOW)[0]  # サーバーが違えば、別に出せる


# ----------------------------- スパチャ（サービス） ----------------------------- #
def _played(eco, uid, games=3):
    with eco.db.tx():
        set_test_games_played(eco, uid, games, guild_id=G)


def test_tip_is_a_transfer_with_the_same_fee_and_daily_limit(eco, tmp_path):
    bot = make_test_bot(repos=open_repositories(eco.db.db_path))
    svc = bot.services.stream
    sid = start(eco)
    res, _ = svc.tip(G, A, sid, 1000)
    assert not res.ok and res.reason.code == "transfer_min_games"  # 送金と同じ条件
    _played(eco, A)
    host_before, a_before = bal(eco, HOST), bal(eco, A)
    res, s = svc.tip(G, A, sid, 1000)
    assert res.ok and res.fee == stream.transfer_fee(1000, 0.10)
    assert bal(eco, HOST) == host_before + 1000 and bal(eco, A) == a_before - 1000 - res.fee
    assert s is not None and (s["tips"], s["tip_count"]) == (1000, 1)
    res, _ = svc.tip(G, A, sid, 20_000)  # 送金と合わせて、1日の上限に数える
    assert not res.ok and res.reason.code == "transfer_daily_limit"
    assert svc.tip(G, HOST, sid, 1000)[0].reason.code == "stream_self_tip"
    assert svc.tip(G, A, sid, stream.TIP_MIN - 1)[0].reason.code == "stream_tip_min"
    eco.streams.stream_end(HOST, sid, NOW)
    assert svc.tip(G, A, sid, 1000)[0].reason.code == "stream_ended"


# ----------------------------- パネルのボタン（cog） ----------------------------- #
class Response:
    def __init__(self):
        self.sent, self.modal = [], None

    async def send_message(self, *args, **kw):
        self.sent.append(kw)

    async def send_modal(self, modal):
        self.modal = modal


class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kw):
        self.sent.append({"content": args[0] if args else kw.get("content"), **kw})


class Msg:
    id = 4242

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
        self.sent.append(kw["embed"])


class Voice:
    def __init__(self, cid=777, name=""):
        self.id, self.name, self.mention, self.deleted = cid, name, f"<#{cid}>", False

    async def delete(self, reason=None):
        self.deleted = True


class Guild:
    """テスト用のサーバー。VC を作る・消すのを記録する。forbidden なら作れない（権限がない）。"""

    def __init__(self, ch, forbidden=False):
        self.id, self.ch, self.forbidden, self.voices = G, ch, forbidden, {}

    def get_channel(self, cid):
        return self.voices.get(cid, self.ch)

    async def create_voice_channel(self, name, *, category=None, reason=None):
        if self.forbidden:
            import discord

            raise discord.Forbidden(cast(Any, SimpleNamespace(status=403, reason="Forbidden")), "Missing Permissions")
        v = Voice(777 + len(self.voices), name)
        self.voices[v.id] = v
        return v


def _setup(eco, forbidden=False):
    ch = Channel()
    guild = Guild(ch, forbidden)
    bot = make_test_bot(repos=open_repositories(eco.db.db_path), config={"allowed_guild_ids": [G]})
    bot.guilds = [guild]
    cog = StreamCog(as_bot(bot), bot.config, bot.services.stream, bot.repos.guild, bot.sessions)
    return cog, ch, guild


def _it(uid, guild, ch, voice=None):
    async def original_response():
        return ch.msg

    return cast(
        Any,
        SimpleNamespace(
            guild_id=G,
            guild=guild,
            channel_id=555,
            channel=None,
            user=SimpleNamespace(
                id=uid,
                mention=f"<@{uid}>",
                display_name=f"u{uid}",
                display_avatar=SimpleNamespace(url=""),
                voice=SimpleNamespace(channel=voice) if voice is not None else None,
            ),
            response=Response(),
            followup=Followup(),
            original_response=original_response,
            client=None,
        ),
    )


def test_panel_buttons_open_forms_and_only_the_streamer_gets_the_menu(eco):
    cog, ch, guild = _setup(eco)
    it = _it(HOST, guild, ch)
    asyncio.run(cast(Any, cog.start.callback)(cog, it, "ランク戦", None))
    assert it.response.sent and it.response.sent[0]["view"] is not None
    sid = must(eco.db.conn.execute("SELECT id FROM streams WHERE guild_id=?", (G,)).fetchone())[0]
    assert must(eco.streams.stream_get(sid))["message_id"] == Msg.id
    viewer = _it(A, guild, ch)
    asyncio.run(cog.handle_button(viewer, "tip", sid))
    assert isinstance(viewer.response.modal, TipModal)
    viewer = _it(A, guild, ch)
    asyncio.run(cog.handle_button(viewer, "win", sid))
    assert isinstance(viewer.response.modal, BetModal) and viewer.response.modal.side == stream.WIN
    viewer = _it(A, guild, ch)
    asyncio.run(cog.handle_button(viewer, "menu", sid))
    assert viewer.response.modal is None and "配信者だけ" in viewer.response.sent[-1]["embed"].description
    host = _it(HOST, guild, ch)
    asyncio.run(cog.handle_button(host, "menu", sid))
    assert isinstance(host.response.sent[-1]["view"], StreamerMenu)


def test_bet_via_cog_updates_the_panel_and_timeout_posts_the_refund(eco):
    cog, ch, guild = _setup(eco)
    ok, sid = cog._stream.start(G, HOST, "ランク戦", "", 555)  # 見回りと同じ時計で始める
    assert ok and not isinstance(sid, Failure)
    eco.streams.stream_set_message(sid, 555, Msg.id)
    assert cog._stream.open_round(G, HOST, sid, "1戦目")[0]
    it = _it(A, guild, ch)
    asyncio.run(cog.do_bet(it, sid, stream.LOSE, 1500))
    assert "賭けました" in it.response.sent[-1]["embed"].title
    text = must(ch.msg.edits[-1]["embed"].description)
    assert "1,500" in text
    asyncio.run(cog._tick_guild(cast(Any, guild)))  # まだ時間切れではない
    assert ch.sent == []
    with eco.db.tx():
        eco.db.conn.execute("UPDATE streams SET ends=0 WHERE id=?", (sid,))
    asyncio.run(cog._tick_guild(cast(Any, guild)))
    assert ch.sent and "無効" in must(ch.sent[-1].description)
    assert ch.msg.edits[-1]["view"] is None and "配信終了" in ch.msg.edits[-1]["embed"].title
    assert "配信中" not in must(panel_embed(must(eco.streams.stream_get(sid))).title)


def test_tip_and_settle_via_cog_post_to_the_channel(eco, monkeypatch):
    from cogs.stream import cog as stream_cog

    async def yes(*a, **k):
        return True

    monkeypatch.setattr(stream_cog, "confirm_action", yes)
    cog, ch, guild = _setup(eco)
    ok, sid = cog._stream.start(G, HOST, "ランク戦", "", 555)
    assert ok and not isinstance(sid, Failure)
    eco.streams.stream_set_message(sid, 555, Msg.id)
    _played(eco, A)
    it = _it(A, guild, ch)
    asyncio.run(cog.do_tip(it, sid, 600, "がんばれ"))
    tip = it.response.sent[-1]["embed"]  # 確認なしの額は、パネルのチャンネルにそのまま出る
    assert tip.description == "がんばれ" and tip.color.value == stream.tip_color(600) and "600" in tip.author.name
    assert "スパチャを送りました" in it.followup.sent[-1]["embed"].title and it.followup.sent[-1]["ephemeral"]
    assert must(eco.streams.stream_get(sid))["tips"] == 600
    # 勝負を始めて、賭けて、メニューから勝敗を決める
    assert cog._stream.open_round(G, HOST, sid, "決勝")[0]
    asyncio.run(cog.do_bet(_it(A, guild, ch), sid, stream.WIN, 1000))
    asyncio.run(cog.do_bet(_it(B, guild, ch), sid, stream.LOSE, 1000))
    a_before = bal(eco, A)
    host = _it(HOST, guild, ch)
    asyncio.run(cog.do_menu(host, sid, "win"))
    assert "精算しました" in host.followup.sent[-1]["content"]
    assert bal(eco, A) == a_before + 2000 - int(1000 * stream.RAKE)
    assert "決勝" in must(ch.sent[-1].description) and "配信者の勝ち" in must(ch.sent[-1].description)
    stranger = _it(A, guild, ch)
    asyncio.run(cog.do_menu(stranger, sid, "end"))  # 配信者でない人は終えられない
    assert must(eco.streams.stream_get(sid))["status"] == "live"
    asyncio.run(cog.do_menu(_it(HOST, guild, ch), sid, "end"))
    assert must(eco.streams.stream_get(sid))["status"] == "ended" and ch.msg.edits[-1]["view"] is None


# ----------------------------- 配信の場所と、配信用の VC（cog） ----------------------------- #
def _only_stream(eco):
    return must(
        eco.streams.stream_get(
            must(eco.db.conn.execute("SELECT id FROM streams ORDER BY id DESC LIMIT 1").fetchone())[0]
        )
    )


def test_panel_shows_the_voice_channel_the_streamer_is_in_and_does_not_delete_it(eco, monkeypatch):
    from cogs.stream import cog as stream_cog

    async def yes(*a, **k):
        return True

    monkeypatch.setattr(stream_cog, "confirm_action", yes)
    cog, ch, guild = _setup(eco)
    mine = Voice(900)
    guild.voices[900] = mine
    asyncio.run(cast(Any, cog.start.callback)(cog, _it(HOST, guild, ch, voice=mine), "ランク戦", None))
    s = _only_stream(eco)
    assert (s["voice_channel_id"], s["created_vc"]) == (900, 0)
    assert "📍 場所: <#900>" in must(ch.msg.edits[-1]["embed"].description)
    assert len(guild.voices) == 1  # 入っている VC があれば、作らない
    asyncio.run(cog.do_menu(_it(HOST, guild, ch), s["id"], "end"))
    assert not mine.deleted  # 配信者が入っていた VC は消さない
    assert "場所" not in must(panel_embed(_only_stream(eco)).description)  # 終わったら場所は出さない


def test_bot_creates_a_voice_channel_when_the_streamer_is_not_in_one_and_deletes_it_at_the_end(eco):
    cog, ch, guild = _setup(eco)
    it = _it(HOST, guild, ch)
    asyncio.run(cast(Any, cog.start.callback)(cog, it, "ランク戦", None))
    s = _only_stream(eco)
    vc = guild.voices[s["voice_channel_id"]]
    assert s["created_vc"] == 1 and vc.name == f"📺 u{HOST}の配信"
    assert f"📍 場所: <#{vc.id}>" in must(ch.msg.edits[-1]["embed"].description)
    assert vc.mention in it.followup.sent[-1]["content"] and it.followup.sent[-1]["ephemeral"]
    with eco.db.tx():  # 時間切れで閉じても、作った VC は消す
        eco.db.conn.execute("UPDATE streams SET ends=0 WHERE id=?", (s["id"],))
    asyncio.run(cog._tick_guild(cast(Any, guild)))
    assert vc.deleted and _only_stream(eco)["status"] == "ended"


def test_no_voice_channel_for_external_streams_and_a_note_when_the_bot_lacks_permission(eco, monkeypatch):
    from cogs.stream import cog as stream_cog

    async def yes(*a, **k):
        return True

    monkeypatch.setattr(stream_cog, "confirm_action", yes)
    cog, ch, guild = _setup(eco)
    it = _it(HOST, guild, ch)
    asyncio.run(cast(Any, cog.start.callback)(cog, it, "ランク戦", "https://youtube.com/live/x"))
    assert guild.voices == {} and _only_stream(eco)["voice_channel_id"] == 0 and it.followup.sent == []
    asyncio.run(cog.do_menu(_it(HOST, guild, ch), _only_stream(eco)["id"], "end"))
    cog, ch, guild = _setup(eco, forbidden=True)
    it = _it(HOST, guild, ch)
    asyncio.run(cast(Any, cog.start.callback)(cog, it, "ランク戦", None))
    s = _only_stream(eco)
    assert s["status"] == "live" and s["voice_channel_id"] == 0  # パネルは出たまま
    assert "チャンネルの管理" in it.followup.sent[-1]["content"]
