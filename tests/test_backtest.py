import json
from datetime import timedelta

import pytest

from favorite_hunter.analytics import bets_from_backtest
from favorite_hunter.backtest import BacktestConfig, load_observations, price_at, run_backtest
from favorite_hunter.config import Settings
from favorite_hunter.database import Database

from .factories import NOW, gamma_market
from .fakes import FakeClient


def closed_market(mid, prices, closed_at, **kw):
    raw = gamma_market(mid, closed=True, **kw)
    raw["outcomePrices"] = json.dumps(prices)
    raw["umaResolutionStatus"] = "resolved"
    raw["closedTime"] = closed_at.isoformat().replace("+00:00", "Z")
    return raw


def hourly(closed_at, price_fn, hours=130):
    """Price points every 15 minutes; price_fn takes hours before the close."""
    steps = [q / 4 for q in range(hours * 4, -1, -1)]
    return [(closed_at - timedelta(hours=h), price_fn(h)) for h in steps]


def test_price_at_uses_last_point_not_future_and_respects_age():
    pts = [(NOW, 0.5), (NOW + timedelta(hours=1), 0.6)]
    assert price_at(pts, NOW + timedelta(minutes=59), timedelta(hours=1)) == 0.5
    assert price_at(pts, NOW + timedelta(hours=1), timedelta(hours=1)) == 0.6
    assert price_at(pts, NOW - timedelta(minutes=1), timedelta(hours=1)) is None
    assert price_at(pts, NOW + timedelta(hours=5), timedelta(hours=1)) is None


def test_backtest_samples_each_bucket_and_settles_with_real_payout():
    closed_at = NOW - timedelta(days=1)
    # YES resolves; YES price drifts 0.70 -> 0.99 as the close approaches
    yes_wins = closed_market("1", ["1", "0"], closed_at, fees_enabled=False)
    # NO resolves; YES priced 0.10 (NO 0.90) throughout but loses
    no_wins = closed_market("2", ["0", "1"], closed_at, fees_enabled=False)
    split = closed_market("3", ["0.5", "0.5"], closed_at, fees_enabled=False)
    pending = closed_market("4", ["0.99", "0.01"], closed_at)
    histories = {
        "11": hourly(closed_at, lambda h: 0.97 if h < 1 else 0.93 if h <= 3 else 0.88 if h <= 12 else 0.85 if h <= 48 else 0.70),
        "21": hourly(closed_at, lambda h: 0.10),
        "31": hourly(closed_at, lambda h: 0.15),
    }
    client = FakeClient([], {}, histories=histories)
    client.iter_markets = lambda **kw: iter([yes_wins, no_wins, split, pending])
    db = Database(":memory:")
    run = run_backtest(client, db, Settings(), BacktestConfig(slippage=0.005), now=NOW)
    assert run.markets_scanned == 4 and run.skipped == {"not finally resolved": 1}
    obs = load_observations(db, run.run_id)
    by_market = {}
    for o in obs:
        by_market.setdefault(o["market_id"], []).append(o)
    m1 = {o["time_bucket"]: o for o in by_market["1"]}
    # 3d+ sample (0.70 + 0.005) is outside the band; the others are inside
    assert set(m1) == {"<1h", "1-6h", "6-24h", "1-3d"}
    assert m1["<1h"]["price"] == pytest.approx(0.975) and m1["<1h"]["won"] == 1
    assert m1["1-6h"]["price"] == pytest.approx(0.935) and m1["1-6h"]["entry_bucket"] == "0.93-0.95"
    m2 = by_market["2"]
    assert len(m2) == 5 and all(o["outcome_index"] == 1 and o["won"] == 1 for o in m2)  # NO side at 0.905
    m3 = by_market["3"]
    assert all(o["payout_per_share"] == 0.5 and o["won"] == 0 for o in m3)
    bets = bets_from_backtest(obs)
    winner = next(b for b in bets if b.label == yes_wins["question"] and b.time_bucket == "<1h")
    assert winner.pnl == pytest.approx(100 / 0.975 - 100)


def test_backtest_reports_data_unavailable():
    client = FakeClient([], {}, fail=True)
    run = run_backtest(client, Database(":memory:"), Settings(), now=NOW)
    assert run.errors and run.observations == 0
