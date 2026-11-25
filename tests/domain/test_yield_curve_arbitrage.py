"""厳密無裁定イールドカーブ（domain/yield_curve.py）の敵対的数値検証テスト（#131）。"""

import math
import random

import pytest

from domain.yield_curve import (
    CirParams,
    LiquidityParams,
    cir_bond_price,
    cir_exact_step,
    cir_yield,
    effective_short_rate,
    excess_reserve,
    indifference_buyback_price,
    indifference_issue_price,
    indifference_value,
    margin_rates,
    marginal_bond_price,
    marginal_yield,
    maturity_weight,
    shadow_price,
    shortfall_potential,
)

# 現実的デフォルトパラメータ（平時 0.3%/日、危機時逆イールド反転、Feller 条件充足）
CIR = CirParams(kappa=2.0, mu=0.003, sigma=0.02, lam=-1.3)
LIQ = LiquidityParams(
    reserve_ratio=0.20,
    loan_recovery_per_day=-24.0 * math.log(1.0 - 0.02),
    distress_cost=0.05,
    capacity=20_000.0,
)
BASE_R = 0.003


def _simulate_q_discount_factors(
    cir: CirParams,
    r0: float,
    taus: tuple[float, ...],
    *,
    n_paths: int,
    dt: float,
    seed: int,
) -> dict[float, float]:
    """リスク中立測度 $\\mathbb{Q}$ で台形則積分 $e^{-\\int_0^\\tau r_s ds}$ の標本平均を算出する。"""
    rng = random.Random(seed)
    max_tau = max(taus)
    step_for_tau = {int(round(t / dt)): t for t in taus}
    max_steps = int(round(max_tau / dt))
    sums = dict.fromkeys(taus, 0.0)
    for _ in range(n_paths):
        r = r0
        integral = 0.0
        for step in range(1, max_steps + 1):
            r_next = cir_exact_step(cir, r, dt, rng, risk_neutral=True)
            integral += 0.5 * (r + r_next) * dt
            r = r_next
            tau = step_for_tau.get(step)
            if tau is not None:
                sums[tau] += math.exp(-integral)
    return {t: sums[t] / n_paths for t in taus}


def _apply_issue(
    cir: CirParams,
    liq: LiquidityParams,
    r: float,
    cash: float,
    locks: float,
    deposits: float,
    bonds: list[tuple[float, float]],
    face: float,
    tau: float,
    *,
    fee_rate: float = 0.0,
) -> tuple[float, float]:
    """銀行が (face, tau) を新規発行して現金と残高を更新し、(支払額, 新 surplus) を返す。"""
    s_before = excess_reserve(cash, locks, deposits, bonds, liq)
    price = indifference_issue_price(cir, liq, r, s_before, face, tau, fee_rate=fee_rate)
    bonds.append((face, tau))
    cash_after = cash + price
    return price, excess_reserve(cash_after, locks, deposits, bonds, liq)


def _apply_buyback(
    cir: CirParams,
    liq: LiquidityParams,
    r: float,
    cash: float,
    locks: float,
    deposits: float,
    bonds: list[tuple[float, float]],
    index: int,
    *,
    fee_rate: float = 0.0,
) -> tuple[float, float]:
    """銀行が bonds[index] を途中買戻して現金と残高を更新し、(受取額, 新 surplus) を返す。"""
    face, tau = bonds[index]
    s_before = excess_reserve(cash, locks, deposits, bonds, liq)
    price = indifference_buyback_price(cir, liq, r, s_before, face, tau, fee_rate=fee_rate)
    bonds.pop(index)
    cash_after = cash - price
    return price, excess_reserve(cash_after, locks, deposits, bonds, liq)


# ---------------------------------------------------------------------------
# 0. パラメータ不変条件と境界数値安定性
# ---------------------------------------------------------------------------


