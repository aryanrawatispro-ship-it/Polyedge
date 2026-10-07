from datetime import timedelta

import pytest

from favorite_hunter.analytics import (
    BREAK_EVEN, INSUFFICIENT, LOSING, WINNING, Bet, breakdown, build_report, calibration,
    extreme_favorites, longest_losing_streak, max_drawdown, summarize, wilson_interval,
)

from .factories import NOW


def bet(cost, won, *, shares=100.0, est=None, category="sports", bucket="1-6h", t=0, edge=None, confidence=None):
    payout = 1.0 if won is True else 0.0 if won is False else 0.5
    return Bet(
        kind="model", category=category, entry_price=cost, cost_per_share=cost, shares=shares,
        amount=shares * cost, payout_per_share=payout, pnl=shares * payout - shares * cost,
        resolved_at=NOW + timedelta(hours=t), est_prob=est, edge=edge, confidence=confidence,
        time_bucket=bucket, strategy="LIVE_EVENT_EDGE",
    )


def test_spec_example_95c_favorites_winning_94pct_is_a_losing_strategy():
    bets = [bet(0.95, i >= 6, t=i) for i in range(100)]  # 94 wins, 6 losses
    s = summarize(bets)
    assert s.win_rate == pytest.approx(0.94)
    assert s.break_even_win_rate == pytest.approx(0.95)
    assert s.edge_vs_break_even == pytest.approx(-0.01)
    # 94 wins x $5 - 6 losses x $95 = 470 - 570 = -100 on $9,500
    assert s.total_pnl == pytest.approx(-100)
    assert s.roi == pytest.approx(-100 / 9500)
    assert s.verdict == LOSING
    assert not s.significant  # 94/100 cannot statistically separate from 95%
    assert s.largest_loss == pytest.approx(-95)
    assert s.wins_needed_per_loss == pytest.approx(19)


def test_break_even_and_winning_verdicts():
    nine_of_ten = [bet(0.90, i != 3, t=i) for i in range(10)]
    s = summarize(nine_of_ten, min_sample=5)
    assert s.total_pnl == pytest.approx(9 * 10 - 90) and s.verdict == BREAK_EVEN
    winners = [bet(0.80, i % 10 != 0, t=i) for i in range(400)]  # 90% wins at 0.80
    w = summarize(winners)
    assert w.verdict == WINNING and w.significant
    assert summarize(winners[:10]).verdict == INSUFFICIENT


def test_wilson_interval_matches_hand_calculation():
    low, high = wilson_interval(94, 100)
    assert low == pytest.approx(0.87523, abs=1e-4)
    assert high == pytest.approx(0.97221, abs=1e-4)


def test_drawdown_and_streak():
    assert max_drawdown([10, 10, -90, 10, -90, 50]) == pytest.approx(170)
    assert max_drawdown([5, 5, 5]) == 0
    seq = [bet(0.9, w, t=i) for i, w in enumerate([True, False, False, True, False, False, False, True])]
    assert longest_losing_streak(seq) == 3


def test_split_counts_as_neither_win_nor_loss():
    s = summarize([bet(0.9, None), bet(0.9, True)], min_sample=1)
    assert s.splits == 1 and s.wins == 1 and s.losses == 0
    assert s.total_pnl == pytest.approx(100 * 0.5 - 90 + 10)


def test_breakdowns_follow_fixed_order():
    bets = [bet(0.96, True), bet(0.81, True), bet(0.91, False), bet(0.975, True)]
    labels = [s.group for s in breakdown(bets, "entry_range", min_sample=1)]
    assert labels == ["0.80-0.85", "0.90-0.93", "0.95-0.97", "0.97-0.98"]
    cats = breakdown([bet(0.9, True, category="crypto"), bet(0.9, True, category="sports")], "category", min_sample=1)
    assert {s.group for s in cats} == {"crypto", "sports"}


def test_extreme_favorites_thresholds():
    bets = [bet(0.91, True), bet(0.955, True), bet(0.975, False), bet(0.85, True)]
    ninety, ninety_five, ninety_seven = extreme_favorites(bets, min_sample=1)
    assert (ninety.group, ninety.n_bets) == ("90c+", 3)
    assert (ninety_five.group, ninety_five.n_bets) == ("95c+", 2)
    assert (ninety_seven.group, ninety_seven.n_bets, ninety_seven.verdict) == ("97c+", 1, LOSING)


def test_calibration_bins():
    bets = [bet(0.90, True, est=0.96), bet(0.90, False, est=0.96), bet(0.90, True, est=0.86)]
    bins = {b.label: b for b in calibration(bets, use="model")}
    assert bins["0.95-0.97"].n == 2 and bins["0.95-0.97"].actual == pytest.approx(0.5)
    assert bins["0.85-0.90"].predicted == pytest.approx(0.86)
    price_bins = {b.label: b for b in calibration(bets, use="price")}
    assert price_bins["0.90-0.93"].n == 3


def test_report_has_all_sections_and_equity_curve():
    bets = [bet(0.9, i % 5 != 0, t=i, est=0.95, edge=0.05, confidence=75) for i in range(40)]
    report = build_report(bets, "test")
    assert set(report.breakdowns) == {"entry_range", "category", "time_remaining", "estimated_edge", "confidence", "liquidity", "volume", "strategy"}
    assert report.breakdowns["estimated_edge"][0].group == "4-6pt"
    assert report.breakdowns["confidence"][0].group == "70-80"
    assert len(report.equity_curve) == 40
    assert report.equity_curve[-1][1] == pytest.approx(report.overall.total_pnl)
    assert report.to_dict()["overall"]["n_bets"] == 40
