"""厳密無裁定 CIR 期間構造 + 流動性凸ポテンシャルに基づくイールドカーブ純粋ドメインモデル（#131）。

銀行の超過準備 $S$ に対する指数ショックの期待不足額ポテンシャル $\\Psi(S; E)$ と、
外生ベースライン短期金利 $r_t$ の厳密 1 ファクター CIR アフィンモデルを組み合わせ、
HJB 無差別価格・限界利回りカーブ・信用取引基準金利を非恣意的かつ無裁定に導出する。
"""

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

_CIR_EXP_SPLIT = 50.0  # gamma * tau がこれ以上なら exp(-gamma * tau) 側へ展開しオーバーフローを防ぐ
_POISSON_KNUTH_LIMIT = 50.0
_NEWTON_MAX_ITER = 30
_NEWTON_TOL = 1e-12


@dataclass(frozen=True)
class CirParams:
    """外生ベースライン短期金利 $r_t$ の CIR パラメータ（1日あたり連続複利単位）。

    実測度 $\\mathbb{P}$ で $dr_t = \\kappa(\\mu - r_t)\\,dt + \\sigma\\sqrt{r_t}\\,dW_t^{\\mathbb{P}}$、
    リスク中立測度 $\\mathbb{Q}$ で $\\kappa^{\\mathbb{Q}} = \\kappa + \\lambda > 0$ に従う。
    Feller 条件 $2\\kappa\\mu \\ge \\sigma^2$ により $r_t > 0$ が保たれる。
    """

    kappa: float
    mu: float
    sigma: float
    lam: float

    def __post_init__(self) -> None:
        if not (
            math.isfinite(self.kappa)
            and math.isfinite(self.mu)
            and math.isfinite(self.sigma)
            and math.isfinite(self.lam)
        ):
            raise ValueError("CIR パラメータはすべて有限の実数である必要があります")
        if self.kappa <= 0.0 or self.mu <= 0.0 or self.sigma <= 0.0:
            raise ValueError("kappa, mu, sigma はすべて正である必要があります")
        if self.kappa + self.lam <= 0.0:
            raise ValueError("リスク中立回帰速度 kappa + lam は正である必要があります")
        if 2.0 * self.kappa * self.mu < self.sigma * self.sigma:
            raise ValueError("Feller 条件 2 * kappa * mu >= sigma**2 を満たしていません")


@dataclass(frozen=True)
class LiquidityParams:
    """銀行の物理定数から決まる流動性不足ポテンシャル $\\Psi(S; E)$ のパラメータ。"""

    reserve_ratio: float
    loan_recovery_per_day: float
    distress_cost: float
    capacity: float

    def __post_init__(self) -> None:
        if not (
            math.isfinite(self.reserve_ratio)
            and math.isfinite(self.loan_recovery_per_day)
            and math.isfinite(self.distress_cost)
            and math.isfinite(self.capacity)
        ):
            raise ValueError("流動性パラメータはすべて有限の実数である必要があります")
        if not (0.0 < self.reserve_ratio < 1.0):
            raise ValueError("reserve_ratio は (0, 1) の範囲である必要があります")
        if self.loan_recovery_per_day <= 0.0 or self.distress_cost <= 0.0 or self.capacity <= 0.0:
            raise ValueError("loan_recovery_per_day, distress_cost, capacity はすべて正である必要があります")


def _require_finite(*values: float, label: str) -> None:
    if not all(math.isfinite(v) for v in values):
        raise ValueError(f"{label}はすべて有限の実数である必要があります")


@dataclass(frozen=True)
class TrustParams:
    """帝愛信託の 2 層熟成・余剰ベース貸出のパラメータ（1 日あたり連続率と安全バッファ）。"""

    seasoning_rate_per_day: float
    deploy_rate_per_day: float
    deploy_buffer: float = 0.0

    def __post_init__(self) -> None:
        _require_finite(
            self.seasoning_rate_per_day, self.deploy_rate_per_day, self.deploy_buffer, label="信託パラメータ"
        )
        if self.seasoning_rate_per_day <= 0.0:
            raise ValueError("seasoning_rate_per_day は正である必要があります")
        if self.deploy_rate_per_day < 0.0 or self.deploy_buffer < 0.0:
            raise ValueError("deploy_rate_per_day と deploy_buffer は非負である必要があります")


