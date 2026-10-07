import math

import pytest

from favorite_hunter.probability.stats import (
    american_to_decimal,
    devig_multiplicative,
    devig_power,
    norm_cdf,
    overround,
    poisson_diff_probs,
    poisson_margin_probs,
    poisson_pmf,
    scaled_t_cdf,
    student_t_cdf,
)


def test_norm_cdf_known_values():
    assert norm_cdf(0) == pytest.approx(0.5)
    assert norm_cdf(1.959964) == pytest.approx(0.975, abs=1e-6)
    assert norm_cdf(-1.644854) == pytest.approx(0.05, abs=1e-6)


@pytest.mark.parametrize("t", [-5.0, -2.7543, -1.0, -0.3, 0.0, 0.4, 1.7, 3.2])
def test_student_t_matches_closed_forms(t):
    assert student_t_cdf(t, 1) == pytest.approx(0.5 + math.atan(t) / math.pi, abs=1e-10)
    assert student_t_cdf(t, 2) == pytest.approx(0.5 + t / (2 * math.sqrt(2 + t * t)), abs=1e-10)
    u = 1 + t * t / 4
    nu4 = 0.5 + 0.375 * (t / math.sqrt(u)) * (1 - t * t / (12 * u))
    assert student_t_cdf(t, 4) == pytest.approx(nu4, abs=1e-10)


def test_hand_checked_t4_value_used_in_crypto_example():
    # spot 100, strike 95, sd 2.616% -> x = ln(0.95) + sd^2/2; t-scale = sd * sqrt(2/4)
    sd = 0.5 * math.sqrt(1 / 365.25)
    x = math.log(0.95) + 0.5 * sd * sd
    assert scaled_t_cdf(x, sd, 4) == pytest.approx(0.02557, abs=2e-5)
    assert 1 - norm_cdf(x / sd) == pytest.approx(0.97428, abs=2e-5)


def test_scaled_t_converges_to_normal():
    assert scaled_t_cdf(0.03, 0.02, 1e6) == pytest.approx(norm_cdf(1.5), abs=1e-5)
    assert scaled_t_cdf(0.03, 0.02, math.inf) == pytest.approx(norm_cdf(1.5))


def test_poisson():
    assert sum(poisson_pmf(k, 2.7) for k in range(60)) == pytest.approx(1.0)
    win, draw, loss = poisson_diff_probs(1.5, 1.1)
    assert win + draw + loss == pytest.approx(1.0)
    assert win > loss
    # Leading 2-0 with nothing left to play is a certain win.
    assert poisson_margin_probs(2, 0.0, 0.0) == pytest.approx((1.0, 0.0, 0.0))
    # Leading by one: lose only if the opponent outscores us by 2+.
    w, d, l = poisson_margin_probs(1, 0.2, 0.3)
    assert l == pytest.approx(sum(poisson_pmf(i, 0.2) * poisson_pmf(j, 0.3) for i in range(30) for j in range(30) if 1 + i - j < 0))


def test_devig_methods():
    odds = [1.25, 4.5]  # favourite and long shot with margin
    assert overround(odds) == pytest.approx(1 / 1.25 + 1 / 4.5 - 1)
    mult = devig_multiplicative(odds)
    power = devig_power(odds)
    assert sum(mult) == pytest.approx(1.0) and sum(power) == pytest.approx(1.0)
    # Power method removes relatively more margin from the long shot.
    assert power[0] > mult[0]
    assert american_to_decimal(-150) == pytest.approx(1 + 100 / 150)
    assert american_to_decimal(130) == pytest.approx(2.3)
