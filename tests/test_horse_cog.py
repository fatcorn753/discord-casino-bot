"""競馬の cog: 定時の確認（申し込み → 出馬表 → 発走）、アニメーション、画面の部品。"""

import asyncio
import random
from types import SimpleNamespace

import pytest

from cogs import _horse_views as views
from cogs import horse_cog
from cogs.horse_cog import HorseCog, comment, lane_text, pick_frames
from core import horse as h
from core import shop
from core.economy import Economy

U, V = 1, 2


class Msg:
    def __init__(self, chan, kw):
        self.chan, self.kw, self.id = chan, kw, len(chan.sent) + 100

    async def edit(self, **kw):
        self.chan.edits.append(kw)


class Chan:
    id = 55

    def __init__(self):
        self.sent, self.edits, self.retired = [], [], []

    def get_partial_message(self, mid):
        chan = self

        class Partial:
            async def edit(self, **kw):
                chan.retired.append((mid, kw))

        return Partial()

    async def send(self, **kw):
        self.sent.append(kw)
        return Msg(self, kw)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    eco = Economy(str(tmp_path / "t.db"))
    chan = Chan()
    guild = SimpleNamespace(id=0, me=object(), system_channel=chan, get_channel=lambda cid: chan, name="g")
    bot = SimpleNamespace(economy=SimpleNamespace(for_guild=lambda gid: eco), config={}, guilds=[guild])
    monkeypatch.setattr(horse_cog.approval, "is_allowed", lambda *a: True)
    monkeypatch.setattr("cogs.horse_cog.announce_channel", lambda g, e: chan)

    async def nap(_):
        return None

    monkeypatch.setattr(horse_cog.asyncio, "sleep", nap)
    monkeypatch.setattr(horse_cog.horse_mod, "ODDS_SIMS", 300)
    return eco, chan, guild, bot, HorseCog(bot)


def test_pick_frames_keeps_first_and_last_and_the_count():
    frames = [[i / 99] * 3 for i in range(100)]
    picked = pick_frames(frames, 13)
    assert len(picked) == 13 and picked[0] == frames[0] and picked[-1] == frames[-1]
    short = pick_frames(frames[:5], 13)
    assert short[0] == frames[0] and short[-1] == frames[4] and len(short) <= 5


def test_lane_text_and_commentary():
    entries = [{"lane": 1, "silks": "🟥", "name": "アルファ"}, {"lane": 2, "silks": "🟦", "name": "ベータ"}]
    text = lane_text(entries, [0.0, 1.0])
    assert "1 🟥" in text and "2 🟦" in text and text.startswith("```") and "アルファ" in text
    assert "スタート" in comment(entries, [0.01, 0.0], None, False)[0]
    assert "ゴール" in comment(entries, [1.0, 0.9], 0, True)[0]
    assert "先頭に立った" in comment(entries, [0.3, 0.5], 0, False)[0]
    assert "最後の直線" in comment(entries, [0.7, 0.6], 0, False)[0]


