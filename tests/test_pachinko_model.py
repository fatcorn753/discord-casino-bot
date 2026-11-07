"""パチンコ（打ち出し型）: 設計の数字、1セットの抽選、連チャンと上限、演出のコマ、1回転ごとの確率。"""

import random

import pytest

from games import pachinko


# ----------------------------- 設計の数字 ----------------------------- #
def test_tables_are_consistent_and_rtp_never_exceeds_one():
    for table in (pachinko.OMEN_TABLE, pachinko.TIER_TABLE):
        assert abs(sum(p for p, _ in table.values()) - 1.0) < 1e-9
    assert abs(sum(p for _, _, p in pachinko.ROUNDS) - 1.0) < 1e-9
    assert 0.0 < pachinko.BASE_HIT < 0.01 and abs(pachinko.mean_hit_prob() - pachinko.HIT_TARGET) < 1e-12
    assert (
        0.965 <= pachinko.rtp() <= 0.975
    )  # 還元率は約97%（スロット・ルーレットと同じくらい）。100%を超えると連打で増えてしまう
    assert 0.07 <= pachinko.hit_rate() <= 0.11  # 当たるセットは約9%


def test_set_distribution_is_a_probability_distribution_with_the_cap():
    dist = pachinko._set_distribution()
    assert len(dist) == pachinko.CAP_MULT + 1 and abs(sum(dist) - 1.0) < 1e-9
    assert dist[1] == 0 and dist[0] > 0.9 and dist[2] > 0  # 払い戻しは0か2倍以上（1倍はない）
    assert sum(a * p for a, p in enumerate(dist)) == pytest.approx(pachinko.rtp())


def test_observed_set_results_follow_the_design():
    rng = random.Random(11)
    n = 60_000
    results = [pachinko.roll_set(rng) for _ in range(n)]
    assert abs(sum(r.starts for r in results) / n - pachinko.BALLS_PER_SET * pachinko.P_START) < 0.1  # 平均7回転
    assert abs(sum(1 for r in results if r.mult) / n - pachinko.hit_rate()) < 0.006
    assert abs(sum(r.mult for r in results) / n - pachinko.rtp()) < 0.08  # 大当たりの偏りで大きくぶれる
    assert all(r.mult == 0 or 2 <= r.mult <= pachinko.CAP_MULT for r in results)  # 払い戻しは0か、2倍以上（上限まで）


def test_hit_probability_adds_the_cues_and_caps_at_certain():
    assert pachinko.hit_probability("", "") == pachinko.BASE_HIT
    assert (
        pachinko.hit_probability("blue", "")
        < pachinko.hit_probability("red", "")
        < pachinko.hit_probability("gold", "")
    )
    assert (
        pachinko.hit_probability("", pachinko.NORMAL)
        < pachinko.hit_probability("", pachinko.SUPER)
        < pachinko.hit_probability("", pachinko.PREMIUM)
    )
    assert abs(pachinko.hit_probability("red", pachinko.SUPER) - (pachinko.BASE_HIT + 0.01 + 0.12)) < 1e-9
    assert (
        pachinko.hit_rate_by_tier()[pachinko.PREMIUM]
        > pachinko.hit_rate_by_tier()[pachinko.SUPER]
        > pachinko.hit_rate_by_tier()[pachinko.NORMAL]
        > pachinko.hit_rate_by_tier()[""]
    )
    assert (
        pachinko.hit_rate_by_omen()["gold"]
        > pachinko.hit_rate_by_omen()["red"]
        > pachinko.hit_rate_by_omen()["blue"]
        > pachinko.hit_rate_by_omen()[""]
    )


# ----------------------------- 1セット ----------------------------- #
def test_not_every_ball_reaches_the_start_hole():
    rng = random.Random(1)
    starts = [pachinko.roll_set(rng).starts for _ in range(2000)]
    assert max(starts) < pachinko.BALLS_PER_SET and min(starts) <= 2  # 全部は入らない。ほとんど入らない回もある
    assert 6.5 < sum(starts) / len(starts) < 7.5


def test_a_set_has_one_spin_per_start_and_totals_match():
    rng = random.Random(2)
    saw_chain = saw_cap = False
    for _ in range(4000):
        r = pachinko.roll_set(rng)
        assert (
            len(r.spins) == r.starts
            and r.raw == sum(s.raw for s in r.spins)
            and r.mult == min(r.raw, pachinko.CAP_MULT)
        )
        assert r.hits == sum(1 for s in r.spins if s.hit) and r.chains == sum(len(s.rounds) for s in r.spins)
        for s in r.spins:
            assert (len(s.rounds) > 0) == s.hit and all(x in {m for _, m, _ in pachinko.ROUNDS} for x in s.rounds)
        saw_chain |= r.max_chain >= 2
        saw_cap |= r.raw > pachinko.CAP_MULT
    assert saw_chain  # 連チャンが起きる


