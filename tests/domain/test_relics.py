"""発掘品の効果（持っている間、1個ごとに +1%）。"""

import random

import pytest

from domain import horse as horse_mod
from domain import labor as lab
from domain import p2p, relics, shop, teiai
from infra.db.economy import Economy

pytestmark = pytest.mark.real_scope

G, U = 10, 1
DAY = "2026-11-10"
T0 = 1_700_000_000.0
H = 3600.0


@pytest.fixture
def eco(tmp_path):
    e = Economy(str(tmp_path / "t.db"), strict=True).for_guild(G)
    e.credit(U, 50_000, "テスト")
    return e


def give(eco, item, qty=1, uid=U):
    eco._conn.execute(
        "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty = qty + excluded.qty",
        (eco._g(), uid, item, qty),
    )
    eco._conn.commit()


# ----------------------------- 純粋なロジック ----------------------------- #
def test_every_relic_has_one_percent_buff_and_a_description():
    assert set(relics.BUFFS) == {
        i for i in shop.RELIC_VALUE if relics.split_id(i)[1] == 0
    }  # 全部の発掘品に、効果がある
    assert len({name for name, _ in relics.BUFFS.values()}) == len(relics.BUFFS)  # 効果は、種類ごとに別
    assert relics.BUFF_RATE == 0.01
    for item_id in relics.BUFFS:
        assert "+1%" in relics.buff_text(item_id) and "+1%" in shop.BY_ID[item_id].desc


def test_buffs_stack_per_item_and_unknown_items_are_ignored():
    got = relics.buffs_of(["relic_fossil", "relic_fossil", "relic_crown", "carrot"])
    assert got == {"click": 0.02, "daily": 0.01}
    assert relics.buffs_of({"relic_bone": 3, "relic_tear": 0, "carrot": 5}) == {"wage": 0.03}
    assert relics.buffs_of([]) == {}


def test_chance_round_keeps_the_average_exact():
    rng = random.Random(1)
    mean = sum(relics.chance_round(10.1, rng) for _ in range(20000)) / 20000
    assert 10.07 < mean < 10.13 and relics.chance_round(7.0, rng) == 7


# ----------------------------- 効果 ----------------------------- #
class _NoCrit:  # 鉱脈ヒットを起こさない（random() が 1.0 なら、しきい値を超えない）
    @staticmethod
    def random():
        return 1.0

    @staticmethod
    def choices(*a, **kw):
        return random.choices(*a, **kw)


def test_fossil_raises_click_pay_by_one_percent_on_average(eco, monkeypatch):
    monkeypatch.setattr(lab, "COMBO_SIZE", 10**9)  # コンボ（10回ごとの上乗せ）で平均がずれないようにする
    plain = 0
    for k in range(1000):
        plain += eco.labor_click(U, T0 + k * 2000, DAY, rng=_NoCrit)["pay"]  # 疲労が回復するほど、間をあける
    other = Economy(eco._db_path, strict=True).for_guild(G)
    give(other, "relic_fossil", uid=2)
    other.credit(2, 1_000, "テスト")
    buffed = sum(other.labor_click(2, T0 + k * 2000, DAY, rng=_NoCrit)["pay"] for k in range(1000))
    assert plain == 10 * 1000  # 工具なし・疲労なしで、1回10
    assert 10_020 < buffed < 10_200  # 平均で +1%（確率で切り上げる）


def test_bone_raises_worker_wage_and_meteor_raises_ore_luck():
    worker = [{"id": 1, "kind": "miner", "expires_at": 9e9}]
    base = lab.settle_plan(worker, True, 0.0, 10 * H, 0, 0, random.Random(1))
    wage = lab.settle_plan(worker, True, 0.0, 10 * H, 0, 0, random.Random(1), wage_bonus=0.01)
    assert base["gain"] <= wage["gain"] <= base["gain"] + 2  # 作業員の稼ぎが +1%（10時間で 100.5 → 101.5）
    rng = random.Random(7)
    a = sum(lab.roll_ores("craftsman", 1, 0.0, rng).get("gold", 0) for _ in range(400000))
    b = sum(lab.roll_ores("craftsman", 1, 0.01, rng).get("gold", 0) for _ in range(400000))
    assert b > a * 0.98  # 出やすさ +1% は、わずかに増える（ばらつきの範囲）


def test_bone_and_meteor_flow_into_the_settlement(eco):
    eco.credit(U, 5_000, "テスト")
    eco.labor_hire(U, "craftsman", T0, DAY)
    plain = eco.labor_state(U, T0 + 10 * H, DAY)["pot_total"]
    give(eco, "relic_bone")
    buffed = eco.labor_state(U, T0 + 10 * H, DAY)["pot_total"]
    assert buffed >= plain and buffed - plain <= 4  # 作業員の稼ぎが +1%（10時間で 235 → 237）