def test_parameter_validation_and_long_horizon_stability() -> None:
    with pytest.raises(ValueError):
        CirParams(kappa=0.0, mu=0.003, sigma=0.02, lam=0.0)
    with pytest.raises(ValueError):
        CirParams(kappa=2.0, mu=0.003, sigma=0.02, lam=-2.0)  # kappa + lam <= 0
    with pytest.raises(ValueError):
        CirParams(kappa=0.1, mu=0.001, sigma=0.05, lam=0.0)  # Feller 違反
    with pytest.raises(ValueError):
        LiquidityParams(reserve_ratio=1.0, loan_recovery_per_day=0.5, distress_cost=0.05, capacity=1000.0)
    with pytest.raises(ValueError):
        LiquidityParams(reserve_ratio=0.2, loan_recovery_per_day=0.5, distress_cost=0.05, capacity=0.0)

    # 長満期・非正満期でもオーバーフローせず有限値が返る
    assert cir_bond_price(CIR, BASE_R, 0.0) == 1.0
    assert cir_bond_price(CIR, BASE_R, -1.0) == 1.0
    assert cir_yield(CIR, BASE_R, 0.0) == BASE_R
    assert math.isfinite(cir_bond_price(CIR, BASE_R, 5000.0))
    assert math.isfinite(cir_yield(CIR, BASE_R, 5000.0))
    assert cir_exact_step(CIR, BASE_R, 0.0, random.Random(1)) == BASE_R


# ---------------------------------------------------------------------------
# 1. CIR Q-マルチンゲール厳密検証（モンテカルロ）
# ---------------------------------------------------------------------------


def test_cir_q_martingale_matches_affine_bond_price() -> None:
    taus = (0.5, 1.0, 3.0, 7.0)
    mc = _simulate_q_discount_factors(CIR, BASE_R, taus, n_paths=2000, dt=1.0 / 48.0, seed=42)
    for tau in taus:
        exact = cir_bond_price(CIR, BASE_R, tau)
        assert mc[tau] == pytest.approx(exact, abs=5e-4)


def test_cir_exact_step_moments_match_analytic_conditional_distribution() -> None:
    """Knuth 法 (lam_p<=50) と PTRS 法 (lam_p>50) の両領域で厳密条件付き平均・分散を検証する。"""
    rng = random.Random(123)
    for dt, r0, risk_neutral in ((1.0 / 48.0, 0.003, True), (1.5, 0.01, False)):
        n_samples = 4000
        samples = [cir_exact_step(CIR, r0, dt, rng, risk_neutral=risk_neutral) for _ in range(n_samples)]
        k = CIR.kappa + CIR.lam if risk_neutral else CIR.kappa
        m = CIR.kappa * CIR.mu / k if risk_neutral else CIR.mu
        e_k = math.exp(-k * dt)
        theo_mean = r0 * e_k + m * (1.0 - e_k)
        theo_var = r0 * (CIR.sigma**2) * e_k * (1.0 - e_k) / k + m * (CIR.sigma**2) * ((1.0 - e_k) ** 2) / (2.0 * k)
        sample_mean = sum(samples) / n_samples
        sample_var = sum((x - sample_mean) ** 2 for x in samples) / (n_samples - 1)
        assert sample_mean == pytest.approx(theo_mean, abs=3.5 * math.sqrt(theo_var / n_samples))
        assert sample_var == pytest.approx(theo_var, rel=0.08)


