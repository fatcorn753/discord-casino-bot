"""週間ランキング: 週の数え方、記録、王者の決定と限定称号、月曜0時の自動発表。"""

import asyncio
import datetime
from types import SimpleNamespace

import pytest

from cogs import weekly_cog
from cogs.shop_cog import ShopCog
from cogs.weekly_cog import WeeklyCog
from core import shop, weekly
from core.economy import Economy
from core.session import Sessions

pytestmark = pytest.mark.real_scope

G, A, B, C = 10, 1, 2, 3
MON, SUN = datetime.date(2026, 10, 26), datetime.date(2026, 11, 1)
NEXT_MON = datetime.date(2026, 11, 2)
KING = shop.WEEKLY_KING


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def eco(tmp_path):
    return Economy(str(tmp_path / "t.db"), strict=True)


def play(a, uid, net, day, n=1):
    for _ in range(n):
        a.record_play(uid, "コイン", 100, net, day)


# ----------------------------- 週の数え方 ----------------------------- #
def test_week_math():
    assert weekly.week_id(MON) == weekly.week_id(SUN) == "2026-10-26"           # 月曜〜日曜が同じ週
    assert weekly.week_id(NEXT_MON) == "2026-11-02" and weekly.previous_week_id(NEXT_MON) == "2026-10-26"
    assert weekly.week_label("2026-10-26") == "10/26〜11/1" and weekly.week_label("2026-12-28") == "12/28〜1/3"


def test_record_play_adds_to_the_right_week(eco):
    a = eco.for_guild(G)
    play(a, A, 100, SUN, 2)
    play(a, A, -30, NEXT_MON)
    assert a.weekly_top("2026-10-26") == [(A, 200, 2)] and a.weekly_top("2026-11-02") == [(A, -30, 1)]
    assert eco.for_guild(20).weekly_top("2026-10-26") == []                       # 別サーバーとは混ざらない


# ----------------------------- 王者の決定と称号 ----------------------------- #
def test_champion_needs_positive_net_and_min_plays_and_gets_the_title(eco):
    a = eco.for_guild(G)
    play(a, A, 100, MON, 5)               # 500 / 5戦
    play(a, B, 900, MON, 1)               # 900 だが1戦だけ（運）→ 対象外
    play(a, C, -50, MON, 8)
    res = a.weekly_award("2026-10-26")
    assert res["champion"] == (A, 500, 5) and res["wins"] == 1
    assert a.inventory(A).get(KING) == 1 and KING not in a.inventory(B)
    assert [u for u, _, _ in res["standings"]] == [B, A, C]                       # 順位表には全員が載る


def test_no_champion_when_nobody_qualifies_and_previous_holder_loses_the_title(eco):
    a = eco.for_guild(G)
    play(a, A, 100, MON, 5)
    a.weekly_award("2026-10-26")
    a.equip(A, "title", KING)                                                    # 王者が称号を身につけている
    play(a, B, 10, NEXT_MON, 2)                                                   # 次の週は誰も条件を満たさない
    res = a.weekly_award("2026-11-02")
    assert res["champion"] is None and res["previous"] == [A]
    assert KING not in a.inventory(A) and a.get_profile(A)["title"] is None       # 持ち物からも、装備からも外れる


def test_back_to_back_champion_keeps_title_and_counts_wins(eco):
    a = eco.for_guild(G)
    play(a, A, 100, MON, 5)
    a.weekly_award("2026-10-26")
    play(a, A, 50, NEXT_MON, 5)
    res = a.weekly_award("2026-11-02")
    assert res["champion"][0] == A and res["wins"] == 2 and res["previous"] == []   # 連続なら「外れた人」には入れない
    assert a.inventory(A)[KING] == 1


def test_tie_goes_to_fewer_plays_and_award_is_idempotent(eco):
    a = eco.for_guild(G)
    play(a, A, 100, MON, 6)               # 600 / 6戦
    play(a, B, 120, MON, 5)               # 600 / 5戦 → 少ないほうが上
    assert a.weekly_award("2026-10-26")["champion"][0] == B
    assert a.weekly_award("2026-10-26") is None and a.weekly_award("2026-10-19") is None      # 発表済みの週は、もう発表しない
    assert a.weekly_last_announced() == "2026-10-26"


