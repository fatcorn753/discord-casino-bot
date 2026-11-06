import random

from games import blackjack, chinchiro, pachinko, roulette, slot
from games.chinchiro import ARASHI, HIFUMI, ME, MENASHI, PINZORO, SHIGORO, classify, compare


def test_chinchiro_classify():
    assert classify((1, 1, 1)).kind == PINZORO
    assert classify((3, 3, 3)) == chinchiro.Hand(ARASHI, 3)
    assert classify((6, 5, 4)).kind == SHIGORO
    assert classify((3, 2, 1)).kind == HIFUMI
    assert classify((2, 2, 5)) == chinchiro.Hand(ME, 5)
    assert classify((6, 4, 4)) == chinchiro.Hand(ME, 6)
    assert classify((1, 3, 5)).kind == MENASHI
    assert classify((2, 4, 6)).kind == MENASHI


def test_chinchiro_strength_order():
    order = [classify(d) for d in [(1, 2, 3), (1, 3, 5), (2, 2, 1), (2, 2, 6), (4, 5, 6), (2, 2, 2), (6, 6, 6), (1, 1, 1)]]
    strengths = [h.strength for h in order]
    assert strengths == sorted(strengths) and len(set(strengths)) == len(strengths)


def test_chinchiro_compare_and_multipliers():
    pin, me5, me3 = classify((1, 1, 1)), classify((2, 2, 5)), classify((1, 1, 3))   # ピンゾロ・5の目・3の目
    assert compare(pin, me5) == chinchiro.Outcome("win", 5)
    assert compare(me5, me3) == chinchiro.Outcome("win", 1)
    assert compare(me3, me5) == chinchiro.Outcome("lose", 1)
    assert compare(me5, classify((5, 3, 3))).result == "draw"                    # (3,3,5)も5の目
    assert compare(me5, classify((2, 5, 2))).result == "draw"                       # 同じ目は引き分け
    assert compare(classify((3, 2, 1)), me3) == chinchiro.Outcome("lose", 2)        # ヒフミは2倍払い
    assert compare(me3, classify((1, 2, 3))) == chinchiro.Outcome("win", 2)         # 親のヒフミ
    hifumi, shigoro = classify((1, 2, 3)), classify((4, 5, 6))
    assert compare(shigoro, hifumi) == chinchiro.Outcome("win", 4)                  # シゴロ対ヒフミは 2×2=4倍
    assert compare(hifumi, shigoro) == chinchiro.Outcome("lose", 4)
    assert compare(classify((2, 2, 2)), hifumi) == chinchiro.Outcome("win", 6)      # 嵐 3×2
    assert compare(classify((1, 1, 1)), hifumi) == chinchiro.Outcome("win", 10)     # ピンゾロ 5×2（最大）
    assert compare(classify((1, 3, 5)), hifumi) == chinchiro.Outcome("win", 2)      # 目なしでも2倍
    assert compare(hifumi, hifumi).result == "draw"
    assert compare(classify((4, 5, 6)), classify((2, 2, 2))).result == "lose"       # 嵐 > シゴロ
    assert compare(classify((1, 3, 5)), classify((2, 4, 6))) == chinchiro.Outcome("lose", 1)   # 目なし同士は親の勝ち


def test_chinchiro_payout():
    assert chinchiro.payout(100, chinchiro.Outcome("win", 3)) == (400, 0)     # 賭け金＋3倍
    assert chinchiro.payout(100, chinchiro.Outcome("draw", 0)) == (100, 0)
    assert chinchiro.payout(100, chinchiro.Outcome("lose", 1)) == (0, 0)
    assert chinchiro.payout(100, chinchiro.Outcome("lose", 5)) == (0, 400)    # 5倍払い: 賭け金のほか4倍を追加で払う


def test_chinchiro_play_hand_rolls_at_most_three_and_stops_on_a_hand():
    rng = random.Random(7)
    for _ in range(2000):
        rolls = chinchiro.play_hand(rng)
        assert 1 <= len(rolls) <= 3
        assert all(h.kind == MENASHI for _, h in rolls[:-1])        # 最後以外はすべて目なし
        if len(rolls) < 3:
            assert rolls[-1][1].kind != MENASHI