# ---------------------------------------------------------------------------
# 2. 分割不変性（Split Invariance）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surplus0", [5.0 * LIQ.capacity, 0.0, -LIQ.capacity])
@pytest.mark.parametrize("tau", [1.0, 3.0, 7.0])
def test_split_invariance_issue_and_buyback(surplus0: float, tau: float) -> None:
    face = 100_000.0
    one_shot = indifference_issue_price(CIR, LIQ, BASE_R, surplus0, face, tau)
    theta_tau = maturity_weight(LIQ, tau)

    for parts in (10, 50):
        chunk = face / parts
        s = surplus0
        total_paid = 0.0
        for _ in range(parts):
            p = indifference_issue_price(CIR, LIQ, BASE_R, s, chunk, tau)
            total_paid += p
            s += p - theta_tau * chunk
        assert total_paid == pytest.approx(one_shot, abs=1e-6)
        assert s == pytest.approx(surplus0 + one_shot - theta_tau * face, abs=1e-6)

    # 買戻しでも分割不変性が成り立つ（10・50分割・不均等分割）
    one_buyback = indifference_buyback_price(CIR, LIQ, BASE_R, surplus0, face, tau)
    for chunks in ([face / 10] * 10, [face / 50] * 50, [37_000.0, 13_000.0, 50_000.0]):
        s_b = surplus0
        total_recv = 0.0
        for chunk in chunks:
            q = indifference_buyback_price(CIR, LIQ, BASE_R, s_b, chunk, tau)
            total_recv += q
            s_b -= q - theta_tau * chunk
        assert total_recv == pytest.approx(one_buyback, abs=1e-6)


# ---------------------------------------------------------------------------
# 3. 往復売買・サンドイッチ操作の厳密ゼロ利益
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surplus0", [5.0 * LIQ.capacity, 0.0, -LIQ.capacity])
def test_sandwich_roundtrip_has_exact_zero_profit_and_conserves_value(surplus0: float) -> None:
    deposits = 100_000.0
    locks = 5_000.0
    cash = surplus0 + locks + LIQ.reserve_ratio * deposits
    bonds: list[tuple[float, float]] = []
    v0 = indifference_value(CIR, LIQ, BASE_R, cash, locks, deposits, bonds)

    # buy(F1) -> buy(F2) -> sell(F1) -> sell(F2)
    p1, _ = _apply_issue(CIR, LIQ, BASE_R, cash, locks, deposits, bonds, 25_000.0, 1.0)
    cash += p1
    assert indifference_value(CIR, LIQ, BASE_R, cash, locks, deposits, bonds) == pytest.approx(v0, abs=1e-6)

    p2, _ = _apply_issue(CIR, LIQ, BASE_R, cash, locks, deposits, bonds, 40_000.0, 7.0)
    cash += p2
    assert indifference_value(CIR, LIQ, BASE_R, cash, locks, deposits, bonds) == pytest.approx(v0, abs=1e-6)

    q1, _ = _apply_buyback(CIR, LIQ, BASE_R, cash, locks, deposits, bonds, 0)
    cash -= q1
    assert indifference_value(CIR, LIQ, BASE_R, cash, locks, deposits, bonds) == pytest.approx(v0, abs=1e-6)

    q2, _ = _apply_buyback(CIR, LIQ, BASE_R, cash, locks, deposits, bonds, 0)
    cash -= q2
    assert indifference_value(CIR, LIQ, BASE_R, cash, locks, deposits, bonds) == pytest.approx(v0, abs=1e-6)

    trader_profit = (q1 + q2) - (p1 + p2)
    assert abs(trader_profit) < 1e-6

    # fee_rate > 0 では厳密にマイナス利益になる
    cash_fee = surplus0 + locks + LIQ.reserve_ratio * deposits
    bonds_fee: list[tuple[float, float]] = []
    pf1, _ = _apply_issue(CIR, LIQ, BASE_R, cash_fee, locks, deposits, bonds_fee, 25_000.0, 1.0, fee_rate=0.001)
    cash_fee += pf1
    pf2, _ = _apply_issue(CIR, LIQ, BASE_R, cash_fee, locks, deposits, bonds_fee, 40_000.0, 7.0, fee_rate=0.001)
    cash_fee += pf2
    qf1, _ = _apply_buyback(CIR, LIQ, BASE_R, cash_fee, locks, deposits, bonds_fee, 0, fee_rate=0.001)
    cash_fee -= qf1
    qf2, _ = _apply_buyback(CIR, LIQ, BASE_R, cash_fee, locks, deposits, bonds_fee, 0, fee_rate=0.001)
    cash_fee -= qf2
    assert (qf1 + qf2) - (pf1 + pf2) < -10.0