# ----------------------------- 過去分の補完 ----------------------------- #
def test_backfill_from_the_ledger_once(eco):
    a = eco.for_guild(G)
    a.balance(A)
    jst = datetime.timezone(datetime.timedelta(hours=9))
    mon_noon = datetime.datetime(2026, 10, 26, 12, 0, tzinfo=jst).timestamp()
    sun_before = datetime.datetime(2026, 10, 25, 23, 0, tzinfo=jst).timestamp()
    rows = [(sun_before, "賭け:コイン", -100), (sun_before + 1, "払い戻し:コイン", 195),                 # 先週の分
            (mon_noon, "賭け:スロット", -100),                                                         # 今週: 負け
            (mon_noon + 60, "賭け:コイン", -100), (mon_noon + 61, "払い戻し:コイン", 195),               # 今週: 勝ち
            (mon_noon + 120, "デイリー（1日連続）", 1000)]                                             # ゲームではない
    for ts, reason, delta in rows:
        eco._conn.execute("INSERT INTO ledger (guild_id, user_id, delta, balance_after, reason, ts) VALUES (?, ?, ?, 0, ?, ?)", (G, A, delta, reason, ts))
    eco._conn.commit()
    assert eco.backfill_weekly(9) == 3
    assert a.weekly_top("2026-10-19") == [(A, 95, 1)] and a.weekly_top("2026-10-26") == [(A, -5, 2)]
    assert eco.backfill_weekly(9) == 0                                              # 2回目は何もしない（二重に数えない）


# ----------------------------- 自動発表 ----------------------------- #
class Chan:
    def __init__(self, can=True):
        self.sent, self._can = [], can
        self.id = 99

    def permissions_for(self, _me):
        return SimpleNamespace(view_channel=self._can, send_messages=self._can, embed_links=self._can)

    async def send(self, **kw):
        self.sent.append(kw)


def make_cog(eco, monkeypatch, today, channel=None, board_channel=None):
    guild = SimpleNamespace(id=G, me=object(), system_channel=channel, get_channel=lambda cid: board_channel if board_channel and cid == board_channel.id else None)
    bot = SimpleNamespace(economy=eco, config={}, sessions=Sessions(cooldown=0), guilds=[guild], get_cog=lambda name: None)
    monkeypatch.setattr(weekly_cog, "local_now", lambda b: datetime.datetime.combine(today[0], datetime.time(0, 5)))
    return WeeklyCog(bot), guild, today


def test_first_run_sets_baseline_then_monday_announces_once(eco, monkeypatch):
    a = eco.for_guild(G)
    chan = Chan()
    cog, guild, today = make_cog(eco, monkeypatch, [SUN], channel=chan)
    play(a, A, 100, MON, 5)
    play(a, B, 30, MON, 5)
    run(cog.run_once())                                                            # 日曜: 初回。基準を決めるだけで、発表しない
    assert chan.sent == [] and a.weekly_last_announced() == "2026-10-19"
    today[0] = NEXT_MON
    run(cog.run_once())                                                            # 月曜0時台: 発表
    assert len(chan.sent) == 1
    sent = chan.sent[0]
    text = sent["embed"].description
    assert "獲得しました" in text and f"<@{A}>" in text and "👑週間王者" in text and "通算1回目" in text
    assert sent["content"] == f"🎉 <@{A}>" and "10/26〜11/1" in sent["embed"].title
    assert a.inventory(A)[KING] == 1
    run(cog.run_once())
    assert len(chan.sent) == 1                                                     # 同じ週は二重に発表しない


def test_downtime_announces_the_latest_finished_week_once(eco, monkeypatch):
    a = eco.for_guild(G)
    chan = Chan()
    cog, guild, today = make_cog(eco, monkeypatch, [MON + datetime.timedelta(days=21)], channel=chan)    # 3週間後に起動
    a.set_weekly_announced("2026-10-12")
    play(a, A, 100, MON + datetime.timedelta(days=14), 5)                         # 直前の週（11/9〜）の分
    run(cog.run_once())
    assert len(chan.sent) == 1 and a.weekly_last_announced() == "2026-11-09"