@dataclass(frozen=True)
class TrustBalance:
    """帝愛信託の 2 層残高（未熟成枠 $D_{\\mathrm{new}}$ とコア枠 $D_{\\mathrm{core}}$）。"""

    unseasoned: float
    core: float

    def __post_init__(self) -> None:
        _require_finite(self.unseasoned, self.core, label="信託残高")
        if self.unseasoned < 0.0 or self.core < 0.0:
            raise ValueError("信託残高は非負である必要があります")


@dataclass(frozen=True)
class TrustWithdrawalQuote:
    """帝愛信託の LIFO 引出見積り結果（未熟成消費・コア消費・払戻額・留保額・事後超過準備）。"""

    from_unseasoned: float
    from_core: float
    payout: float
    exit_fee: float
    surplus_after: float


def _cir_log_bond_price(params: CirParams, r: float, tau: float) -> float:
    """ゼロクーポン債価格の対数 $\\ln P_0(r, \\tau) = \\ln A(\\tau) - B(\\tau) r$（$\\tau > 0$ 用）。"""
    kappa_q = params.kappa + params.lam
    gamma = math.hypot(kappa_q, math.sqrt(2.0) * params.sigma)
    feller_exp = 2.0 * params.kappa * params.mu / (params.sigma * params.sigma)
    gamma_tau = gamma * tau
    if gamma_tau < _CIR_EXP_SPLIT:
        exp_m1 = math.expm1(gamma_tau)
        denom = (gamma + kappa_q) * exp_m1 + 2.0 * gamma
        b_tau = 2.0 * exp_m1 / denom
        ln_a_base = 0.5 * (gamma + kappa_q) * tau - math.log1p((gamma + kappa_q) * exp_m1 / (2.0 * gamma))
    else:
        exp_neg = math.exp(-gamma_tau)
        denom_scaled = (gamma + kappa_q) + (gamma - kappa_q) * exp_neg
        b_tau = 2.0 * (1.0 - exp_neg) / denom_scaled
        ln_a_base = math.log(2.0 * gamma) + 0.5 * (kappa_q - gamma) * tau - math.log(denom_scaled)
    return feller_exp * ln_a_base - b_tau * r


def cir_bond_price(params: CirParams, r: float, tau: float) -> float:
    """CIR ベースラインのゼロクーポン債価格 $P_0(r, \\tau) = A(\\tau) e^{-B(\\tau) r}$。"""
    if tau <= 0.0:
        return 1.0
    return math.exp(_cir_log_bond_price(params, r, tau))


def cir_yield(params: CirParams, r: float, tau: float) -> float:
    """CIR ベースラインの連続複利スポット利回り $y_0(r, \\tau) = -\\ln P_0(r, \\tau) / \\tau$。"""
    if tau <= 0.0:
        return r
    return -_cir_log_bond_price(params, r, tau) / tau


def _sample_poisson(lam_p: float, rng: random.Random) -> int:
    """非心カイ二乗サンプリング用の厳密ポアソン乱数（Knuth 法 + Hörmann PTRS 法）。"""
    if lam_p <= 0.0:
        return 0
    if lam_p <= _POISSON_KNUTH_LIMIT:
        limit = math.exp(-lam_p)
        prod = 1.0
        count = 0
        while True:
            count += 1
            prod *= rng.random()
            if prod <= limit:
                return count - 1
    # 大強度領域は Hörmann (1993) の PTRS 棄却法で O(1) 厳密サンプリングする
    slam = math.sqrt(lam_p)
    b = 0.931 + 2.53 * slam
    a = -0.059 + 0.02483 * b
    inv_alpha = 1.1239 + 1.1328 / (b - 3.4)
    v_r = 0.9277 - 3.6224 / (b - 2.0)
    log_lam = math.log(lam_p)
    log_inv_alpha = math.log(inv_alpha)
    while True:
        u = rng.random() - 0.5
        v = rng.random()
        us = 0.5 - abs(u)
        if us <= 0.0:
            continue
        k = math.floor((2.0 * a / us + b) * u + lam_p + 0.43)
        if us >= 0.07 and v <= v_r:
            return k
        if k < 0 or (us < 0.013 and v > us):
            continue
        if v <= 0.0:
            return k
        if math.log(v) + log_inv_alpha - math.log(a / (us * us) + b) <= -lam_p + k * log_lam - math.lgamma(k + 1):
            return k