def test_random_20_step_trade_sequence_conserves_value_and_yields_zero_profit() -> None:
    rng = random.Random(777)
    deposits = 200_000.0
    locks = 0.0
    cash = 0.5 * LIQ.capacity + LIQ.reserve_ratio * deposits
    bonds: list[tuple[float, float]] = []
    v0 = indifference_value(CIR, LIQ, BASE_R, cash, locks, deposits, bonds)
    trader_cash = 0.0

    for _ in range(20):
        if not bonds or rng.random() < 0.6:
            face = rng.uniform(1_000.0, 30_000.0)
            tau = rng.choice((0.5, 1.0, 3.0, 7.0))
            paid, _ = _apply_issue(CIR, LIQ, BASE_R, cash, locks, deposits, bonds, face, tau)
            cash += paid
            trader_cash -= paid
        else:
            idx = rng.randrange(len(bonds))
            recv, _ = _apply_buyback(CIR, LIQ, BASE_R, cash, locks, deposits, bonds, idx)
            cash -= recv
            trader_cash += recv
        assert indifference_value(CIR, LIQ, BASE_R, cash, locks, deposits, bonds) == pytest.approx(v0, abs=1e-6)

    while bonds:
        recv, _ = _apply_buyback(CIR, LIQ, BASE_R, cash, locks, deposits, bonds, 0)
        cash -= recv
        trader_cash += recv
        assert indifference_value(CIR, LIQ, BASE_R, cash, locks, deposits, bonds) == pytest.approx(v0, abs=1e-6)

    assert abs(trader_cash) < 1e-6


# ---------------------------------------------------------------------------
# 4. 信託預入・引出クロス操作攻撃
# ---------------------------------------------------------------------------


def test_trust_deposit_pump_attack_produces_zero_surplus_and_price_change() -> None:
    """攻撃 1: 信託預入は準備率相当しか cash に入らないため ΔS ≡ 0、債券価格も不変。"""
    deposits = 100_000.0
    locks = 0.0
    bonds: list[tuple[float, float]] = [(10_000.0, 3.0)]
    for s_init in (5.0 * LIQ.capacity, 0.0, -LIQ.capacity):
        cash = s_init + locks + LIQ.reserve_ratio * deposits + maturity_weight(LIQ, 3.0) * 10_000.0
        s0 = excess_reserve(cash, locks, deposits, bonds, LIQ)
        p_issue_before = indifference_issue_price(CIR, LIQ, BASE_R, s0, 20_000.0, 3.0)
        p_marg_before = marginal_bond_price(CIR, LIQ, BASE_R, s0, 3.0)

        delta_d = 500_000.0
        cash_after = cash + LIQ.reserve_ratio * delta_d
        deposits_after = deposits + delta_d
        s1 = excess_reserve(cash_after, locks, deposits_after, bonds, LIQ)

        assert s1 - s0 == pytest.approx(0.0, abs=1e-9)
        assert indifference_issue_price(CIR, LIQ, BASE_R, s1, 20_000.0, 3.0) == pytest.approx(p_issue_before, abs=1e-12)
        assert marginal_bond_price(CIR, LIQ, BASE_R, s1, 3.0) == pytest.approx(p_marg_before, abs=1e-12)