def test_announcement_goes_to_the_ranking_board_channel_first(eco, monkeypatch):
    a = eco.for_guild(G)
    board, system = Chan(), Chan()
    eco.set_leaderboard(G, board.id, 5)
    cog, guild, today = make_cog(eco, monkeypatch, [NEXT_MON], channel=system, board_channel=board)
    a.set_weekly_announced("2026-10-19")
    play(a, A, 100, MON, 5)
    run(cog.run_once())
    assert len(board.sent) == 1 and system.sent == []                              # 掲示のチャンネルへ


def test_no_permission_still_awards_and_does_not_crash(eco, monkeypatch):
    a = eco.for_guild(G)
    chan = Chan(can=False)
    cog, guild, today = make_cog(eco, monkeypatch, [NEXT_MON], channel=chan)
    a.set_weekly_announced("2026-10-19")
    play(a, A, 100, MON, 5)
    run(cog.run_once())
    assert chan.sent == [] and a.inventory(A)[KING] == 1                           # 投稿できなくても、称号は贈られる


def test_nobody_played_means_no_post(eco, monkeypatch):
    chan = Chan()
    cog, guild, today = make_cog(eco, monkeypatch, [NEXT_MON], channel=chan)
    eco.for_guild(G).set_weekly_announced("2026-10-19")
    run(cog.run_once())
    assert chan.sent == [] and eco.for_guild(G).weekly_last_announced() == "2026-10-26"


def test_unapproved_guild_is_skipped(eco, monkeypatch):
    chan = Chan()
    cog, guild, today = make_cog(eco, monkeypatch, [NEXT_MON], channel=chan)
    cog.bot.config = {"require_approval": True, "owner_ids": [1]}
    run(cog.run_once())
    assert chan.sent == [] and eco.for_guild(G).weekly_last_announced() is None


# ----------------------------- 限定称号 ----------------------------- #
def test_limited_title_is_not_for_sale_but_can_be_worn_when_owned(eco):
    assert shop.BY_ID[KING].limited and KING not in {i.id for i in shop.of_kind(shop.TITLE)}
    assert KING in {i.id for i in shop.of_kind(shop.TITLE, include_limited=True)}
    a = eco.for_guild(G)
    a.credit(A, 10_000_000, "テスト")
    assert not a.purchase(A, KING, "👑週間王者", 0, True).ok                         # 値段0は買えない（かりに呼ばれても）
    bot = SimpleNamespace(economy=eco, config={}, sessions=Sessions(cooldown=0), get_guild=lambda gid: None)
    cog = ShopCog(bot)
    sent = []

    async def send(*args, **kw):
        sent.append(kw)
    it = SimpleNamespace(user=SimpleNamespace(id=A), guild=SimpleNamespace(id=G), response=SimpleNamespace(send_message=send, defer=send), followup=SimpleNamespace(send=send))
    run(ShopCog.buy.callback(cog, it, "👑週間王者", 1))
    assert "買えません" in sent[-1]["embed"].title and a.inventory(A) == {}
    assert run(cog._catalog_autocomplete(it, "週間")) == []                         # 購入の候補にも出ない


def test_previous_holder_is_synced_off_the_nickname(eco, monkeypatch):
    a = eco.for_guild(G)
    chan = Chan()
    cog, guild, today = make_cog(eco, monkeypatch, [NEXT_MON], channel=chan)
    synced = []

    class FakeShop:
        async def _sync_title_nick(self, uid, g):
            synced.append(uid)
    cog.bot.get_cog = lambda name: FakeShop() if name == "ShopCog" else None
    a.set_weekly_announced("2026-10-19")
    play(a, A, 100, MON, 5)
    run(cog.run_once())                                                            # 10/26 の週の王者は A
    today[0] = NEXT_MON + datetime.timedelta(days=7)
    play(a, B, 100, NEXT_MON, 5)
    run(cog.run_once())                                                            # 11/2 の週の王者は B → A の称号が外れる
    assert synced == [A]