def cir_exact_step(
    params: CirParams,
    r: float,
    dt: float,
    rng: random.Random,
    *,
    risk_neutral: bool = False,
) -> float:
    """非心カイ二乗分布（ポアソン-ガンマ混合）による CIR 短期金利の厳密遷移サンプリング。"""
    if dt <= 0.0:
        return r
    if risk_neutral:
        k_tilde = params.kappa + params.lam
        mu_tilde = params.kappa * params.mu / k_tilde
    else:
        k_tilde = params.kappa
        mu_tilde = params.mu
    sigma2 = params.sigma * params.sigma
    e_kappa = math.exp(-k_tilde * dt)
    one_minus_e = -math.expm1(-k_tilde * dt)
    c_step = sigma2 * one_minus_e / (4.0 * k_tilde)
    if c_step <= 0.0:
        return max(r, 0.0)
    d = 4.0 * k_tilde * mu_tilde / sigma2
    xi = max(r, 0.0) * e_kappa / c_step
    n = _sample_poisson(0.5 * xi, rng)
    return 2.0 * c_step * rng.gammavariate(0.5 * d + n, 1.0)


def maturity_weight(params: LiquidityParams, tau: float) -> float:
    """残存期間 $\\tau$ に対する実効準備重み $\\theta(\\tau) = \\theta_0 + (1 - \\theta_0) e^{-\\rho \\max(0, \\tau)}$。"""
    if tau <= 0.0:
        return 1.0
    return params.reserve_ratio + (1.0 - params.reserve_ratio) * math.exp(-params.loan_recovery_per_day * tau)


def required_reserve(
    locks: float,
    deposits: float,
    bonds: Sequence[tuple[float, float]],
    params: LiquidityParams,
    *,
    unseasoned_deposits: float = 0.0,
) -> float:
    """必要準備額 $R = \\text{locks} + D_{\\mathrm{new}} + \\theta_0 D_{\\mathrm{core}} + \\sum_i \\theta(\\tau_i) F_i$。"""
    if not math.isfinite(unseasoned_deposits) or unseasoned_deposits < 0.0:
        raise ValueError("unseasoned_deposits は非負の有限実数である必要があります")
    if unseasoned_deposits > 0.0 and unseasoned_deposits > deposits:
        raise ValueError("unseasoned_deposits は信託総額 deposits 以下である必要があります")
    core_deposits = deposits - unseasoned_deposits
    bond_burden = math.fsum(maturity_weight(params, tau) * face for face, tau in bonds)
    return locks + unseasoned_deposits + params.reserve_ratio * core_deposits + bond_burden


def excess_reserve(
    cash: float,
    locks: float,
    deposits: float,
    bonds: Sequence[tuple[float, float]],
    params: LiquidityParams,
    *,
    unseasoned_deposits: float = 0.0,
) -> float:
    """銀行の超過準備 $S = (C - D_{\\mathrm{new}} - \\text{locks}) - \\theta_0 D_{\\mathrm{core}} - \\sum_i \\theta(\\tau_i) F_i$。"""
    return cash - required_reserve(locks, deposits, bonds, params, unseasoned_deposits=unseasoned_deposits)


def shortfall_potential(surplus: float, params: LiquidityParams) -> float:
    """指数流動性ショック $X \\sim \\mathrm{Exp}(\\text{mean } E)$ の期待不足額ポテンシャル $\\Psi(S; E)$。"""
    if surplus >= 0.0:
        return params.distress_cost * params.capacity * math.exp(-surplus / params.capacity)
    return params.distress_cost * (params.capacity - surplus)


def shadow_price(surplus: float, params: LiquidityParams) -> float:
    """流動性の限界シャドー価格 $g(S) = -\\Psi'(S; E) \\in [0, c]$（全域で連続）。"""
    if surplus >= 0.0:
        return params.distress_cost * math.exp(-surplus / params.capacity)
    return params.distress_cost


def indifference_value(
    cir: CirParams,
    liq: LiquidityParams,
    r: float,
    cash: float,
    locks: float,
    deposits: float,
    bonds: Sequence[tuple[float, float]],
    *,
    unseasoned_deposits: float = 0.0,
) -> float:
    """銀行の HJB 無差別価値関数 $V = C - D - \\sum_i P_0(r, \\tau_i) F_i - \\Psi(S; E)$。"""
    surplus = excess_reserve(cash, locks, deposits, bonds, liq, unseasoned_deposits=unseasoned_deposits)
    bond_pv = math.fsum(cir_bond_price(cir, r, tau) * face for face, tau in bonds)
    return cash - deposits - bond_pv - shortfall_potential(surplus, liq)