def test_chinchiro_house_has_small_edge():
    ev = chinchiro.expected_value(150_000, seed=3)
    assert -0.06 < ev < 0.0          # プレイヤーはわずかに不利（極端に有利・不利ではない）


def test_slot_rtp_is_reasonable():
    assert 0.93 <= slot.rtp() <= 0.98
    assert slot.multiplier(("7️⃣",) * 3) == 500 and slot.multiplier(("🍒", "🍒", "🍋")) == 1.5
    assert slot.multiplier(("🍒", "🍋", "🍇")) == 0.3 and slot.multiplier(("🍋", "🍇", "🔔")) == 0


def test_pachinko_probabilities_and_rtp():
    for table in (pachinko.OMEN_TABLE, pachinko.TIER_TABLE):
        assert abs(sum(p for p, _ in table.values()) - 1.0) < 1e-9
    assert set(pachinko.PRIZE_WEIGHTS) == set(pachinko.TIER_TABLE)
    assert all(len(w) == len(pachinko.PRIZES) and abs(sum(w) - 1.0) < 1e-9 for w in pachinko.PRIZE_WEIGHTS.values())
    assert 0.999 <= pachinko.rtp() <= 1.0                       # 還元率は100%に限りなく近く、超えない（超えると連打で増えてしまう）
    assert max(m for _, m in pachinko.PRIZES) == 100             # 一撃: 最高100倍
    assert 0.20 <= pachinko.hit_rate() <= 0.23


def test_pachinko_hit_probability_adds_the_cues_and_caps_at_certain():
    assert pachinko.hit_probability("", "") == pachinko.BASE_HIT
    assert pachinko.hit_probability("blue", "") < pachinko.hit_probability("red", "") < pachinko.hit_probability("gold", "")
    assert pachinko.hit_probability("", pachinko.NORMAL) < pachinko.hit_probability("", pachinko.SUPER) < pachinko.hit_probability("", pachinko.PREMIUM)
    assert abs(pachinko.hit_probability("red", pachinko.SUPER) - (pachinko.BASE_HIT + 0.15 + 0.35)) < 1e-9      # 足し算
    assert pachinko.hit_probability("gold", pachinko.PREMIUM) == 1.0                                          # 金予告 + プレミアムは確定


def test_pachinko_observed_rates_follow_the_design():
    rng = random.Random(21)
    n = 120_000
    by_omen, by_tier, total_hits = {}, {}, 0
    for _ in range(n):
        outcome, omen, tier = pachinko.roll(rng)
        hit = outcome.multiplier > 0
        total_hits += hit
        for table, key in ((by_omen, omen), (by_tier, tier)):
            c = table.setdefault(key, [0, 0])
            c[0] += hit
            c[1] += 1
    assert abs(total_hits / n - pachinko.hit_rate()) < 0.01
    for key, designed in pachinko.hit_rate_by_omen().items():
        assert abs(by_omen[key][0] / by_omen[key][1] - designed) < 0.02, key
    for key, designed in pachinko.hit_rate_by_tier().items():
        assert abs(by_tier[key][0] / by_tier[key][1] - designed) < 0.02, key


def test_pachinko_reels_match_outcome():
    rng = random.Random(1)
    for _ in range(500):
        spin = pachinko.build_spin(rng)
        won = spin.outcome.multiplier > 0
        assert (len(set(spin.reels)) == 1) == won                  # 当たりのときだけ3つ揃う
        assert len(spin.frames) >= 8
        assert spin.frames[-1].text.startswith("┃")


def test_roulette_payouts():
    assert roulette.payout("red", 100, 1) == 200 and roulette.payout("red", 100, 2) == 0
    assert roulette.payout("even", 100, 0) == 0                    # 0 は偶数でもハズレ
    assert roulette.payout("number", 100, 17, 17) == 3600 and roulette.payout("number", 100, 0, 0) == 3600
    assert roulette.payout("dozen2", 100, 13) == 300 and roulette.payout("high", 100, 19) == 200
    for kind in roulette.BETS:
        assert abs(roulette.rtp(kind) - 36 / 37) < 0.05 + 0.03     # 約97%


