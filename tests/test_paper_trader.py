import json
from datetime import timedelta

import pytest

from favorite_hunter.config import Settings
from favorite_hunter.database import Database
from favorite_hunter.edge_calculator import compute_edge
from favorite_hunter.market_scanner import MarketScanner
from favorite_hunter.models import parse_book, parse_market
from favorite_hunter.paper_trader import BASELINE, MODEL, PaperTrader, entry_bucket, resolution_from_gamma
from favorite_hunter.probability.base import ProbabilityEstimate
from favorite_hunter.runner import Runner

from .factories import NOW, clob_book, gamma_market, mirrored_no_book
from .fakes import FakeClient


def candidate(market_id="1", asks=((0.90, 1000),), est=0.96, settings=None, event_id=None, outcome_index=0):
    settings = settings or Settings()
    raw = gamma_market(market_id, best_bid=0.89, best_ask=asks[0][0], fees_enabled=False)
    if event_id:
        raw["events"] = [{"id": event_id, "slug": event_id, "title": event_id}]
    market = parse_market(raw)
    book_raw = clob_book(market.token_ids[0], bids=[(0.89, 100)], asks=list(asks))
    if outcome_index == 1:
        book_raw = clob_book(market.token_ids[1], bids=[(0.89, 100)], asks=list(asks))
    book = parse_book(book_raw)
    c = MarketScanner(FakeClient([], {}), settings).build_candidate(market, outcome_index, book, NOW)
    assert c is not None
    if est is not None:
        c.estimate = ProbabilityEstimate(outcome_index=outcome_index, probability=est, engine="test", method="test", uncertainty=0.01)
        c.edge = compute_edge(c.entry_price, c.fee_per_share, est)
        c.status = "TRADE"
    return c


def make_trader(**paper):
    settings = Settings()
    for k, v in paper.items():
        setattr(settings.paper, k, v)
    db = Database(":memory:")
    return PaperTrader(db, settings), db


@pytest.mark.parametrize(
    ("price", "bucket"),
    [(0.79, "<0.80"), (0.80, "0.80-0.85"), (0.8499, "0.80-0.85"), (0.85, "0.85-0.90"), (0.90, "0.90-0.93"),
     (0.9299, "0.90-0.93"), (0.93, "0.93-0.95"), (0.95, "0.95-0.97"), (0.97, "0.97-0.98"), (0.98, "0.97-0.98"),
     (0.985, ">0.98"), (None, "unknown")],
)
def test_entry_buckets(price, bucket):
    assert entry_bucket(price) == bucket


def test_fixed_stake_trade_records_full_audit_trail():
    trader, db = make_trader(fixed_stake_usd=100)
    c = candidate()
    trade_id = trader.open_model_trade(c, NOW)
    row = db.query("SELECT * FROM paper_trades WHERE trade_id=?", (trade_id,))[0]
    assert row["kind"] == MODEL and row["status"] == "open"
    assert row["entry_price"] == pytest.approx(0.90)
    assert row["shares"] == pytest.approx(111.11)  # floor(100 / 0.90, 2dp)
    assert row["amount_invested"] == pytest.approx(111.11 * 0.90)
    assert row["est_prob"] == 0.96
    assert row["edge"] == pytest.approx(0.06)
    assert row["expected_profit"] == pytest.approx(111.11 * 0.96 - 111.11 * 0.90)
    assert row["entry_bucket"] == "0.90-0.93" and row["time_bucket"] == "1-6h"
    detail = json.loads(row["detail_json"])
    assert detail["book_at_entry"]["asks"] == [[0.9, 1000.0]]
    assert "fixed stake" in detail["sizing"]


def test_fill_stops_where_edge_would_drop_below_minimum():
    trader, db = make_trader(fixed_stake_usd=250, max_stake_usd=250)
    # q = 0.95, min edge 0.04 -> no share above 0.91 may be bought
    c = candidate(asks=((0.89, 50), (0.90, 50), (0.92, 1000)), est=0.95)
    trade_id = trader.open_model_trade(c, NOW)
    row = db.query("SELECT * FROM paper_trades WHERE trade_id=?", (trade_id,))[0]
    assert row["shares"] == pytest.approx(100)
    assert row["amount_invested"] == pytest.approx(50 * 0.89 + 50 * 0.90)


def test_sizing_caps():
    trader, _ = make_trader(fixed_stake_usd=500, max_stake_usd=250)
    c = candidate()
    c.max_exec_usd = 120.0
    decision = trader.size_trade(c)
    assert decision.budget_usdc == pytest.approx(120.0)
    assert "max stake" in decision.reason and "minimum edge" in decision.reason
    c.max_exec_usd = 3.0
    assert trader.size_trade(c).budget_usdc == 0.0  # below $5 minimum


def test_kelly_sizing():
    trader, _ = make_trader(sizing="kelly", kelly_multiplier=0.1, max_stake_usd=10_000, starting_bankroll=10_000,
                            max_total_exposure_pct=1.0, max_exposure_per_event_usd=10_000)
    c = candidate(est=0.96)  # full Kelly = 0.06 / 0.10 = 0.6
    assert trader.size_trade(c).budget_usdc == pytest.approx(10_000 * 0.6 * 0.1)


