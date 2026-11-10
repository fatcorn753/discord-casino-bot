"""競馬: ステータスの配分、シミュレーションとオッズ、馬の購入・出走・馬券・精算。"""

import datetime
import random

import pytest

from core import horse as h
from core import shop
from core.economy import INITIAL_BALANCE, Economy

U, V, W = 1, 2, 3
NOW = 1_000_000.0


@pytest.fixture
def eco(tmp_path):
    return Economy(str(tmp_path / "t.db"))


def give(eco, uid, item, qty=1):
    eco._conn.execute(
        "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty = qty + excluded.qty",
        (eco._g(), uid, item, qty),
    )
    eco._conn.commit()


def make_horse(eco, uid=U, name="テストホース"):
    eco.credit(uid, 50_000, "テスト")
    give(eco, uid, shop.HORSE_VOUCHER)
    ok, hid = eco.horse_create(uid, name, "栗毛", "🟥")
    assert ok, hid
    return hid


# ----------------------------- ステータスと名前 ----------------------------- #
def test_stats_total_is_fixed_and_carrot_moves_points_within_limits():
    s = dict(h.DEFAULT_STATS)
    assert h.valid_stats(s) and sum(s.values()) == h.STAT_TOTAL
    ok, new = h.move_points(s, "speed", "guts")
    assert ok and new["speed"] == 45 and new["guts"] == 55 and sum(new.values()) == h.STAT_TOTAL
    assert not h.move_points(s, "speed", "speed")[0]
    low = {"speed": 20, "stamina": 60, "power": 60, "guts": 60}
    assert "最低" in h.move_points(low, "speed", "guts")[1]
    high = {"speed": 80, "stamina": 40, "power": 40, "guts": 40}
    assert "最高" in h.move_points(high, "stamina", "speed")[1]


def test_random_stats_are_always_valid():
    rng = random.Random(1)
    assert all(h.valid_stats(h.random_stats(rng)) for _ in range(500))


@pytest.mark.parametrize("name", ["", "   ", "あ" * 13, "@everyone", "a<b", "http://x", "a`b", "a*b", "x\ny"])
def test_bad_horse_names_are_rejected(name):
    assert not h.check_name(name)[0]


def test_good_horse_names_are_trimmed():
    assert h.check_name("  ハヤテ ") == (True, "ハヤテ")


# ----------------------------- シミュレーションとオッズ ----------------------------- #
def field(rng, n=8):
    return [h.Runner(i + 1, f"h{i}", **h.random_stats(rng), form=h.random_form(rng)) for i in range(n)]


def test_race_is_decided_by_the_seed_and_has_a_full_order():
    runners = field(random.Random(3))
    a = h.simulate(runners, h.Track(1800, "良"), random.Random(9), keep_frames=True)
    b = h.simulate(runners, h.Track(1800, "良"), random.Random(9))
    assert a["order"] == b["order"] and sorted(a["order"]) == list(range(8))
    assert all(x <= y for x, y in zip(a["times"], a["times"][1:]))  # 着順 = ゴールの時刻の順
    first, last = a["frames"][0], a["frames"][-1]
    assert first == [0.0] * 8 and all(p == 1.0 for p in last)


def test_the_stronger_build_wins_more_often_but_not_always():
    strong = h.Runner(1, "s", 62, 50, 44, 44)
    weak = h.Runner(2, "w", 38, 50, 56, 56)
    win, _ = h.estimate([strong, weak], h.Track(1200, "良"), sims=1500, seed=1)
    assert 0.55 < win[0] < 0.97  # 強い馬が有利だが、負けることもある


def _record_runner(lane, races, wins, places, form=0, **stats):
    s = {"speed": 50, "stamina": 50, "power": 50, "guts": 50, **stats}
    return h.Runner(lane, f"r{lane}", s["speed"], s["stamina"], s["power"], s["guts"], form, races, wins, places)


