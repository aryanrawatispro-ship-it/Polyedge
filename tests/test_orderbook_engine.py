import pytest

from favorite_hunter.models import BookLevel, FeeSchedule
from favorite_hunter.orderbook_engine import depth_within, max_executable, simulate_buy

NO_FEE = FeeSchedule(rate=0.0, exponent=1, source="test")
FEE5 = FeeSchedule(rate=0.05, exponent=1, source="test")
ASKS = [BookLevel(0.88, 100), BookLevel(0.89, 200), BookLevel(0.90, 500)]


def test_budget_walks_levels_without_assuming_unlimited_liquidity():
    fill = simulate_buy(ASKS, NO_FEE, budget_usdc=250)
    # 100 @ 0.88 = $88; remaining $162 / 0.89 = 182.022 -> floor 182.02
    assert [(l.price, l.shares) for l in fill.levels] == [(0.88, 100), (0.89, 182.02)]
    assert fill.shares == pytest.approx(282.02)
    assert fill.notional == pytest.approx(88 + 182.02 * 0.89)
    assert fill.vwap == pytest.approx((88 + 182.02 * 0.89) / 282.02)
    assert fill.worst_price == 0.89
    assert fill.stop_reason == "filled"
    assert fill.total_cost <= 250 + 1e-9


def test_budget_includes_fees():
    fill = simulate_buy(ASKS, FEE5, budget_usdc=100)
    fee_88 = 0.05 * 0.88 * 0.12  # 0.00528 per share
    fee_89 = 0.05 * 0.89 * 0.11  # 0.004895 per share
    # Level 1: all 100 shares cost 100 * (0.88 + 0.00528) = 88.528
    remaining = 100 - 100 * (0.88 + fee_88)
    # Level 2: 11.472 / 0.894895 = 12.819 -> floor to 12.81 shares
    level2 = int(remaining / (0.89 + fee_89) * 100) / 100
    assert level2 == pytest.approx(12.81)
    assert [(l.price, l.shares) for l in fill.levels] == [(0.88, 100), (0.89, level2)]
    assert fill.fees == pytest.approx(100 * fee_88 + level2 * fee_89)
    assert fill.total_cost <= 100 + 1e-9
    assert fill.avg_cost_per_share == pytest.approx(fill.total_cost / fill.shares)


def test_share_target_and_limit_price():
    fill = simulate_buy(ASKS, NO_FEE, shares=250, limit_price=0.885)
    assert fill.shares == pytest.approx(100)
    assert fill.stop_reason == "limit_price"
    full = simulate_buy(ASKS, NO_FEE, shares=250)
    assert full.shares == pytest.approx(250)
    assert full.vwap == pytest.approx((100 * 0.88 + 150 * 0.89) / 250)


def test_edge_floor_stops_at_marginal_cost():
    fill = simulate_buy(ASKS, NO_FEE, budget_usdc=10_000, max_cost_per_share=0.895)
    assert fill.shares == pytest.approx(300)
    assert fill.stop_reason == "edge_floor"


def test_book_exhausted_and_min_size():
    fill = simulate_buy(ASKS, NO_FEE, budget_usdc=1_000_000)
    assert fill.stop_reason == "book_exhausted"
    assert fill.shares == pytest.approx(800)
    tiny = simulate_buy([BookLevel(0.9, 3)], NO_FEE, budget_usdc=100, min_order_size=5)
    assert tiny.shares == pytest.approx(3)
    assert not tiny.meets_min_size
    empty = simulate_buy([], NO_FEE, budget_usdc=100)
    assert empty.shares == 0 and empty.vwap is None and empty.stop_reason == "empty_book"


def test_max_executable_respects_edge_with_fees():
    asks = ASKS + [BookLevel(0.91, 1000)]
    # q = 0.95, min edge 0.04 -> every marginal share must cost <= 0.91
    shares, usd = max_executable(asks, NO_FEE, max_cost_per_share=0.91)
    assert shares == pytest.approx(1800)
    assert usd == pytest.approx(88 + 178 + 450 + 910)
    shares_fee, usd_fee = max_executable(asks, FEE5, max_cost_per_share=0.91)
    # 0.91 + 0.05 * 0.91 * 0.09 = 0.9141 > 0.91, so the 0.91 level is excluded
    assert shares_fee == pytest.approx(800)
    expected = sum(l.size * (l.price + FEE5.fee_per_share(l.price)) for l in ASKS)
    assert usd_fee == pytest.approx(expected)


def test_depth_within_window():
    assert depth_within(ASKS, 0.88, 0.01, side="ask") == pytest.approx(88 + 178)
    bids = [BookLevel(0.87, 100), BookLevel(0.86, 100), BookLevel(0.80, 1000)]
    assert depth_within(bids, 0.87, 0.02, side="bid") == pytest.approx(87 + 86)
    assert depth_within(ASKS, None, 0.02, side="ask") == 0.0


def test_requires_size():
    with pytest.raises(ValueError):
        simulate_buy(ASKS, NO_FEE)