def test_blackjack_values_and_results():
    c = lambda *ranks: [(r, "♠") for r in ranks]          # noqa: E731
    assert blackjack.value(c("A", "K")) == 21 and blackjack.is_blackjack(c("A", "K"))
    assert blackjack.value(c("A", "A", "9")) == 21        # Aは必要なら1として数える
    assert blackjack.value(c("K", "Q", "5")) == 25
    g = blackjack.Game(random.Random(1))
    g.player, g.dealer, g.finished = c("10", "9"), c("10", "8"), True
    assert g.result() == "win"
    g.dealer = c("10", "9")
    assert g.result() == "push"
    g.player = c("K", "Q", "5")
    assert g.result() == "bust"
    g.player, g.dealer = c("A", "K"), c("A", "9")
    assert g.result() == "blackjack"
    g.dealer = c("A", "K")
    assert g.result() == "push"


def test_blackjack_payout_and_dealer_rule():
    assert blackjack.payout("blackjack", 100) == 250 and blackjack.payout("win", 100) == 200
    assert blackjack.payout("push", 100) == 100 and blackjack.payout("lose", 100) == 0 and blackjack.payout("bust", 100) == 0
    for seed in range(200):
        g = blackjack.Game(random.Random(seed))
        if g.finished:
            continue
        g.stand()
        assert blackjack.value(g.dealer) >= 17 or blackjack.value(g.player) > 21


def test_blackjack_double_takes_one_card_and_finishes():
    for seed in range(50):
        g = blackjack.Game(random.Random(seed))
        if g.finished:
            continue
        n = len(g.player)
        g.double()
        assert g.doubled and len(g.player) == n + 1 and g.finished


def test_pachinko_staging_is_decided_first_and_shown_consistently():
    rng = random.Random(3)
    seen_tiers, seen_omens, surprise = set(), set(), 0
    for _ in range(4000):
        spin = pachinko.build_spin(rng)
        assert (spin.tier != "") == spin.reach                        # リーチのときだけ格がある
        assert spin.tier in pachinko.TIER_TABLE and spin.omen in pachinko.OMEN_TABLE
        assert spin.hit_prob == pachinko.hit_probability(spin.omen, spin.tier)
        won = spin.outcome.multiplier > 0
        if won and not spin.reach:
            surprise += 1                                             # リーチなしで当たる「サプライズ当たり」
            assert spin.frames[-2].text.endswith("…！？")
        if not won and not spin.reach:
            assert len(set(spin.reels)) == 3                          # リーチなしのハズレはバラバラ
        if not won and spin.reach:
            assert spin.reels[0] == spin.reels[1] != spin.reels[2]    # リーチのハズレは「あと一歩」
        if spin.omen:
            assert any(f.mood == spin.omen for f in spin.frames)      # 予告の色が演出に出る
        seen_tiers.add(spin.tier)
        seen_omens.add(spin.omen)
        assert all(f.mood in {"normal", "blue", "red", "gold"} for f in spin.frames)
        assert len(spin.frames) <= 18                                 # 編集の回数を抑える（約20秒まで）
    assert seen_tiers == {"", pachinko.NORMAL, pachinko.SUPER, pachinko.PREMIUM} and seen_omens == {"", "blue", "red", "gold"}
    assert surprise > 0


def test_pachinko_higher_tier_means_bigger_prizes():
    rng = random.Random(8)
    total = {t: [0, 0] for t in (pachinko.NORMAL, pachinko.PREMIUM)}
    for _ in range(80_000):
        outcome, omen, tier = pachinko.roll(rng)
        if tier in total and outcome.multiplier:
            total[tier][0] += outcome.multiplier
            total[tier][1] += 1
    assert total[pachinko.NORMAL][0] / total[pachinko.NORMAL][1] < total[pachinko.PREMIUM][0] / total[pachinko.PREMIUM][1]


def test_pachinko_stronger_cues_mean_better_odds():
    rng = random.Random(4)
    wins = {"normal": [0, 0], "super": [0, 0], "premium": [0, 0]}
    for _ in range(60000):
        spin = pachinko.build_spin(rng)
        if spin.reach:
            wins[spin.tier][0] += spin.outcome.multiplier > 0
            wins[spin.tier][1] += 1
    rate = {t: w / n for t, (w, n) in wins.items()}
    assert rate["normal"] < rate["super"] < rate["premium"]           # 格が上がるほど当たりやすい