def test_odds_follow_the_record_keep_the_house_edge_and_round_down():
    runners = [
        _record_runner(i + 1, 10, w, p) for i, (w, p) in enumerate([(6, 9), (3, 6), (2, 5), (1, 4)] + [(0, 2)] * 4)
    ]
    odds = h.make_odds(runners)
    for ow, op in odds:
        assert ow == int(ow * 10) / 10 and 1.0 <= ow <= h.MAX_ODDS and 1.0 <= op <= h.MAX_ODDS
    wins = [o[0] for o in odds]
    assert wins == sorted(wins)  # 戦績がよい順に、オッズが低い
    assert (
        sum((1 - h.TAKEOUT) / o for o in wins) <= 1.05
    )  # 控除ぶん、勝つ確率の見積もりの合計は 1 を超えない（切り捨ての余裕だけ）


def test_a_first_run_horse_pays_more_than_an_average_veteran():
    runners = [_record_runner(1, 0, 0, 0)] + [_record_runner(i, 8, 1, 3) for i in range(2, 9)]
    odds = h.make_all_odds(runners)
    assert odds["win"]["1"] > odds["win"]["2"] and odds["place"]["1"] > odds["place"]["2"]


def test_few_runs_pull_the_record_toward_the_average():
    lucky_once = [_record_runner(1, 1, 1, 1)] + [_record_runner(i, 10, 1, 3) for i in range(2, 9)]
    proven = [_record_runner(1, 10, 10, 10)] + [_record_runner(i, 10, 1, 3) for i in range(2, 9)]
    assert (
        h.make_all_odds(lucky_once)["win"]["1"] > h.make_all_odds(proven)["win"]["1"]
    )  # 1戦1勝より、10戦10勝のほうが低い


def test_form_moves_the_odds_but_stats_do_not():
    base = [_record_runner(i, 8, 1, 3) for i in range(1, 9)]
    good = [_record_runner(1, 8, 1, 3, form=1)] + base[1:]
    bad = [_record_runner(1, 8, 1, 3, form=-1)] + base[1:]
    strong = [_record_runner(1, 8, 1, 3, speed=80, stamina=40, power=40, guts=40)] + base[1:]
    flat = h.make_all_odds(base)["win"]["1"]
    assert h.make_all_odds(good)["win"]["1"] < flat < h.make_all_odds(bad)["win"]["1"]  # 好調ほど低い
    assert h.make_all_odds(strong)["win"]["1"] == flat  # ステータスは、オッズに入らない


def test_npc_records_are_plausible():
    rng = random.Random(3)
    for _ in range(200):
        races, wins, places = h.npc_record(rng)
        assert 0 <= wins <= places <= races <= 12


def test_favorite_and_longshot_spread_is_reasonable():
    rng = random.Random(7)
    tops, bottoms = [], []
    for k in range(12):
        win, _ = h.estimate(field(rng), h.random_track(rng), sims=800, seed=k)
        tops.append(max(win))
        bottoms.append(min(win))
    assert 0.2 < sum(tops) / len(tops) < 0.6 and sum(bottoms) / len(bottoms) < 0.05


def test_race_slots_are_two_a_day_in_local_time():
    tz = 9.0
    base = datetime.datetime(2026, 11, 9, 11, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=tz))).timestamp()
    slots = Economy.race_slots(base, tz)
    local = [datetime.datetime.fromtimestamp(s, datetime.timezone(datetime.timedelta(hours=tz))) for s in slots[:3]]
    assert [(x.day, x.hour) for x in local] == [(9, 12), (9, 20), (10, 12)]


# ----------------------------- 馬 ----------------------------- #
def test_horse_needs_a_voucher_and_is_limited_to_three(eco):
    ok, reason = eco.horse_create(U, "なし", "栗毛", "🟥")
    assert not ok and "購入券" in reason
    ids = [make_horse(eco, U, f"馬{i}") for i in range(3)]
    assert len(set(ids)) == 3
    give(eco, U, shop.HORSE_VOUCHER)
    ok, reason = eco.horse_create(U, "4頭目", "栗毛", "🟥")
    assert not ok and "3 頭" in reason and eco.inventory(U)[shop.HORSE_VOUCHER] == 1  # 券は減らない
    assert not eco.horse_create(V, "x", "ない毛色", "🟥")[0]