def _solve_indifference_price(
    cir: CirParams,
    liq: LiquidityParams,
    r: float,
    surplus: float,
    face: float,
    tau: float,
    *,
    sign: float,
) -> float:
    """発行（sign=+1）または買戻（sign=-1）で $V$ を保存する厳密無差別価格をニュートン法で解く。"""
    if face < 0.0:
        raise ValueError("額面 face は非負である必要があります")
    if face == 0.0:
        return 0.0
    pv = cir_bond_price(cir, r, tau) * face
    theta_face = maturity_weight(liq, tau) * face
    psi_before = shortfall_potential(surplus, liq)
    p = pv
    for _ in range(_NEWTON_MAX_ITER):
        s_after = surplus + sign * (p - theta_face)
        val = sign * (p - pv) - (shortfall_potential(s_after, liq) - psi_before)
        deriv = sign * (1.0 + shadow_price(s_after, liq))
        step = val / deriv
        p -= step
        tol = max(_NEWTON_TOL, 1e-14 * max(1.0, abs(p)))
        if abs(step) <= tol and abs(val) <= tol:
            break
    return p


def indifference_issue_price(
    cir: CirParams,
    liq: LiquidityParams,
    r: float,
    surplus: float,
    face: float,
    tau: float,
    *,
    fee_rate: float = 0.0,
) -> float:
    """額面 `face`・残存期間 `tau` の社債を銀行が新規発行する際の厳密無差別価格。"""
    if fee_rate < 0.0:
        raise ValueError("fee_rate は非負である必要があります")
    p = _solve_indifference_price(cir, liq, r, surplus, face, tau, sign=1.0)
    return p * (1.0 + fee_rate)


def indifference_buyback_price(
    cir: CirParams,
    liq: LiquidityParams,
    r: float,
    surplus: float,
    face: float,
    tau: float,
    *,
    fee_rate: float = 0.0,
) -> float:
    """額面 `face`・残存期間 `tau` の社債を銀行が途中買戻する際の厳密無差別価格。"""
    if fee_rate < 0.0:
        raise ValueError("fee_rate は非負である必要があります")
    p = _solve_indifference_price(cir, liq, r, surplus, face, tau, sign=-1.0)
    return p * (1.0 - fee_rate)


def marginal_bond_price(cir: CirParams, liq: LiquidityParams, r: float, surplus: float, tau: float) -> float:
    """無限小取引の限界債券価格 $P_{\\mathrm{marg}}(r, S, \\tau) = \\frac{P_0(r, \\tau) + g(S)\\theta(\\tau)}{1 + g(S)}$。"""
    g = shadow_price(surplus, liq)
    return (cir_bond_price(cir, r, tau) + g * maturity_weight(liq, tau)) / (1.0 + g)


def effective_short_rate(liq: LiquidityParams, r: float, surplus: float) -> float:
    """限界利回りの $\\tau \\to 0^+$ 極限である実効短期金利 $r_{\\mathrm{eff}}(r, S)$。"""
    g = shadow_price(surplus, liq)
    return (r + g * liq.loan_recovery_per_day * (1.0 - liq.reserve_ratio)) / (1.0 + g)


def marginal_yield(cir: CirParams, liq: LiquidityParams, r: float, surplus: float, tau: float) -> float:
    """連続複利の限界スポット利回り $y(r, S, \\tau) = -\\ln P_{\\mathrm{marg}}(r, S, \\tau) / \\tau$。"""
    if tau <= 0.0:
        return effective_short_rate(liq, r, surplus)
    g = shadow_price(surplus, liq)
    if g == 0.0:
        return cir_yield(cir, r, tau)
    ln_p0 = _cir_log_bond_price(cir, r, tau)
    q0 = -math.expm1(ln_p0)
    q_theta = -(1.0 - liq.reserve_ratio) * math.expm1(-liq.loan_recovery_per_day * tau)
    one_minus_p = (q0 + g * q_theta) / (1.0 + g)
    if one_minus_p < 0.5:
        return -math.log1p(-one_minus_p) / tau
    return -math.log(marginal_bond_price(cir, liq, r, surplus, tau)) / tau


