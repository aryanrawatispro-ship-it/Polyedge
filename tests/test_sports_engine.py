import math
from datetime import timedelta

import pytest

from favorite_hunter.config import Settings
from favorite_hunter.market_scanner import MarketScanner
from favorite_hunter.models import parse_book, parse_market
from favorite_hunter.probability.base import DATA_LAG, EXTERNAL_ODDS_EDGE, LIVE_EVENT_EDGE
from favorite_hunter.probability.sports_engine import SportsEngine, parse_sports_market, parse_teams
from favorite_hunter.probability.stats import norm_cdf, poisson_margin_probs
from favorite_hunter.sources.espn import GameState, TeamState
from favorite_hunter.sources.odds_api import BookPrice, OddsEvent

from .factories import NOW, clob_book, gamma_market
from .fakes import FakeClient


def sports_candidate(question, *, outcomes=("Yes", "No"), index=0, tags=("sports", "nba"), event_title=None, extra=None):
    raw = gamma_market("55", question=question, outcomes=outcomes, tags=list(tags), fees_enabled=False,
                       extra={"gameStartTime": (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z"), **(extra or {})})
    if event_title:
        raw["events"] = [{"id": "E55", "slug": "e55", "title": event_title}]
    market = parse_market(raw)
    book = parse_book(clob_book(market.token_ids[index], bids=[(0.89, 500)], asks=[(0.90, 500)]))
    c = MarketScanner(FakeClient([], {}), Settings()).build_candidate(market, index, book, NOW)
    assert c is not None
    return c


def game(sport, home, away, hs, as_, *, period, clock, state="in", detail="", odds=None, reds=None, completed=False):
    home_t = TeamState([home], hs, "home")
    away_t = TeamState([away], as_, "away")
    if reds:
        home_t.red_cards, away_t.red_cards = reds
    return GameState("ESPN", sport, "league", "1", f"{away} at {home}", home_t, away_t, state, completed, period,
                     clock, "", detail, NOW - timedelta(hours=2), NOW, "https://espn.example", odds=odds)


class FakeEspn:
    def __init__(self, g=None, note="matched"):
        self.g = g
        self.note = note

    def find_game(self, league, team_a, team_b, start):
        return self.g, self.note


class FakeOdds:
    def __init__(self, event=None, enabled=True):
        self.event = event
        self.enabled = enabled

    def find_event(self, league, a, b):
        return self.event


def test_parse_market_types():
    ml = parse_sports_market(sports_candidate("Lakers vs. Celtics", outcomes=("Lakers", "Celtics")))
    assert (ml.market_type, ml.team_a, ml.team_b, ml.league_tag) == ("moneyline", "Lakers", "Celtics", "nba")
    win = parse_sports_market(sports_candidate("Will Arsenal win on 2026-10-07?", tags=("sports", "epl"), event_title="Arsenal vs. Chelsea"))
    assert (win.market_type, win.team_a, win.team_b, win.yes_index) == ("win", "Arsenal", "Chelsea", 0)
    draw = parse_sports_market(sports_candidate("Will Arsenal vs. Chelsea end in a draw?", tags=("sports", "epl")))
    assert draw.market_type == "draw" and draw.team_a == "Arsenal" and draw.team_b == "Chelsea"
    total = parse_sports_market(sports_candidate("Lakers vs. Celtics: O/U 225.5", outcomes=("Over", "Under")))
    assert total.market_type == "total" and total.line == 225.5
    spread = parse_sports_market(sports_candidate("Spread: Lakers (-3.5)", outcomes=("Lakers", "Celtics")))
    assert spread.market_type == "spread" and spread.team_a == "Lakers" and spread.line == -3.5
    assert parse_teams("Boston Celtics @ Los Angeles Lakers") == ("Boston Celtics", "Los Angeles Lakers")


def test_nba_late_lead_hand_checked():
    c = sports_candidate("Lakers vs. Celtics", outcomes=("Lakers", "Celtics"))
    g = game("nba", "Lakers", "Celtics", 110, 100, period=4, clock=120.0, detail="2:00 - 4th Quarter")
    est = SportsEngine(FakeEspn(g), FakeOdds(enabled=False)).estimate(c, NOW)
    frac = 2 / 48
    sd = 12.5 * math.sqrt(frac)
    expected = 1 - 0.5 * norm_cdf((0.5 - 10) / sd) - 0.5 * norm_cdf((-0.5 - 10) / sd)
    assert est.available and est.probability == pytest.approx(expected, abs=1e-9)
    assert est.probability > 0.9999 and est.opportunity_type == LIVE_EVENT_EDGE
    assert any("neutral prior" in r for r in est.risks)  # no pregame line in this fixture
    other = SportsEngine(FakeEspn(g), FakeOdds(enabled=False)).estimate(
        sports_candidate("Lakers vs. Celtics", outcomes=("Lakers", "Celtics"), index=1), NOW)
    assert other.probability == pytest.approx(1 - expected, abs=1e-9)


def test_pregame_spread_shifts_the_prior():
    c = sports_candidate("Lakers vs. Celtics", outcomes=("Lakers", "Celtics"))
    odds = {"provider": "ESPN BET", "details": "BOS -6.5", "spread": 6.5, "home_favorite": False, "over_under": 224.0, "home_moneyline": None, "away_moneyline": None}
    g = game("nba", "Lakers", "Celtics", 60, 56, period=3, clock=0.0, detail="End of 3rd", odds=odds)
    est = SportsEngine(FakeEspn(g), FakeOdds(enabled=False)).estimate(c, NOW)
    frac = 12 / 48
    sd = 12.5 * math.sqrt(frac)
    mu = -6.5 * frac  # Lakers (home) were 6.5-point underdogs
    expected = 1 - 0.5 * norm_cdf((0.5 - 4 - mu) / sd) - 0.5 * norm_cdf((-0.5 - 4 - mu) / sd)
    assert est.probability == pytest.approx(expected, abs=1e-9)


def test_soccer_two_goal_lead_at_78_minutes():
    c = sports_candidate("Will Arsenal win on 2026-10-07?", tags=("sports", "epl"), event_title="Arsenal vs. Chelsea")
    g = game("soccer", "Arsenal", "Chelsea", 2, 0, period=2, clock=78 * 60.0, detail="78'", reds=(0, 0))
    est = SportsEngine(FakeEspn(g), FakeOdds(enabled=False)).estimate(c, NOW)
    left = 12 + 4  # minutes of regulation + assumed stoppage
    frac = left / 90
    lam_home = 1.35 * 1.10 * frac
    lam_away = 1.35 / 1.10 * frac
    win, _, _ = poisson_margin_probs(2, lam_home, lam_away)
    assert est.probability == pytest.approx(win, abs=1e-9)
    assert 0.95 < est.probability < 0.995
    assert "regulation" in " ".join(est.calculation)


def test_red_card_lowers_probability_and_missing_cards_is_flagged():
    c = sports_candidate("Will Arsenal win on 2026-10-07?", tags=("sports", "epl"), event_title="Arsenal vs. Chelsea")
    base = SportsEngine(FakeEspn(game("soccer", "Arsenal", "Chelsea", 1, 0, period=2, clock=60 * 60.0, reds=(0, 0))), FakeOdds(enabled=False)).estimate(c, NOW)
    red = SportsEngine(FakeEspn(game("soccer", "Arsenal", "Chelsea", 1, 0, period=2, clock=60 * 60.0, reds=(1, 0))), FakeOdds(enabled=False)).estimate(c, NOW)
    unknown = SportsEngine(FakeEspn(game("soccer", "Arsenal", "Chelsea", 1, 0, period=2, clock=60 * 60.0)), FakeOdds(enabled=False)).estimate(c, NOW)
    assert red.probability < base.probability
    assert any("Red-card data DATA UNAVAILABLE" in r for r in unknown.risks)


def test_draw_market_and_final_whistle():
    draw = sports_candidate("Will Arsenal vs. Chelsea end in a draw?", tags=("sports", "epl"), index=1)  # buying NO on the draw
    g = game("soccer", "Arsenal", "Chelsea", 3, 0, period=2, clock=92 * 60.0, detail="90'+2'", reds=(0, 0))
    est = SportsEngine(FakeEspn(g), FakeOdds(enabled=False)).estimate(draw, NOW)
    assert est.probability > 0.999
    final = game("nba", "Lakers", "Celtics", 101, 99, period=4, clock=0.0, state="post", completed=True, detail="Final")
    c = sports_candidate("Lakers vs. Celtics", outcomes=("Lakers", "Celtics"))
    est = SportsEngine(FakeEspn(final), FakeOdds(enabled=False)).estimate(c, NOW)
    assert est.probability == 1.0 and est.opportunity_type == DATA_LAG


def test_unclear_state_or_no_match_is_unavailable():
    c = sports_candidate("Lakers vs. Celtics", outcomes=("Lakers", "Celtics"))
    postponed = game("nba", "Lakers", "Celtics", 0, 0, period=0, clock=0.0, state="post", detail="Postponed")
    est = SportsEngine(FakeEspn(postponed), FakeOdds(enabled=False)).estimate(c, NOW)
    assert est.probability is None and "unclear event state" in est.reason
    est = SportsEngine(FakeEspn(None, "no ESPN game matched both teams"), FakeOdds(enabled=False)).estimate(c, NOW)
    assert est.probability is None and "no ESPN game matched" in est.reason
    assert "ODDS_API_KEY not set" in est.reason


def test_sportsbook_consensus_and_conflict():
    c = sports_candidate("Lakers vs. Celtics", outcomes=("Lakers", "Celtics"))
    books = [
        BookPrice("Pinnacle", {"Los Angeles Lakers": 0.93, "Boston Celtics": 0.07}, 0.02, NOW),
        BookPrice("DraftKings", {"Los Angeles Lakers": 0.92, "Boston Celtics": 0.08}, 0.045, NOW),
    ]
    event = OddsEvent("basketball_nba", "Los Angeles Lakers", "Boston Celtics", NOW, books, "https://odds.example")
    est = SportsEngine(FakeEspn(None, "no match"), FakeOdds(event)).estimate(c, NOW)
    assert est.available and est.probability == pytest.approx(0.93)  # Pinnacle preferred
    assert all(component is not est for component in est.components)
    serialized = est.to_dict()  # regression: must not recurse into itself
    assert serialized["components"][0]["data_status"] == "DATA UNAVAILABLE"
    assert est.opportunity_type == EXTERNAL_ODDS_EDGE
    # live model says ~0.9999 while books say 0.93: conflict beyond 5 points
    g = game("nba", "Lakers", "Celtics", 110, 100, period=4, clock=120.0)
    both = SportsEngine(FakeEspn(g), FakeOdds(event), conflict_threshold=0.05).estimate(c, NOW)
    assert both.probability is None and both.data_status == "CONFLICTING SOURCES"
    agree = SportsEngine(FakeEspn(g), FakeOdds(event), conflict_threshold=0.10).estimate(c, NOW)
    assert agree.available and 0.93 < agree.probability < 1.0 and len(agree.sources) == 2


def test_nhl_tie_counts_half():
    c = sports_candidate("Rangers vs. Bruins", outcomes=("Rangers", "Bruins"), tags=("sports", "nhl"))
    g = game("nhl", "Rangers", "Bruins", 3, 1, period=3, clock=300.0, detail="5:00 - 3rd")
    est = SportsEngine(FakeEspn(g), FakeOdds(enabled=False)).estimate(c, NOW)
    frac = 5 / 60
    win, draw, _ = poisson_margin_probs(2, 3.05 * frac, 3.05 * frac)
    assert est.probability == pytest.approx(win + 0.5 * draw, abs=1e-9)