def test_carrot_moves_five_points_and_is_consumed(eco):
    hid = make_horse(eco)
    ok, reason = eco.horse_carrot(U, hid, "speed", "guts")
    assert not ok and "にんじん" in reason
    give(eco, U, shop.CARROT, 2)
    ok, stats = eco.horse_carrot(U, hid, "speed", "guts")
    assert ok and stats["speed"] == 45 and stats["guts"] == 55 and eco.inventory(U)[shop.CARROT] == 1
    assert not eco.horse_carrot(V, hid, "speed", "guts")[0]  # 他人の馬には使えない
    assert sum(eco.horse_get(hid)[k] for k in h.STAT_KEYS) == h.STAT_TOTAL


def test_closing_entries_copies_the_record_and_gives_npcs_one(eco):
    hid = make_horse(eco, U)
    eco._conn.execute("UPDATE horses SET races=6, wins=2, places=4 WHERE id=?", (hid,))
    rid = open_race(eco)
    assert eco.race_register(U, hid, rid, NOW + 1)[0]
    eco.race_close_entries(rid, random.Random(2))
    rows = eco.race_entries(rid)
    mine = next(e for e in rows if e["horse_id"] == hid)
    assert (mine["races"], mine["wins"], mine["places"]) == (6, 2, 4)
    assert all(e["wins"] <= e["places"] <= e["races"] for e in rows)


# ----------------------------- レースの流れ ----------------------------- #
def open_race(eco, now=NOW):
    return eco.race_create_special(now, 10, 10)


def test_full_race_flow_pays_bettors_and_owners_and_is_settled_once(eco):
    hid = make_horse(eco, U)
    rid = open_race(eco)
    before = eco.balance(U)
    assert eco.race_register(U, hid, rid, NOW + 1) == (True, "")
    assert eco.balance(U) == before - h.ENTRY_FEE
    assert (
        "1頭" in eco.race_register(U, hid, rid, NOW + 2)[1] or "出走予定" in eco.race_register(U, hid, rid, NOW + 2)[1]
    )
    field_ = eco.race_close_entries(rid, random.Random(1))
    assert len(field_["runners"]) == h.FIELD_SIZE
    eco.race_set_odds(rid, h.make_odds(field_["runners"]))
    entries = eco.race_entries(rid)
    assert (
        sorted(e["lane"] for e in entries) == list(range(1, 9)) and sum(1 for e in entries if e["owner_id"] == U) == 1
    )
    eco.credit(V, 20_000, "テスト")
    # 全頭に単勝を買う → ちょうど1頭が当たる
    for lane in range(1, 9):
        assert eco.race_bet(V, rid, "win", lane, 100, NOW + 700)[0]
    result = eco.race_run(rid, random.Random(5))
    assert result is not None and eco.race_run(rid, random.Random(5)) is None  # 二重に精算しない
    winners = [b for b in result["bets"] if b["payout"]]
    assert len(winners) == 1 and winners[0]["lane"] == result["entries"][result["order"][0]]["lane"]
    assert eco.race_get(rid)["status"] == "done" and all(b["payout"] >= 0 for b in result["bets"])
    mine = next(e for e in result["entries"] if e["owner_id"] == U)
    prize = h.PRIZES[mine["finish"] - 1] if mine["finish"] <= 3 else 0
    assert result["prizes"].get(U, 0) == prize
    stats = eco.horse_get(hid)
    assert stats["races"] == 1 and stats["wins"] == (1 if mine["finish"] == 1 else 0) and stats["earnings"] == prize