def test_scheduled_flow_announces_closes_with_odds_runs_animation_and_settles(setup, monkeypatch):
    eco, chan, guild, bot, cog = setup
    eco.credit(U, 100_000, "テスト")
    eco._conn.execute(
        "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, 1)", (eco._g(), U, shop.HORSE_VOUCHER)
    )
    ok, hid = eco.horse_create(U, "テストホース", "栗毛", "🟥")
    assert ok
    now = 1_700_000_000.0
    monkeypatch.setattr("cogs.horse_cog.time.time", lambda: now)
    asyncio.run(cog.run_once())  # 次の定時レースを作って、申し込み開始を知らせる
    race = eco.race_open()[0]
    assert race["status"] == "entry" and "申し込みが始まりました" in chan.sent[0]["content"]
    assert eco.race_register(U, hid, race["id"], now)[0]
    monkeypatch.setattr("cogs.horse_cog.time.time", lambda: race["entry_close"] + 1)
    asyncio.run(cog.run_once())  # 締切 → 出馬表とオッズ
    race = eco.race_get(race["id"])
    assert race["status"] == "betting" and "出馬表とオッズ" in chan.sent[-1]["content"]
    entries = eco.race_entries(race["id"])
    assert len(entries) == h.FIELD_SIZE and all(e["odds_win"] >= 1.0 for e in entries)
    assert eco.race_bet(V, race["id"], "win", 1, 100, race["entry_close"] + 2)[1] != ""  # V には残高があるので買える
    assert len(chan.retired) == 1 and "受付終了" in chan.retired[0][1]["embed"].title  # 申し込みの投稿を書き換えた
    assert chan.retired[0][1]["view"] is None and "R>" not in chan.retired[0][1]["embed"].description
    sent_before = len(chan.sent)
    monkeypatch.setattr("cogs.horse_cog.time.time", lambda: race["start_at"] + 1)
    asyncio.run(cog.run_once())  # 発走 → 精算 → アニメーション → 結果
    assert eco.race_get(race["id"])["status"] == "done"
    assert len(chan.retired) == 2 and "発売終了" in chan.retired[1][1]["embed"].title  # 出馬表の投稿も、書き換えた
    assert "R>" not in chan.retired[1][1]["embed"].description
    assert len(chan.sent) == sent_before + 3  # 次のレースの申し込み開始・アニメーションのメッセージ・結果
    assert "発走" in chan.sent[-2]["embed"].title
    assert len(chan.edits) >= 5 and "ゴール" in chan.edits[-1]["embed"].description
    assert "結果" in chan.sent[-1]["embed"].title and eco.horse_get(hid)["races"] == 1
    asyncio.run(cog.run_once())  # もう一度確認しても、二重に走らない
    assert eco.horse_get(hid)["races"] == 1


def test_special_race_command_path_creates_and_announces(setup, monkeypatch):
    eco, chan, guild, bot, cog = setup
    monkeypatch.setattr("cogs.horse_cog.time.time", lambda: 1_700_000_000.0)
    rid = asyncio.run(cog.start_special(guild, 3, 4))
    race = eco.race_get(rid)
    assert race["kind"] == "special" and race["entry_close"] == 1_700_000_000.0 + 180
    assert race["bet_close"] == race["entry_close"] + 240 and "申し込みが始まりました" in chan.sent[0]["content"]


def test_special_race_can_use_a_chosen_channel(setup, monkeypatch):
    eco, chan, guild, bot, cog = setup
    other = Chan()
    other.id = 77
    guild.get_channel = lambda cid: other if cid == 77 else chan
    monkeypatch.setattr("cogs.horse_cog.time.time", lambda: 1_700_000_000.0)
    rid = asyncio.run(cog.start_special(guild, 3, 4, other))
    assert eco.race_get(rid)["channel_id"] == 77 and not chan.sent
    assert "申し込みが始まりました" in other.sent[0]["content"]


def test_card_embed_for_each_stage_and_bet_panel(setup):
    eco, chan, guild, bot, cog = setup
    eco.credit(V, 10_000, "テスト")
    rid = eco.race_create_special(1_700_000_000.0, 1, 1)
    race = eco.race_get(rid)
    assert "出走受付中" in views.card_embed(eco, race).title
    field = eco.race_close_entries(rid, random.Random(1))
    eco.race_set_odds(rid, h.make_odds(field["runners"], field["track"], sims=200, seed=1))
    race = eco.race_get(rid)
    card = views.card_embed(eco, race)
    assert "発売中" in card.title and card.description.count("単勝") >= h.FIELD_SIZE
    panel = views.BetPanel(
        SimpleNamespace(economy=SimpleNamespace(for_guild=lambda g: eco)), V, {**race, "guild_id": 0}
    )
    assert len(panel.children) == 3 and "馬券を買う" in panel.embed(eco).title
    eco.race_run(rid, random.Random(2))
    assert "結果" in views.card_embed(eco, eco.race_get(rid)).title


def test_stable_view_shows_horses_and_stats(setup):
    eco, chan, guild, bot, cog = setup
    eco.credit(U, 50_000, "テスト")
    eco._conn.execute(
        "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, 2)", (eco._g(), U, shop.HORSE_VOUCHER)
    )
    eco.horse_create(U, "ホースA", "鹿毛", "🟦")
    stable = views.Stable(bot, U, 0)
    emb = stable.embed()
    assert "ホースA" in emb.fields[0].name and "スピード" in emb.fields[0].value
    assert any(isinstance(c, views.discord.ui.Select) for c in stable.children)
    assert "購入券 **1枚**" in emb.description