def test_event_exposure_cap():
    trader, _ = make_trader(fixed_stake_usd=300, max_stake_usd=300, max_exposure_per_event_usd=500)
    first = candidate("1", event_id="E")
    assert trader.open_model_trade(first, NOW) is not None
    second = candidate("2", event_id="E")
    decision = trader.size_trade(second)
    assert decision.budget_usdc == pytest.approx(500 - first_cost(trader))
    assert "event exposure" in decision.reason


def first_cost(trader):
    return trader.db.query("SELECT amount_invested FROM paper_trades")[0]["amount_invested"]


def test_duplicate_and_opposite_side_guards():
    trader, _ = make_trader()
    c = candidate()
    assert trader.open_model_trade(c, NOW) is not None
    assert trader.open_model_trade(c, NOW) is None
    assert trader.can_open(c)[1] == "already holding this market side"
    other = candidate(outcome_index=1)
    assert trader.can_open(other)[1] == "holding the opposite side of this market"


def test_no_estimate_never_trades():
    trader, _ = make_trader()
    c = candidate(est=None)
    assert trader.open_model_trade(c, NOW) is None


def resolved(cid, prices, status="resolved"):
    return {"conditionId": cid, "closed": True, "outcomePrices": json.dumps(prices), "umaResolutionStatus": status,
            "closedTime": "2026-10-07T16:00:00Z"}


def test_settlement_won_lost_split_and_pending():
    trader, db = make_trader()
    trades = {}
    for mid in ("1", "2", "3", "4"):
        c = candidate(mid)
        trades[mid] = trader.open_model_trade(c, NOW)
    cid = {mid: "0x" + mid.rjust(64, "0") for mid in trades}
    client = FakeClient([], {}, closed_markets={
        cid["1"]: resolved(cid["1"], ["1", "0"]),
        cid["2"]: resolved(cid["2"], ["0", "1"]),
        cid["3"]: resolved(cid["3"], ["0.5", "0.5"]),
        cid["4"]: resolved(cid["4"], ["0.9995", "0.0005"], status="proposed"),
    })
    settled = trader.settle(client, NOW + timedelta(hours=5))
    assert len(settled) == 3
    rows = {r["market_id"]: r for r in db.query("SELECT * FROM paper_trades")}
    shares, cost = rows["1"]["shares"], rows["1"]["amount_invested"]
    assert rows["1"]["status"] == "won" and rows["1"]["pnl"] == pytest.approx(shares - cost)
    assert rows["2"]["status"] == "lost" and rows["2"]["pnl"] == pytest.approx(-cost)
    assert rows["3"]["status"] == "split" and rows["3"]["pnl"] == pytest.approx(shares * 0.5 - cost)
    assert rows["4"]["status"] == "open"
    bank = trader.bankroll()
    assert bank["realized_pnl"] == pytest.approx((shares - cost) - cost + (shares * 0.5 - cost))


def test_resolution_requires_final_payouts():
    assert resolution_from_gamma({"closed": False, "outcomePrices": '["1","0"]'}) is None
    assert resolution_from_gamma(resolved("x", ["1", "0"], status="disputed")) is None
    assert resolution_from_gamma(resolved("x", ["0.97", "0.03"])) is None
    assert resolution_from_gamma(resolved("x", ["0", "1"])).outcome_index == 1


def test_baseline_once_per_side_and_time_bucket():
    trader, db = make_trader()
    c = candidate(est=None)
    assert trader.record_baseline(c, NOW) is not None
    assert trader.record_baseline(c, NOW) is None  # same bucket
    c.resolution_time = NOW + timedelta(minutes=30)  # now in the <1h bucket
    assert trader.record_baseline(c, NOW) is not None
    rows = db.query("SELECT kind, time_bucket, est_prob, strategy FROM paper_trades ORDER BY trade_id")
    assert [(r["kind"], r["time_bucket"]) for r in rows] == [(BASELINE, "1-6h"), (BASELINE, "<1h")]
    assert rows[0]["est_prob"] is None and rows[0]["strategy"] == "blind_favorite"
    assert trader.bankroll()["open_cost"] == 0  # baselines never touch the paper bankroll


def test_mark_to_market_uses_best_bid():
    trader, db = make_trader()
    c = candidate()
    trader.open_model_trade(c, NOW)
    book = parse_book(clob_book(c.token_id, bids=[(0.93, 10)], asks=[(0.94, 10)]))
    assert trader.mark_to_market({c.token_id: book}, NOW) == 1
    assert db.query("SELECT last_mark FROM paper_trades")[0]["last_mark"] == 0.93


def test_runner_records_baselines_and_settles():
    market = gamma_market("1", best_bid=0.89, best_ask=0.90, fees_enabled=False)
    yes = clob_book("11", bids=[(0.89, 500)], asks=[(0.90, 400)])
    cid = market["conditionId"]
    client = FakeClient([market], {"11": yes, "12": mirrored_no_book("12", yes)})
    db = Database(":memory:")
    runner = Runner(Settings(), client=client, db=db)
    report = runner.run_once()
    assert report.baselines_recorded == 1 and report.trades_opened == []
    client.closed_markets[cid] = resolved(cid, ["1", "0"])
    runner._last_settle = None
    report = runner.run_once()
    assert len(report.settled) == 1
    row = db.query("SELECT * FROM paper_trades")[0]
    assert row["status"] == "won" and row["pnl"] > 0