def test_withdraw_crash_redeposit_and_permutation_grid_yields_no_profit() -> None:
    """攻撃 2: 引出クラッシュ→購入→再預入→売却、および購入→引出→売却の網羅グリッドで純利益 <= 1e-9。"""
    for s0 in (5.0 * LIQ.capacity, LIQ.capacity, 0.0, -LIQ.capacity):
        for delta_d in (1_000.0, 20_000.0, 100_000.0):
            for face in (1_000.0, 50_000.0):
                for tau in (1.0, 3.0, 7.0):
                    # パターン A: 引出 → 購入 → 再預入 → 売却
                    deposits = delta_d + 50_000.0
                    cash = s0 + LIQ.reserve_ratio * deposits
                    bonds: list[tuple[float, float]] = []

                    # 1. 引出（銀行から現金 ΔD 流出、ΔS = -(1 - theta_0) ΔD）
                    cash -= delta_d
                    deposits -= delta_d
                    s_after_wd = excess_reserve(cash, 0.0, deposits, bonds, LIQ)
                    assert s_after_wd - s0 == pytest.approx(-(1.0 - LIQ.reserve_ratio) * delta_d, abs=1e-9)

                    # 2. 購入
                    paid, _ = _apply_issue(CIR, LIQ, BASE_R, cash, 0.0, deposits, bonds, face, tau)
                    cash += paid

                    # 3. 再預入（ΔC = theta_0 ΔD, ΔS = 0）
                    cash += LIQ.reserve_ratio * delta_d
                    deposits += delta_d
                    assert excess_reserve(cash, 0.0, deposits, bonds, LIQ) == pytest.approx(
                        s_after_wd + paid - maturity_weight(LIQ, tau) * face, abs=1e-9
                    )

                    # 4. 売却
                    recv, _ = _apply_buyback(CIR, LIQ, BASE_R, cash, 0.0, deposits, bonds, 0)
                    assert recv - paid <= 1e-9

                    # パターン B: 購入 → 引出 → 売却（買戻価格低下により損失 <= 0）
                    deposits_b = delta_d + 50_000.0
                    cash_b = s0 + LIQ.reserve_ratio * deposits_b
                    bonds_b: list[tuple[float, float]] = []
                    paid_b, _ = _apply_issue(CIR, LIQ, BASE_R, cash_b, 0.0, deposits_b, bonds_b, face, tau)
                    cash_b += paid_b
                    cash_b -= delta_d
                    deposits_b -= delta_d
                    recv_b, _ = _apply_buyback(CIR, LIQ, BASE_R, cash_b, 0.0, deposits_b, bonds_b, 0)
                    assert recv_b - paid_b <= 1e-9

                    # パターン C: F1購入 → 引出 → F2購入 → 再預入 → F1売却 → F2売却
                    deposits_c = delta_d + 50_000.0
                    cash_c = s0 + LIQ.reserve_ratio * deposits_c
                    bonds_c: list[tuple[float, float]] = []
                    paid_c1, _ = _apply_issue(CIR, LIQ, BASE_R, cash_c, 0.0, deposits_c, bonds_c, face, tau)
                    cash_c += paid_c1
                    cash_c -= delta_d
                    deposits_c -= delta_d
                    paid_c2, _ = _apply_issue(CIR, LIQ, BASE_R, cash_c, 0.0, deposits_c, bonds_c, face, tau)
                    cash_c += paid_c2
                    cash_c += LIQ.reserve_ratio * delta_d
                    deposits_c += delta_d
                    recv_c1, _ = _apply_buyback(CIR, LIQ, BASE_R, cash_c, 0.0, deposits_c, bonds_c, 0)
                    cash_c -= recv_c1
                    recv_c2, _ = _apply_buyback(CIR, LIQ, BASE_R, cash_c, 0.0, deposits_c, bonds_c, 0)
                    assert (recv_c1 + recv_c2) - (paid_c1 + paid_c2) <= 1e-9