def test_display_frames_end_when_the_winner_crosses_and_never_move_backward():
    rng = random.Random(3)
    rs = [h.Runner(i + 1, f"馬{i + 1}", **h.random_stats(rng)) for i in range(8)]
    sim = h.simulate(rs, h.Track(1800, "良"), rng, keep_frames=True)
    winner = sim["order"][0]
    frames = horse_cog.display_frames(sim["frames"], winner)
    assert frames[-1][winner] == 1.0 and all(p <= 1.0 for row in frames for p in row)
    assert sum(1 for p in frames[-1] if p < 1.0) >= 6  # ゴールの瞬間に、全馬が並んでいない（差が見える）
    for a, b in zip(frames, frames[1:]):
        assert all(y >= x for x, y in zip(a, b))
    entries = [{"lane": r.lane, "silks": "🟥", "name": r.name} for r in rs]
    text, leader = comment(entries, frames[-1], None, True, winner)
    assert entries[winner]["name"] in text and leader == winner


def test_bet_panel_builds_for_every_kind_and_shows_the_odds(setup):
    eco, chan, guild, bot, cog = setup
    rid = eco.race_create_special(1_700_000_000.0, 1, 1)
    field = eco.race_close_entries(rid, random.Random(1))
    eco.race_set_odds(rid, h.make_all_odds(field["runners"], field["track"], sims=200, seed=1))
    race = {**eco.race_get(rid), "guild_id": 0}
    panel = views.BetPanel(SimpleNamespace(economy=SimpleNamespace(for_guild=lambda g: eco)), V, race)
    selects = {}
    for kind in h.BET_KINDS:
        panel.kind = kind
        panel._build(eco)
        selects[kind] = [c for c in panel.children if isinstance(c, views.discord.ui.Select)]
        embed = panel.embed(eco)
        assert h.BET_KINDS[kind] in embed.description and "オッズ" in embed.description
    assert [len(v) for v in selects.values()] == [2, 2, 2, 2, 3, 2]  # 馬単だけ、1着と2着の2つ
    assert (selects["trio"][1].min_values, selects["trio"][1].max_values) == (3, 3)
    assert (selects["quinella"][1].min_values, selects["quinella"][1].max_values) == (2, 2)
    panel.kind = "win"
    assert panel.picked() == panel.lanes[:1]


def test_auction_screen_lists_seized_horses_with_a_horse_select(setup):
    eco, chan, guild, bot, cog = setup
    from cogs import shop_cog

    eco.credit(U, 50_000, "テスト")
    eco._conn.execute(
        "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, 1)", (eco._g(), U, shop.HORSE_VOUCHER)
    )
    eco.horse_create(U, "競売の馬", "鹿毛", "🟦")
    eco._conn.execute("UPDATE users SET balance=0, debt=20000 WHERE user_id=?", (U,))
    assert eco.bankrupt(U)[0]
    fake_bot = SimpleNamespace(economy=SimpleNamespace(for_guild=lambda g: eco))
    cog_shop = shop_cog.ShopCog(fake_bot)
    embed = cog_shop._auction_embed(0, V)
    assert any("競売の馬" in f.value for f in embed.fields)
    view = shop_cog.ShopView(cog_shop, V, 0, screen="auction")
    assert any(getattr(c, "placeholder", "") == "競走馬を買う" for c in view.children)


def test_bet_is_announced_publicly_in_the_race_channel(setup):
    eco, chan, guild, bot, cog = setup
    bot.get_cog = lambda name: cog
    rid = eco.race_create_special(1_700_000_000.0, 1, 1)
    race = eco.race_get(rid)
    panel = views.BetPanel(bot, V, {**race, "guild_id": 0})
    interaction = SimpleNamespace(guild=guild, guild_id=0, user=SimpleNamespace(id=V))
    result = {"kind": "win", "lanes": [3], "name": "アルファ", "stake": 500, "odds": 4.2}
    asyncio.run(panel.announce_bet(interaction, "3", result))
    text = chan.sent[-1]["content"]
    assert f"<@{V}>" in text and "単勝" in text and "アルファ" in text and "4.2" in text