def test_bet_rules(eco):
    rid = open_race(eco)
    eco.credit(V, 100_000, "テスト")
    assert "まだ" in eco.race_bet(V, rid, "win", 1, 100, NOW + 1)[1]  # 締切前（発売前）は買えない
    field_ = eco.race_close_entries(rid, random.Random(2))
    eco.race_set_odds(rid, h.make_odds(field_["runners"]))
    assert "締め切りました" in eco.race_bet(V, rid, "win", 1, 100, NOW + 10_000)[1]
    assert "賭け金" in eco.race_bet(V, rid, "win", 1, 50, NOW + 700)[1]
    assert "賭け金" in eco.race_bet(V, rid, "win", 1, h.MAX_STAKE + 1, NOW + 700)[1]
    assert "種類" in eco.race_bet(V, rid, "bogus", 1, 100, NOW + 700)[1]
    assert "出走しません" in eco.race_bet(V, rid, "win", 9, 100, NOW + 700)[1]
    for _ in range(3):
        assert eco.race_bet(V, rid, "place", 1, h.MAX_STAKE, NOW + 700)[0]
    assert "合計" in eco.race_bet(V, rid, "place", 1, 100, NOW + 700)[1]
    assert "足りません" in eco.race_bet(W, rid, "win", 1, 100, NOW + 700)[1] or eco.balance(W) >= 100


def test_bet_locks_in_the_odds_and_place_pays_for_top_three(eco):
    rid = open_race(eco)
    eco.credit(V, 100_000, "テスト")
    field_ = eco.race_close_entries(rid, random.Random(2))
    eco.race_set_odds(rid, [(5.0, 2.0)] * h.FIELD_SIZE)
    ok, bet = eco.race_bet(V, rid, "place", 3, 1000, NOW + 700)
    assert ok and bet["odds"] == 2.0 and eco.balance(V) == INITIAL_BALANCE + 100_000 - 1000
    eco.race_set_odds(rid, [(9.0, 9.0)] * h.FIELD_SIZE)  # あとでオッズが変わっても、買った時点のオッズ
    result = eco.race_run(rid, random.Random(3))
    mine = next(b for b in result["bets"] if b["user_id"] == V)
    in_top3 = any(result["entries"][i]["lane"] == 3 for i in result["order"][:3])
    assert mine["payout"] == (2000 if in_top3 else 0)
    assert eco.balance(V) == INITIAL_BALANCE + 100_000 - 1000 + mine["payout"]
    assert field_["track"].distance in h.DISTANCES


def test_registration_rules_withdraw_and_close(eco):
    a, b = make_horse(eco, U, "ア"), make_horse(eco, V, "イ")
    rid = open_race(eco)
    assert not eco.race_register(V, a, rid, NOW + 1)[0]  # 他人の馬は出せない
    assert eco.race_register(U, a, rid, NOW + 1)[0] and eco.race_register(V, b, rid, NOW + 1)[0]
    bal = eco.balance(U)
    assert eco.race_withdraw(U, a, rid, NOW + 2) == (True, "") and eco.balance(U) == bal + h.ENTRY_FEE
    assert not eco.race_withdraw(U, a, rid, NOW + 2)[0]
    assert "締め切りました" in eco.race_register(U, a, rid, NOW + 700)[1]
    field_ = eco.race_close_entries(rid, random.Random(1))
    assert len(field_["runners"]) == h.FIELD_SIZE and eco.race_close_entries(rid) is None  # 締切は1回だけ
    names = [e["name"] for e in eco.race_entries(rid)]
    assert "イ" in names and "ア" not in names and len(set(names)) == h.FIELD_SIZE
    assert not eco.race_register(U, a, rid, NOW + 3)[0]


def test_scheduled_race_is_created_once_per_slot(eco):
    first = eco.race_ensure_scheduled(NOW, 9.0)
    assert first is not None and eco.race_ensure_scheduled(NOW, 9.0) is None
    race = eco.race_get(first)
    assert race["kind"] == "regular" and race["entry_close"] == race["start_at"] - h.ENTRY_LEAD_MIN * 60
    assert race["bet_close"] == race["start_at"] - h.BET_LEAD_SEC


