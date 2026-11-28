"""パチンコ（打ち出し型）: 設計の数字、1セットの抽選、連チャン（上限なし）、演出のコマ、1回転ごとの確率。"""

import random

import pytest

from cogs.casino import pachinko as play_pa
from domain.games import pachinko


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


def test_set_distribution_is_a_probability_distribution_up_to_the_analysis_limit():
    dist = pachinko._set_distribution()
    assert len(dist) == pachinko.ANALYSIS_MAX + 1 and abs(sum(dist) - 1.0) < 1e-9
    assert dist[1] == 0 and dist[0] > 0.9 and dist[2] > 0  # 払い戻しは0か2倍以上（1倍はない）
    assert sum(a * p for a, p in enumerate(dist)) == pytest.approx(pachinko.rtp())


def test_observed_set_results_follow_the_design():
    rng = random.Random(11)
    n = 60_000
    results = [pachinko.roll_set(rng) for _ in range(n)]
    assert abs(sum(r.starts for r in results) / n - pachinko.BALLS_PER_SET * pachinko.P_START) < 0.1  # 平均7回転
    assert abs(sum(1 for r in results if r.mult) / n - pachinko.hit_rate()) < 0.006
    assert abs(sum(r.mult for r in results) / n - pachinko.rtp()) < 0.08  # 大当たりの偏りで大きくぶれる
    assert all(r.mult == 0 or r.mult >= 2 for r in results)  # 払い戻しは0か、2倍以上（上限はない）


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
    saw_chain = False
    for _ in range(4000):
        r = pachinko.roll_set(rng)
        assert len(r.spins) == r.starts and r.raw == sum(s.raw for s in r.spins) and r.mult == r.raw
        assert r.hits == sum(1 for s in r.spins if s.hit) and r.chains == sum(len(s.rounds) for s in r.spins)
        for s in r.spins:
            assert (len(s.rounds) > 0) == s.hit and all(x in {m for _, m, _ in pachinko.ROUNDS} for x in s.rounds)
        saw_chain |= r.max_chain >= 2
    assert saw_chain  # 連チャンが起きる


def test_there_is_no_cap_on_the_payout():
    spin = pachinko.SpinInfo("", "", True, (30,) * 8, 0.1)  # 30倍×8連 = 240倍
    result = pachinko.SetResult(1, (spin,), spin.raw)
    assert (
        result.raw == result.mult == 240 and result.max_chain == 8 and result.chains == 8
    )  # 200倍を超えても、そのまま払う


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
        if not s.hit and not s.tier:
            continue  # リーチのないハズレは、演出しない
        staging = play_pa.staging_for(rng, s)
        frames = play_pa.spin_frames(rng, s, "3回転目", staging)
        reels = reels_of(frames[-1])
        assert reels is not None and play_pa.SPINNING not in reels  # 最後のコマは、出目
        assert all(f.mood in {"normal", "blue", "red", "gold"} for f in frames) and len(frames) <= 20
        if staging.omen:
            assert any(f.mood == staging.omen for f in frames)
        assert (len(set(reels)) == 1) == s.hit  # 当たりのときだけ3つ揃う
        if s.hit and not staging.tier:
            seen_surprise = True
            assert any("まさか" in f.text for f in frames)
        if not s.hit:
            seen_reach_miss = True
            assert reels[0] == reels[1] != reels[2] and "ハズレ" in frames[-1].text  # 外れたリーチは「あと一歩」
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
            [
                f
                for f in play_pa.spin_frames(rng, pachinko.SpinInfo(o, "", True, (2,), 0.1), "", play_pa.Staging(o, ""))
                if "【予告】" in f.text
            ]
        )
        for o in ("", "blue", "red", "gold")
    }
    assert n == {"": 0, "blue": 1, "red": 2, "gold": 3}  # 予告は、色が高いほどステップが増える
    size = [
        min(
            len(play_pa.spin_frames(rng, pachinko.SpinInfo("", t, True, (10,), 0.5), "", play_pa.Staging("", t)))
            for _ in range(200)
        )
        for t in ("", pachinko.NORMAL, pachinko.SUPER, pachinko.PREMIUM)
    ]
    assert size == sorted(size) and size[-1] > size[0]  # 格が高いほど、長く見せる（最短どうしで比べる）
    last = play_pa.spin_frames(rng, pachinko.SpinInfo("gold", pachinko.PREMIUM, True, (30,) * 3, 0.5))[-1]
    assert last.mood == "normal" and "当たり" not in last.text  # 出目のコマでは、当たりを宣言しない


