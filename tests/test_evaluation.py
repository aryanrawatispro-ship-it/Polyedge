
import pytest

from favorite_hunter.config import Settings
from favorite_hunter.database import Database
from favorite_hunter.evaluator import Evaluator
from favorite_hunter.market_scanner import MarketScanner
from favorite_hunter.models import parse_book, parse_market
from favorite_hunter.probability.base import (
    CONFLICT,
    DATA_LAG,
    LIVE_EVENT_EDGE,
    ORDER_BOOK_EDGE,
    PRICE_DISLOCATION,
    ProbabilityEstimate,
)
from favorite_hunter.probability.combine import combine_estimates
from favorite_hunter.probability.engine import ProbabilityEngine
from favorite_hunter.risk_engine import STATUS_CONFLICT, STATUS_DATA_UNAVAILABLE, STATUS_FILTERED, STATUS_NO_EDGE, STATUS_TRADE
from favorite_hunter.runner import Runner

from .factories import NOW, clob_book, gamma_market, mirrored_no_book
from .fakes import FakeClient


def est(p, *, unc=0.01, sources=("A", "B"), quality=0.85, certainty=0.8, opp=LIVE_EVENT_EDGE, age=5.0, idx=0):
    return ProbabilityEstimate(
        outcome_index=idx, probability=p, engine="fake", method="fake model", uncertainty=unc, opportunity_type=opp,
        sources=list(sources), source_quality=quality, event_certainty=certainty, data_age_seconds=age,
        calculation=["fake calc"], main_reason="fake reason",
    )


class StubEngine:
    name = "stub"

    def __init__(self, estimate):
        self._estimate = estimate

    def applies(self, candidate):
        return True

    def estimate(self, candidate, now):
        return self._estimate


def make_candidate(asks=((0.90, 2000),), bids=((0.89, 2000),), extra=None, description=None, settings=None):
    kwargs = {"description": description} if description is not None else {}
    raw = gamma_market("1", best_bid=bids[0][0], best_ask=asks[0][0], fees_enabled=False, extra=extra, **kwargs)
    market = parse_market(raw)
    book_raw = clob_book(market.token_ids[0], bids=list(bids), asks=list(asks))
    book = parse_book(book_raw)
    return MarketScanner(FakeClient([], {}), settings or Settings()).build_candidate(market, 0, book, NOW)


def evaluate(c, estimate, settings=None):
    settings = settings or Settings()
    Evaluator(settings, ProbabilityEngine(settings, [StubEngine(estimate)]))([c], NOW)
    return c


def test_trade_status_with_full_metrics():
    c = evaluate(make_candidate(), est(0.97))
    assert c.status == STATUS_TRADE and c.skip_reasons == []
    assert c.edge.probability_edge == pytest.approx(0.07)
    assert c.edge.expected_roi == pytest.approx(0.07 / 0.90)
    # every ask level at 0.90 keeps >= 4pt edge: whole level executable
    assert c.max_exec_usd == pytest.approx(2000 * 0.90)
    assert 0 < c.confidence.value <= 100
    assert c.opportunity_type == LIVE_EVENT_EDGE


def test_spec_examples_interesting_vs_skip():
    good = evaluate(make_candidate(asks=((0.89, 2000),), bids=((0.88, 2000),)), est(0.95))
    assert good.edge.probability_edge == pytest.approx(0.06) and good.status == STATUS_TRADE
    thin = evaluate(make_candidate(asks=((0.94, 2000),), bids=((0.93, 2000),)), est(0.95))
    assert thin.edge.probability_edge == pytest.approx(0.01)
    assert thin.status == STATUS_NO_EDGE and "below minimum" in thin.skip_reasons[0]


def test_lower_bound_edge_requirement():
    c = evaluate(make_candidate(), est(0.95, unc=0.06))  # edge 5pt but 0.95 - 0.06 < 0.90
    assert c.status == STATUS_NO_EDGE
    assert any("uncertainty" in r for r in c.skip_reasons)