def test_vase_speeds_up_fatigue_recovery(eco):
    assert lab.fatigue_now(50.0, 0.0, 100.0, 0.01) < lab.fatigue_now(50.0, 0.0, 100.0)  # 回復が +1% 速い
    assert lab.fatigue_now(50.0, 0.0, 100.0, 0.0) == lab.fatigue_now(50.0, 0.0, 100.0)
    for k in range(60):
        eco.labor_click(U, T0 + k * 1.5, DAY)
    give(eco, "relic_vase")
    plain_state = eco.labor_state(U, T0 + 300, DAY)["fatigue"]
    eco._conn.execute("DELETE FROM inventory WHERE item_id='relic_vase'")
    assert plain_state <= eco.labor_state(U, T0 + 300, DAY)["fatigue"]  # 壺を持っているほうが、疲労が低い（以上）


def test_crown_raises_the_daily_bonus_and_tear_raises_horse_prizes(eco):
    import datetime

    d = datetime.date(2026, 11, 10)
    first = eco.claim_daily(U, d, 0)
    other = Economy(eco._db_path, strict=True).for_guild(G)
    give(other, "relic_crown", uid=2)
    got = other.claim_daily(2, d, 0)
    assert got.amount == round(first.amount * 1.01)  # 初日の1,000 → 1,010


def test_tear_adds_one_percent_to_the_owners_prize(eco):
    give(eco, shop.HORSE_VOUCHER)
    ok, hid = eco.horse_create(U, "ホース", "栗毛", "🟥")
    assert ok
    rid = eco.race_create_special(T0, 10, 10)
    assert eco.race_register(U, hid, rid, T0 + 1)[0]
    field = eco.race_close_entries(rid, random.Random(2))
    eco.race_set_odds(rid, horse_mod.make_all_odds(field["runners"]))
    give(eco, "relic_tear")
    before = eco.balance(U)
    result = eco.race_run(rid, random.Random(3))
    prize = result["prizes"].get(U, 0)
    if prize:  # 入賞したとき: 賞金は、表の額の1%増し（さらに、出走料ぶんの上限とピンハネがかかる）
        want = {
            round(round(round(p * horse_mod.prize_pool_scale(1, p)) * 1.01) * (1 - teiai.HORSE_PRIZE_SKIM_RATE))
            for p in horse_mod.PRIZES
        }
        assert prize in want
        assert eco.balance(U) >= before + prize - 1


def test_listed_relics_do_not_give_their_buff_and_the_buff_returns_after_cancelling(eco):
    give(eco, "relic_bone")
    assert eco._relic_buffs(U) == {"wage": 0.01}
    ok, lid = eco.p2p_list(U, "item", "relic_bone", 1, "fixed", shop.RELIC_VALUE["relic_bone"], None, T0)
    assert ok and eco._relic_buffs(U) == {}  # 出品中は、持ち物にない
    assert eco.p2p_cancel(U, lid, T0 + 1)[0] and eco._relic_buffs(U) == {"wage": 0.01}
    assert p2p.tradable("relic_bone")


def test_all_six_buffs_can_be_held_together_and_stack_by_count(eco):
    for item_id in relics.BUFFS:
        give(eco, item_id, 3)
    assert eco._relic_buffs(U) == {name: 0.03 for name, _ in relics.BUFFS.values()}


def test_star_ids_buffs_and_values():
    assert relics.split_id("relic_fossil_s2") == ("relic_fossil", 2) and relics.split_id("relic_fossil") == (
        "relic_fossil",
        0,
    )
    assert relics.buffs_of({"relic_fossil_s1": 1, "relic_fossil": 3, "relic_bone_s2": 1}) == {
        "click": 0.13,
        "wage": 1.0,
    }
    assert shop.BY_ID["relic_fossil_s2"].name == "三葉虫の化石★★" and shop.RELIC_VALUE["relic_fossil_s2"] == 150_000
    assert "+10%" in relics.buff_text("relic_fossil_s1")


def test_merge_ten_into_one_star_and_stack_further(eco):
    give(eco, "relic_fossil", 25)
    ok, new = eco.relic_merge(U, "relic_fossil")
    assert ok and new == "relic_fossil_s1"
    assert eco.relic_stock(U) == {"relic_fossil": 15, "relic_fossil_s1": 1}
    assert eco.relic_merge(U, "relic_fossil")[0] and eco.relic_merge(U, "relic_fossil")[0] is False  # 5個では足りない
    assert abs(eco._relic_buffs(U)["click"] - 0.25) < 1e-9  # 残り5個 ×1% + ★1 が2個 ×10%
    give(eco, "relic_fossil_s1", 8)  # ★1 が10個になる
    assert eco.relic_merge(U, "relic_fossil_s1") == (True, "relic_fossil_s2")


def test_max_star_and_non_relics_cannot_merge(eco):
    give(eco, "relic_tear_s3", 10)
    ok, reason = eco.relic_merge(U, "relic_tear_s3")
    assert not ok and "最上位" in reason
    assert not eco.relic_merge(U, "carrot")[0]