# ---------------------------------------------------------------------------
# 5. 満期境界連続性（tau -> 0+）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surplus", [5.0 * LIQ.capacity, 0.0, -LIQ.capacity])
def test_maturity_boundary_continuity_and_redemption_neutrality(surplus: float) -> None:
    r_eff = effective_short_rate(LIQ, BASE_R, surplus)
    assert marginal_bond_price(CIR, LIQ, BASE_R, surplus, 0.0) == 1.0
    assert marginal_yield(CIR, LIQ, BASE_R, surplus, 0.0) == pytest.approx(r_eff, abs=1e-12)
    assert marginal_yield(CIR, LIQ, BASE_R, surplus, -0.5) == pytest.approx(r_eff, abs=1e-12)

    p_tiny = marginal_bond_price(CIR, LIQ, BASE_R, surplus, 1e-6)
    y_tiny = marginal_yield(CIR, LIQ, BASE_R, surplus, 1e-9)
    assert p_tiny == pytest.approx(1.0, abs=1e-4)
    assert y_tiny == pytest.approx(r_eff, abs=1e-8)
    assert cir_yield(CIR, BASE_R, 1e-9) == pytest.approx(BASE_R, abs=1e-8)

    # 満期償還 (tau = 0): cash -= F かつ bonds から (F, 0) を消去すると ΔS = 0, ΔV = 0
    face = 25_000.0
    deposits = 50_000.0
    bonds: list[tuple[float, float]] = [(face, 0.0)]
    cash = surplus + LIQ.reserve_ratio * deposits + maturity_weight(LIQ, 0.0) * face
    s_before = excess_reserve(cash, 0.0, deposits, bonds, LIQ)
    v_before = indifference_value(CIR, LIQ, BASE_R, cash, 0.0, deposits, bonds)

    s_after = excess_reserve(cash - face, 0.0, deposits, [], LIQ)
    v_after = indifference_value(CIR, LIQ, BASE_R, cash - face, 0.0, deposits, [])
    assert s_after - s_before == pytest.approx(0.0, abs=1e-9)
    assert v_after - v_before == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# 6. 平時順イールド <-> 危機時逆イールドの自動反転
# ---------------------------------------------------------------------------


def test_yield_curve_auto_inverts_from_normal_to_inverted_on_liquidity_stress() -> None:
    y_abundant = [marginal_yield(CIR, LIQ, BASE_R, 5.0 * LIQ.capacity, t) for t in (1.0, 3.0, 7.0)]
    assert y_abundant[0] < y_abundant[1] < y_abundant[2]

    for s_tight in (0.0, -LIQ.capacity):
        y_tight = [marginal_yield(CIR, LIQ, BASE_R, s_tight, t) for t in (1.0, 3.0, 7.0)]
        assert y_tight[0] > y_tight[1] > y_tight[2]
        assert all(yt > ya for yt, ya in zip(y_tight, y_abundant, strict=True))

    # 限界利回りは surplus に対して単調非増加
    surpluses = [5.0 * LIQ.capacity, LIQ.capacity, 0.0, -LIQ.capacity]
    for tau in (1.0, 3.0, 7.0):
        ys = [marginal_yield(CIR, LIQ, BASE_R, s, tau) for s in surpluses]
        assert ys == sorted(ys)


# ---------------------------------------------------------------------------
# 7. 動的戦略スイープ（1日債 x 3 乗換 vs 3日債、信用両建て vs 社債）
# ---------------------------------------------------------------------------


def test_rolling_1d_bond_three_times_matches_3d_bond_under_q_measure() -> None:
    rng = random.Random(99)
    n_paths = 2000
    steps_per_day = 48
    dt = 1.0 / steps_per_day
    abundant_s = 100.0 * LIQ.capacity  # g(S) ≈ 0（CIR ベースラインと一致する基準域）

    pv_3d_sum = 0.0
    pv_roll_sum = 0.0
    p0_3d = indifference_issue_price(CIR, LIQ, BASE_R, abundant_s, 1.0, 3.0)

    for _ in range(n_paths):
        r = BASE_R
        integral = 0.0
        wealth_roll = 1.0
        for _day in range(3):
            p_1d = indifference_issue_price(CIR, LIQ, r, abundant_s, 1.0, 1.0)
            wealth_roll /= p_1d
            for _ in range(steps_per_day):
                r_next = cir_exact_step(CIR, r, dt, rng, risk_neutral=True)
                integral += 0.5 * (r + r_next) * dt
                r = r_next
        disc = math.exp(-integral)
        pv_3d_sum += disc / p0_3d
        pv_roll_sum += disc * wealth_roll

    mc_3d = pv_3d_sum / n_paths
    mc_roll = pv_roll_sum / n_paths
    assert mc_3d == pytest.approx(1.0, abs=5e-4)
    assert mc_roll == pytest.approx(1.0, abs=5e-4)
    assert mc_roll == pytest.approx(mc_3d, abs=5e-4)