def test_cap_applies_to_the_whole_set():
    spin = pachinko.SpinInfo("", "", True, (30,) * 8, 0.1)  # 30倍×8連 = 240倍
    result = pachinko.SetResult(1, (spin,), spin.raw, min(spin.raw, pachinko.CAP_MULT))
    assert (
        result.raw == 240 and result.mult == pachinko.CAP_MULT == 200 and result.max_chain == 8 and result.chains == 8
    )


def test_chain_continues_with_the_configured_probability():
    rng = random.Random(5)
    lengths = [len(pachinko.roll_rounds(rng)) for _ in range(40_000)]
    assert min(lengths) == 1 and abs(sum(lengths) / len(lengths) - 1 / (1 - pachinko.CONTINUE)) < 0.05  # 平均2連


def test_round_amounts_follow_the_table():
    rng = random.Random(6)
    counts = {m: 0 for _, m, _ in pachinko.ROUNDS}
    for _ in range(30_000):
        counts[pachinko.roll_rounds(rng)[0]] += 1
    for _, m, p in pachinko.ROUNDS:
        assert abs(counts[m] / 30_000 - p) < 0.01


def test_stronger_cues_mean_better_odds_per_spin():
    rng = random.Random(4)
    wins = {t: [0, 0] for t in ("", pachinko.NORMAL, pachinko.SUPER, pachinko.PREMIUM)}
    for _ in range(300_000):
        s = pachinko.roll_spin(rng)
        wins[s.tier][0] += s.hit
        wins[s.tier][1] += 1
    rate = {t: w / n for t, (w, n) in wins.items()}
    assert rate[""] < rate[pachinko.NORMAL] < rate[pachinko.SUPER] < rate[pachinko.PREMIUM]


# ----------------------------- 演出 ----------------------------- #
def test_spin_frames_show_the_cues_and_end_on_the_reels():
    rng = random.Random(3)
    seen_surprise = seen_reach_miss = False
    for _ in range(3000):
        s = pachinko.roll_spin(rng)
        frames = pachinko.spin_frames(rng, s, "3回転目")
        last = frames[-1].text.splitlines()[-1]
        assert last.startswith("┃") and last.endswith("┃")  # 最後のコマは、出目
        assert all(f.mood in {"normal", "blue", "red", "gold"} for f in frames) and len(frames) <= 16
        if s.omen:
            assert any(f.mood == s.omen for f in frames)
        reels = [x.strip() for x in last.strip("┃").split("┃")]
        assert (len(set(reels)) == 1) == s.hit  # 当たりのときだけ3つ揃う
        if s.hit and not s.tier:
            seen_surprise = True
            assert any("フリーズ" in f.text for f in frames)
        if not s.hit and s.tier:
            seen_reach_miss = True
    assert seen_surprise and seen_reach_miss


def test_pick_reels_match_the_round_size():
    rng = random.Random(1)
    assert len(set(pachinko.pick_reels(2, "", rng))) == 1 and set(pachinko.pick_reels(2, "", rng)) <= set(
        pachinko.SMALL
    )
    assert pachinko.pick_reels(10, "", rng) == ("7️⃣",) * 3 and pachinko.pick_reels(30, "", rng) == ("🔥",) * 3
    miss_reach = pachinko.pick_reels(0, pachinko.NORMAL, rng)
    assert miss_reach[0] == miss_reach[1] != miss_reach[2]  # リーチのハズレは「あと一歩」
    assert len(set(pachinko.pick_reels(0, "", rng))) == 3  # リーチなしのハズレはバラバラ


def test_hit_spins_returns_only_the_hits_with_their_position():
    miss = pachinko.SpinInfo("", "", False, (), 0.01)
    reach_miss = pachinko.SpinInfo("gold", pachinko.PREMIUM, False, (), 0.4)
    hit1 = pachinko.SpinInfo("", "", True, (2,), 0.01)
    hit2 = pachinko.SpinInfo("red", pachinko.SUPER, True, (10, 2), 0.2)
    assert pachinko.hit_spins([miss, hit1, reach_miss, hit2]) == [
        (2, hit1),
        (4, hit2),
    ]  # リーチが外れた回転は、含めない
    assert pachinko.hit_spins([miss, reach_miss]) == [] and pachinko.hit_spins([]) == []