# ----------------------------- 第2段階: 組み合わせの馬券 ----------------------------- #
def test_combo_keys_and_hits():
    assert h.combo_key("quinella", [5, 2]) == "2-5" and h.combo_key("trio", [7, 1, 4]) == "1-4-7"
    assert h.combo_key("exacta", [5, 2]) == "5>2" and h.combo_key("wide", [8, 3]) == "3-8"
    order = [4, 1, 6, 2, 3, 5, 7, 8]  # 枠番の着順
    assert h.combo_hit("quinella", [1, 4], order) and not h.combo_hit("quinella", [1, 6], order)
    assert h.combo_hit("exacta", [4, 1], order) and not h.combo_hit("exacta", [1, 4], order)
    assert (
        h.combo_hit("wide", [6, 4], order)
        and h.combo_hit("wide", [1, 6], order)
        and not h.combo_hit("wide", [4, 2], order)
    )
    assert h.combo_hit("trio", [6, 4, 1], order) and not h.combo_hit("trio", [4, 1, 2], order)


def test_all_odds_cover_every_combination_and_never_exceed_the_cap():
    rng = random.Random(11)
    runners = field(rng)
    odds = h.make_all_odds(runners)
    assert {k: len(v) for k, v in odds.items()} == {
        "win": 8,
        "place": 8,
        "quinella": 28,
        "wide": 28,
        "exacta": 56,
        "trio": 56,
    }
    for kind in h.COMBO_SIZE:
        assert all(1.0 <= o <= h.MAX_COMBO_ODDS for o in odds[kind].values())
    # 3着以内の2頭（ワイド）は、1・2着の2頭（馬連）より、当たりやすいので、オッズが低い
    assert sum(odds["wide"].values()) < sum(odds["quinella"].values())


def test_combo_odds_are_consistent_with_the_single_win_odds():
    runners = [_record_runner(i + 1, 10, w, w + 2) for i, w in enumerate([5, 3, 2, 1, 1, 0, 0, 0])]
    odds = h.make_all_odds(runners)
    # 強い馬が絡む組み合わせほど、オッズが低い（馬連 1-2 は、馬連 7-8 より低い）
    assert odds["quinella"]["1-2"] < odds["quinella"]["7-8"]
    assert odds["exacta"]["1>2"] < odds["exacta"]["2>1"]  # 強い馬が先着のほうが、起こりやすい


def test_distances_and_goings_have_more_variety_and_stay_balanced():
    assert len(h.DISTANCES) >= 7 and h.GOINGS[-1] == "不良"
    assert h.speed_coef(1000) > h.speed_coef(1800) > h.speed_coef(3000)
    rng = random.Random(1)
    seen = {h.random_track(rng) for _ in range(600)}
    assert {t.distance for t in seen} == set(h.DISTANCES) and {t.going for t in seen} == set(h.GOINGS)
    tops = []
    for k, d in enumerate((1000, 3000)):
        win, _ = h.estimate(field(rng), h.Track(d, "不良"), sims=600, seed=k)
        tops.append(max(win))
    assert all(0.15 < t < 0.9 for t in tops)


def _race_with_odds(eco, now=NOW):
    rid = eco.race_create_special(now, 10, 10)
    field_ = eco.race_close_entries(rid, random.Random(4))
    eco.race_set_odds(rid, h.make_all_odds(field_["runners"]))
    return rid


def test_combo_bets_validate_picks_and_lock_in_the_odds(eco):
    rid = _race_with_odds(eco)
    eco.credit(V, 100_000, "テスト")
    at = NOW + 700
    assert "2 頭" in eco.race_bet(V, rid, "quinella", [1], 100, at)[1]
    assert "違う馬" in eco.race_bet(V, rid, "quinella", [3, 3], 100, at)[1]
    assert "3 頭" in eco.race_bet(V, rid, "trio", [1, 2], 100, at)[1]
    assert "出走しません" in eco.race_bet(V, rid, "wide", [1, 9], 100, at)[1]
    ok, bet = eco.race_bet(V, rid, "exacta", [5, 2], 200, at)
    assert ok and bet["lanes"] == [5, 2] and bet["odds"] == eco.race_odds(rid, "exacta", [5, 2])
    assert eco.race_odds(rid, "exacta", [2, 5]) is not None  # 順番が違えば、別の馬券
    ok, bet = eco.race_bet(V, rid, "quinella", [6, 1], 300, at)
    assert ok and eco.race_odds(rid, "quinella", [1, 6]) == bet["odds"]  # 順不同は、小さい順にそろえる


