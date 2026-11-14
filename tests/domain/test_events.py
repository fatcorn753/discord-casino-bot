"""突発イベント: 日付とサーバーで決まる開催、勝ち分の上乗せと上限、開始・終了の告知。"""

import asyncio
import datetime
from types import SimpleNamespace

import pytest

from cogs.casino import common
from cogs.event import cog as event_cog
from cogs.event.cog import EventCog
from domain import events
from infra.db.economy import Economy
from services.session import Sessions

pytestmark = [pytest.mark.real_scope, pytest.mark.real_events]

G = 10
DAY = datetime.date(2026, 11, 2)
JST = datetime.timezone(datetime.timedelta(hours=9))


def run(coro):
    return asyncio.run(coro)


def at(day, minutes):
    return datetime.datetime.combine(day, datetime.time(minutes // 60, minutes % 60), tzinfo=JST)


def test_schedule_is_deterministic_and_inside_the_window():
    seen_games = set()
    for i in range(200):
        day = DAY + datetime.timedelta(days=i)
        ev = events.schedule(G, day)
        assert ev == events.schedule(G, day)  # 同じ日・同じサーバーなら同じ
        assert events.EARLIEST_MIN <= ev.start_min <= events.LATEST_MIN and ev.start_min % 5 == 0
        assert ev.end_min <= 24 * 60  # 0時をまたがない
        seen_games.add(ev.game)
    assert seen_games == set(events.GAMES)  # どのゲームも選ばれうる
    assert any(
        events.schedule(G, DAY + datetime.timedelta(days=i)) != events.schedule(G + 1, DAY + datetime.timedelta(days=i))
        for i in range(10)
    )


def test_active_only_during_the_thirty_minutes():
    ev = events.schedule(G, DAY)
    assert events.active(G, at(DAY, ev.start_min)) == ev
    assert events.active(G, at(DAY, ev.end_min - 1)) == ev
    assert events.active(G, at(DAY, ev.end_min)) is None and events.active(G, at(DAY, ev.start_min - 1)) is None


def test_bonus_only_on_wins_and_is_capped():
    assert events.bonus(1000, 3000) == 1000  # 勝ち分2000の半分
    assert events.bonus(1000, 1000) == 0 and events.bonus(1000, 0) == 0
    assert events.bonus(100_000, 1_000_000) == events.BONUS_CAP


def make_bot(tmp_path, monkeypatch, now):
    bot = SimpleNamespace(
        economy=Economy(str(tmp_path / "t.db"), strict=True), config={}, sessions=Sessions(cooldown=0), guilds=[]
    )
    monkeypatch.setattr(common, "local_now", lambda b: now[0])
    return bot


def test_settle_adds_the_bonus_only_for_the_event_game(tmp_path, monkeypatch):
    ev = events.schedule(G, DAY)
    now = [at(DAY, ev.start_min + 1)]
    bot = make_bot(tmp_path, monkeypatch, now)
    other = next(g for g in events.GAMES if g != ev.game)
    for game, expect_bonus in ((ev.game, 1000), (other, 0)):
        eco = bot.economy.for_guild(G)
        eid = eco.place_bet(1, 1000, game)
        ctx = common.GameContext(bot, 1, eid, game, 1000, G)
        before = eco.balance(1)
        net = ctx.settle(3000)
        assert ctx.event_bonus == expect_bonus and net == 2000 + expect_bonus
        assert eco.balance(1) == before + 3000 + expect_bonus
    now[0] = at(DAY, ev.end_min + 1)  # 終わったあとは付かない
    eid = eco.place_bet(1, 1000, ev.game)
    ctx = common.GameContext(bot, 1, eid, ev.game, 1000, G)
    assert ctx.settle(3000) == 2000 and ctx.event_bonus == 0


class Chan:
    def __init__(self):
        self.sent = []

    def permissions_for(self, _me):
        return SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)

    async def send(self, **kw):
        self.sent.append(kw)


def test_cog_announces_start_and_end_once_even_after_restart(tmp_path, monkeypatch):
    ev = events.schedule(G, DAY)
    now = [at(DAY, ev.start_min - 10)]
    chan = Chan()
    guild = SimpleNamespace(id=G, me=object(), system_channel=chan, get_channel=lambda cid: None)
    bot = make_bot(tmp_path, monkeypatch, now)
    bot.guilds = [guild]
    monkeypatch.setattr(event_cog, "local_now", lambda b: now[0])
    cog = EventCog(bot)
    run(cog.run_once())
    assert chan.sent == []  # 開始前は何も投稿しない
    now[0] = at(DAY, ev.start_min + 2)
    run(cog.run_once())
    run(EventCog(bot).run_once())  # 再起動しても二重に告知しない
    assert (
        len(chan.sent) == 1
        and "突発イベント" in chan.sent[0]["embed"].title
        and ev.game in chan.sent[0]["embed"].description
    )
    now[0] = at(DAY, ev.end_min + 1)
    run(cog.run_once())
    run(cog.run_once())
    assert len(chan.sent) == 2 and "終了" in chan.sent[1]["embed"].title


def test_event_missed_while_down_is_not_announced(tmp_path, monkeypatch):
    ev = events.schedule(G, DAY)
    chan = Chan()
    guild = SimpleNamespace(id=G, me=object(), system_channel=chan, get_channel=lambda cid: None)
    now = [at(DAY, ev.end_min + 5)]
    bot = make_bot(tmp_path, monkeypatch, now)
    bot.guilds = [guild]
    monkeypatch.setattr(event_cog, "local_now", lambda b: now[0])
    run(EventCog(bot).run_once())
    assert chan.sent == []


# ----------------------------- 持ち主が開く特別イベント ----------------------------- #
import time  # noqa: E402


def test_bonus_takes_a_custom_boost_and_best_boost_never_stacks():
    assert events.bonus(1000, 3000, 1.0) == 2000 and events.bonus(1000, 3000, 0.2) == 400
    assert events.bonus(1000, 100_000, 3.0) == events.BONUS_CAP
    ev = events.Event("スロット", 0, DAY)
    manual = {"game": "スロット", "boost": 120}
    assert events.best_boost("スロット", None, None) == 0
    assert events.best_boost("スロット", ev, None) == events.BOOST
    assert events.best_boost("スロット", None, manual) == 1.2
    assert events.best_boost("スロット", ev, manual) == 1.2  # 重ねず、大きいほう
    assert events.best_boost("スロット", ev, {"game": "スロット", "boost": 20}) == events.BOOST
    assert events.best_boost("ルーレット", ev, manual) == 0  # 別のゲームには付かない


def test_manual_event_lifecycle(tmp_path):
    eco = Economy(str(tmp_path / "t.db")).for_guild(G)
    assert (
        eco.manual_event_get(100) is None
        and eco.manual_event_stop(100) is None
        and eco.manual_event_finish_due(100) is None
    )
    eco.manual_event_start("ルーレット", 80, 50, 200)
    assert eco.manual_event_get(49) is None  # 開始前
    assert (
        eco.manual_event_get(50)["boost"] == 80
        and eco.manual_event_get(199) is not None
        and eco.manual_event_get(200) is None
    )
    assert eco.manual_event_finish_due(199) is None  # まだ
    done = eco.manual_event_finish_due(200)
    assert done["game"] == "ルーレット" and eco.manual_event_finish_due(300) is None  # 終了の告知は1回だけ
    eco.manual_event_start("パチンコ", 50, 0, 1000)
    eco.manual_event_start("スロット", 60, 0, 1000)  # 新しく開くと置き換わる
    assert eco.manual_event_get(10)["game"] == "スロット"
    stopped = eco.manual_event_stop(500)
    assert stopped["game"] == "スロット" and eco.manual_event_get(501) is None
    assert (
        eco.manual_event_stop(501) is None and eco.manual_event_finish_due(5000) is None
    )  # 手動終了のあとに二重告知しない


def test_manual_events_are_per_guild(tmp_path):
    eco = Economy(str(tmp_path / "t.db"))
    eco.for_guild(G).manual_event_start("ルーレット", 50, 0, 1000)
    assert eco.for_guild(G).manual_event_get(10) and eco.for_guild(G + 1).manual_event_get(10) is None


def test_settle_applies_the_manual_boost_for_its_game_only(tmp_path, monkeypatch):
    now = [at(DAY, 60)]  # 1:00 → 定例イベントはない
    bot = make_bot(tmp_path, monkeypatch, now)
    eco = bot.economy.for_guild(G)
    t = time.time()
    eco.manual_event_start("スロット", 100, t - 10, t + 600)
    for game, expect in (("スロット", 2000), ("ルーレット", 0)):
        eid = eco.place_bet(1, 1000, game)
        ctx = common.GameContext(bot, 1, eid, game, 1000, G)
        net = ctx.settle(3000)
        assert ctx.event_bonus == expect and net == 2000 + expect  # 勝ち分2000に+100%
    eco.manual_event_stop(time.time())  # 終わらせたあとは付かない
    eid = eco.place_bet(1, 1000, "スロット")
    ctx = common.GameContext(bot, 1, eid, "スロット", 1000, G)
    assert ctx.settle(3000) == 2000 and ctx.event_bonus == 0


def test_manual_boost_does_not_stack_with_the_scheduled_event(tmp_path, monkeypatch):
    ev = events.schedule(G, DAY)
    now = [at(DAY, ev.start_min + 1)]
    bot = make_bot(tmp_path, monkeypatch, now)
    eco = bot.economy.for_guild(G)
    t = time.time()
    eco.manual_event_start(ev.game, 20, t - 10, t + 600)  # 定例(50%)より小さい特別イベント
    eid = eco.place_bet(1, 1000, ev.game)
    ctx = common.GameContext(bot, 1, eid, ev.game, 1000, G)
    assert ctx.settle(3000) == 2000 + 1000 and ctx.event_bonus == 1000  # 50% のほうだけ


def make_cog(tmp_path, monkeypatch):
    chan = Chan()
    guild = SimpleNamespace(id=G, name="鯖", me=object(), system_channel=chan, get_channel=lambda cid: None)
    now = [at(DAY, 60)]
    bot = make_bot(tmp_path, monkeypatch, now)
    bot.guilds = [guild]
    monkeypatch.setattr(event_cog, "local_now", lambda b: now[0])
    monkeypatch.setattr(event_cog.approval, "is_allowed", lambda *a: True)
    return EventCog(bot), bot, guild, chan


def test_cog_start_announces_replaces_and_stop_announces_the_end(tmp_path, monkeypatch):
    cog, bot, guild, chan = make_cog(tmp_path, monkeypatch)
    ok, text = run(cog.start_manual(guild, "スロット", 80, 45))
    assert ok and "45分" in text and "+80%" in text and "置き換え" not in text
    e = chan.sent[-1]["embed"]
    assert "特別イベント発生" in e.title and "1.8倍" in e.description and "/カジノ スロット" in e.description
    ok, text = run(cog.start_manual(guild, "ルーレット", 50, 30))
    assert "置き換え" in text and bot.economy.for_guild(G).manual_event_get(time.time())["game"] == "ルーレット"
    ok, text = run(cog.stop_manual(guild))
    assert ok and "終了" in chan.sent[-1]["embed"].title
    ok, text = run(cog.stop_manual(guild))
    assert not ok and len(chan.sent) == 3  # 開催中でなければ何も出さない


def test_cog_announces_the_end_once_when_time_runs_out(tmp_path, monkeypatch):
    cog, bot, guild, chan = make_cog(tmp_path, monkeypatch)
    t = time.time()
    bot.economy.for_guild(G).manual_event_start("ルーレット", 50, t - 100, t - 1)  # 時間が過ぎた
    run(cog.run_once())
    run(cog.run_once())
    run(EventCog(bot).run_once())
    assert len(chan.sent) == 1 and "終了" in chan.sent[0]["embed"].title


def test_a_removed_game_in_a_running_manual_event_does_not_break_the_announcements(tmp_path, monkeypatch):
    """廃止したゲーム（例: コイン）の特別イベントが DB に残っていても、終了の告知で落ちない。"""
    cog, bot, guild, chan = make_cog(tmp_path, monkeypatch)
    t = time.time()
    bot.economy.for_guild(G).manual_event_start("コイン", 50, t - 100, t - 1)
    run(cog.run_once())
    assert len(chan.sent) == 1 and "終了" in chan.sent[0]["embed"].title
    bot.economy.for_guild(G).manual_event_start("コイン", 50, t - 10, t + 600)
    ok, text = run(cog.stop_manual(guild))
    assert ok and "コイン" in text


def test_the_coin_game_is_gone():
    from cogs.casino.cog import Casino
    from domain import missions

    assert "コイン" not in events.GAMES and "コイン" not in missions.GAMES
    assert not hasattr(Casino, "coin")