def test_higher_omen_has_more_steps_and_higher_tier_has_longer_staging():
    rng = random.Random(9)
    n = {
        o: len(
            [f for f in pachinko.spin_frames(rng, pachinko.SpinInfo(o, "", True, (2,), 0.1)) if "【予告】" in f.text]
        )
        for o in ("", "blue", "red", "gold")
    }
    assert n == {"": 0, "blue": 1, "red": 2, "gold": 3}  # 予告は、色が高いほどステップが増える
    size = [
        min(len(pachinko.spin_frames(rng, pachinko.SpinInfo("", t, True, (10,), 0.5))) for _ in range(200))
        for t in ("", pachinko.NORMAL, pachinko.SUPER, pachinko.PREMIUM)
    ]
    assert size == sorted(size) and size[-1] > size[0]  # 格が高いほど、長く見せる（最短どうしで比べる）
    last = pachinko.spin_frames(rng, pachinko.SpinInfo("gold", pachinko.PREMIUM, True, (30,), 0.5))[-1]
    assert last.mood == "normal" and "当たり" not in last.text  # 出目のコマでは、当たりを宣言しない


# ----------------------------- 当たった場所だけ演出する ----------------------------- #
from play import pachinko as play_pa  # noqa: E402

HIT = pachinko.SpinInfo("red", pachinko.SUPER, True, (10, 2), 0.2)
MISS = pachinko.SpinInfo("", "", False, (), 0.01)
REACH_MISS = pachinko.SpinInfo("gold", pachinko.PREMIUM, False, (), 0.4)
REEL = "┃"


def make_result(spins):
    raw = sum(s.raw for s in spins)
    return pachinko.SetResult(len(spins), tuple(spins), raw, min(raw, pachinko.CAP_MULT))


def reel_frames(frames):
    return [f for f in frames if REEL in f.text or "🌀" in f.text]


def test_a_set_with_no_hit_shows_no_reels_even_if_a_reach_missed():
    rng = random.Random(1)
    frames = play_pa.single_frames(rng, make_result([MISS, REACH_MISS, MISS]))
    assert reel_frames(frames) == [] and len(frames) == 4  # 打ち出し2 + 入賞 + まとめ1
    assert (
        "すべてハズレ" in frames[-1].text and "リーチが **1回**" in frames[-1].text
    )  # 外れたリーチは、文字で触れるだけ


def test_a_set_with_no_start_stops_after_the_ball_frames():
    frames = play_pa.single_frames(random.Random(1), make_result([]))
    assert len(frames) == 3 and "1回も入らなかった" in frames[-1].text and reel_frames(frames) == []


def test_only_the_hit_spin_gets_the_reel_staging_and_the_gap_is_summarized():
    rng = random.Random(2)
    frames = play_pa.single_frames(rng, make_result([MISS, MISS, HIT, REACH_MISS, MISS]))
    assert any("…2回転 経過…" in f.text for f in frames)  # ハズレの回転は、まとめて触れる
    assert not any("ハズレ… そして" in f.text for f in frames)  # つなぎの文で、当たりを先に教えない
    assert any("3回転目" in f.text and REEL in f.text for f in frames)  # 当たった回転には、リールの演出
    assert not any("プレミアムリーチ" in f.text for f in frames)  # 外れたプレミアムリーチは、演出しない
    assert any("1連目" in f.text for f in frames) and any("2連目" in f.text for f in frames)
    assert len(frames) <= play_pa.FRAME_BUDGET + 12


def test_the_hit_is_announced_only_after_the_staging():
    rng = random.Random(5)
    frames = play_pa.single_frames(rng, make_result([MISS, HIT]))
    first_hit = next(i for i, f in enumerate(frames) if "当たり" in f.text or "連** スタート" in f.text)
    last_reel = max(i for i, f in enumerate(frames) if REEL in f.text and "2回転目" in f.text)
    assert last_reel < first_hit  # 演出（出目）のあとに、宣言
    assert not any("当たり" in f.text or "🎉" in f.text for f in frames[: last_reel + 1])


def test_consecutive_hits_are_all_staged_in_full_within_the_budget():
    rng = random.Random(3)
    h1 = pachinko.SpinInfo("", pachinko.NORMAL, True, (2,), 0.1)
    h2 = pachinko.SpinInfo("blue", "", True, (10,), 0.1)
    h3 = pachinko.SpinInfo("", "", True, (2,), 0.1)
    frames = play_pa.single_frames(rng, make_result([h1, MISS, h2, h3]))
    for n in (1, 3, 4):
        assert any(f"{n}回転目" in f.text and REEL in f.text for f in frames)  # 連続する当たりも、省略せずに見せる
    assert "合計 **+" in frames[-1].text and len(frames) <= play_pa.FRAME_BUDGET + 12