# ----------------------------- 当たった場所だけ演出する ----------------------------- #
HIT = pachinko.SpinInfo("red", pachinko.SUPER, True, (10, 2), 0.2)
MISS = pachinko.SpinInfo("", "", False, (), 0.01)
REACH_MISS = pachinko.SpinInfo("gold", pachinko.PREMIUM, False, (), 0.4)
REEL = "┃"


def make_result(spins):
    raw = sum(s.raw for s in spins)
    return pachinko.SetResult(len(spins), tuple(spins), raw)


def reel_frames(frames):
    return [f for f in frames if REEL in f.text or "🌀" in f.text]


def test_a_set_with_no_hit_shows_the_missed_reach_as_a_fake_out():
    rng = random.Random(1)
    frames = play_pa.single_frames(rng, make_result([MISS, REACH_MISS, MISS]))
    assert any("2回転目" in f.text for f in frames) and any(
        "**リーチ！**" in f.text for f in frames
    )  # ガセも同じ流れで見せる
    stop = reels_of(frames[-2])
    assert (
        stop is not None and "ハズレ" in frames[-2].text and stop[0] == stop[1] != stop[2]
    )  # 外れたリーチは「あと一歩」
    assert not any("プレミアムリーチ" in f.text or "全回転" in f.text for f in frames)  # 金とプレミアムは当たり専用
    assert "すべてハズレ" in frames[-1].text and "リーチが **1回**" in frames[-1].text
    assert not any("当たり" in f.text or "🎉" in f.text for f in frames)


def test_fake_outs_are_limited_per_set():
    rng = random.Random(2)
    miss = pachinko.SpinInfo("red", pachinko.SUPER, False, (), 0.1)
    frames = play_pa.single_frames(rng, make_result([miss] * 6))
    staged = {line for f in frames for line in f.text.splitlines() if line.endswith("回転目**")}
    assert len(staged) == play_pa.SHOWN_MISSES and "リーチが **6回**" in frames[-1].text


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
        assert any(f"{n}回転目" in f.text and REEL in f.text for f in frames)  # 連続する当たりも、省略せず見せる
    assert "合計 **+" in frames[-1].text and len(frames) <= play_pa.FRAME_BUDGET + 12


def test_hits_beyond_the_frame_budget_are_counted():
    rng = random.Random(3)
    big = pachinko.SpinInfo("gold", pachinko.PREMIUM, True, (30,), 0.5)  # 出玉が大きいほど演出が長く、上限に届く
    frames = play_pa.single_frames(rng, make_result([big] * 6))
    assert "さらに、大当たりが" in frames[-1].text and len(frames) <= play_pa.FRAME_BUDGET + 20