def test_combo_bets_are_settled_by_the_real_finishing_order(eco):
    rid = _race_with_odds(eco)
    eco.credit(V, 200_000, "テスト")
    at = NOW + 700
    kinds = {"quinella": 2, "wide": 2, "exacta": 2, "trio": 3}
    placed = []
    import itertools

    for kind, n in kinds.items():
        combos = itertools.permutations(range(1, 9), n) if kind == "exacta" else itertools.combinations(range(1, 9), n)
        for lanes in list(combos)[:40]:
            ok, bet = eco.race_bet(V, rid, kind, list(lanes), 100, at)
            if ok:
                placed.append((kind, list(lanes), bet["odds"]))
    result = eco.race_run(rid, random.Random(8))
    order_lanes = [result["entries"][i]["lane"] for i in result["order"]]
    by_pick = {(b["kind"], b["picks"]): b for b in result["bets"]}
    assert placed and len(result["bets"]) == len(placed)
    for kind, lanes, odds in placed:
        b = by_pick[(kind, h.combo_key(kind, lanes))]
        expected = int(100 * b["odds"]) if h.combo_hit(kind, lanes, order_lanes) else 0
        assert b["payout"] == expected


# ----------------------------- 第2段階: 破産で馬を差し押さえて競売へ ----------------------------- #
def test_bankruptcy_seizes_horses_and_lists_them_at_the_auction(eco):
    hid = make_horse(eco, U, "差押馬")
    rid = open_race(eco)
    assert eco.race_register(U, hid, rid, NOW + 1)[0]
    eco._conn.execute("UPDATE users SET balance=0, debt=20000 WHERE user_id=?", (U,))
    ok, result = eco.bankrupt(U)
    assert ok and result["shop_value"] >= int(eco.HORSE_PRICE * 0.5)  # 馬は、購入券の値段の50%で換金
    horse = eco.horse_get(hid)
    assert horse["owner_id"] == -1 and horse["listed_at"] is not None and eco.horse_list(U) == []
    assert eco.race_entries(rid, registered_only=True) == []  # 申し込みは取り下げられる
    listed = eco.auction_horses()
    assert [x["name"] for x in listed] == ["差押馬"] and listed[0]["price"] == round(eco.HORSE_PRICE * 0.9)


def test_a_horse_already_in_the_field_is_replaced_by_a_house_horse(eco):
    hid = make_horse(eco, U, "出走中")
    rid = open_race(eco)
    assert eco.race_register(U, hid, rid, NOW + 1)[0]
    eco.race_close_entries(rid, random.Random(1))
    assert any(e["owner_id"] == U for e in eco.race_entries(rid))
    eco._conn.execute("UPDATE users SET balance=0, debt=20000 WHERE user_id=?", (U,))
    assert eco.bankrupt(U)[0]
    assert not any(e["owner_id"] == U for e in eco.race_entries(rid))  # 賞金は、破産した馬主には入らない


def test_auction_horse_price_falls_and_only_katagi_with_room_can_buy(eco):
    hid = make_horse(eco, U, "競売馬")
    eco._conn.execute("UPDATE users SET balance=0, debt=20000 WHERE user_id=?", (U,))
    assert eco.bankrupt(U)[0]
    listed_at = eco.horse_get(hid)["listed_at"]
    day = 86400
    assert eco.horse_auction_price(listed_at, listed_at + 5 * day) == round(eco.HORSE_PRICE * 0.6)  # 下限
    eco.credit(V, 100_000, "テスト")
    assert not eco.auction_buy_horse(U, hid).ok  # 破産者は買えない
    res = eco.auction_buy_horse(V, hid, listed_at)
    assert res.ok and eco.horse_get(hid)["owner_id"] == V and eco.horse_get(hid)["name"] == "競売馬"
    assert "売れて" in eco.auction_buy_horse(V, hid).reason  # もう売れた
    for i in range(2):
        make_horse(eco, V, f"馬{i}")
    other = make_horse(eco, W, "別の馬")
    eco._conn.execute("UPDATE users SET balance=0, debt=20000 WHERE user_id=?", (W,))
    assert eco.bankrupt(W)[0]
    assert "3 頭" in eco.auction_buy_horse(V, other).reason  # すでに3頭