def test_carry_sweep_across_rate_and_liquidity_grid_has_no_risk_free_profit() -> None:
    """全 (r_t, S) グリッドで信用両建て・現物+空売り+社債のキャリーが <= 0 であることを検証する。"""
    # 物理的無裁定上限: r <= rho * (1 - theta_0) ≈ 0.3879 / 日
    r_threshold = LIQ.loan_recovery_per_day * (1.0 - LIQ.reserve_ratio)
    r_grid = (0.0005, 0.003, 0.01, 0.03, 0.05, 0.20, 0.35)
    s_grid = (-5.0 * LIQ.capacity, -LIQ.capacity, 0.0, LIQ.capacity, 5.0 * LIQ.capacity)
    param_pairs = ((0.0, 0.0), (0.001, 0.0005))

    for r in r_grid:
        assert r < r_threshold
        for s in s_grid:
            r_eff = effective_short_rate(LIQ, r, s)
            assert r_eff >= r  # r <= rho*(1-theta_0) により実効短期金利は常にベース r 以上

            for long_spread, borrow_fee in param_pairs:
                r_long, r_short_rebate = margin_rates(LIQ, r, s, long_spread=long_spread, borrow_fee=borrow_fee)
                # 1. margin_long + margin_short の純キャリー <= 0
                assert r_short_rebate - r_long <= 0.0
                # 2. spot_long + margin_short - instantaneous_bond の純キャリー <= 0（全レバレッジ L>=1 で成立）
                assert r_short_rebate - r_eff <= 0.0
                for lev in (1, 2, 3, 5):
                    w_short = 1.0
                    w_spot = float(lev) * w_short
                    excess_carry = r_short_rebate * w_short - r_eff * (w_spot + w_short)
                    assert excess_carry <= 0.0
                # 3. 信用借入で短期債を買ってもキャリー <= 0
                assert r_eff - r_long <= 0.0

    # 閾値超過（r > rho*(1-theta_0) ≈ 0.3879/日）の理論境界も明示確認する
    assert effective_short_rate(LIQ, r_threshold + 0.05, 0.0) < r_threshold + 0.05

    # r0 = 0.05 > mu_Q の逆イールド初期状態でも、Q-期待割引で spot_long + margin_short は 3日債を超えない
    rng = random.Random(2026)
    r_high = 0.05
    borrow_fee = 0.0005
    n_paths = 1000
    steps_per_day = 48
    dt = 1.0 / steps_per_day
    pv_short_rebate = 0.0
    for _ in range(n_paths):
        r = r_high
        disc_int = 0.0
        rebate_int = 0.0
        for _ in range(3 * steps_per_day):
            r_next = cir_exact_step(CIR, r, dt, rng, risk_neutral=True)
            _, reb0 = margin_rates(LIQ, r, 0.0, long_spread=0.001, borrow_fee=borrow_fee)
            _, reb1 = margin_rates(LIQ, r_next, 0.0, long_spread=0.001, borrow_fee=borrow_fee)
            disc_int += 0.5 * (r + r_next) * dt
            rebate_int += 0.5 * (reb0 + reb1) * dt
            r = r_next
        pv_short_rebate += math.exp(rebate_int - disc_int)
    assert pv_short_rebate / n_paths < 1.0


def test_convexity_and_shadow_price_continuity_at_zero() -> None:
    assert shadow_price(0.0, LIQ) == pytest.approx(LIQ.distress_cost, abs=1e-12)
    assert shadow_price(-1e-9, LIQ) == pytest.approx(LIQ.distress_cost, abs=1e-12)
    assert shadow_price(1e-9, LIQ) == pytest.approx(LIQ.distress_cost, abs=1e-9)
    assert shortfall_potential(0.0, LIQ) == pytest.approx(LIQ.distress_cost * LIQ.capacity, abs=1e-12)
    assert shortfall_potential(-1e-9, LIQ) == pytest.approx(shortfall_potential(1e-9, LIQ), abs=1e-8)
