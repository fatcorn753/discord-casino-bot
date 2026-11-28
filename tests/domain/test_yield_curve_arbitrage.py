"""厳密無裁定イールドカーブ（domain/yield_curve.py）の敵対的数値検証テスト（#131）。"""

import math
import random

import pytest

from domain.yield_curve import (
    CirParams,
    LiquidityParams,
    TrustBalance,
    TrustParams,
    bank_net_worth,
    cir_bond_price,
    cir_exact_step,
    cir_yield,
    core_reserve_ratio,
    effective_short_rate,
    excess_reserve,
    indifference_buyback_price,
    indifference_issue_price,
    indifference_value,
    margin_rates,
    marginal_bond_price,
    marginal_trust_payout_rate,
    marginal_yield,
    maturity_weight,
    required_reserve,
    season_trust_balance,
    shadow_price,
    shortfall_potential,
    solvency_capped_dividend_pool,
    surplus_lending_step,
    trust_withdraw_payout,
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


# ---------------------------------------------------------------------------
# 8. 帝愛信託の 2 層熟成・HJB スイングプライシング・余剰貸出・純資産配当上限
# ---------------------------------------------------------------------------

TRUST = TrustParams(
    seasoning_rate_per_day=1.0,
    deploy_rate_per_day=2.0,
    deploy_buffer=LIQ.capacity,
)


def _solve_face_for_payment(
    cir: CirParams,
    liq: LiquidityParams,
    r: float,
    surplus: float,
    budget: float,
    tau: float,
) -> float:
    """支払額 `budget` で銀行が新規発行する社債の額面 `face` を二分法で厳密に求める。"""
    if budget <= 0.0:
        return 0.0
    lo = 0.0
    hi = max(1.0, budget / cir_bond_price(cir, r, tau))
    while indifference_issue_price(cir, liq, r, surplus, hi, tau) < budget:
        hi *= 2.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if indifference_issue_price(cir, liq, r, surplus, mid, tau) < budget:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def test_trust_parameter_and_balance_validation() -> None:
    with pytest.raises(ValueError):
        TrustParams(seasoning_rate_per_day=0.0, deploy_rate_per_day=1.0)
    with pytest.raises(ValueError):
        TrustParams(seasoning_rate_per_day=1.0, deploy_rate_per_day=-0.1)
    with pytest.raises(ValueError):
        TrustParams(seasoning_rate_per_day=1.0, deploy_rate_per_day=1.0, deploy_buffer=-1.0)
    with pytest.raises(ValueError):
        TrustParams(seasoning_rate_per_day=float("nan"), deploy_rate_per_day=1.0)
    with pytest.raises(ValueError):
        TrustParams(seasoning_rate_per_day=1.0, deploy_rate_per_day=float("inf"))

    with pytest.raises(ValueError):
        TrustBalance(unseasoned=-1.0, core=0.0)
    with pytest.raises(ValueError):
        TrustBalance(unseasoned=0.0, core=-1.0)
    with pytest.raises(ValueError):
        TrustBalance(unseasoned=float("nan"), core=0.0)

    with pytest.raises(ValueError):
        excess_reserve(100_000.0, 0.0, 50_000.0, [], LIQ, unseasoned_deposits=60_000.0)
    with pytest.raises(ValueError):
        excess_reserve(100_000.0, 0.0, 50_000.0, [], LIQ, unseasoned_deposits=-1.0)

    bal = TrustBalance(unseasoned=1_000.0, core=2_000.0)
    with pytest.raises(ValueError):
        trust_withdraw_payout(LIQ, 0.0, bal, 3_001.0)
    with pytest.raises(ValueError):
        trust_withdraw_payout(LIQ, 0.0, bal, -1.0)


def test_deposit_withdraw_loop_leaves_state_invariant() -> None:
    """預入 100% 現金受入 + 未熟成優先 LIFO 引出により 100 回ループでも全状態が不変・利益 0。"""
    for s0 in (5.0 * LIQ.capacity, 0.0, -LIQ.capacity):
        core0 = 100_000.0
        unseasoned0 = 5_000.0
        deposits0 = core0 + unseasoned0
        locks = 2_000.0
        loaned0 = 80_000.0
        bonds: list[tuple[float, float]] = [(15_000.0, 3.0)]
        cash0 = s0 + required_reserve(locks, deposits0, bonds, LIQ, unseasoned_deposits=unseasoned0)

        bal = TrustBalance(unseasoned=unseasoned0, core=core0)
        cash = cash0
        loaned = loaned0
        trader_cash = 0.0

        v0 = indifference_value(CIR, LIQ, BASE_R, cash, locks, deposits0, bonds, unseasoned_deposits=bal.unseasoned)
        ratio0 = core_reserve_ratio(cash, locks, bal.core, bal.unseasoned, 15_000.0)
        nw0 = bank_net_worth(cash, loaned, locks, deposits0, 15_000.0)

        for _ in range(100):
            x = 25_000.0
            # 預入: 全額 cash と unseasoned へ入る
            cash += x
            trader_cash -= x
            bal = TrustBalance(unseasoned=bal.unseasoned + x, core=bal.core)

            # 即時引出: dt=0 のまま LIFO で unseasoned から全額戻る
            s_mid = excess_reserve(
                cash, locks, bal.unseasoned + bal.core, bonds, LIQ, unseasoned_deposits=bal.unseasoned
            )
            quote = trust_withdraw_payout(LIQ, s_mid, bal, x)
            assert quote.from_unseasoned == pytest.approx(x, abs=1e-12)
            assert quote.from_core == 0.0
            assert quote.exit_fee == 0.0
            cash -= quote.payout
            trader_cash += quote.payout
            bal = TrustBalance(unseasoned=bal.unseasoned - quote.from_unseasoned, core=bal.core - quote.from_core)

        deposits_end = bal.unseasoned + bal.core
        assert cash == pytest.approx(cash0, abs=1e-9)
        assert loaned == pytest.approx(loaned0, abs=1e-9)
        assert deposits_end == pytest.approx(deposits0, abs=1e-9)
        assert bal.unseasoned == pytest.approx(unseasoned0, abs=1e-9)
        assert bal.core == pytest.approx(core0, abs=1e-9)
        assert excess_reserve(
            cash, locks, deposits_end, bonds, LIQ, unseasoned_deposits=bal.unseasoned
        ) == pytest.approx(s0, abs=1e-9)
        assert core_reserve_ratio(cash, locks, bal.core, bal.unseasoned, 15_000.0) == pytest.approx(ratio0, abs=1e-12)
        assert bank_net_worth(cash, loaned, locks, deposits_end, 15_000.0) == pytest.approx(nw0, abs=1e-9)
        assert indifference_value(
            CIR, LIQ, BASE_R, cash, locks, deposits_end, bonds, unseasoned_deposits=bal.unseasoned
        ) == pytest.approx(v0, abs=1e-9)
        assert trader_cash == pytest.approx(0.0, abs=1e-9)


def test_trust_seasoning_semigroup_and_conservation() -> None:
    bal0 = TrustBalance(unseasoned=80_000.0, core=20_000.0)
    assert season_trust_balance(bal0, 0.0, TRUST) == bal0
    assert season_trust_balance(bal0, -1.0, TRUST) == bal0

    dt1, dt2 = 0.37, 1.13
    step1 = season_trust_balance(bal0, dt1, TRUST)
    step12 = season_trust_balance(step1, dt2, TRUST)
    direct = season_trust_balance(bal0, dt1 + dt2, TRUST)

    assert step1.unseasoned < bal0.unseasoned
    assert step1.core > bal0.core
    assert step12.unseasoned == pytest.approx(direct.unseasoned, abs=1e-11)
    assert step12.core == pytest.approx(direct.core, abs=1e-11)
    assert step12.unseasoned + step12.core == pytest.approx(bal0.unseasoned + bal0.core, abs=1e-11)

    long_horizon = season_trust_balance(bal0, 100.0, TRUST)
    assert long_horizon.unseasoned < 1e-30
    assert long_horizon.core == pytest.approx(bal0.unseasoned + bal0.core, abs=1e-11)


def test_trust_swing_pricing_split_invariance_bounds_and_value_conservation() -> None:
    x_total = 100_000.0
    for s0 in (5.0 * LIQ.capacity, LIQ.capacity, 0.0, -LIQ.capacity):
        one_shot = trust_withdraw_payout(LIQ, s0, TrustBalance(unseasoned=0.0, core=x_total), x_total)
        assert one_shot.from_core == x_total
        assert LIQ.reserve_ratio * x_total <= one_shot.payout <= x_total
        assert one_shot.exit_fee == pytest.approx(x_total - one_shot.payout, abs=1e-9)

        for chunks in ([x_total / 10] * 10, [x_total / 50] * 50, [37_000.0, 13_000.0, 50_000.0]):
            s = s0
            rem = x_total
            total_payout = 0.0
            for chunk in chunks:
                q = trust_withdraw_payout(LIQ, s, TrustBalance(unseasoned=0.0, core=rem), chunk)
                total_payout += q.payout
                s = q.surplus_after
                rem -= chunk
            assert total_payout == pytest.approx(one_shot.payout, abs=1e-6)
            assert s == pytest.approx(one_shot.surplus_after, abs=1e-6)

        # 限界払戻率との連続接続
        tiny = 1e-6
        q_tiny = trust_withdraw_payout(LIQ, s0, TrustBalance(unseasoned=0.0, core=x_total), tiny)
        assert q_tiny.payout / tiny == pytest.approx(marginal_trust_payout_rate(LIQ, s0), abs=1e-8)

    # 危機時 (S <= 0) の閉形式解: exit_fee = c * (1 - theta_0) / (1 + c) * X
    expected_crisis_rate = LIQ.distress_cost * (1.0 - LIQ.reserve_ratio) / (1.0 + LIQ.distress_cost)
    for s_crisis in (0.0, -LIQ.capacity):
        q_crisis = trust_withdraw_payout(LIQ, s_crisis, TrustBalance(unseasoned=0.0, core=x_total), x_total)
        assert q_crisis.exit_fee == pytest.approx(expected_crisis_rate * x_total, abs=1e-9)

    # 潤沢時 (S = 5E) で小口解約は実質無料 (< 0.1%)
    q_abundant = trust_withdraw_payout(LIQ, 5.0 * LIQ.capacity, TrustBalance(unseasoned=0.0, core=x_total), 5_000.0)
    assert q_abundant.exit_fee / 5_000.0 < 0.001

    # 混合 LIFO 解約でも V と surplus_after が厳密保存される
    bal_mix = TrustBalance(unseasoned=15_000.0, core=120_000.0)
    locks = 3_000.0
    bonds = [(20_000.0, 3.0)]
    deposits = bal_mix.unseasoned + bal_mix.core
    cash = LIQ.capacity + required_reserve(locks, deposits, bonds, LIQ, unseasoned_deposits=bal_mix.unseasoned)
    s_before = excess_reserve(cash, locks, deposits, bonds, LIQ, unseasoned_deposits=bal_mix.unseasoned)
    v_before = indifference_value(
        CIR, LIQ, BASE_R, cash, locks, deposits, bonds, unseasoned_deposits=bal_mix.unseasoned
    )

    q_mix = trust_withdraw_payout(LIQ, s_before, bal_mix, 45_000.0)
    assert q_mix.from_unseasoned == pytest.approx(15_000.0, abs=1e-12)
    assert q_mix.from_core == pytest.approx(30_000.0, abs=1e-12)
    cash_after = cash - q_mix.payout
    unseasoned_after = bal_mix.unseasoned - q_mix.from_unseasoned
    core_after = bal_mix.core - q_mix.from_core
    deposits_after = unseasoned_after + core_after
    s_check = excess_reserve(cash_after, locks, deposits_after, bonds, LIQ, unseasoned_deposits=unseasoned_after)
    v_after = indifference_value(
        CIR, LIQ, BASE_R, cash_after, locks, deposits_after, bonds, unseasoned_deposits=unseasoned_after
    )
    assert q_mix.surplus_after == pytest.approx(s_check, abs=1e-9)
    assert v_after == pytest.approx(v_before, abs=1e-6)


def test_hold_to_maturity_self_induced_crisis_yields_no_excess_return() -> None:
    """コア信託を一斉引出して危機割引社債を満期保有しても、無リスク割引現在価値は元の元本 X 未満。"""
    for s0 in (5.0 * LIQ.capacity, LIQ.capacity, 0.0, -LIQ.capacity):
        for x in (1_000.0, 20_000.0, 100_000.0):
            for tau in (0.5, 1.0, 3.0, 7.0):
                for r in (0.003, 0.02):
                    bal = TrustBalance(unseasoned=0.0, core=x + 50_000.0)
                    quote = trust_withdraw_payout(LIQ, s0, bal, x)
                    face = _solve_face_for_payment(CIR, LIQ, r, quote.surplus_after, quote.payout, tau)
                    assert cir_bond_price(CIR, r, tau) * face < x


def test_lyapunov_no_arbitrage_across_mixed_trust_and_bond_sequences() -> None:
    """ポンプ&ダンプ（熟成前後）およびランダム 30 手混合列で V 単調非減少・純利益 <= 0 を検証する。"""
    # 1. 明示的ポンプ&ダンプ（社債買 -> 預入 -> [熟成] -> 社債売 -> 引出）
    for season_days in (0.0, 1.0):
        for s0 in (LIQ.capacity, 0.0, -LIQ.capacity):
            bal = TrustBalance(unseasoned=0.0, core=100_000.0)
            bonds: list[tuple[float, float]] = []
            cash = s0 + required_reserve(0.0, bal.core, bonds, LIQ, unseasoned_deposits=bal.unseasoned)
            trader_cash = 0.0
            v_prev = indifference_value(
                CIR, LIQ, BASE_R, cash, 0.0, bal.unseasoned + bal.core, bonds, unseasoned_deposits=bal.unseasoned
            )

            # 社債購入
            s_cur = excess_reserve(cash, 0.0, bal.unseasoned + bal.core, bonds, LIQ, unseasoned_deposits=bal.unseasoned)
            p_buy = indifference_issue_price(CIR, LIQ, BASE_R, s_cur, 50_000.0, 3.0)
            bonds.append((50_000.0, 3.0))
            cash += p_buy
            trader_cash -= p_buy
            v_cur = indifference_value(
                CIR, LIQ, BASE_R, cash, 0.0, bal.unseasoned + bal.core, bonds, unseasoned_deposits=bal.unseasoned
            )
            assert v_cur >= v_prev - 1e-6
            v_prev = v_cur

            # 大口預入
            dep = 200_000.0
            bal = TrustBalance(unseasoned=bal.unseasoned + dep, core=bal.core)
            cash += dep
            trader_cash -= dep
            v_cur = indifference_value(
                CIR, LIQ, BASE_R, cash, 0.0, bal.unseasoned + bal.core, bonds, unseasoned_deposits=bal.unseasoned
            )
            assert v_cur >= v_prev - 1e-6
            v_prev = v_cur

            # 熟成
            if season_days > 0.0:
                bal = season_trust_balance(bal, season_days, TRUST)
                v_cur = indifference_value(
                    CIR, LIQ, BASE_R, cash, 0.0, bal.unseasoned + bal.core, bonds, unseasoned_deposits=bal.unseasoned
                )
                assert v_cur >= v_prev - 1e-6
                v_prev = v_cur

            # 社債売却
            face, tau = bonds[0]
            s_before_sell = excess_reserve(
                cash, 0.0, bal.unseasoned + bal.core, bonds, LIQ, unseasoned_deposits=bal.unseasoned
            )
            p_sell = indifference_buyback_price(CIR, LIQ, BASE_R, s_before_sell, face, tau)
            bonds.pop(0)
            cash -= p_sell
            trader_cash += p_sell
            v_cur = indifference_value(
                CIR, LIQ, BASE_R, cash, 0.0, bal.unseasoned + bal.core, bonds, unseasoned_deposits=bal.unseasoned
            )
            assert v_cur >= v_prev - 1e-6
            v_prev = v_cur

            # 追加した預金を全額引出
            s_before_wd = excess_reserve(
                cash, 0.0, bal.unseasoned + bal.core, bonds, LIQ, unseasoned_deposits=bal.unseasoned
            )
            quote = trust_withdraw_payout(LIQ, s_before_wd, bal, dep)
            cash -= quote.payout
            trader_cash += quote.payout
            bal = TrustBalance(unseasoned=bal.unseasoned - quote.from_unseasoned, core=bal.core - quote.from_core)
            v_cur = indifference_value(
                CIR, LIQ, BASE_R, cash, 0.0, bal.unseasoned + bal.core, bonds, unseasoned_deposits=bal.unseasoned
            )
            assert v_cur >= v_prev - 1e-6
            assert trader_cash <= 1e-6

    # 2. ランダム 30 手混合操作列
    rng = random.Random(31415)
    bal = TrustBalance(unseasoned=0.0, core=150_000.0)
    trader_trust = 0.0
    bonds = []
    cash = 0.5 * LIQ.capacity + required_reserve(0.0, bal.core, bonds, LIQ)
    trader_cash = 0.0
    v_prev = indifference_value(CIR, LIQ, BASE_R, cash, 0.0, bal.core, bonds)

    for _ in range(30):
        move = rng.choice(("deposit", "season", "withdraw", "buy_bond", "sell_bond"))
        if move == "deposit":
            x = rng.uniform(1_000.0, 40_000.0)
            bal = TrustBalance(unseasoned=bal.unseasoned + x, core=bal.core)
            cash += x
            trader_trust += x
            trader_cash -= x
        elif move == "season":
            bal = season_trust_balance(bal, rng.uniform(0.05, 0.5), TRUST)
        elif move == "withdraw" and trader_trust > 1.0:
            x = rng.uniform(1.0, trader_trust)
            s_cur = excess_reserve(cash, 0.0, bal.unseasoned + bal.core, bonds, LIQ, unseasoned_deposits=bal.unseasoned)
            q = trust_withdraw_payout(LIQ, s_cur, bal, x)
            cash -= q.payout
            trader_cash += q.payout
            trader_trust -= x
            bal = TrustBalance(unseasoned=bal.unseasoned - q.from_unseasoned, core=bal.core - q.from_core)
        elif move == "buy_bond":
            face = rng.uniform(1_000.0, 30_000.0)
            tau = rng.choice((0.5, 1.0, 3.0, 7.0))
            s_cur = excess_reserve(cash, 0.0, bal.unseasoned + bal.core, bonds, LIQ, unseasoned_deposits=bal.unseasoned)
            paid = indifference_issue_price(CIR, LIQ, BASE_R, s_cur, face, tau)
            bonds.append((face, tau))
            cash += paid
            trader_cash -= paid
        elif move == "sell_bond" and bonds:
            idx = rng.randrange(len(bonds))
            face, tau = bonds[idx]
            s_cur = excess_reserve(cash, 0.0, bal.unseasoned + bal.core, bonds, LIQ, unseasoned_deposits=bal.unseasoned)
            recv = indifference_buyback_price(CIR, LIQ, BASE_R, s_cur, face, tau)
            bonds.pop(idx)
            cash -= recv
            trader_cash += recv

        v_cur = indifference_value(
            CIR, LIQ, BASE_R, cash, 0.0, bal.unseasoned + bal.core, bonds, unseasoned_deposits=bal.unseasoned
        )
        assert v_cur >= v_prev - 1e-6
        v_prev = v_cur

    while bonds:
        face, tau = bonds[0]
        s_cur = excess_reserve(cash, 0.0, bal.unseasoned + bal.core, bonds, LIQ, unseasoned_deposits=bal.unseasoned)
        recv = indifference_buyback_price(CIR, LIQ, BASE_R, s_cur, face, tau)
        bonds.pop(0)
        cash -= recv
        trader_cash += recv
    if trader_trust > 0.0:
        s_cur = excess_reserve(cash, 0.0, bal.unseasoned + bal.core, bonds, LIQ, unseasoned_deposits=bal.unseasoned)
        q = trust_withdraw_payout(LIQ, s_cur, bal, trader_trust)
        cash -= q.payout
        trader_cash += q.payout
    assert trader_cash <= 1e-6


def test_surplus_lending_step_buffer_floor_semigroup_and_conservation() -> None:
    req = 50_000.0
    s_buf = TRUST.deploy_buffer

    # 1. 領域 1 (S < S_buf): 回収のみ進み loaned は単調減少、C + L 保存
    cash1, loaned1 = req + s_buf - 10_000.0, 100_000.0
    c_next, l_next = surplus_lending_step(cash1, loaned1, req, 0.1, LIQ, TRUST)
    assert l_next < loaned1
    assert c_next + l_next == pytest.approx(cash1 + loaned1, abs=1e-9)

    # 2. 領域 1 -> 領域 2 の境界跨ぎを含む半群不変性
    cash_cross, loaned_cross = req + s_buf - 10_000.0, 120_000.0
    dt1, dt2 = 0.25, 0.75
    c_step1, l_step1 = surplus_lending_step(cash_cross, loaned_cross, req, dt1, LIQ, TRUST)
    c_step12, l_step12 = surplus_lending_step(c_step1, l_step1, req, dt2, LIQ, TRUST)
    c_direct, l_direct = surplus_lending_step(cash_cross, loaned_cross, req, dt1 + dt2, LIQ, TRUST)
    assert c_step12 == pytest.approx(c_direct, abs=1e-9)
    assert l_step12 == pytest.approx(l_direct, abs=1e-9)
    assert c_direct + l_direct == pytest.approx(cash_cross + loaned_cross, abs=1e-9)
    assert c_direct - req >= s_buf - 1e-9

    # 3. 領域 2 (S >= S_buf): 任意の dt で S >= S_buf を維持し、長時間で定常比 alpha / (rho + alpha) に収束
    cash2, loaned2 = req + s_buf + 80_000.0, 40_000.0
    pool = (cash2 - req - s_buf) + loaned2
    for dt in (0.01, 0.5, 5.0):
        c2, l2 = surplus_lending_step(cash2, loaned2, req, dt, LIQ, TRUST)
        assert c2 - req >= s_buf - 1e-9
        assert c2 + l2 == pytest.approx(cash2 + loaned2, abs=1e-9)
    c_inf, l_inf = surplus_lending_step(cash2, loaned2, req, 200.0, LIQ, TRUST)
    expected_share = TRUST.deploy_rate_per_day / (LIQ.loan_recovery_per_day + TRUST.deploy_rate_per_day)
    assert l_inf / pool == pytest.approx(expected_share, abs=1e-9)
    assert c_inf - req >= s_buf - 1e-9

    # 4. deploy_rate_per_day == 0 のときは純粋な指数回収に一致
    no_lend = TrustParams(seasoning_rate_per_day=1.0, deploy_rate_per_day=0.0, deploy_buffer=s_buf)
    c_rec, l_rec = surplus_lending_step(cash2, loaned2, req, 1.5, LIQ, no_lend)
    expected_l = loaned2 * math.exp(-LIQ.loan_recovery_per_day * 1.5)
    assert l_rec == pytest.approx(expected_l, abs=1e-9)
    assert c_rec + l_rec == pytest.approx(cash2 + loaned2, abs=1e-9)


def test_kyosai_solvency_cap_and_core_ratio_block_deposit_gaming() -> None:
    # 1. 純資産 NW <= 0 のとき、大口預入しても融資回収しても共済配当は 0
    core_dep = 100_000.0
    loaned = 80_000.0
    cash = 20_000.0
    locks = 0.0
    bonds_p = 0.0
    nw0 = bank_net_worth(cash, loaned, locks, core_dep, bonds_p)
    assert nw0 == pytest.approx(0.0, abs=1e-9)

    dep_big = 2_000_000.0
    cash_after = cash + dep_big
    nw_after = bank_net_worth(cash_after, loaned, locks, core_dep + dep_big, bonds_p)
    assert nw_after == pytest.approx(0.0, abs=1e-9)
    assert (
        solvency_capped_dividend_pool(
            cash_after - locks,
            core_dep + bonds_p,
            nw_after,
            target_ratio=0.30,
            dividend_share=0.05,
            dividend_cap=50_000.0,
            unseasoned_deposits=dep_big,
        )
        == 0.0
    )
    # 融資回収が進んでも NW <= 0 なら偽配当は出ない
    assert (
        solvency_capped_dividend_pool(
            cash + 60_000.0 - locks,
            core_dep + bonds_p,
            nw0,
            target_ratio=0.30,
            dividend_share=0.05,
            dividend_cap=50_000.0,
        )
        == 0.0
    )

    # 2. コア準備率が 0.10 (< 0.20) の危機時に未熟成枠へ 100 万預入しても θ_core は 0.10 のまま
    cash_tight = 10_000.0
    ratio_before = core_reserve_ratio(cash_tight, locks, core_dep, 0.0, bonds_p)
    assert ratio_before == pytest.approx(0.10, abs=1e-12)
    ratio_after = core_reserve_ratio(cash_tight + 1_000_000.0, locks, core_dep, 1_000_000.0, bonds_p)
    assert ratio_after == pytest.approx(0.10, abs=1e-12)

    # 3. 健全時 (NW > 0) でも未熟成預入は配当原資を水増ししない
    cash_rich = 80_000.0
    loaned_rich = 60_000.0
    nw_rich = bank_net_worth(cash_rich, loaned_rich, locks, core_dep, bonds_p)
    pool_before = solvency_capped_dividend_pool(
        cash_rich - locks,
        core_dep + bonds_p,
        nw_rich,
        target_ratio=0.30,
        dividend_share=0.05,
        dividend_cap=50_000.0,
    )
    pool_after = solvency_capped_dividend_pool(
        cash_rich + 500_000.0 - locks,
        core_dep + bonds_p,
        bank_net_worth(cash_rich + 500_000.0, loaned_rich, locks, core_dep + 500_000.0, bonds_p),
        target_ratio=0.30,
        dividend_share=0.05,
        dividend_cap=50_000.0,
        unseasoned_deposits=500_000.0,
    )
    assert pool_after == pytest.approx(pool_before, abs=1e-9)