def _open_race(eco):
    rid = eco.race_create_special(1_700_000_000.0, 1, 1)
    field = eco.race_close_entries(rid, random.Random(1))
    eco.race_set_odds(rid, h.make_all_odds(field["runners"], field["track"], sims=200, seed=1))
    race = eco.race_get(rid)
    return rid, race, race["bet_close"] - 10


def test_win_and_place_select_several_horses_at_once(setup):
    eco, chan, guild, bot, cog = setup
    rid, race, now = _open_race(eco)
    panel = views.BetPanel(
        SimpleNamespace(economy=SimpleNamespace(for_guild=lambda g: eco)), V, {**race, "guild_id": 0}
    )
    for kind in h.MULTI_KINDS:
        panel.kind = kind
        panel._build(eco)
        sel = [c for c in panel.children if isinstance(c, views.discord.ui.Select)][1]
        assert (sel.min_values, sel.max_values) == (1, len(eco.race_entries(rid)))  # 何頭でも選べる
    panel.multi = [5, 2, 7]
    assert panel.picked() == [2, 5, 7]
    text = panel.embed(eco).description
    assert "3頭" in text and "2枠" in text and "5枠" in text and "7枠" in text


def test_race_bet_many_buys_one_ticket_per_horse_with_the_same_stake_each(setup):
    eco, chan, guild, bot, cog = setup
    eco.credit(V, 10_000, "テスト")
    rid, race, now = _open_race(eco)
    before = eco.balance(V)
    ok, bought = eco.race_bet_many(V, rid, "place", [2, 5, 7, 5], 500, now)  # 重複した枠は1回だけ
    assert ok and [b["lane"] for b in bought] == [2, 5, 7] and all(b["stake"] == 500 for b in bought)
    assert eco.balance(V) == before - 1_500 and len(eco.race_bets(V, rid)) == 3
    assert {b["kind"] for b in eco.race_bets(V, rid)} == {"place"}
    assert eco.race_bet_many(V, rid, "quinella", [1, 2], 500, now)[0] is False  # まとめ買いは、単勝・複勝だけ
    assert eco.race_bet_many(V, rid, "place", [], 500, now)[0] is False


def test_race_bet_many_checks_the_total_before_buying_anything(setup):
    eco, chan, guild, bot, cog = setup
    eco.set_balance(V, 1_000, "テスト")
    rid, race, now = _open_race(eco)
    ok, why = eco.race_bet_many(V, rid, "place", [1, 2, 3], 500, now)  # 合計 1,500 で足りない
    assert not ok and "足りません" in why and eco.balance(V) == 1_000 and eco.race_bets(V, rid) == []
    eco.set_balance(V, 100_000, "テスト")
    ok, why = eco.race_bet_many(V, rid, "place", list(range(1, 9)), h.MAX_STAKE, now)  # 合計が1レースの上限を超える
    assert not ok and "合計" in why and eco.race_bets(V, rid) == []


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_stake_modal_buys_every_selected_horse_and_announces_once(setup):
    eco, chan, guild, bot, cog = setup
    bot.get_cog = lambda name: cog
    eco.credit(V, 10_000, "テスト")
    before = eco.balance(V)
    rid, race, now = _open_race(eco)
    panel = views.BetPanel(bot, V, {**race, "guild_id": 0})
    panel.kind, panel.multi = "place", [3, 6]
    modal = views.StakeModal(panel)
    assert "1頭あたり" in modal.stake.label
    modal.stake._value = "400"
    sent = []

    class Resp:
        async def send_message(self, **kw):
            sent.append(kw)

    it = SimpleNamespace(guild=guild, guild_id=0, user=SimpleNamespace(id=V), response=Resp())
    import time as _t

    orig = _t.time
    _t.time = lambda: now
    try:
        asyncio.run(modal.on_submit(it))
    finally:
        _t.time = orig
    assert len(eco.race_bets(V, rid)) == 2 and eco.balance(V) == before - 800
    assert "合計" in sent[-1]["embed"].description
    posts = [m["content"] for m in chan.sent if "content" in m and "🎫" in m["content"]]
    assert len(posts) == 1 and "複勝" in posts[0] and "3枠" in posts[0] and "6枠" in posts[0]