def test_reveal_frames_hide_the_chain_count_until_it_ends():
    spin = pachinko.SpinInfo("", "", True, (2,) * 8, 0.1)
    frames = play_pa.reveal_frames(spin, rng=random.Random(1))
    texts = [f.text for f in frames]
    assert "連" not in texts[0]  # 宣言では、続くかどうかも、何連になるかも、まだ出さない
    assert "# 🔥 1連目" in texts[1] and "継続！ 2連目" in texts[3]
    assert any("5連 達成" in t for t in texts)  # 5連は節目
    assert any("8連チャン" in t for t in texts[-2:])  # 表示しきれない分は、終わりにまとめて出す
    assert "連チャン終了" in texts[-1] and "+16倍" in texts[-1] and "好調" in texts[-1]
    first_count = min(i for i, t in enumerate(texts) if "8連" in t)
    assert first_count >= len(texts) - 2
    one, two = (
        play_pa.reveal_frames(pachinko.SpinInfo("", "", True, r, 0.1), rng=random.Random(1)) for r in ((10,), (10, 2))
    )
    assert [f.text.splitlines()[-1] for f in one[:3]] != [] and len(one) == 4  # 宣言 + 1連目 + 継続待ち + 終了
    assert one[2].mood == two[2].mood and "継続" not in one[-1].text.splitlines()[0]  # 最後の1回も、同じように待つ
    assert "大当たり" in one[0].text and "好調" in one[-1].text and not any("達成" in f.text for f in one)
    huge = play_pa.reveal_frames(pachinko.SpinInfo("", "", True, (30,) * 8, 0.1))
    assert "+240倍" in huge[-1].text and "伝説級" in huge[-1].text  # 200倍を超えても、そのまま出る
    long = play_pa.reveal_frames(pachinko.SpinInfo("", "", True, (2,) * 25, 0.1))
    assert len(long) <= 2 * play_pa.SHOWN_ROUNDS + 3 and any("20連 突破" in f.text for f in long)


def test_reveal_colors_follow_the_size_of_the_win():
    small = play_pa.reveal_frames(pachinko.SpinInfo("", "", True, (2,), 0.1), rng=random.Random(1))
    big = play_pa.reveal_frames(pachinko.SpinInfo("", "", True, (30, 10), 0.1), rng=random.Random(1))
    assert small[0].mood == "blue" and small[-1].mood == "blue"  # 2倍を金色で飾らない
    assert big[0].mood == "gold" and big[-1].mood == "gold"


def test_staging_grows_with_this_hit_and_ignores_the_chain_yet_to_come():
    rng = random.Random(11)
    rank = {"": 0, "blue": 1, "red": 2, "gold": 3, pachinko.NORMAL: 1, pachinko.SUPER: 2, pachinko.PREMIUM: 3}

    def mean(raw, key):
        spins = [
            play_pa.staging_for(rng, pachinko.SpinInfo("gold", pachinko.PREMIUM, True, (raw,), 0.5)) for _ in range(400)
        ]
        return sum(rank[getattr(s, key)] for s in spins) / len(spins)

    for key in ("omen", "tier"):
        assert mean(2, key) < mean(10, key) < mean(30, key)  # 抽選の演出が派手でも、今回の当たりの出玉に合わせる
    for _ in range(300):
        small = play_pa.staging_for(rng, pachinko.SpinInfo("gold", pachinko.PREMIUM, True, (2,), 0.5))
        assert small.omen != "gold" and small.tier != pachinko.PREMIUM  # 2倍に、金予告やプレミアムは出ない
    miss = play_pa.staging_for(rng, pachinko.SpinInfo("gold", pachinko.PREMIUM, False, (), 0.5))
    assert miss == play_pa.Staging(
        "gold", pachinko.SUPER
    )  # ガセにも金は出る（金で当たりを確定させない）。プレミアムは当たり専用
    # 同じ当たりなら、あとの連チャンが何連になっても、リーチの時点の演出は同じ
    for rounds in ((10,), (10, 30, 30, 30)):
        seen = {play_pa.staging_for(random.Random(3), pachinko.SpinInfo("", "", True, rounds, 0.5))}
        assert seen == {play_pa.staging_for(random.Random(3), pachinko.SpinInfo("", "", True, (10,), 0.5))}