def margin_rates(
    liq: LiquidityParams,
    r: float,
    surplus: float,
    *,
    long_spread: float,
    borrow_fee: float,
) -> tuple[float, float]:
    """信用買い借入金利 $r_{\\mathrm{long}}$ と空売りリベート率 $r_{\\mathrm{short\\_rebate}}$ の組。"""
    r_long = effective_short_rate(liq, r, surplus) + long_spread
    r_short_rebate = r - borrow_fee
    return (r_long, r_short_rebate)


def season_trust_balance(balance: TrustBalance, dt: float, trust: TrustParams) -> TrustBalance:
    """経過日数 `dt` だけ未熟成枠 $D_{\\mathrm{new}}$ をコア枠 $D_{\\mathrm{core}}$ へ連続熟成する（総額保存）。"""
    if not math.isfinite(dt):
        raise ValueError("経過日数 dt は有限の実数である必要があります")
    if dt <= 0.0 or balance.unseasoned == 0.0:
        return balance
    matured = balance.unseasoned * (-math.expm1(-trust.seasoning_rate_per_day * dt))
    unseasoned_next = max(0.0, balance.unseasoned - matured)
    core_next = balance.core + (balance.unseasoned - unseasoned_next)
    return TrustBalance(unseasoned=unseasoned_next, core=core_next)


def core_reserve_ratio(
    cash: float,
    locks: float,
    core_deposits: float,
    unseasoned_deposits: float,
    bonds_principal: float,
) -> float | None:
    """未熟成枠を $100\\%$ 控除したコア準備率 $\\theta_{\\mathrm{core}} = \\frac{C - D_{\\mathrm{new}} - \\text{locks}}{D_{\\mathrm{core}} + B}$。"""
    _require_finite(cash, locks, core_deposits, unseasoned_deposits, bonds_principal, label="コア準備率の入力")
    if core_deposits < 0.0 or unseasoned_deposits < 0.0 or bonds_principal < 0.0:
        raise ValueError("残高・元本は非負である必要があります")
    liabilities = core_deposits + bonds_principal
    if liabilities <= 0.0:
        return None
    return (cash - unseasoned_deposits - locks) / liabilities


def marginal_trust_payout_rate(liq: LiquidityParams, surplus: float) -> float:
    """コア信託引出の無限小極限での限界払戻率 $\\frac{1 + g(S)\\theta_0}{1 + g(S)}$。"""
    _require_finite(surplus, label="超過準備 surplus ")
    g = shadow_price(surplus, liq)
    return (1.0 + g * liq.reserve_ratio) / (1.0 + g)


def _shortfall_diff(surplus: float, delta: float, params: LiquidityParams) -> float:
    """桁落ちを抑えてポテンシャル差分 $\\Psi(S + \\delta; E) - \\Psi(S; E)$ を計算する。"""
    s_after = surplus + delta
    if surplus >= 0.0 and s_after >= 0.0:
        return shortfall_potential(surplus, params) * math.expm1(-delta / params.capacity)
    if surplus <= 0.0 and s_after <= 0.0:
        return -params.distress_cost * delta
    if surplus > 0.0 > s_after:
        return (
            -params.distress_cost * params.capacity * math.expm1(-surplus / params.capacity)
            - params.distress_cost * s_after
        )
    return (
        params.distress_cost * params.capacity * math.expm1(-s_after / params.capacity) + params.distress_cost * surplus
    )


def trust_withdraw_payout(
    liq: LiquidityParams,
    surplus: float,
    balance: TrustBalance,
    amount: float,
) -> TrustWithdrawalQuote:
    """LIFO 優先（未熟成枠無料 $\\to$ コア枠 HJB 無差別スイング・プライシング）で解約額の払戻額を算出する。"""
    _require_finite(surplus, amount, label="surplus と amount ")
    if amount < 0.0:
        raise ValueError("解約額 amount は非負である必要があります")
    total = balance.unseasoned + balance.core
    if amount > total:
        raise ValueError("解約額 amount は信託残高以下である必要があります")

    from_unseasoned = min(amount, balance.unseasoned)
    from_core = amount - from_unseasoned
    if from_core == 0.0:
        return TrustWithdrawalQuote(
            from_unseasoned=from_unseasoned,
            from_core=0.0,
            payout=from_unseasoned,
            exit_fee=0.0,
            surplus_after=surplus,
        )

    theta_core = liq.reserve_ratio * from_core
    q_core = from_core
    for _ in range(_NEWTON_MAX_ITER):
        delta = theta_core - q_core
        s_after = surplus + delta
        val = (from_core - q_core) - _shortfall_diff(surplus, delta, liq)
        deriv = -(1.0 + shadow_price(s_after, liq))
        step = val / deriv
        q_core -= step
        tol = max(_NEWTON_TOL, 1e-14 * max(1.0, abs(q_core)))
        if abs(step) <= tol and abs(val) <= tol:
            break
    q_core = min(from_core, max(theta_core, q_core))
    exit_fee = max(0.0, from_core - q_core)
    payout = from_unseasoned + q_core
    surplus_after = surplus - q_core + theta_core
    return TrustWithdrawalQuote(
        from_unseasoned=from_unseasoned,
        from_core=from_core,
        payout=payout,
        exit_fee=exit_fee,
        surplus_after=surplus_after,
    )