# ----------------------------- 臨時レースは、出走予定の馬も出せる ----------------------------- #
def test_a_horse_scheduled_for_a_regular_race_can_still_enter_a_special_race(eco):
    hid = make_horse(eco, U)
    regular = eco.race_ensure_scheduled(NOW, 9.0)
    assert eco.race_register(U, hid, regular, NOW + 1) == (True, "")  # 定時に出走予定
    special = open_race(eco)
    assert eco.race_register(U, hid, special, NOW + 2) == (True, "")  # 臨時にも出せる
    assert eco.race_withdraw(U, hid, special, NOW + 3)[0]  # 臨時だけを取り下げても、定時はそのまま
    assert [e["race_id"] for e in eco.race_entries(regular, registered_only=True)] == [regular]


def test_a_horse_in_a_special_race_can_enter_a_regular_race_but_two_regular_races_stay_exclusive(eco):
    hid = make_horse(eco, U)
    special = open_race(eco)
    assert eco.race_register(U, hid, special, NOW + 1)[0]
    regular = eco.race_ensure_scheduled(NOW, 9.0)
    assert eco.race_register(U, hid, regular, NOW + 2)[0]  # 臨時に出ていても、定時に出せる
    second_special = open_race(eco, NOW + 3)
    assert eco.race_register(U, hid, second_special, NOW + 4)[0]  # 臨時どうしも、重ねて出せる
    start = eco.race_get(regular)["start_at"]
    eco._conn.execute(
        "INSERT INTO horse_races (guild_id, kind, start_at, entry_close, bet_close, status, created) "
        "VALUES (?, 'regular', ?, ?, ?, 'entry', ?)",
        (eco._g(), start + 43200, start + 43000, start + 43100, NOW),
    )
    other_regular = eco._conn.execute("SELECT MAX(id) FROM horse_races").fetchone()[0]
    ok, why = eco.race_register(U, hid, other_regular, NOW + 5)
    assert not ok and "ほかのレースに出走予定" in why  # 定時どうしは、これまでどおり、重ねて出せない


def test_npc_silks_never_overlap_other_horses_but_player_silks_may(tmp_path):
    eco = Economy(str(tmp_path / "s.db"))
    for seed in range(30):
        rid = eco.race_create_special(1_700_000_000.0 + seed, 1, 1)
        eco.race_close_entries(rid, random.Random(seed))
        rows = eco.race_entries(rid)
        silks = [e["silks"] for e in rows]
        assert len(rows) == h.FIELD_SIZE and len(set(silks)) == len(silks)  # 全員 NPC なら、全員ちがう色


def test_npc_silks_avoid_the_colors_players_already_use(tmp_path):
    eco = Economy(str(tmp_path / "p.db"))
    for uid, silks in ((11, "🟥"), (12, "🟥"), (13, "🟦")):  # プレイヤー同士の色被りは、そのまま
        eco.credit(uid, 100_000, "テスト")
        eco._conn.execute(
            "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, 1)",
            (eco._g(), uid, shop.HORSE_VOUCHER),
        )
        ok, hid = eco.horse_create(uid, f"馬{uid}", "栗毛", silks)
        assert ok
    rid = eco.race_create_special(1_700_000_000.0, 1, 1)
    for hid in [x["id"] for u in (11, 12, 13) for x in eco.horse_list(u)]:
        assert eco.race_register(next(x["owner_id"] for x in [eco.horse_get(hid)]), hid, rid, 1_700_000_001.0)[0]
    eco.race_close_entries(rid, random.Random(3))
    rows = eco.race_entries(rid)
    npc = [e["silks"] for e in rows if e["owner_id"] == 0]
    assert len(npc) == 5 and len(set(npc)) == 5 and not ({"🟥", "🟦"} & set(npc))