def test_pseudo_chain_never_shows_the_total_in_advance():
    rng = random.Random(12)
    for _ in range(200):
        text = "\n".join(
            f.text
            for f in play_pa.spin_frames(rng, hit_spin(pachinko.PREMIUM), "", play_pa.Staging("", pachinko.PREMIUM))
        )
        labels = [line for line in text.splitlines() if "擬似連" in line]
        assert labels and all("/" not in line for line in labels)  # 「擬似連 1/3」のように、合計を出さない
        numbers = [int(line.split("擬似連 ")[1].split("*")[0]) for line in labels]
        assert numbers == list(range(2, 2 + len(numbers)))  # 擬似2 → 擬似3 … と1段ずつ上がる


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


def reels_of(frame):
    """コマに出ているリール（3つの絵柄。回っているものは 🌀）。リールのないコマは None。"""
    for line in frame.text.splitlines():
        if line.startswith("┃") and line.endswith("┃"):
            return [x.strip() for x in line.strip("┃").split("┃")]
    return None


def hit_spin(tier, omen="", rounds=(10,)):
    return pachinko.SpinInfo(omen, tier, True, rounds, 0.5)


def test_reels_follow_the_rules_of_a_real_machine_in_every_script():
    """① 3つ揃うのは最後の1コマだけ ② リーチ（左右が同じ）になったら、出目まで左右を変えない（全回転は別）③ 止まったように見えるコマはリーチに見えない。"""
    rng = random.Random(21)
    for tier in ("", pachinko.NORMAL, pachinko.SUPER, pachinko.PREMIUM):
        for omen in ("", "blue", "red", "gold"):
            for rounds in ((2,), (10,), (30, 2)):
                for _ in range(150):
                    frames = play_pa.spin_frames(
                        rng, hit_spin(tier, omen, rounds), "3回転目", play_pa.Staging(omen, tier)
                    )
                    reels = [reels_of(f) for f in frames]
                    last = reels[-1]
                    assert last is not None and len(set(last)) == 1 and play_pa.SPINNING not in last  # 最後は、3つ揃い
                    reach = None
                    full_spin_before_last = False
                    for i, r in enumerate(reels[:-1]):
                        if r is None:
                            continue
                        if (
                            "外れた" in frames[i].text
                        ):  # 復活演出: 左右が揃ったまま3つ目がずれて止まり、そのあと再回転して、最後に揃う
                            assert tier and r[0] == r[1] != r[2] and play_pa.SPINNING not in r and reach is not None
                            assert reels[i + 1] == r and "復活" in frames[i + 2].text and i + 3 == len(frames) - 1
                            assert reels[i + 2] == [play_pa.SPINNING] * 3
                            full_spin_before_last = True
                            break
                        stopped = play_pa.SPINNING not in r
                        assert not (stopped and len(set(r)) == 1), frames[i].text  # 途中で3つ揃って見えない
                        assert not (stopped and r[0] == r[1]), frames[i].text  # 止まったコマが、リーチに見えない
                        if reach is not None:
                            if r == [play_pa.SPINNING] * 3:
                                full_spin_before_last = True  # 全回転（再回転）
                                assert i == len(frames) - 2, "全回転のあとは、すぐ出目"
                            else:
                                assert r[:2] == reach[:2] and r[2] == play_pa.SPINNING, frames[
                                    i
                                ].text  # リーチ中は、左右が変わらない
                            assert "擬似連" not in frames[i].text  # リーチになってから、擬似連は始まらない
                        elif r[0] == r[1] and r[0] != play_pa.SPINNING and r[2] == play_pa.SPINNING:
                            reach = r
                    if tier:
                        assert reach is not None  # リーチなしの当たり以外は、必ずリーチを通る
                        assert (tier == pachinko.PREMIUM) == full_spin_before_last or tier != pachinko.PREMIUM