def test_filters_produce_reasons():
    thin_book = evaluate(make_candidate(asks=((0.90, 100),), bids=((0.80, 100),)), est(0.97))
    assert thin_book.status == STATUS_FILTERED
    joined = " | ".join(thin_book.skip_reasons)
    assert "ask depth" in joined and "spread" in joined
    stale = evaluate(make_candidate(), est(0.97, age=10_000))
    assert stale.status == STATUS_FILTERED and any("stale" in r for r in stale.skip_reasons)
    vague = evaluate(make_candidate(description="tbd"), est(0.97))
    assert vague.status == STATUS_FILTERED and any("ambiguous" in r for r in vague.skip_reasons)


def test_unavailable_and_conflict_statuses():
    c = evaluate(make_candidate(), ProbabilityEstimate.unavailable(0, "crypto", "Binance: blocked"))
    assert c.status == STATUS_DATA_UNAVAILABLE and c.edge.probability_edge is None
    assert c.confidence.value == 0 and "blocked" in c.skip_reasons[0]
    conflict = combine_estimates([est(0.97), est(0.85)], outcome_index=0, engine="x", conflict_threshold=0.05)
    c = evaluate(make_candidate(), conflict)
    assert conflict.data_status == CONFLICT and c.status == STATUS_CONFLICT


def test_combine_weights_by_uncertainty():
    combined = combine_estimates([est(0.96, unc=0.01), est(0.94, unc=0.02)], outcome_index=0, engine="x", conflict_threshold=0.05)
    # weights 1/0.01^2 : 1/0.02^2 = 4 : 1
    assert combined.probability == pytest.approx((4 * 0.96 + 1 * 0.94) / 5)
    assert combined.uncertainty >= 0.01  # never below half the disagreement


def test_opportunity_labels():
    lag = evaluate(make_candidate(), est(0.999, opp=DATA_LAG))
    assert lag.opportunity_type == DATA_LAG
    dislocated = evaluate(make_candidate(extra={"oneHourPriceChange": -0.07}), est(0.97))
    assert dislocated.opportunity_type == PRICE_DISLOCATION
    gap = make_candidate()
    gap.book.last_trade_price = 0.94
    gap = evaluate(gap, est(0.97))
    assert gap.opportunity_type == ORDER_BOOK_EDGE


def test_confidence_is_not_probability_and_responds_to_quality():
    strong = evaluate(make_candidate(), est(0.97, sources=("A", "B", "C"), quality=0.9, certainty=0.95))
    weak = evaluate(make_candidate(), est(0.97, sources=("A",), quality=0.5, certainty=0.3))
    assert strong.confidence.value > weak.confidence.value
    assert strong.confidence.value != pytest.approx(97.0)


def test_runner_opens_model_trade_for_approved_candidate():
    market = gamma_market("1", best_bid=0.89, best_ask=0.90, fees_enabled=False)
    yes = clob_book("11", bids=[(0.89, 2000)], asks=[(0.90, 2000)])
    client = FakeClient([market], {"11": yes, "12": mirrored_no_book("12", yes)})
    settings = Settings()
    db = Database(":memory:")
    evaluator = Evaluator(settings, ProbabilityEngine(settings, [StubEngine(est(0.97))]))
    runner = Runner(settings, client=client, db=db, evaluator=evaluator, clock=lambda: NOW)
    report = runner.run_once()
    assert len(report.trades_opened) == 1 and report.opportunities == 1
    trade = db.query("SELECT * FROM paper_trades WHERE kind='model'")[0]
    assert trade["est_prob"] == 0.97 and trade["opportunity_type"] == LIVE_EVENT_EDGE
    assert trade["confidence"] is not None
    opp = db.query("SELECT * FROM opportunities")[0]
    assert opp["status"] == STATUS_TRADE and opp["edge"] == pytest.approx(0.07)
    # second cycle: already holding -> no duplicate trade
    assert runner.run_once().trades_opened == []
