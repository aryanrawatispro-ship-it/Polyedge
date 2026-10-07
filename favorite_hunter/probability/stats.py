"""Small numeric toolkit (no SciPy dependency)."""

from __future__ import annotations

import math


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Numerical Recipes)."""
    max_iter, eps, fpmin = 300, 3e-14, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > fpmin else fpmin)
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > fpmin else fpmin)
        c = 1.0 + aa / c
        c = c if abs(c) > fpmin else fpmin
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > fpmin else fpmin)
        c = 1.0 + aa / c
        c = c if abs(c) > fpmin else fpmin
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    ln_front = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x)
    front = math.exp(ln_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_cdf(t: float, nu: float) -> float:
    """CDF of Student's t with ``nu`` degrees of freedom."""
    if math.isinf(nu):
        return norm_cdf(t)
    x = nu / (nu + t * t)
    tail = 0.5 * regularized_incomplete_beta(nu / 2.0, 0.5, x)
    return 1.0 - tail if t > 0 else tail


def scaled_t_cdf(x: float, scale_sd: float, nu: float) -> float:
    """P(X <= x) for a Student-t variable rescaled to standard deviation ``scale_sd``."""
    if scale_sd <= 0:
        return 1.0 if x >= 0 else 0.0
    if math.isinf(nu) or nu <= 2:
        return norm_cdf(x / scale_sd)
    scale = scale_sd * math.sqrt((nu - 2.0) / nu)
    return student_t_cdf(x / scale, nu)


def poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(k * math.log(lam) - lam - math.lgamma(k + 1))


def poisson_diff_probs(lam_a: float, lam_b: float, max_goals: int = 25) -> tuple[float, float, float]:
    """P(A - B > 0), P(A - B = 0), P(A - B < 0) for independent Poisson counts."""
    pa = [poisson_pmf(k, lam_a) for k in range(max_goals + 1)]
    pb = [poisson_pmf(k, lam_b) for k in range(max_goals + 1)]
    win = draw = loss = 0.0
    for i, p_i in enumerate(pa):
        for j, p_j in enumerate(pb):
            joint = p_i * p_j
            if i > j:
                win += joint
            elif i == j:
                draw += joint
            else:
                loss += joint
    total = win + draw + loss
    return win / total, draw / total, loss / total


def poisson_margin_probs(lead: int, lam_a: float, lam_b: float, max_goals: int = 25) -> tuple[float, float, float]:
    """Final outcome probabilities for team A leading by ``lead`` with
    Poisson(lam_a) and Poisson(lam_b) further scoring."""
    pa = [poisson_pmf(k, lam_a) for k in range(max_goals + 1)]
    pb = [poisson_pmf(k, lam_b) for k in range(max_goals + 1)]
    win = draw = loss = 0.0
    for i, p_i in enumerate(pa):
        for j, p_j in enumerate(pb):
            margin = lead + i - j
            joint = p_i * p_j
            if margin > 0:
                win += joint
            elif margin == 0:
                draw += joint
            else:
                loss += joint
    total = win + draw + loss
    return win / total, draw / total, loss / total


# ------------------------------------------------------------ bookmaker odds


def american_to_decimal(american: float) -> float:
    if american >= 100:
        return 1.0 + american / 100.0
    if american <= -100:
        return 1.0 + 100.0 / -american
    raise ValueError(f"invalid American odds {american}")


def devig_multiplicative(decimal_odds: list[float]) -> list[float]:
    implied = [1.0 / o for o in decimal_odds]
    total = sum(implied)
    return [p / total for p in implied]


def devig_power(decimal_odds: list[float], tol: float = 1e-12) -> list[float]:
    """Power method: find k with sum((1/o)^k) = 1. Removes more margin from
    long shots than favorites, matching the favourite-longshot bias."""
    implied = [1.0 / o for o in decimal_odds]
    if abs(sum(implied) - 1.0) < tol:
        return implied
    lo, hi = 0.5, 3.0
    for _ in range(200):
        k = (lo + hi) / 2.0
        total = sum(p**k for p in implied)
        if total > 1.0:
            lo = k
        else:
            hi = k
        if hi - lo < tol:
            break
    k = (lo + hi) / 2.0
    probs = [p**k for p in implied]
    total = sum(probs)
    return [p / total for p in probs]


def overround(decimal_odds: list[float]) -> float:
    return sum(1.0 / o for o in decimal_odds) - 1.0


def clamp(x: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, x))
