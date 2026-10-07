from datetime import timedelta

import pytest

from favorite_hunter.config import Settings
from favorite_hunter.market_scanner import MarketScanner
from favorite_hunter.models import parse_book, parse_market
from favorite_hunter.probability.base import EXTERNAL_ODDS_EDGE, LIVE_EVENT_EDGE, POLLING_EDGE
from favorite_hunter.probability.evidence_book import EvidenceBook
from favorite_hunter.probability.politics_engine import ManualEngine, PoliticsEngine, vote_count_probability
from favorite_hunter.probability.stats import norm_cdf
from favorite_hunter.sources.kalshi import KalshiQuote

from .factories import NOW, clob_book, gamma_market
from .fakes import FakeClient


def pol_candidate(index=0, market_id="900", question="Will Jane Doe win the 2026 governor election?", tags=("politics",)):
    raw = gamma_market(market_id, question=question, tags=list(tags), fees_enabled=False)
    market = parse_market(raw)
    book = parse_book(clob_book(market.token_ids[index], bids=[(0.89, 500)], asks=[(0.90, 500)]))
    return MarketScanner(FakeClient([], {}), Settings()).build_candidate(market, index, book, NOW)


def write_book(tmp_path, text):
    path = tmp_path / "manual_evidence.yaml"
    path.write_text(text)
    return EvidenceBook(path)


def test_vote_count_model_hand_checked():
    p, calc = vote_count_probability(1_250_000, 1_100_000, 0.88, 0.47, 0.04)
    remaining = 2_350_000 * 0.12 / 0.88
    threshold = (1_100_000 - 1_250_000 + remaining) / (2 * remaining)
    assert threshold == pytest.approx(0.26596, abs=1e-5)
    assert p == pytest.approx(1 - norm_cdf((threshold - 0.47) / 0.04))
    close, _ = vote_count_probability(1_010_000, 1_000_000, 0.80, 0.49, 0.03)
    assert 0.3 < close < 0.95  # a narrow lead with 20% outstanding is not near-certain
    assert vote_count_probability(10, 5, 1.0, 0.5, 0.05)[0] == 1.0


def test_no_configured_source_is_data_unavailable(tmp_path):
    book = EvidenceBook(tmp_path / "missing.yaml")
    est = PoliticsEngine(book, None).estimate(pol_candidate(), NOW)
    assert est.probability is None and "no polls, model forecast" in est.reason


def test_poll_entry_vote_count_and_expiry(tmp_path):
    book = write_book(tmp_path, f"""
entries:
  - match: {{market_id: "900"}}
    outcome: "Yes"
    probability: 0.96
    uncertainty: 0.02
    source: "Example model"
    url: "https://example.org/model"
    as_of: "{(NOW - timedelta(hours=1)).isoformat()}"
    expires: "{(NOW + timedelta(days=1)).isoformat()}"
  - match: {{market_id: "900"}}
    outcome: "Yes"
    probability: 0.50
    source: "Expired poll"
    as_of: "{(NOW - timedelta(days=5)).isoformat()}"
    expires: "{(NOW - timedelta(days=1)).isoformat()}"
  - match: {{market_id: "900"}}
    probability: 0.9
    note: "missing source and as_of -> ignored"
""")
    yes = PoliticsEngine(book, None).estimate(pol_candidate(0), NOW)
    assert yes.probability == pytest.approx(0.96) and yes.opportunity_type == POLLING_EDGE
    no = PoliticsEngine(book, None).estimate(pol_candidate(1), NOW)
    assert no.probability == pytest.approx(0.04)
    assert len(book.errors) == 1 and "required" in book.errors[0]


def test_vote_count_entry(tmp_path):
    book = write_book(tmp_path, f"""
entries:
  - match: {{question_contains: "governor"}}
    vote_count:
      leader_outcome: "Yes"
      leader_votes: 1250000
      trailer_votes: 1100000
      pct_reporting: 0.88
    source: "State results page"
    as_of: "{(NOW - timedelta(minutes=5)).isoformat()}"
""")
    est = PoliticsEngine(book, None).estimate(pol_candidate(0), NOW)
    assert est.available and est.opportunity_type == LIVE_EVENT_EDGE
    assert any("assumed" in r for r in est.risks)  # no remaining share/sd given
    assert est.probability > 0.99


class FakeKalshi:
    def market(self, ticker):
        return KalshiQuote(ticker, "Jane Doe wins", 0.95, 0.97, 0.96, "active", NOW, "https://kalshi.com/markets/" + ticker)


def test_kalshi_mapping_and_conflict(tmp_path):
    book = write_book(tmp_path, f"""
entries:
  - match: {{market_id: "900"}}
    kalshi_ticker: "KXGOV-26"
    kalshi_yes_outcome: "Yes"
    source: "Kalshi"
    as_of: "{(NOW - timedelta(days=1)).isoformat()}"
  - match: {{market_id: "900"}}
    outcome: "Yes"
    probability: 0.80
    uncertainty: 0.02
    source: "Poll average"
    as_of: "{(NOW - timedelta(hours=2)).isoformat()}"
""")
    est = PoliticsEngine(book, FakeKalshi(), conflict_threshold=0.05).estimate(pol_candidate(0), NOW)
    assert est.probability is None and est.data_status == "CONFLICTING SOURCES"
    only_kalshi = write_book(tmp_path, f"""
entries:
  - match: {{market_id: "900"}}
    kalshi_ticker: "KXGOV-26"
    kalshi_yes_outcome: "Yes"
    source: "Kalshi"
    as_of: "{(NOW - timedelta(days=1)).isoformat()}"
""")
    est = PoliticsEngine(only_kalshi, FakeKalshi()).estimate(pol_candidate(0), NOW)
    assert est.probability == pytest.approx(0.96) and est.opportunity_type == EXTERNAL_ODDS_EDGE


def test_manual_engine_only_applies_to_matching_markets(tmp_path):
    book = write_book(tmp_path, f"""
entries:
  - match: {{market_id: "901"}}
    outcome: "Yes"
    probability: 0.97
    source: "Official announcement"
    as_of: "{(NOW - timedelta(hours=1)).isoformat()}"
""")
    engine = ManualEngine(book, None)
    other = pol_candidate(market_id="902", question="Will the film win best picture?", tags=("culture",))
    assert not engine.applies(other)
    match = pol_candidate(market_id="901", question="Will the film win best picture?", tags=("culture",))
    assert engine.applies(match) and engine.estimate(match, NOW).probability == pytest.approx(0.97)
