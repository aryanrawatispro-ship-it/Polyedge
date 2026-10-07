import math

import pytest

from favorite_hunter.edge_calculator import compute_edge, wins_to_recover_one_loss
from favorite_hunter.models import FeeSchedule


def test_spec_example_090_vs_096_no_fee():
    m = compute_edge(0.90, 0.0, 0.96)
    assert m.payout_if_correct == 1.0
    assert m.profit_if_correct == pytest.approx(0.10)
    assert m.loss_if_wrong == pytest.approx(0.90)
    assert m.break_even_probability == pytest.approx(0.90)
    assert m.ev_per_share == pytest.approx(0.06)  # 0.96 * 1 - 0.90
    assert m.expected_roi == pytest.approx(0.06 / 0.90)  # ~6.67%
    assert m.probability_edge == pytest.approx(0.06)
    assert m.risk_reward_ratio == pytest.approx(9.0)  # risk $0.90 to make $0.10
    assert m.kelly_fraction == pytest.approx(0.6)  # (q - c) / (1 - c)


def test_spec_examples_edge_size():
    interesting = compute_edge(0.89, 0.0, 0.95)
    assert interesting.probability_edge == pytest.approx(0.06)
    skip = compute_edge(0.94, 0.0, 0.95)
    assert skip.probability_edge == pytest.approx(0.01)


def test_fee_reduces_edge_and_raises_break_even():
    fee = FeeSchedule(rate=0.05, exponent=1, source="test")
    fee_ps = fee.fee_per_share(0.90)
    assert fee_ps == pytest.approx(0.05 * 0.90 * 0.10)  # 0.0045
    m = compute_edge(0.90, fee_ps, 0.96)
    assert m.effective_cost == pytest.approx(0.9045)
    assert m.break_even_probability == pytest.approx(0.9045)
    assert m.ev_per_share == pytest.approx(0.0555)
    assert m.expected_roi == pytest.approx(0.0555 / 0.9045)
    assert m.gross_edge == pytest.approx(0.06)
    assert m.probability_edge == pytest.approx(0.0555)


def test_fee_is_symmetric_and_peaks_at_half():
    fee = FeeSchedule(rate=0.07, exponent=1, source="test")
    assert fee.fee_per_share(0.30) == pytest.approx(fee.fee_per_share(0.70))
    assert fee.fee_per_share(0.50) == pytest.approx(0.07 * 0.25)
    assert fee.fee_per_share(0.98) < fee.fee_per_share(0.80)


def test_no_estimate_means_no_ev():
    m = compute_edge(0.92, 0.001, None)
    assert m.estimated_probability is None
    assert m.ev_per_share is None and m.expected_roi is None and m.probability_edge is None
    assert m.break_even_probability == pytest.approx(0.921)


def test_lower_bound_edge_uses_uncertainty():
    m = compute_edge(0.90, 0.0, 0.96, probability_uncertainty=0.03, lower_bound_z=1.0)
    assert m.lower_bound_probability == pytest.approx(0.93)
    assert m.lower_bound_edge == pytest.approx(0.03)


def test_negative_edge_has_zero_kelly():
    m = compute_edge(0.95, 0.0, 0.94)
    assert m.ev_per_share == pytest.approx(-0.01)
    assert m.kelly_fraction == 0.0


def test_wins_to_recover_one_loss():
    assert wins_to_recover_one_loss(0.90) == pytest.approx(9.0)
    assert wins_to_recover_one_loss(0.80) == pytest.approx(4.0)
    assert math.isinf(wins_to_recover_one_loss(1.0))


@pytest.mark.parametrize("bad_price", [0.0, 1.0, -0.1, 1.2])
def test_rejects_invalid_price(bad_price):
    with pytest.raises(ValueError):
        compute_edge(bad_price, 0.0, 0.9)