def test_the_script_is_chosen_per_spin_not_every_cue_every_time():
    rng = random.Random(8)

    def texts(tier, n=400):
        return [
            "\n".join(f.text for f in play_pa.spin_frames(rng, hit_spin(tier), "1回転目", play_pa.Staging("", tier)))
            for _ in range(n)
        ]

    normal, superr, premium = texts(pachinko.NORMAL), texts(pachinko.SUPER), texts(pachinko.PREMIUM)
    assert not any("カットイン" in t or "擬似連" in t or "全回転" in t for t in normal)  # ノーマルは、控えめ
    assert any("カットイン" in t for t in superr) and not all(
        "カットイン" in t for t in superr
    )  # カットインは、ある回とない回がある
    assert any("擬似連" in t for t in superr) and not all(
        "擬似連" in t for t in superr
    )  # 擬似連も、ある回とない回がある
    assert all("全回転" in t for t in premium) and all("擬似連" in t for t in premium)  # プレミアムは、豪華
    assert len({next(x for x in ("波の予告", "魚群", "ロングリーチ") if x in t) for t in normal}) == 3
    assert (
        len({next(x for x in ("ストーリー", "ダッシュ", "スーパーリーチ") if x in t) for t in superr}) == 3
    )  # 発展は、毎回1つを選ぶ
    assert not any(
        sum(x in t for x in ("ストーリー", "ダッシュ", "スーパーリーチ")) > 1 for t in superr
    )  # 全部が流れることはない


def test_pseudo_chain_counts_do_not_depend_on_the_result():
    rng = random.Random(7)
    counts = {True: set(), False: set()}
    for hit in (True, False):
        for _ in range(300):
            spin = pachinko.SpinInfo("", pachinko.SUPER, hit, (10,) if hit else (), 0.2)
            counts[hit].add(
                sum(
                    1
                    for f in play_pa.spin_frames(rng, spin, "", play_pa.Staging("", pachinko.SUPER))
                    if "擬似連" in f.text and "再回転" not in f.text
                )
            )
    assert counts[True] == counts[False] == {0, 1, 2}  # 回数は、当たり外れと無関係


def test_revival_scene_appears_only_in_hits_with_a_reach_and_more_often_for_higher_tiers():
    rng = random.Random(33)

    def share(tier, n=1500):
        return (
            sum(
                "外れた"
                in "\n".join(
                    f.text for f in play_pa.spin_frames(rng, hit_spin(tier), "1回転目", play_pa.Staging("", tier))
                )
                for _ in range(n)
            )
            / n
        )

    assert share("") == 0  # リーチなしのサプライズ当たりには、復活はない
    for tier, chance in pachinko.REVIVE_CHANCE.items():
        assert abs(share(tier) - chance) < 0.05
    assert (
        pachinko.REVIVE_CHANCE[pachinko.NORMAL]
        < pachinko.REVIVE_CHANCE[pachinko.SUPER]
        < pachinko.REVIVE_CHANCE[pachinko.PREMIUM]
    )
    lines = set()
    for _ in range(600):
        for f in play_pa.spin_frames(rng, hit_spin(pachinko.PREMIUM), "1回転目", play_pa.Staging("", pachinko.PREMIUM)):
            lines.update(t for t, _ in play_pa.REVIVE_TEXT if f"**{t}**" in f.text)
    assert {"それでも!!!", "と、思っていたのか"} <= lines  # 台詞は、回ごとに選ぶ


def test_a_miss_never_gets_a_revival_scene():
    rng = random.Random(5)
    for _ in range(300):
        result = pachinko.roll_set(rng)
        if result.mult:
            continue
        text = "\n".join(f.text for f in play_pa.single_frames(rng, result))
        assert "…**外れた**…" not in text and "復活" not in text


def test_small_wins_never_get_the_revival_scene():
    rng = random.Random(13)
    for tier in (pachinko.NORMAL, pachinko.SUPER, pachinko.PREMIUM):
        for _ in range(300):
            frames = play_pa.spin_frames(rng, hit_spin(tier, rounds=(2,)), "", play_pa.Staging("", tier))
            assert not any("復活" in f.text for f in frames)  # 2倍の当たりを、復活で大げさに見せない