def test_hits_beyond_the_frame_budget_are_counted():
    rng = random.Random(3)
    big = pachinko.SpinInfo("gold", pachinko.PREMIUM, True, (2,), 0.5)
    frames = play_pa.single_frames(rng, make_result([big] * 6))
    assert "さらに、大当たりが" in frames[-1].text and len(frames) <= play_pa.FRAME_BUDGET + 20


def test_reveal_frames_show_big_counters_milestones_and_a_finish():
    spin = pachinko.SpinInfo("", "", True, (2,) * 8, 0.1)
    frames = play_pa.reveal_frames(spin)
    texts = [f.text for f in frames]
    assert "8連" in texts[0] and "# 🔥 1連目" in texts[1]  # 連チャン数は、大きく出す
    assert any("5連 達成" in t for t in texts)  # 5連は節目
    assert any("8連チャン" in t for t in texts[-3:-1])  # 表示しきれない分は、まとめて突破表示
    assert "連チャン終了" in texts[-1] and "+16倍" in texts[-1] and "好調" in texts[-1]
    short = play_pa.reveal_frames(pachinko.SpinInfo("", "", True, (10,), 0.1))
    assert len(short) == 3 and "大当たり" in short[0].text and "好調" in short[-1].text  # 宣言 + 1連目 + 終了
    assert not any("達成" in f.text for f in short)
    huge = play_pa.reveal_frames(pachinko.SpinInfo("", "", True, (30,) * 8, 0.1))
    assert "（上限）" in huge[-1].text and "+200倍" in huge[-1].text and "伝説級" in huge[-1].text
    assert len(play_pa.reveal_frames(pachinko.SpinInfo("", "", True, (2,) * 25, 0.1))) <= 11
    assert any("20連 突破" in f.text for f in play_pa.reveal_frames(pachinko.SpinInfo("", "", True, (2,) * 25, 0.1)))


def test_multi_set_stages_hit_sets_before_their_table_line():
    rng = random.Random(4)
    hit_small = pachinko.SpinInfo("", "", True, (2,), 0.1)
    hit_big = pachinko.SpinInfo("gold", pachinko.PREMIUM, True, (30,), 0.5)
    results = [
        make_result([MISS, MISS]),
        make_result([hit_small]),
        make_result([MISS]),
        make_result([hit_big]),
        make_result([hit_small, MISS]),
    ]
    frames = play_pa.multi_frames(rng, results)
    table = [i for i, f in enumerate(frames) if "入賞" in f.text]
    assert len(table) == 5  # セットごとに、表のコマが1つ
    staged = [i for i, f in enumerate(frames) if "セットの" in f.text and "回転目" in f.text]
    assert staged and all(i not in table for i in staged)
    assert any("④" in frames[i].text for i in staged)  # 大きい当たりが先に確保される
    big_stage = max(i for i in staged if "④" in frames[i].text)
    assert big_stage < table[3]  # 表に載る前に、演出
    assert not any("①" in f.text and "回転目" in f.text for f in frames)  # ハズレのセットは、演出しない
    assert not any("🎉" in f.text for f in frames[: min(staged)])
    assert len(frames) <= play_pa.FRAME_BUDGET + 12
    none = play_pa.multi_frames(rng, [make_result([MISS]), make_result([MISS])])
    assert reel_frames(none) == [] and len(none) == 2 + 2


def test_reach_patterns_and_pseudo_chain_counts_vary_but_not_with_the_result():
    rng = random.Random(7)
    for tier in (pachinko.NORMAL, pachinko.SUPER, pachinko.PREMIUM):
        seen = {
            tuple(
                f.text.splitlines()[0]
                for f in pachinko.spin_frames(rng, pachinko.SpinInfo("", tier, True, (10,), 0.5))[2:-1]
            )
            for _ in range(300)
        }
        assert len(seen) >= 3  # 同じ格でも、流れが何通りかある
    counts = {"hit": set(), "miss": set()}
    for hit in (True, False):
        for _ in range(300):
            spin = pachinko.SpinInfo("", pachinko.SUPER, hit, (10,) if hit else (), 0.2)
            n = sum(1 for f in pachinko.spin_frames(rng, spin) if "擬似連" in f.text)
            counts["hit" if hit else "miss"].add(n)
    assert counts["hit"] == counts["miss"] == {1, 2, 3}  # 回数は、当たり外れと無関係
