from favorite_hunter.categories import CRYPTO, OTHER, POLITICS, SPORTS, detect_category
from favorite_hunter.models import parse_market
from favorite_hunter.rules import assess_rules

from .factories import gamma_market


def market(**kwargs):
    return parse_market(gamma_market("1", **kwargs))


def test_category_from_tags_and_text():
    assert detect_category(market(tags=["nba"])) == SPORTS
    assert detect_category(market(tags=["crypto"])) == CRYPTO
    assert detect_category(market(tags=["elections"])) == POLITICS
    assert detect_category(market(question="Will Bitcoin be above $120,000 on October 8?")) == CRYPTO
    assert detect_category(market(question="Will Jane Doe win the 2026 Senate election?")) == POLITICS
    assert detect_category(market(question="Will the album top the charts?")) == OTHER
    assert detect_category(market(extra={"gameId": "123"})) == SPORTS


def test_clear_rules_score_higher_than_vague_rules():
    clear = assess_rules(market())
    vague = assess_rules(
        market(description="Resolves based on a consensus of credible reporting, at the sole discretion of the team.")
    )
    assert clear.score > 60
    assert vague.score < clear.score
    assert "relies on 'consensus of credible reporting'" in vague.flags
    assert "discretionary resolution" in vague.flags


def test_rule_risks_detected():
    rules = assess_rules(market(description="If the game is postponed or cancelled, this market resolves 50-50. Source: https://nba.com at 7:00 PM ET."))
    assert any("50-50" in r for r in rules.risks)
    assert any("postponement" in r for r in rules.risks)


def test_missing_rules_are_ambiguous():
    assert assess_rules(market(description="")).ambiguous