def surplus_lending_step(
    cash: float,
    loaned: float,
    req_reserve: float,
    dt: float,
    liq: LiquidityParams,
    trust: TrustParams,
) -> tuple[float, float]:
    """超過準備バッファを超える余剰のみを運用配分する区分線形 ODE の厳密遷移（`cash + loaned` 保存）。"""
    _require_finite(cash, loaned, req_reserve, dt, label="surplus_lending_step の入力")
    if loaned < 0.0 or dt < 0.0:
        raise ValueError("loaned と dt は非負である必要があります")
    if dt == 0.0:
        return (cash, loaned)

    rho = liq.loan_recovery_per_day
    alpha = trust.deploy_rate_per_day
    s_buf = trust.deploy_buffer
    s0 = cash - req_reserve

    if s0 < s_buf:
        max_rec = loaned * (-math.expm1(-rho * dt))
        gap = s_buf - s0
        if loaned <= 0.0 or max_rec <= gap:
            loaned_next = max(0.0, loaned - max_rec)
            cash_next = cash + (loaned - loaned_next)
            return (cash_next, loaned_next)
        t_cross = -math.log1p(-gap / loaned) / rho
        cash_mid = cash + gap
        loaned_mid = loaned - gap
        rem = max(0.0, dt - t_cross)
        u0 = 0.0
    else:
        cash_mid = cash
        loaned_mid = loaned
        rem = dt
        u0 = s0 - s_buf

    if rem <= 0.0:
        return (cash_mid, loaned_mid)
    pool = u0 + loaned_mid
    if pool <= 0.0:
        return (cash_mid, 0.0)
    omega = rho + alpha
    u_star = (rho / omega) * pool
    decay = math.exp(-omega * rem)
    u_next = u0 * decay + u_star * (-math.expm1(-omega * rem))
    loaned_next = min(pool, max(0.0, pool - u_next))
    cash_next = cash_mid + (loaned_mid - loaned_next)
    return (cash_next, loaned_next)


def bank_net_worth(
    cash: float,
    loaned: float,
    locks: float,
    deposits: float,
    bonds_liability: float,
) -> float:
    """銀行の純資産（自己資本）$NW = (C + L - \\text{locks}) - (D + B)$。"""
    _require_finite(cash, loaned, locks, deposits, bonds_liability, label="純資産計算の入力")
    return (cash + loaned - locks) - (deposits + bonds_liability)


def solvency_capped_dividend_pool(
    available: float,
    core_liabilities: float,
    net_worth: float,
    *,
    target_ratio: float,
    dividend_share: float,
    dividend_cap: float,
    unseasoned_deposits: float = 0.0,
) -> float:
    """純資産 $NW$ とコア流動性余剰の両方でキャップした共済の日次配当原資。"""
    _require_finite(
        available,
        core_liabilities,
        net_worth,
        target_ratio,
        dividend_share,
        dividend_cap,
        unseasoned_deposits,
        label="配当計算の入力",
    )
    if not (0.0 <= target_ratio <= 1.0) or not (0.0 <= dividend_share <= 1.0):
        raise ValueError("target_ratio と dividend_share は [0, 1] の範囲である必要があります")
    if core_liabilities < 0.0 or dividend_cap < 0.0 or unseasoned_deposits < 0.0:
        raise ValueError("core_liabilities, dividend_cap, unseasoned_deposits は非負である必要があります")
    core_available = available - unseasoned_deposits
    liq_surplus = core_available - target_ratio * core_liabilities
    distributable = min(liq_surplus, net_worth)
    if distributable <= 0.0:
        return 0.0
    return min(dividend_cap, distributable * dividend_share)
